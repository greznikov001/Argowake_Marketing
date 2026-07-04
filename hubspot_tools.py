from __future__ import annotations

import csv
import json
import os
import re
import base64
import hashlib
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import structlog

from oauth_tools import open_in_edge


LOGGER = structlog.get_logger(__name__).bind(source="hubspot_tools.py")


_DEFAULT_BASE_URL = "https://api.hubapi.com"
_DEFAULT_AUTH_BASE_URL = "https://mcp-na2.hubspot.com/oauth/authorize/user"
_DEFAULT_TOKEN_URL = "https://api.hubapi.com/oauth/v1/token"
_DEFAULT_SCOPES = [
    "crm.objects.contacts.read",
    "crm.objects.contacts.write",
    "crm.objects.companies.read",
    "crm.objects.companies.write",
]


class HubSpotError(RuntimeError):
    pass


@dataclass(frozen=True)
class HubSpotOAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: list[str]
    token_cache_path: Path
    auth_base_url: str = _DEFAULT_AUTH_BASE_URL
    token_url: str = _DEFAULT_TOKEN_URL
    timeout_seconds: int = 30

    @classmethod
    def from_env(cls) -> "HubSpotOAuthConfig":
        client_id = os.getenv("ARGOWAKE_HUBSPOT_CLIENT_ID", "").strip()
        client_secret = os.getenv("ARGOWAKE_HUBSPOT_CLIENT_SECRET", "").strip()
        redirect_uri = os.getenv("ARGOWAKE_HUBSPOT_REDIRECT_URI", "http://localhost:8400").strip()
        scopes = [
            scope.strip()
            for scope in os.getenv("ARGOWAKE_HUBSPOT_SCOPES", ",".join(_DEFAULT_SCOPES)).split(",")
            if scope.strip()
        ]
        token_cache_path = Path(
            os.getenv("ARGOWAKE_HUBSPOT_TOKEN_CACHE_FILE", ".state/hubspot_token_cache.json")
        )
        timeout_raw = os.getenv("ARGOWAKE_HUBSPOT_TIMEOUT_SECONDS", "30").strip()
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=scopes,
            token_cache_path=token_cache_path,
            timeout_seconds=int(timeout_raw),
        )

    def validate(self) -> None:
        if not self.client_id:
            raise ValueError("ARGOWAKE_HUBSPOT_CLIENT_ID is required for HubSpot OAuth.")
        if not self.client_secret:
            raise ValueError("ARGOWAKE_HUBSPOT_CLIENT_SECRET is required for HubSpot OAuth.")
        if not self.redirect_uri:
            raise ValueError("ARGOWAKE_HUBSPOT_REDIRECT_URI is required for HubSpot OAuth.")


@dataclass
class HubSpotTokenBundle:
    access_token: str
    refresh_token: str | None
    expires_at: float | None
    scope: str | None = None
    token_type: str | None = None

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() >= (self.expires_at - 60)

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
            "token_type": self.token_type,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "HubSpotTokenBundle":
        return cls(
            access_token=str(payload.get("access_token", "")),
            refresh_token=_clean_text(payload.get("refresh_token")),
            expires_at=float(payload["expires_at"]) if payload.get("expires_at") is not None else None,
            scope=_clean_text(payload.get("scope")),
            token_type=_clean_text(payload.get("token_type")),
        )


class _HubSpotOAuthCallbackServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass,
        state: str,
        callback_path: str,
        code_verifier: str,
    ):
        super().__init__(server_address, RequestHandlerClass)
        self.expected_state = state
        self.callback_path = callback_path
        self.code_verifier = code_verifier
        self.authorization_code: str | None = None
        self.authorization_error: str | None = None
        self.authorization_error_description: str | None = None
        self.done = threading.Event()


