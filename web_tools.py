from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

import structlog

try:
    from bs4 import BeautifulSoup  # type: ignore
except Exception:  # pragma: no cover
    BeautifulSoup = None

try:
    import extruct  # type: ignore
except Exception:  # pragma: no cover
    extruct = None

try:
    import phonenumbers  # type: ignore
except Exception:  # pragma: no cover
    phonenumbers = None

try:
    import usaddress  # type: ignore
except Exception:  # pragma: no cover
    usaddress = None


LOGGER = structlog.get_logger(__name__).bind(source="web_tools.py")

_CONTACT_LINK_RE = re.compile(
    r"\b(contact|about|team|company|who-we-are|our-story|get in touch|reach out|connect|meet the team|our people|who we are|the team|contact us)\b",
    re.I,
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}")
_POSTAL_RE = re.compile(r"\b\d{1,6}\s+[A-Za-z0-9.'-]+(?:\s+[A-Za-z0-9.'-]+){0,5},?\s+[A-Z]{2}\s+\d{5}(?:-\d{4})?\b")
_STREET_ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9.'-]+(?:\s+[A-Za-z0-9.'-]+){0,5}\s+"
    r"(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Ln|Lane|Dr|Drive|Ct|Court|Way|Pkwy|Parkway|Pl|Place|Cir|Circle|Hwy|Highway)\.?"
    r"(?:,?\s+[A-Za-z0-9.'-]+(?:\s+[A-Za-z0-9.'-]+){0,3},\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?)?",
    re.I,
)
_PERSON_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b")
_ROLE_RE = re.compile(
    r"\b(founder|co-founder|ceo|owner|president|principal|general manager|director|manager|"
    r"marketing manager|sales manager|project manager|accounting manager|hr manager|production manager|"
    r"graphic designer|sign tech|chief executive officer|chief operating officer)\b",
    re.I,
)
_TEAM_CONTEXT_RE = re.compile(r"\b(team|about us|leadership|people|staff|management)\b", re.I)
_ROLE_WORDS = {"general", "senior", "junior", "lead", "founder", "owner", "president", "principal", "ceo", "cto", "cfo", "manager", "director"}
_BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "li",
    "tr",
    "td",
    "th",
    "header",
    "footer",
    "main",
    "aside",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}


@dataclass
class ParsedPage:
    url: str
    title: str | None = None
    text: str = ""
    blocks: list[str] = field(default_factory=list)
    structured_data: list[dict[str, Any]] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class WebsiteParseResult:
    input_value: str
    resolved_homepage: str | None
    discovered_pages: list[str]
    names: list[str]
    founder_names: list[str]
    contacts: list[dict[str, str]]
    contact_addresses: list[str]
    contact_emails: list[str]
    contact_phones: list[str]
    notes: list[str]


def save_website_parse_json(result: WebsiteParseResult, output_dir: str | Path = ".state/scrapes") -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    company_name = _derive_company_name(result)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    file_path = output_path / f"{_slugify(company_name)}_{timestamp}.json"
    file_path.write_text(render_website_parse_json(result), encoding="utf-8")
    LOGGER.info("Saved website parse JSON", file=str(file_path))
    return file_path


