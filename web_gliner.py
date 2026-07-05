from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

try:
    import phonenumbers  # type: ignore
except Exception:  # pragma: no cover
    phonenumbers = None

try:
    import usaddress  # type: ignore
except Exception:  # pragma: no cover
    usaddress = None

from web_render import (
    render_site_for_llm,
    render_rendered_web_pages_json,
    render_rendered_web_pages_text,
    save_rendered_web_pages,
)

try:
    from gliner2 import GLiNER2  # type: ignore
except Exception:  # pragma: no cover
    GLiNER2 = None


LOGGER = structlog.get_logger(__name__).bind(source="web_gliner.py")

_LABELS = ["person", "company", "location", "email", "phone", "address"]
_CONTACT_CONTEXT_RE = re.compile(r"\b(contact|about|team|leadership|people|staff|founder|owner|ceo|director|manager)\b", re.I)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}")
_ADDRESS_LABEL_RE = re.compile(r"\b(address|business information|mailing address|office address|location)\s*[:\-]?\s*(.*)$", re.I)
_ADDRESS_PART_RE = re.compile(r"\b(?:suite|ste|unit|apt|fl|floor|building|bldg|room|po box|p\.?o\.?\s*box)\b|\b\d{1,6}\s+[A-Za-z0-9.'-]+", re.I)


@dataclass
class GlinerExtractionResult:
    input_value: str
    resolved_homepage: str | None
    discovered_pages: list[str] = field(default_factory=list)
    rendered: dict[str, Any] = field(default_factory=dict)
    entities: dict[str, list[str]] = field(default_factory=dict)
    normalized: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def extract_with_gliner(input_value: str, max_pages: int = 3, max_blocks: int = 12) -> GlinerExtractionResult:
    LOGGER.info("Starting GLiNER extraction", input_value=input_value, max_pages=max_pages, max_blocks=max_blocks)
    rendered = render_site_for_llm(input_value, max_pages=max_pages, max_blocks=max_blocks)
    rendered_json = json.loads(render_rendered_web_pages_json(rendered))

    if GLiNER2 is None:
        return GlinerExtractionResult(
            input_value=input_value,
            resolved_homepage=rendered.resolved_homepage,
            discovered_pages=rendered.discovered_pages,
            rendered=rendered_json,
            notes=["gliner2 is not installed in the current environment."],
        )

    try:
        extractor = GLiNER2.from_pretrained("fastino/gliner2-base-v1")
    except Exception:
        LOGGER.exception("Failed to load GLiNER2 model")
        return GlinerExtractionResult(
            input_value=input_value,
            resolved_homepage=rendered.resolved_homepage,
            discovered_pages=rendered.discovered_pages,
            rendered=rendered_json,
            notes=["Failed to load GLiNER2 model."],
        )

    entities: dict[str, list[str]] = {label: [] for label in _LABELS}
    for page in rendered_json.get("page_text", []):
        blocks = page.get("blocks", []) if isinstance(page, dict) else []
        if not isinstance(blocks, list):
            continue
        candidate_blocks = _split_contact_blocks(blocks)
        if not candidate_blocks:
            continue
        for text in candidate_blocks:
            try:
                matches = extractor.extract_entities(text, _LABELS)
            except Exception:
                LOGGER.exception("GLiNER extraction failed", page_url=page.get("url"))
                continue
            for label, items in matches.items():
                if label not in entities:
                    entities[label] = []
                for item in items or []:
                    value = _coerce_entity_value(item)
                    if value and value not in entities[label]:
                        entities[label].append(value)

    normalized = _normalize_entities(entities, rendered.discovered_pages, rendered_json.get("page_text", []))
    return GlinerExtractionResult(
        input_value=input_value,
        resolved_homepage=rendered.resolved_homepage,
        discovered_pages=rendered.discovered_pages,
        rendered=rendered_json,
        entities=entities,
        normalized=normalized,
    )


def save_gliner_extraction(result: GlinerExtractionResult, output_dir: str | Path = ".state/web_gliner") -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = _slugify(result.resolved_homepage or result.input_value)
    file_path = output_path / f"{name}_{timestamp}.json"
    file_path.write_text(render_gliner_json(result), encoding="utf-8")
    LOGGER.info("Saved GLiNER output", file=str(file_path))
    return file_path


def render_gliner_json(result: GlinerExtractionResult) -> str:
    return json.dumps(
        {
            "input": result.input_value,
            "homepage": result.resolved_homepage,
            "pages": result.discovered_pages,
            "entities": result.entities,
            "normalized": result.normalized,
            "rendered": result.rendered,
            "notes": result.notes,
        },
        indent=2,
        sort_keys=True,
    )