class _HubSpotOAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        server = self.server  # type: ignore[assignment]
        if not isinstance(server, _HubSpotOAuthCallbackServer):
            self.send_error(500)
            return

        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path != server.callback_path:
            self.send_error(404)
            return

        error = query.get("error", [None])[0]
        if error:
            server.authorization_error = error
            server.authorization_error_description = query.get("error_description", [None])[0]
            self._write_response(
                400,
                "HubSpot authorization failed. You can close this window and return to the terminal.",
            )
            server.done.set()
            return

        code = query.get("code", [None])[0]
        if not code:
            self._write_response(400, "Missing authorization code. You can close this window.")
            server.authorization_error = "missing_code"
            server.done.set()
            return

        server.authorization_code = code
        self._write_response(200, "HubSpot authorization complete. You can close this window.")
        server.done.set()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _write_response(self, status: int, message: str) -> None:
        body = f"<html><body><p>{message}</p></body></html>".encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class HubSpotTokenStore:
    def __init__(self, config: HubSpotOAuthConfig) -> None:
        self.config = config

    @classmethod
    def from_env(cls) -> "HubSpotTokenStore":
        return cls(HubSpotOAuthConfig.from_env())

    def load_cache(self) -> HubSpotTokenBundle | None:
        if not self.config.token_cache_path.exists():
            return None
        payload = json.loads(self.config.token_cache_path.read_text(encoding="utf-8"))
        return HubSpotTokenBundle.from_dict(payload)

    def save_cache(self, bundle: HubSpotTokenBundle) -> None:
        self.config.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.token_cache_path.write_text(
            json.dumps(bundle.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def acquire_token(self) -> tuple[str, bool]:
        LOGGER.info("Acquiring HubSpot token")
        try:
            self.config.validate()
            cached = self.load_cache()
            if cached and cached.access_token and not cached.is_expired:
                return cached.access_token, True
            if cached and cached.refresh_token:
                refreshed = self.refresh_token(cached.refresh_token)
                self.save_cache(refreshed)
                return refreshed.access_token, False
            interactive = self.acquire_token_interactive()
            self.save_cache(interactive)
            return interactive.access_token, False
        except Exception:
            LOGGER.exception("HubSpot token acquisition failed")
            raise

    def refresh_token(self, refresh_token: str) -> HubSpotTokenBundle:
        payload = self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "refresh_token": refresh_token,
            }
        )
        return self._bundle_from_token_response(payload, fallback_refresh_token=refresh_token)

    def acquire_token_interactive(self) -> HubSpotTokenBundle:
        try:
            LOGGER.info("Starting HubSpot interactive auth")
            state = secrets.token_urlsafe(24)
            code_verifier = _generate_code_verifier()
            code_challenge = _create_code_challenge(code_verifier)
            redirect = urlparse(self.config.redirect_uri)
            callback_path = redirect.path or "/"
            server = _HubSpotOAuthCallbackServer(
                (redirect.hostname or "localhost", redirect.port or 80),
                _HubSpotOAuthCallbackHandler,
                state=state,
                callback_path=callback_path,
                code_verifier=code_verifier,
            )
            auth_url = self._build_authorization_url(state, code_challenge)
            browser_opened = open_in_edge(auth_url)
            if not browser_opened:
                webbrowser.open(auth_url, new=1, autoraise=True)

            try:
                while not server.done.wait(timeout=0.25):
                    server.handle_request()
            finally:
                server.server_close()

            if server.authorization_error:
                description = server.authorization_error_description or ""
                raise RuntimeError(
                    f"HubSpot authorization failed: {server.authorization_error}. {description}".strip()
                )
            if not server.authorization_code:
                raise RuntimeError("HubSpot authorization did not return a code.")

            payload = self._token_request(
                {
                    "grant_type": "authorization_code",
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "redirect_uri": self.config.redirect_uri,
                    "code": server.authorization_code,
                    "code_verifier": server.code_verifier,
                }
            )
            LOGGER.info("Completed HubSpot interactive auth")
            return self._bundle_from_token_response(payload)
        except Exception:
            LOGGER.exception("HubSpot interactive auth failed")
            raise

    def get_access_token_silent(self) -> str:
        cached = self.load_cache()
        if not cached:
            raise RuntimeError("No cached HubSpot token found. Run `python main.py hubspot auth` first.")
        if cached.access_token and not cached.is_expired:
            return cached.access_token
        if cached.refresh_token:
            refreshed = self.refresh_token(cached.refresh_token)
            self.save_cache(refreshed)
            return refreshed.access_token
        raise RuntimeError("HubSpot token cache is expired and has no refresh token. Re-authenticate.")

    def _build_authorization_url(self, state: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "client_id": self.config.client_id,
                "redirect_uri": self.config.redirect_uri,
                "scope": " ".join(self.config.scopes),
                "state": state,
                "response_type": "code",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.config.auth_base_url}?{query}"

    def _token_request(self, fields: dict[str, str]) -> dict[str, Any]:
        payload = urlencode(fields).encode("utf-8")
        request = Request(
            self.config.token_url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            error_body = error.read().decode("utf-8", errors="replace")
            raise HubSpotError(
                f"HubSpot OAuth token error ({error.code}): {error_body}"
            ) from error
        except URLError as error:
            raise HubSpotError(f"HubSpot OAuth token request failed: {error.reason}") from error

    def _bundle_from_token_response(
        self,
        payload: dict[str, Any],
        fallback_refresh_token: str | None = None,
    ) -> HubSpotTokenBundle:
        access_token = _clean_text(payload.get("access_token"))
        if not access_token:
            raise HubSpotError("HubSpot OAuth response did not include an access token.")
        refresh_token = _clean_text(payload.get("refresh_token")) or fallback_refresh_token
        expires_in_raw = payload.get("expires_in")
        expires_at = None
        if expires_in_raw is not None:
            expires_at = time.time() + int(expires_in_raw)
        return HubSpotTokenBundle(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            scope=_clean_text(payload.get("scope")),
            token_type=_clean_text(payload.get("token_type")),
        )


@dataclass(frozen=True)
class HubSpotConfig:
    access_token: str
    base_url: str = _DEFAULT_BASE_URL
    timeout_seconds: int = 30

    @classmethod
    def from_env(cls) -> "HubSpotConfig":
        access_token = os.getenv("ARGOWAKE_HUBSPOT_ACCESS_TOKEN", "").strip()
        if not access_token:
            access_token = HubSpotTokenStore.from_env().acquire_token()[0]
        base_url = os.getenv("ARGOWAKE_HUBSPOT_BASE_URL", _DEFAULT_BASE_URL).strip()
        timeout_raw = os.getenv("ARGOWAKE_HUBSPOT_TIMEOUT_SECONDS", "30").strip()
        timeout_seconds = int(timeout_raw)
        return cls(access_token=access_token, base_url=base_url, timeout_seconds=timeout_seconds)


@dataclass(frozen=True)
class ProspectRecord:
    name: str | None = None
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    company: str | None = None
    website: str | None = None
    domain: str | None = None
    phone: str | None = None
    city: str | None = None
    state: str | None = None
    industry: str | None = None
    job_title: str | None = None
    description: str | None = None
    opportunity: str | None = None
    priority: str | None = None
    source: str | None = None
    contact_name: str | None = None
    contact_title: str | None = None

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any]) -> "ProspectRecord":
        lowered = {str(key).strip().lower(): value for key, value in mapping.items()}
        return cls(
            name=_clean_text(lowered.get("name") or lowered.get("company") or lowered.get("company_name")),
            email=_clean_text(lowered.get("email")),
            first_name=_clean_text(lowered.get("first_name") or lowered.get("firstname")),
            last_name=_clean_text(lowered.get("last_name") or lowered.get("lastname")),
            company=_clean_text(lowered.get("company") or lowered.get("company_name") or lowered.get("name")),
            website=_clean_text(lowered.get("website") or lowered.get("url")),
            domain=_clean_text(lowered.get("domain")),
            phone=_clean_text(lowered.get("phone")),
            city=_clean_text(lowered.get("city")),
            state=_clean_text(lowered.get("state") or lowered.get("region")),
            industry=_clean_text(lowered.get("industry")),
            job_title=_clean_text(lowered.get("job_title") or lowered.get("jobtitle") or lowered.get("title")),
            description=_clean_text(lowered.get("description") or lowered.get("notes") or lowered.get("note")),
            opportunity=_clean_text(lowered.get("opportunity")),
            priority=_clean_text(lowered.get("priority")),
            source=_clean_text(lowered.get("source")),
            contact_name=_clean_text(lowered.get("contact_name") or lowered.get("contact")),
            contact_title=_clean_text(lowered.get("contact_title") or lowered.get("contact role")),
        )

    def inferred_domain(self) -> str | None:
        if self.domain:
            return _normalize_domain(self.domain)
        if self.website:
            return _normalize_domain(self.website)
        return None

    def display_name(self) -> str:
        if self.company:
            return self.company
        if self.name:
            return self.name
        if self.email:
            return self.email
        return "Unknown prospect"

    def hubspot_company_description(self) -> str | None:
        parts: list[str] = []
        if self.description:
            parts.append(self.description)
        details: list[str] = []
        if self.contact_name:
            contact = self.contact_name
            if self.contact_title:
                contact = f"{contact} ({self.contact_title})"
            details.append(f"Contact: {contact}")
        if self.opportunity:
            details.append(f"Opportunity: {self.opportunity}")
        if self.priority:
            details.append(f"Priority: {self.priority}")
        if self.source:
            details.append(f"Source: {self.source}")
        if details:
            parts.append(" | ".join(details))
        if not parts:
            return None
        return "\n".join(parts)

    def contact_full_name(self) -> str | None:
        if self.contact_name:
            return self.contact_name
        if self.first_name or self.last_name:
            return " ".join(part for part in (self.first_name, self.last_name) if part)
        if self.name:
            return self.name
        return self.company

    def contact_first_last(self) -> tuple[str | None, str | None]:
        if self.first_name or self.last_name:
            return self.first_name, self.last_name
        full_name = self.contact_full_name()
        if not full_name:
            return None, None
        parts = full_name.split()
        if len(parts) == 1:
            return parts[0], None
        return parts[0], " ".join(parts[1:])


