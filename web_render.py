from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import structlog

from web_tools import ParsedPage, _fetch_page, _resolve_homepage, _same_site, _slugify


LOGGER = structlog.get_logger(__name__).bind(source="web_render.py")


@dataclass
class RenderedWebPages:
    input_value: str
    resolved_homepage: str | None
    discovered_pages: list[str] = field(default_factory=list)
    page_text: list[dict[str, Any]] = field(default_factory=list)
    prompt: str = ""
    notes: list[str] = field(default_factory=list)


def render_site_for_llm(
    input_value: str,
    max_pages: int = 3,
    max_blocks: int = 12,
    timeout_seconds: int = 20,
) -> RenderedWebPages:
    LOGGER.info("Rendering site for LLM", input_value=input_value, max_pages=max_pages)
    homepage = _resolve_homepage(input_value)
    if not homepage:
        return RenderedWebPages(
            input_value=input_value,
            resolved_homepage=None,
            notes=["Could not resolve a homepage from the provided input."],
        )

    discovered_pages: list[str] = []
    page_text: list[dict[str, Any]] = []
    notes: list[str] = []
    visited: set[str] = set()
    queued: list[str] = [homepage]

    while queued and len(visited) < max_pages:
        url = queued.pop(0)
        if url in visited:
            continue
        visited.add(url)
        page = _fetch_page(url, timeout_seconds)
        if page is None:
            notes.append(f"Failed to fetch {url}.")
            continue
        discovered_pages.append(url)
        page_text.append(_page_payload(page))
        for link_text, href in page.links:
            candidate = f"{link_text} {href}"
            if not any(keyword in candidate.lower() for keyword in ("contact", "about", "team", "company", "our story", "get in touch", "reach out", "connect", "leadership", "people", "who we are")):
                continue
            absolute = urljoin(url, href)
            if _same_site(homepage, absolute) and absolute not in visited and absolute not in queued:
                queued.append(absolute)

    page_text = [_dedupe_page_blocks(page) for page in page_text]
    page_text = _filter_cross_page_blocks(page_text)
    page_text = [_limit_page_blocks(page, max_blocks=max_blocks) for page in page_text]
    prompt = _build_prompt(input_value=input_value, homepage=homepage, pages=page_text)
    return RenderedWebPages(
        input_value=input_value,
        resolved_homepage=homepage,
        discovered_pages=discovered_pages,
        page_text=page_text,
        prompt=prompt,
        notes=notes,
    )


def save_rendered_web_pages(result: RenderedWebPages, output_dir: str | Path = ".state/web_render") -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = _slugify(result.resolved_homepage or result.input_value)
    file_path = output_path / f"{name}_{timestamp}.json"
    file_path.write_text(render_rendered_web_pages_json(result), encoding="utf-8")
    LOGGER.info("Saved rendered site JSON", file=str(file_path))
    return file_path


def render_rendered_web_pages_json(result: RenderedWebPages) -> str:
    return json.dumps(
        {
            "input": result.input_value,
            "homepage": result.resolved_homepage,
            "pages": result.discovered_pages,
            "prompt": result.prompt,
            "page_text": result.page_text,
            "notes": result.notes,
        },
        indent=2,
        sort_keys=True,
    )


def render_rendered_web_pages_text(result: RenderedWebPages) -> str:
    lines = [
        f"Input: {result.input_value}",
        f"Homepage: {result.resolved_homepage or 'unresolved'}",
        f"Pages checked: {len(result.discovered_pages)}",
    ]
    lines.append("Prompt:")
    lines.append(result.prompt)
    lines.append("Pages:")
    for page in result.page_text:
        lines.append(f"- URL: {page['url']}")
        lines.append(f"  Title: {page.get('title') or 'none'}")
        lines.append("  Text:")
        lines.extend(f"    {line}" for line in page.get("blocks", []))
    if result.notes:
        lines.append("Notes:")
        lines.extend(f"- {note}" for note in result.notes)
    return "\n".join(lines)


def _page_payload(page: ParsedPage) -> dict[str, Any]:
    return {
        "url": page.url,
        "title": page.title,
        "blocks": page.blocks,
    }


def _dedupe_page_blocks(page: dict[str, Any]) -> dict[str, Any]:
    blocks = page.get("blocks", [])
    if not isinstance(blocks, list):
        return page
    deduped: list[str] = []
    seen: set[str] = set()
    for block in blocks:
        if not isinstance(block, str):
            continue
        cleaned = _normalize_block(block)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cleaned)
    return {**page, "blocks": deduped}