def render_gliner_text(result: GlinerExtractionResult) -> str:
    lines = [
        f"Input: {result.input_value}",
        f"Homepage: {result.resolved_homepage or 'unresolved'}",
        f"Pages checked: {len(result.discovered_pages)}",
        "Normalized:",
        json.dumps(result.normalized, indent=2, ensure_ascii=False),
    ]
    if result.notes:
        lines.append("Notes:")
        lines.extend(f"- {note}" for note in result.notes)
    return "\n".join(lines)


def _coerce_entity_value(item: Any) -> str | None:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("text", "label", "value"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _normalize_entities(entities: dict[str, list[str]], pages: list[str], page_text: list[dict[str, Any]]) -> dict[str, Any]:
    names = [value for value in entities.get("person", []) if _looks_like_name(value)]
    companies = entities.get("company", [])
    emails = _dedupe([value.lower() for value in entities.get("email", []) if _looks_like_email(value)])
    phones = _dedupe([_normalize_phone(value) for value in entities.get("phone", []) if _normalize_phone(value)])
    addresses = _dedupe([value for value in entities.get("address", []) if value])
    locations = _dedupe([value for value in entities.get("location", []) if value])
    for page in page_text:
        blocks = page.get("blocks", []) if isinstance(page, dict) else []
        if not isinstance(blocks, list):
            continue
        joined = "\n".join(block for block in blocks if isinstance(block, str))
        emails = _dedupe(emails + _extract_emails(joined))
        phones = _dedupe(phones + _extract_phones(joined))
        addresses = _dedupe(addresses + _extract_addresses(joined))
        companies = _dedupe(companies + _extract_companies(joined))
    contacts: list[dict[str, Any]] = []

    for name in names:
        first_name, last_name = _split_name(name)
        if not first_name or not last_name:
            continue
        contacts.append(
            {
                "first_name": first_name,
                "last_name": last_name,
                "source_page": pages[0] if pages else None,
            }
        )

    if not contacts and (emails or phones or addresses):
        source_page = pages[0] if pages else None
        contacts.append(
            {
                "first_name": None,
                "last_name": None,
                "email": emails[0] if emails else None,
                "phone": phones[0] if phones else None,
                "address": addresses[0] if addresses else None,
                "source_page": source_page,
            }
        )
    return {
        "names": names,
        "companies": _dedupe(companies),
        "emails": emails,
        "phones": phones,
        "addresses": addresses,
        "locations": locations,
        "contacts": contacts,
    }


def _looks_like_name(candidate: str) -> bool:
    parts = candidate.split()
    return 2 <= len(parts) <= 4 and all(part[:1].isalpha() and part[:1].isupper() for part in parts)


def _looks_like_email(candidate: str) -> bool:
    return bool(re.match(r"^[\w.+-]+@[\w-]+(?:\.[\w-]+)+$", candidate))


def _extract_emails(text: str) -> list[str]:
    return _dedupe([value.lower() for value in _EMAIL_RE.findall(text)])


def _extract_phones(text: str) -> list[str]:
    values: list[str] = []
    if phonenumbers is not None:
        for match in phonenumbers.PhoneNumberMatcher(text, "US"):
            try:
                values.append(phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.NATIONAL))
            except Exception:
                continue
    else:
        for value in _PHONE_RE.findall(text):
            normalized = _normalize_phone(value)
            if normalized:
                values.append(normalized)
    return _dedupe(values)


def _extract_addresses(text: str) -> list[str]:
    values: list[str] = []
    lines = [line.strip() for line in re.split(r"[\n\r]+", text) if line.strip()]
    for index, line in enumerate(lines):
        match = _ADDRESS_LABEL_RE.search(line)
        if match:
            candidates = []
            span = match.group(2).strip()
            if span:
                candidates.append(span)
            candidates.extend(lines[index + 1 : index + 4])
        elif len(line) <= 120 and _ADDRESS_PART_RE.search(line) and any(ch.isdigit() for ch in line):
            candidates = [line]
        else:
            continue
        for candidate in candidates:
            cleaned = _extract_us_address(_trim_contact_span(candidate))
            if cleaned:
                values.append(cleaned)
    return _dedupe(values)


def _extract_companies(text: str) -> list[str]:
    values: list[str] = []
    for line in re.split(r"[\n\r]+", text):
        cleaned = line.strip()
        if not cleaned:
            continue
        if any(token in cleaned.lower() for token in ("inc", "llc", "corp", "company", "co.")):
            values.append(cleaned)
    return _dedupe(values)