class HubSpotClient:
    def __init__(self, config: HubSpotConfig) -> None:
        self.config = config

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
        LOGGER.info("HubSpot request", method=method, path=path)
        try:
            url = f"{self.config.base_url.rstrip('/')}{path}"
            data = None
            headers = {
                "Authorization": f"Bearer {self.config.access_token}",
                "Content-Type": "application/json",
            }
            if payload is not None:
                data = json.dumps(payload).encode("utf-8")
            request = Request(url, data=data, headers=headers, method=method)
            attempt = 0
            while True:
                try:
                    with urlopen(request, timeout=self.config.timeout_seconds) as response:
                        body = response.read()
                    break
                except HTTPError as error:
                    if error.code == 429 and attempt < 4:
                        retry_after = error.headers.get("Retry-After")
                        sleep_seconds = int(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                        time.sleep(max(1, sleep_seconds))
                        attempt += 1
                        continue
                    error_body = error.read().decode("utf-8", errors="replace")
                    raise HubSpotError(f"HubSpot API error ({error.code}) for {method} {path}: {error_body}") from error
                except URLError as error:
                    raise HubSpotError(f"HubSpot API connection failed for {method} {path}: {error.reason}") from error

            if not body:
                return None
            return json.loads(body.decode("utf-8"))
        except Exception:
            LOGGER.exception("HubSpot request failed", method=method, path=path)
            raise

    def _search(self, object_type: str, property_name: str, value: str) -> dict[str, Any] | None:
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": property_name,
                            "operator": "EQ",
                            "value": value,
                        }
                    ]
                }
            ],
            "limit": 1,
        }
        response = self._request("POST", f"/crm/v3/objects/{object_type}/search", payload)
        if not response:
            return None
        results = response.get("results") or []
        return results[0] if results else None

    def upsert_company(self, record: ProspectRecord) -> dict[str, Any]:
        LOGGER.info("Upserting company", name=record.company or record.name or "unknown")
        try:
            company_name = record.company or record.name
            if not company_name:
                raise ValueError("A company name is required to sync a prospect company.")

            existing = None
            domain = record.inferred_domain()
            if domain:
                try:
                    existing = self._search("companies", "domain", domain)
                except HubSpotError:
                    existing = None
            if existing is None:
                try:
                    existing = self._search("companies", "name", company_name)
                except HubSpotError:
                    existing = None

            properties = _compact_properties(
                {
                    "name": company_name,
                    "domain": domain,
                    "website": record.website or (f"https://{domain}" if domain else None),
                    "phone": record.phone,
                    "city": record.city,
                    "state": record.state,
                    "industry": record.industry,
                    "description": record.hubspot_company_description(),
                }
            )

            if existing is None:
                response = self._request("POST", "/crm/v3/objects/companies", {"properties": properties})
                return {"object_type": "company", "action": "created", "id": response["id"], "properties": response["properties"]}

            response = self._request(
                "PATCH",
                f"/crm/v3/objects/companies/{existing['id']}",
                {"properties": properties},
            )
            return {"object_type": "company", "action": "updated", "id": response["id"], "properties": response["properties"]}
        except Exception:
            LOGGER.exception("Upsert company failed")
            raise

    def upsert_contact(self, record: ProspectRecord) -> dict[str, Any]:
        LOGGER.info("Upserting contact", email=record.email or "unknown")
        try:
            contact_first_name, contact_last_name = record.contact_first_last()
            search_field = "email" if record.email else "firstname"
            search_value = record.email or (contact_first_name or record.company or record.display_name())
            existing = self._search("contacts", search_field, search_value) if search_value else None
            properties = _compact_properties(
                {
                    "email": record.email,
                    "firstname": contact_first_name,
                    "lastname": contact_last_name,
                    "company": record.company,
                    "phone": record.phone,
                    "jobtitle": record.job_title,
                    "city": record.city,
                    "state": record.state,
                    "lifecyclestage": "lead",
                }
            )

            if existing is None:
                response = self._request("POST", "/crm/v3/objects/contacts", {"properties": properties})
                return {"object_type": "contact", "action": "created", "id": response["id"], "properties": response["properties"]}

            response = self._request(
                "PATCH",
                f"/crm/v3/objects/contacts/{existing['id']}",
                {"properties": properties},
            )
            return {"object_type": "contact", "action": "updated", "id": response["id"], "properties": response["properties"]}
        except Exception:
            LOGGER.exception("Upsert contact failed")
            raise

    def upsert_record(self, record: ProspectRecord) -> dict[str, Any]:
        if record.email:
            return self.upsert_contact(record)
        return self.upsert_company(record)