def parse_website(input_value: str, max_pages: int = 3, timeout_seconds: int = 20) -> WebsiteParseResult:
    LOGGER.info("Parsing website", input_value=input_value, max_pages=max_pages)
    try:
        homepage = _resolve_homepage(input_value)
        if not homepage:
            return WebsiteParseResult(
                input_value=input_value,
                resolved_homepage=None,
                discovered_pages=[],
                names=[],
                founder_names=[],
                contacts=[],
                contact_addresses=[],
                contact_emails=[],
                contact_phones=[],
                notes=["Could not resolve a homepage from the provided input."],
            )

        pages_to_visit = [homepage]
        visited: set[str] = set()
        discovered_pages: list[str] = []
        names: list[str] = []
        founder_names: list[str] = []
        contacts: list[dict[str, str]] = []
        contact_addresses: list[str] = []
        contact_emails: list[str] = []
        contact_phones: list[str] = []
        notes: list[str] = []
        seen_contacts: set[tuple[str, str, str, str]] = set()
        initial_pages: list[ParsedPage] = []
        second_stage_pages: list[str] = []

        while pages_to_visit and len(visited) < max_pages:
            url = pages_to_visit.pop(0)
            if url in visited:
                continue
            visited.add(url)
            page = _fetch_page(url, timeout_seconds)
            if page is None:
                notes.append(f"Failed to fetch {url}.")
                continue
            discovered_pages.append(url)
            initial_pages.append(page)
            if _is_likely_company_page(page):
                contact_addresses.extend(_dedupe(_collect_addresses(page)))
                names.extend(_dedupe(_collect_names(page)))
                founder_names.extend(_dedupe(_collect_founders(page)))
                contacts.extend(_collect_contacts(page, seen_contacts))

            for link_text, href in page.links:
                candidate = f"{link_text} {href}"
                if not _CONTACT_LINK_RE.search(candidate):
                    continue
                absolute = urljoin(url, href)
                if _same_site(homepage, absolute) and absolute not in visited and absolute not in pages_to_visit:
                    second_stage_pages.append(absolute)

        if _has_likely_company(initial_pages) and second_stage_pages:
            for url in second_stage_pages:
                if len(visited) >= max_pages:
                    break
                if url in visited:
                    continue
                visited.add(url)
                page = _fetch_page(url, timeout_seconds)
                if page is None:
                    notes.append(f"Failed to fetch {url}.")
                    continue
                discovered_pages.append(url)
                contact_addresses.extend(_dedupe(_collect_addresses(page)))
                names.extend(_dedupe(_collect_names(page)))
                founder_names.extend(_dedupe(_collect_founders(page)))
                contacts.extend(_collect_contacts(page, seen_contacts))

        for contact in contacts:
            if email := contact.get("email"):
                contact_emails.append(email)
            if phone := contact.get("phone"):
                contact_phones.append(phone)

        return WebsiteParseResult(
            input_value=input_value,
            resolved_homepage=homepage,
            discovered_pages=discovered_pages,
            names=_dedupe(names),
            founder_names=_dedupe(founder_names),
            contacts=contacts,
            contact_addresses=_dedupe(contact_addresses),
            contact_emails=_dedupe(contact_emails),
            contact_phones=_dedupe(contact_phones),
            notes=notes,
        )
    except Exception:
        LOGGER.exception("Website parse failed")
        raise


def render_website_parse_result(result: WebsiteParseResult) -> str:
    lines = [
        f"Input: {result.input_value}",
        f"Homepage: {result.resolved_homepage or 'unresolved'}",
        f"Pages checked: {len(result.discovered_pages)}",
    ]
    if result.discovered_pages:
        lines.extend(f"- {page}" for page in result.discovered_pages)
    lines.append(f"Names: {', '.join(result.names) if result.names else 'none found'}")
    lines.append(f"Founder names: {', '.join(result.founder_names) if result.founder_names else 'none found'}")
    lines.append(f"Contacts: {len(result.contacts)} found")
    lines.append(f"Contact addresses: {', '.join(result.contact_addresses) if result.contact_addresses else 'none found'}")
    lines.append(f"Emails: {', '.join(result.contact_emails) if result.contact_emails else 'none found'}")
    lines.append(f"Phones: {', '.join(result.contact_phones) if result.contact_phones else 'none found'}")
    if result.notes:
        lines.append("Notes:")
        lines.extend(f"- {note}" for note in result.notes)
    return "\n".join(lines)


def render_website_parse_json(result: WebsiteParseResult) -> str:
    return json.dumps(
        {
            "input": result.input_value,
            "homepage": result.resolved_homepage,
            "pages": result.discovered_pages,
            "names": result.names,
            "founder_names": result.founder_names,
            "contacts": result.contacts,
            "addresses": result.contact_addresses,
            "emails": result.contact_emails,
            "phones": result.contact_phones,
            "notes": result.notes,
        },
        indent=2,
        sort_keys=True,
    )


def _fetch_page(url: str, timeout_seconds: int) -> ParsedPage | None:
    LOGGER.info("Fetching page", url=url)
    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 (Argowake web parser)"})
        with urlopen(request, timeout=timeout_seconds) as response:
            content_type = response.headers.get("Content-Type", "")
            if "text/html" not in content_type and "application/xhtml+xml" not in content_type and content_type:
                return None
            html = response.read().decode("utf-8", errors="replace")
        return _parse_html_page(url, html)
    except (HTTPError, URLError, TimeoutError):
        LOGGER.exception("Fetch page failed", url=url)
        return None