def _normalize_phone(value: str) -> str | None:
    digits = re.sub(r"\D+", "", value)
    return value.strip() if len(digits) >= 10 else None


def _split_name(name: str) -> tuple[str | None, str | None]:
    parts = name.split()
    if len(parts) < 2:
        return None, None
    return parts[0], parts[1]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.lower().strip()
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _select_contact_blocks(blocks: list[Any]) -> str:
    return "\n".join(_split_contact_blocks(blocks))


def _split_contact_blocks(blocks: list[Any]) -> list[str]:
    cleaned_blocks: list[str] = []
    for block in blocks:
        if not isinstance(block, str):
            continue
        for candidate in _expand_contact_block(block):
            cleaned = " ".join(candidate.split())
            if not cleaned:
                continue
            if len(cleaned) > 240:
                continue
            if _CONTACT_CONTEXT_RE.search(cleaned) or _EMAIL_RE.search(cleaned) or _PHONE_RE.search(cleaned) or _ADDRESS_LABEL_RE.search(cleaned):
                cleaned_blocks.append(cleaned)
    if not cleaned_blocks:
        for block in blocks:
            if not isinstance(block, str):
                continue
            for candidate in _expand_contact_block(block):
                cleaned = " ".join(candidate.split())
                if cleaned and len(cleaned) <= 180:
                    cleaned_blocks.append(cleaned)
    cleaned_blocks = _dedupe(cleaned_blocks)
    cleaned_blocks.sort(key=len, reverse=True)
    return cleaned_blocks[:12]


def _expand_contact_block(block: str) -> list[str]:
    text = " ".join(block.split())
    if not text:
        return []
    # Prefer short spans around explicit labels so we never feed a full mixed-content block.
    spans: list[str] = []
    label_patterns = (
        r"\b(address|business information|mailing address|office address|location)\s*[:\-]\s*",
        r"\b(phone|telephone|tel)\s*[:\-]\s*",
        r"\b(email|e-mail)\s*[:\-]\s*",
        r"\b(contact|about|team|leadership|people|staff|founder|owner|ceo|director|manager)\b",
    )
    for pattern in label_patterns:
        for match in re.finditer(pattern, text, re.I):
            start = max(0, match.start() - 20)
            end = min(len(text), match.end() + 120)
            spans.append(text[start:end].strip(" ,-"))
    for regex in (_EMAIL_RE, _PHONE_RE):
        for match in regex.finditer(text):
            start = max(0, match.start() - 25)
            end = min(len(text), match.end() + 40)
            spans.append(text[start:end].strip(" ,-"))
    for match in _ADDRESS_LABEL_RE.finditer(text):
        candidate = match.group(2).strip()
        if candidate:
            spans.append(candidate)
    # If nothing else matched, retain only short lines, not the entire block.
    if not spans:
        for line in re.split(r"[\n\r]+", block):
            cleaned = " ".join(line.split()).strip()
            if cleaned and len(cleaned) <= 160:
                spans.append(cleaned)
    return spans


def _extract_us_address(candidate: str) -> str | None:
    cleaned = " ".join(candidate.split()).strip(" ,-")
    if not cleaned or len(cleaned) < 8:
        return None
    if usaddress is not None:
        try:
            parsed, label = usaddress.tag(cleaned)
            parts: list[str] = []
            street = " ".join(
                part
                for key in ("AddressNumber", "StreetNamePreDirectional", "StreetName", "StreetNamePostType", "OccupancyType", "OccupancyIdentifier")
                if (part := parsed.get(key))
            ).strip()
            city = parsed.get("PlaceName")
            state = parsed.get("StateName")
            zipcode = parsed.get("ZipCode")
            if street:
                parts.append(street)
            if city:
                parts.append(city)
            if state:
                parts.append(state)
            if zipcode:
                parts.append(zipcode)
            if parts and label in {"Street Address", "Intersection Address", "PO Box"}:
                return ", ".join(parts)
        except Exception:
            pass
    if any(ch.isdigit() for ch in cleaned) and any(token in cleaned.lower() for token in ("st", "street", "ave", "avenue", "road", "rd", "lane", "ln", "blvd", "suite", "unit")):
        return cleaned
    return None


def _trim_contact_span(text: str) -> str:
    cleaned = " ".join(text.split())
    if not cleaned:
        return cleaned
    cutoff_tokens = (" phone ", " email ", " hours ", " contact ", " about ", " business information ", " operating hours ")
    lowered = f" {cleaned.lower()} "
    cut = len(cleaned)
    for token in cutoff_tokens:
        index = lowered.find(token)
        if index != -1:
            cut = min(cut, index)
    return cleaned[:cut].strip(" ,-")


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or "website"
