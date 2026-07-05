from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import structlog

from web_tools import _CONTACT_LINK_RE, _fetch_page, _resolve_homepage, _same_site, _slugify


LOGGER = structlog.get_logger(__name__).bind(source="web_email.py")

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_MAILTO_RE = re.compile(r"mailto:([^?\s#]+)", re.I)
_TEL_RE = re.compile(r"tel:([^?\s#]+)", re.I)
_EMAIL_WORD_RE = re.compile(r"\bemail\b", re.I)
_PHONE_WORD_RE = re.compile(r"\bphone\b", re.I)
_PHONE_LINE_RE = re.compile(r"^(?:\+\d{1,2}\s?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}$")
_ADDRESS_UNIT_RE = re.compile(r"\b(suite|ste|unit|building|bldg)\b", re.I)
_ADDRESS_STREET_RE = re.compile(r"\b(street|st|road|rd|dr|blvd|boul|lane|ln|court|ct|way|wy|pl|parkway|pk|cir|highway|hwy|hiway)\b", re.I)
_ADDRESS_REGEX = re.compile(
    r"\b(?P<number>\d{1,6})\s+"
    r"(?P<street>[A-Za-z0-9.'-]+(?:\s+[A-Za-z0-9.'-]+){0,7})\s+"
    r"(?:(?P<unit_type>Suite|Ste|Unit|Building|Bldg)\s+(?P<unit_value>[A-Za-z0-9.-]+)\s+)?"
    r"(?P<city>[A-Za-z][A-Za-z .'-]+?)\s*,?\s*"
    r"(?P<state>[A-Z]{2}|[A-Za-z][A-Za-z .'-]{1,20})\s+"
    r"(?P<zip>\d{5}(?:-\d{4})?)\b",
    re.I,
)