def load_prospect_records(path: str | Path) -> list[ProspectRecord]:
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix == ".json":
        raw = json.loads(file_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw = raw.get("records", raw.get("prospects", []))
        if not isinstance(raw, list):
            raise ValueError("JSON import must be a list of prospect objects or a dict with records/prospects.")
        return [ProspectRecord.from_mapping(item) for item in raw]
    if suffix == ".csv":
        with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            return [ProspectRecord.from_mapping(row) for row in reader]
    if suffix in {".md", ".markdown", ".txt"}:
        return _load_markdown_records(file_path.read_text(encoding="utf-8"))
    raise ValueError(f"Unsupported input format: {suffix}")


def _load_markdown_records(text: str) -> list[ProspectRecord]:
    records: list[ProspectRecord] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-", "*")):
            line = line[1:].strip()
        elif re.match(r"^\d+[.)]\s+", line):
            line = re.sub(r"^\d+[.)]\s+", "", line)
        if not line:
            continue
        if "|" in line:
            parts = [part.strip() for part in line.split("|")]
            if len(parts) >= 1:
                name = _strip_markdown_link(parts[0])
                website = _extract_url(parts)
                records.append(ProspectRecord(company=name, website=website))
            continue
        records.append(ProspectRecord(company=_strip_markdown_link(line)))
    return records


def sync_records(client: HubSpotClient, records: list[ProspectRecord]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for record in records:
        results.append(client.upsert_record(record))
    return results


def sync_contacts(client: HubSpotClient, records: list[ProspectRecord]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for record in records:
        results.append(client.upsert_contact(record))
    return results


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _compact_properties(properties: dict[str, str | None]) -> dict[str, str]:
    return {key: value for key, value in properties.items() if value}


def _normalize_domain(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = re.sub(r"^www\.", "", text)
    text = text.split("/")[0]
    return text


def _strip_markdown_link(value: str) -> str:
    match = re.match(r"^\[(.+?)\]\(.+\)$", value)
    if match:
        return match.group(1).strip()
    return value.strip()


def _extract_url(parts: list[str]) -> str | None:
    for part in parts:
        if part.startswith("http://") or part.startswith("https://"):
            return part
    return None


def _generate_code_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("ascii").rstrip("=")


def _create_code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