def _parse_html_page(url: str, html: str) -> ParsedPage:
    if BeautifulSoup is None:
        text = _normalize_whitespace(html)
        return ParsedPage(url=url, text=text, blocks=[text] if text else [], structured_data=_extract_structured_data(html, url))

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        if tag.name == "script" and tag.get("type", "").lower() == "application/ld+json":
            continue
        tag.decompose()

    title = soup.title.get_text(" ", strip=True) if soup.title else None
    links: list[tuple[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        text = _normalize_whitespace(anchor.get_text(" ", strip=True))
        links.append((text, anchor["href"]))

    blocks: list[str] = []
    for node in soup.find_all(list(_BLOCK_TAGS)):
        text = _normalize_whitespace(node.get_text("\n", strip=True))
        if text:
            blocks.append(text)
    if not blocks:
        body_text = _normalize_whitespace(soup.get_text("\n", strip=True))
        if body_text:
            blocks.append(body_text)

    return ParsedPage(
        url=url,
        title=title,
        text="\n".join(blocks),
        blocks=blocks,
        structured_data=_extract_structured_data(html, url),
        links=links,
    )


def _extract_structured_data(html: str, url: str) -> list[dict[str, Any]]:
    if extruct is None:
        return []
    try:
        extracted = extruct.extract(html, base_url=url, uniform=True)
    except Exception:
        LOGGER.exception("Structured data extraction failed", url=url)
        return []
    results: list[dict[str, Any]] = []
    for key in ("json-ld", "microdata", "rdfa"):
        value = extracted.get(key)
        if isinstance(value, list):
            results.extend(item for item in value if isinstance(item, dict))
    return results


def _collect_emails(page: ParsedPage) -> list[str]:
    return []


def _collect_addresses(page: ParsedPage) -> list[str]:
    results: list[str] = []
    for item in page.structured_data:
        address = item.get("address")
        if isinstance(address, dict):
            parts = [address.get(key) for key in ("streetAddress", "addressLocality", "addressRegion", "postalCode") if isinstance(address.get(key), str)]
            if parts:
                results.append(_normalize_whitespace(", ".join(parts)))
        elif isinstance(address, str):
            validated = _validate_address(address)
            if validated:
                results.append(validated)
    if results:
        return results

    for block in page.blocks:
        lines = [line.strip() for line in re.split(r"[\n\r]+", block) if line.strip()]
        for index, line in enumerate(lines):
            if not re.search(r"\baddress\b\s*:?", line, re.I):
                continue
            candidate_lines: list[str] = []
            first = re.sub(r"(?i)^address\s*:?\s*", "", line).strip()
            if first:
                candidate_lines.append(first)
            for offset in range(1, 4):
                if index + offset < len(lines):
                    candidate_lines.append(lines[index + offset])
            validated = _validate_address(" ".join(candidate_lines))
            if validated:
                results.append(validated)
    return results


def _collect_names(page: ParsedPage) -> list[str]:
    results: list[str] = []
    for item in page.structured_data:
        label = _type_label(item)
        if "person" in label and isinstance(item.get("name"), str):
            name = item["name"].strip()
            if _looks_like_name(name):
                results.append(name)
        if any(term in label for term in ("organization", "localbusiness", "place")) and isinstance(item.get("name"), str):
            results.append(item["name"].strip())
    return results


def _collect_founders(page: ParsedPage) -> list[str]:
    results: list[str] = []
    for item in page.structured_data:
        label = _type_label(item)
        if any(term in label for term in ("founder", "owner", "president", "ceo", "principal")) and isinstance(item.get("name"), str):
            if _looks_like_name(item["name"]):
                results.append(item["name"].strip())
    return results


def _is_likely_company_page(page: ParsedPage) -> bool:
    if page.structured_data:
        return True
    if page.title and any(term in page.title.lower() for term in ("about", "contact", "company", "team")):
        return True
    return bool(page.blocks)


def _has_likely_company(pages: list[ParsedPage]) -> bool:
    if not pages:
        return False
    for page in pages:
        if page.structured_data:
            for item in page.structured_data:
                if any(term in _type_label(item) for term in ("organization", "localbusiness", "person", "place")):
                    return True
        if page.title and any(term in page.title.lower() for term in ("about", "contact", "company")):
            return True
    return True


def _collect_contacts(page: ParsedPage, seen_contacts: set[tuple[str, str, str, str]]) -> list[dict[str, str]]:
    contacts: list[dict[str, str]] = []
    for item in page.structured_data:
        contact = _contact_from_structured_item(item, page.url)
        if contact:
            key = (contact.get("first_name", "").lower(), contact.get("last_name", "").lower(), contact.get("email", "").lower(), contact.get("phone", "").lower())
            if key not in seen_contacts:
                seen_contacts.add(key)
                contacts.append(contact)

    for block in page.blocks:
        contact = _contact_from_block(block, page.url)
        if contact:
            key = (contact.get("first_name", "").lower(), contact.get("last_name", "").lower(), contact.get("email", "").lower(), contact.get("phone", "").lower())
            if key not in seen_contacts:
                seen_contacts.add(key)
                contacts.append(contact)
    return contacts


def _contact_from_structured_item(item: dict[str, Any], page_url: str) -> dict[str, str] | None:
    if "person" not in _type_label(item):
        return None
    name = item.get("name")
    if not isinstance(name, str) or not _looks_like_name(name):
        return None
    first_name, last_name = _split_name(name)
    if not first_name or not last_name:
        return None
    contact: dict[str, str] = {"first_name": first_name, "last_name": last_name, "source_page": page_url}
    for key in ("jobTitle", "title"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            contact["title"] = value.strip()
            break
    email = item.get("email")
    if isinstance(email, str) and email.strip():
        contact["email"] = email.strip()
    phone = item.get("telephone")
    if isinstance(phone, str):
        normalized = _normalize_phone(phone)
        if normalized:
            contact["phone"] = normalized
    return contact if len(contact) > 3 else None


def _contact_from_block(block: str, page_url: str) -> dict[str, str] | None:
    lines = [line.strip() for line in re.split(r"[\n\r]+", block) if line.strip()]
    if not lines:
        return None
    joined = " ".join(lines)
    if not _TEAM_CONTEXT_RE.search(joined) and not _ROLE_RE.search(joined):
        return None

    name = None
    for line in lines[:4]:
        candidate = _first_person_name(line)
        if candidate:
            name = candidate
            break
    if not name:
        return None

    email = _first_match(joined, _EMAIL_RE)
    phone = _first_phone(joined)
    title = _first_role(joined)
    if not (email or phone):
        return None

    first_name, last_name = _split_name(name)
    if not first_name or not last_name:
        return None
    contact: dict[str, str] = {"first_name": first_name, "last_name": last_name, "source_page": page_url}
    if title:
        contact["title"] = title
    if email:
        contact["email"] = email
    if phone:
        contact["phone"] = phone
    return contact


def _first_person_name(text: str) -> str | None:
    if any(term in text.lower() for term in ("contact us", "about us", "learn more", "privacy")):
        return None
    match = _PERSON_RE.search(text)
    if not match:
        return None
    candidate = match.group(1).strip()
    return candidate if _looks_like_name(candidate) else None


def _first_role(text: str) -> str | None:
    match = _ROLE_RE.search(text)
    return _normalize_whitespace(match.group(1)) if match else None


def _first_match(text: str, pattern: re.Pattern[str]) -> str | None:
    match = pattern.search(text)
    return match.group(0) if match else None


def _first_phone(text: str) -> str | None:
    if phonenumbers is not None:
        for match in _PHONE_RE.finditer(text):
            normalized = _normalize_phone(match.group(0))
            if normalized:
                return normalized
    return None


def _normalize_phone(value: str) -> str | None:
    if phonenumbers is None:
        digits = re.sub(r"\D+", "", value)
        return value if len(digits) >= 10 else None
    try:
        parsed = phonenumbers.parse(value, "US")
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.NATIONAL)
    except Exception:
        return None
    return None


def _validate_address(text: str) -> str | None:
    if usaddress is not None:
        try:
            parsed, label = usaddress.tag(text)
            if label in {"Street Address", "PO Box", "Intersection", "Landmark"}:
                parts = [parsed.get(key) for key in ("AddressNumber", "StreetName", "StreetNamePostType", "OccupancyType", "OccupancyIdentifier", "PlaceName", "StateName", "ZipCode") if parsed.get(key)]
                if parts:
                    return _normalize_whitespace(", ".join(parts))
        except Exception:
            pass
    if _POSTAL_RE.search(text) or _STREET_ADDRESS_RE.search(text):
        return _normalize_whitespace(text)
    return None


def _type_label(item: dict[str, Any]) -> str:
    value = item.get("@type")
    if isinstance(value, list):
        return " ".join(str(part).lower() for part in value)
    if isinstance(value, str):
        return value.lower()
    return ""


def _looks_like_name(candidate: str) -> bool:
    parts = candidate.split()
    return 2 <= len(parts) <= 3 and all(part[:1].isalpha() and part[:1].isupper() and part[1:].islower() for part in parts) and not any(part.lower() in _ROLE_WORDS for part in parts)


def _split_name(name: str) -> tuple[str | None, str | None]:
    parts = [part for part in name.split() if part.lower() not in _ROLE_WORDS]
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


def _derive_company_name(result: WebsiteParseResult) -> str:
    if result.names:
        return result.names[0]
    if result.resolved_homepage:
        host = urlparse(result.resolved_homepage).netloc.lower().removeprefix("www.")
        return host.split(".")[0]
    return "website"


def _resolve_homepage(input_value: str) -> str | None:
    candidate = input_value.strip()
    if not candidate:
        return None
    if not re.match(r"^https?://", candidate, re.I):
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    if not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _same_site(base_url: str, candidate_url: str) -> bool:
    return urlparse(base_url).netloc.lower().lstrip("www.") == urlparse(candidate_url).netloc.lower().lstrip("www.")


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or "website"