@dataclass
class EmailScrapeResult:
    input_value: str
    resolved_homepage: str | None
    discovered_pages: list[str] = field(default_factory=list)
    emails: list[dict[str, Any]] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)
    phones: list[dict[str, Any]] = field(default_factory=list)
    contacts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    full_json: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def scrape_emails(input_value: str, max_pages: int = 3, timeout_seconds: int = 20) -> EmailScrapeResult:
    LOGGER.info("Starting email scrape", input_value=input_value, max_pages=max_pages)
    homepage = _resolve_homepage(input_value)
    if not homepage:
        return EmailScrapeResult(input_value=input_value, resolved_homepage=None, notes=["Could not resolve a homepage from the provided input."])

    queue: list[str] = [homepage]
    contact_pages: list[str] = []
    visited: set[str] = set()
    pages: list[dict[str, Any]] = []

    while queue and len(visited) < max_pages:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        page = _fetch_page(url, timeout_seconds)
        if page is None:
            continue
        pages.append({"url": page.url, "title": page.title, "blocks": page.blocks, "links": page.links, "script_urls": page.script_urls})
        for link_text, href in page.links:
            candidate = f"{link_text} {href}"
            if not _CONTACT_LINK_RE.search(candidate):
                continue
            absolute = urljoin(url, href)
            if _same_site(homepage, absolute) and absolute not in visited and absolute not in queue:
                contact_pages.append(absolute)
                queue.append(absolute)

    email_hits: dict[str, dict[str, Any]] = {}
    address_hits: list[str] = []
    phone_hits: list[dict[str, Any]] = []
    contact_hits: list[dict[str, Any]] = []
    for page in pages:
        page_emails = _extract_emails_from_page(page)
        for item in page_emails:
            email = item["email"]
            key = email.lower()
            if key not in email_hits:
                email_hits[key] = {
                    "email": email,
                    "pages": [],
                    "methods": [],
                    "evidence": [],
                }
            hit = email_hits[key]
            if item["page"] not in hit["pages"]:
                hit["pages"].append(item["page"])
            if item["source"] not in hit["methods"]:
                hit["methods"].append(item["source"])
            hit["evidence"].append(item)
        address_hits.extend(_extract_addresses_from_page(page))
        phone_hits.extend(_extract_phones_from_page(page))
        contact_hits.extend(_extract_contacts_from_page(page))
    if not phone_hits:
        phone_hits.extend(_collect_phones_from_html(pages))

    js_fallback_used = False
    if not email_hits:
        LOGGER.info("No emails found in HTML pass; starting JS fallback", page_count=len(pages))
        js_hits = _scrape_js_for_emails(pages, homepage, timeout_seconds)
        if js_hits:
            js_fallback_used = True
            LOGGER.info("JS fallback returned email hits", hit_count=len(js_hits))
        else:
            LOGGER.info("JS fallback returned no email hits")
        for item in js_hits:
            email = item["email"]
            key = email.lower()
            if key not in email_hits:
                email_hits[key] = {
                    "email": email,
                    "pages": [],
                    "methods": [],
                    "evidence": [],
                }
            hit = email_hits[key]
            if item["page"] not in hit["pages"]:
                hit["pages"].append(item["page"])
            if item["source"] not in hit["methods"]:
                hit["methods"].append(item["source"])
            hit["evidence"].append(item)

    emails = list(email_hits.values())
    addresses = _dedupe_strings(address_hits)
    phones = _dedupe_phone_records(phone_hits)
    contacts = _dedupe_contacts(contact_hits)
    warnings = _validate_email_domains(emails, homepage) + _validate_phone_numbers(phones, homepage)
    notes = ["JS fallback scanned after HTML pass found no emails."] if js_fallback_used else []
    full_json = {
        "input": input_value,
        "homepage": homepage,
        "pages": [page["url"] for page in pages] + [page for page in contact_pages if page not in visited],
        "emails": emails,
        "addresses": addresses,
        "phones": phones,
        "contacts": contacts,
        "warnings": warnings,
        "notes": notes,
    }

    return EmailScrapeResult(
        input_value=input_value,
        resolved_homepage=homepage,
        discovered_pages=[page["url"] for page in pages] + [page for page in contact_pages if page not in visited],
        emails=emails,
        addresses=addresses,
        phones=phones,
        contacts=contacts,
        warnings=warnings,
        full_json=full_json,
        notes=notes,
    )


def save_email_scrape(result: EmailScrapeResult, output_dir: str | Path = ".state/web_email") -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = _slugify(result.resolved_homepage or result.input_value)
    file_path = output_path / f"{name}_{timestamp}.json"
    file_path.write_text(render_email_scrape_json(result), encoding="utf-8")
    LOGGER.info("Saved email scrape output", file=str(file_path))
    return file_path


def render_email_scrape_json(result: EmailScrapeResult) -> str:
    return json.dumps(
        result.full_json
        or {
            "input": result.input_value,
            "homepage": result.resolved_homepage,
            "pages": result.discovered_pages,
            "emails": result.emails,
            "addresses": result.addresses,
            "phones": result.phones,
            "contacts": result.contacts,
            "warnings": result.warnings,
            "notes": result.notes,
        },
        indent=2,
        sort_keys=True,
    )


def render_email_scrape_text(result: EmailScrapeResult) -> str:
    lines = [
        f"Input: {result.input_value}",
        f"Homepage: {result.resolved_homepage or 'unresolved'}",
        f"Pages checked: {len(result.discovered_pages)}",
        f"Emails: {', '.join(item['email'] for item in result.emails) if result.emails else 'none found'}",
    ]
    if result.addresses:
        lines.append(f"Addresses: {', '.join(result.addresses)}")
    if result.phones:
        lines.append("Phones:")
        for phone in result.phones:
            lines.append(f"- {phone['phone']} ({phone.get('description') or 'no description'})")
    if result.contacts:
        lines.append("Contacts:")
        for contact in result.contacts:
            lines.append(
                f"- {contact.get('first_name', '')} {contact.get('last_name', '')}".strip()
                + f" | {contact.get('description') or 'no description'}"
            )
    if result.notes:
        lines.append("Notes:")
        lines.extend(f"- {note}" for note in result.notes)
    return "\n".join(lines)