def _filter_cross_page_blocks(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    block_counts: dict[str, int] = {}
    block_lengths: dict[str, int] = {}
    normalized_pages: list[dict[str, Any]] = []
    for page in pages:
        blocks = page.get("blocks", [])
        if not isinstance(blocks, list):
            normalized_pages.append(page)
            continue
        normalized_blocks: list[str] = []
        for block in blocks:
            if not isinstance(block, str):
                continue
            cleaned = _normalize_block(block)
            if not cleaned:
                continue
            key = cleaned.lower()
            block_counts[key] = block_counts.get(key, 0) + 1
            block_lengths[key] = len(cleaned)
            normalized_blocks.append(cleaned)
        normalized_pages.append({**page, "blocks": normalized_blocks})

    filtered_pages: list[dict[str, Any]] = []
    for page in normalized_pages:
        blocks = page.get("blocks", [])
        if not isinstance(blocks, list):
            filtered_pages.append(page)
            continue
        filtered_blocks = [
            block
            for block in blocks
            if _keep_block(block, block_counts, block_lengths)
        ]
        filtered_pages.append({**page, "blocks": filtered_blocks})
    return filtered_pages


def _limit_page_blocks(page: dict[str, Any], max_blocks: int) -> dict[str, Any]:
    blocks = page.get("blocks", [])
    if not isinstance(blocks, list):
        return page
    scored: list[tuple[int, str]] = []
    for block in blocks:
        if not isinstance(block, str):
            continue
        cleaned = _normalize_block(block)
        if not cleaned:
            continue
        scored.append((len(cleaned), cleaned))
    scored.sort(key=lambda item: item[0], reverse=True)
    limited = [block for _, block in scored[:max_blocks]]
    return {**page, "blocks": limited}


def _build_prompt(input_value: str, homepage: str, pages: list[dict[str, Any]]) -> str:
    pages_json = json.dumps(pages, indent=2, ensure_ascii=False)
    return (
        "You are extracting company contact information from website text.\n"
        "Return ONLY valid JSON with these fields:\n"
        '{\n'
        '  "company_name": string | null,\n'
        '  "contacts": [\n'
        '    {\n'
        '      "first_name": string | null,\n'
        '      "last_name": string | null,\n'
        '      "title": string | null,\n'
        '      "email": string | null,\n'
        '      "phone": string | null,\n'
        '      "address": string | null,\n'
        '      "source_url": string | null,\n'
        '      "confidence": number\n'
        '    }\n'
        '  ],\n'
        '  "addresses": [string],\n'
        '  "emails": [string],\n'
        '  "phones": [string],\n'
        '  "notes": [string]\n'
        '}\n'
        "Rules:\n"
        "- Use only the provided page text.\n"
        "- Ignore partial names unless first and last name are both clear.\n"
        "- Prefer explicit labels like Address, Phone, Email, Title, Founder, Owner.\n"
        "- Do not invent data.\n"
        "- Keep notes short and factual.\n"
        f"Input website: {input_value}\n"
        f"Resolved homepage: {homepage}\n"
        f"Page payloads:\n{pages_json}\n"
    )


def _normalize_block(text: str) -> str:
    text = " ".join(text.split())
    return text.strip()


def _keep_block(block: str, block_counts: dict[str, int], block_lengths: dict[str, int]) -> bool:
    lowered = block.lower()
    if not block.strip():
        return False
    if _is_menu_or_form_block(lowered):
        return False
    if block_counts.get(lowered, 0) > 1 and block_lengths.get(lowered, 0) < 300:
        return False
    if block_lengths.get(lowered, 0) < 40 and not any(token in lowered for token in ("contact", "about", "team", "address", "phone", "email", "services")):
        return False
    return True


def _is_menu_or_form_block(text: str) -> bool:
    menu_markers = ("home", "about", "contact", "services", "blog", "privacy policy", "schedule today", "join our team", "financing", "membership")
    form_markers = ("first name", "last name", "email", "phone", "address", "city", "state", "zip", "upload file", "message", "best times to call back")
    tokens = text.split()
    if len(tokens) <= 3 and any(marker in text for marker in menu_markers):
        return True
    if any(marker in text for marker in form_markers) and len(text) < 220:
        return True
    if len(tokens) < 8 and text.count(" | ") >= 1:
        return True
    return False