def _extract_emails_from_page(page: dict[str, Any]) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for block in page.get("blocks", []):
        if not isinstance(block, str):
            continue
        results.extend(_extract_emails_from_block(block, page.get("url", "")))
    return results


def _extract_addresses_from_page(page: dict[str, Any]) -> list[str]:
    results: list[str] = []
    for block in page.get("blocks", []):
        if not isinstance(block, str):
            continue
        results.extend(_extract_address_candidates(block))
    return results


def _extract_phones_from_page(page: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for block in page.get("blocks", []):
        if not isinstance(block, str):
            continue
        results.extend(_extract_phones_from_block(block, page.get("url", "")))
        for phone in _extract_phone_candidates(block):
            results.append({"phone": phone, "description": _infer_phone_description(block), "page": page.get("url", "")})
    return results


def _extract_contacts_from_page(page: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for block in page.get("blocks", []):
        if not isinstance(block, str):
            continue
        contact = _extract_contact_candidate(block, page.get("url", ""))
        if contact:
            results.append(contact)
    return results


def _collect_phones_from_html(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for page in pages:
        for block in page.get("blocks", []):
            if not isinstance(block, str):
                continue
            results.extend(_extract_phones_from_block(block, page.get("url", "")))
    return results


def _extract_emails_from_block(block: str, page_url: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for match in _MAILTO_RE.finditer(block):
        email = match.group(1).strip().strip(">,;")
        if _looks_like_email(email):
            results.append({"email": email.lower(), "page": page_url, "source": "mailto"})
    for line in re.split(r"[\n\r]+", block):
        cleaned = " ".join(line.split())
        if not cleaned:
            continue
        if not _EMAIL_WORD_RE.search(cleaned):
            continue
        if "@" not in cleaned or "." not in cleaned:
            continue
        for email in _EMAIL_RE.findall(cleaned):
            if _looks_like_email(email):
                results.append({"email": email.lower(), "page": page_url, "source": "keyword-email"})
    return results


def _extract_phones_from_block(block: str, page_url: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for match in _TEL_RE.finditer(block):
        phone = _normalize_phone_candidate(match.group(1))
        if phone:
            results.append({"phone": phone, "description": "tel link", "page": page_url, "source": "tel"})
    for line in re.split(r"[\n\r]+", block):
        cleaned = " ".join(line.split())
        if not cleaned or not _PHONE_WORD_RE.search(cleaned):
            continue
        phone = _match_phone_line(cleaned)
        if phone:
            results.append({"phone": phone, "description": "phone line", "page": page_url, "source": "phone-line"})
    return results


def _extract_address_candidates(block: str) -> list[str]:
    lines = [line.strip() for line in re.split(r"[\n\r]+", block) if line.strip()]
    results: list[str] = []
    for index, line in enumerate(lines):
        unit_candidate = _address_candidate_from_line(line, lines, index, _ADDRESS_UNIT_RE)
        if unit_candidate:
            results.append(unit_candidate)
            continue
        street_candidate = _address_candidate_from_line(line, lines, index, _ADDRESS_STREET_RE)
        if street_candidate:
            results.append(street_candidate)
    return results


def _address_candidate_from_line(line: str, lines: list[str], index: int, pattern: re.Pattern[str]) -> str | None:
    lowered = line.lower()
    if not pattern.search(lowered):
        return None
    if not any(ch.isdigit() for ch in line):
        return None
    return _normalize_address_candidate(line, pattern)


def _normalize_address_candidate(candidate: str, pattern: re.Pattern[str]) -> str | None:
    cleaned = " ".join(candidate.split()).strip(" ,-")
    if not cleaned or len(cleaned) < 10:
        return None
    match = _ADDRESS_REGEX.search(cleaned)
    if not match:
        return None
    if pattern is _ADDRESS_UNIT_RE and not _ADDRESS_UNIT_RE.search(cleaned):
        return None
    if pattern is _ADDRESS_STREET_RE and not _ADDRESS_STREET_RE.search(cleaned):
        return None
    address = f"{match.group('number')} {match.group('street').strip()}"
    if match.group("unit_type") and match.group("unit_value"):
        address += f" {match.group('unit_type')} {match.group('unit_value')}"
    address += f" {match.group('city').strip()}, {match.group('state')} {match.group('zip')}"
    address = re.sub(r"\s+", " ", address).strip(" ,-")
    return address


def _extract_phone_candidates(block: str) -> list[str]:
    phones: list[str] = []
    for match in re.finditer(r"(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}", block):
        phones.append(match.group(0))
    return _dedupe_strings(phones)


def _match_phone_line(text: str) -> str | None:
    match = re.search(r"(?:\+\d{1,2}\s?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}", text)
    if not match:
        return None
    candidate = _normalize_phone_candidate(match.group(0))
    return candidate


def _normalize_phone_candidate(value: str) -> str | None:
    cleaned = " ".join(value.split()).strip()
    if not cleaned:
        return None
    digits = re.sub(r"\D+", "", cleaned)
    if len(digits) < 10:
        return None
    return cleaned


def _infer_phone_description(block: str) -> str | None:
    lowered = block.lower()
    for keyword, description in (
        ("main", "main line"),
        ("support", "support"),
        ("office", "office"),
        ("sales", "sales"),
        ("contact", "contact"),
        ("help", "help"),
        ("text", "text"),
    ):
        if keyword in lowered:
            return description
    return None


def _extract_contact_candidate(block: str, page_url: str) -> dict[str, Any] | None:
    if not any(keyword in block.lower() for keyword in ("team", "about", "leadership", "staff", "manager", "founder", "owner")):
        return None
    name_match = re.search(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b", block)
    if not name_match:
        return None
    phones = _extract_phone_candidates(block)
    if not phones:
        return None
    parts = name_match.group(1).split()
    return {
        "first_name": parts[0],
        "last_name": parts[1] if len(parts) > 1 else None,
        "description": _infer_phone_description(block),
        "phone": phones[0],
        "page": page_url,
    }


def _looks_like_email(value: str) -> bool:
    return bool(_EMAIL_RE.fullmatch(value.strip()))


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.lower().strip()
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _dedupe_phone_records(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for value in values:
        phone = value.get("phone")
        if not isinstance(phone, str):
            continue
        key = phone.lower().strip()
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _dedupe_contacts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, Any]] = []
    for value in values:
        first = str(value.get("first_name", "")).strip().lower()
        last = str(value.get("last_name", "")).strip().lower()
        phone = str(value.get("phone", "")).strip().lower()
        key = (first, last, phone)
        if key not in seen and (first or last or phone):
            seen.add(key)
            result.append(value)
    return result


def _validate_email_domains(emails: list[dict[str, Any]], homepage: str) -> list[dict[str, Any]]:
    site_domain = _hostname_from_url(homepage)
    warnings: list[dict[str, Any]] = []
    for email_item in emails:
        email = email_item.get("email")
        if not isinstance(email, str) or "@" not in email:
            continue
        email_domain = email.split("@", 1)[1].lower()
        if site_domain and site_domain.lower() not in email_domain:
            warnings.append(
                {
                    "email": email,
                    "tag": "domain_mismatch",
                    "message": f"Email domain {email_domain} does not contain site domain {site_domain}.",
                    "page": (email_item.get("pages") or [None])[0],
                }
            )
    return warnings


def _validate_phone_numbers(phones: list[dict[str, Any]], homepage: str) -> list[dict[str, Any]]:
    site_domain = _hostname_from_url(homepage)
    warnings: list[dict[str, Any]] = []
    for phone_item in phones:
        description = str(phone_item.get("description") or "").lower()
        if site_domain and description and "support" in description:
            continue
        phone = phone_item.get("phone")
        if not isinstance(phone, str):
            continue
        if not _PHONE_LINE_RE.match(phone):
            warnings.append(
                {
                    "phone": phone,
                    "tag": "phone_format",
                    "message": "Phone number did not match the expected phone pattern.",
                    "page": phone_item.get("page"),
                }
            )
    return warnings


def _hostname_from_url(url: str) -> str | None:
    match = re.match(r"^[a-z]+://([^/]+)", url, re.I)
    if not match:
        return None
    return match.group(1).lower().lstrip("www.")


def _scrape_js_for_emails(pages: list[dict[str, Any]], homepage: str, timeout_seconds: int) -> list[dict[str, str]]:
    site_domain = _hostname_from_url(homepage)
    script_urls: list[str] = []
    for page in pages:
        base_url = page.get("url", homepage)
        for script_url in page.get("script_urls", []) or []:
            if isinstance(script_url, str) and _js_url_allowed(script_url, site_domain) and script_url not in script_urls:
                script_urls.append(script_url)
        for block in page.get("blocks", []):
            if not isinstance(block, str):
                continue
            for href in _extract_js_hrefs(block, base_url):
                if _js_url_allowed(href, site_domain) and href not in script_urls:
                    script_urls.append(href)
    LOGGER.info("Discovered JS assets", count=len(script_urls), pages=len(pages))
    results: list[dict[str, str]] = []
    for url in script_urls[:20]:
        LOGGER.info("Fetching JS asset", url=url)
        js_text = _fetch_text_asset(url, timeout_seconds)
        if not js_text:
            LOGGER.info("JS asset fetch returned no text", url=url)
            continue
        LOGGER.info("Fetched JS asset text", url=url, chars=len(js_text))
        for email in _EMAIL_RE.findall(js_text):
            if _looks_like_email(email):
                results.append({"email": email.lower(), "page": url, "source": "js-file"})
    LOGGER.info("Completed JS fallback scan", hit_count=len(results), asset_count=len(script_urls))
    return results


def _extract_js_hrefs(block: str, base_url: str) -> list[str]:
    hrefs: list[str] = []
    if ".js" not in block.lower():
        return hrefs
    for match in re.finditer(r"""(?:src|href)\s*=\s*["']([^"']+\.js(?:\?[^"']*)?)["']|['"]([^'"]+\.js(?:\?[^'"]*)?)['"]""", block, re.I):
        candidate = match.group(1) or match.group(2)
        if not candidate:
            continue
        absolute = urljoin(base_url, candidate)
        if absolute not in hrefs:
            hrefs.append(absolute)
    return hrefs


def _js_url_allowed(url: str, site_domain: str | None) -> bool:
    host = _hostname_from_url(url)
    if not host:
        return False
    if site_domain and site_domain not in host:
        return False
    blocked = ("googletagmanager", "google-analytics", "doubleclick", "facebook", "hotjar", "intercom", "segment", "mixpanel", "hubspot", "clarity", "cdn")
    return not any(token in host for token in blocked)


def _fetch_text_asset(url: str, timeout_seconds: int) -> str | None:
    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 (Argowake web parser)"})
        with urlopen(request, timeout=timeout_seconds) as response:
            content_type = response.headers.get("Content-Type", "")
            if content_type and "javascript" not in content_type and "text/plain" not in content_type:
                LOGGER.info("Skipped non-JS asset", url=url, content_type=content_type)
                return None
            text = response.read().decode("utf-8", errors="replace")
            return text
    except (HTTPError, URLError, TimeoutError):
        LOGGER.exception("Fetch JS asset failed", url=url)
        return None
