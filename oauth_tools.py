from __future__ import annotations

import json
import os
import subprocess
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from msal import PublicClientApplication, SerializableTokenCache


DEFAULT_SCOPES = [
    "https://outlook.office.com/IMAP.AccessAsUser.All",
    "https://outlook.office.com/SMTP.Send",
]

DEFAULT_CLIENT_ID = "149074f2-4df2-4589-9352-6ddf1dc95244"
DEFAULT_TENANT_ID = "3d2a33ec-963c-4a36-aef6-b9401a859744"


def _default_state_dir() -> Path:
    return Path(os.getenv("ARGOWAKE_STATE_DIR", ".state"))


def _default_token_cache_path() -> Path:
    return Path(os.getenv("ARGOWAKE_TOKEN_CACHE_FILE", str(_default_state_dir() / "office365_token_cache.json")))


def _edge_candidates() -> list[str]:
    program_files = os.getenv("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.getenv("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_app_data = os.getenv("LocalAppData", "")
    candidates = [
        str(Path(program_files) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
        str(Path(program_files_x86) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
    ]
    if local_app_data:
        candidates.append(str(Path(local_app_data) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))
    candidates.append("msedge")
    return candidates


def open_in_edge(url: str) -> bool:
    for candidate in _edge_candidates():
        try:
            subprocess.Popen([candidate, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except FileNotFoundError:
            continue
        except OSError:
            continue
    try:
        webbrowser.open(url, new=1, autoraise=True)
        return True
    except Exception:
        return False


@dataclass
class MsalTokenStore:
    client_id: str
    authority: str
    username: str
    token_cache_path: Path
    scopes: list[str]
    redirect_port: int

    @classmethod
    def from_env(cls) -> "MsalTokenStore":
        client_id = os.getenv("ARGOWAKE_OAUTH_CLIENT_ID", DEFAULT_CLIENT_ID).strip()
        tenant_id = os.getenv("ARGOWAKE_OAUTH_TENANT_ID", DEFAULT_TENANT_ID).strip() or DEFAULT_TENANT_ID
        username = os.getenv("ARGOWAKE_EMAIL_ADDRESS", "help@argowake.com").strip()
        token_cache_path = _default_token_cache_path()
        scopes = [scope.strip() for scope in os.getenv("ARGOWAKE_OAUTH_SCOPES", ",".join(DEFAULT_SCOPES)).split(",") if scope.strip()]
        authority = f"https://login.microsoftonline.com/{tenant_id}"
        return cls(
            client_id=client_id,
            authority=authority,
            username=username,
            token_cache_path=token_cache_path,
            scopes=scopes,
            redirect_port=int(os.getenv("ARGOWAKE_OAUTH_REDIRECT_PORT", "8400")),
        )

    def validate(self) -> None:
        if not self.client_id:
            raise ValueError("ARGOWAKE_OAUTH_CLIENT_ID is required for Microsoft sign-in.")

    def load_cache(self) -> SerializableTokenCache:
        cache = SerializableTokenCache()
        if self.token_cache_path.exists():
            cache.deserialize(self.token_cache_path.read_text(encoding="utf-8"))
        return cache

    def save_cache(self, cache: SerializableTokenCache) -> None:
        self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        if cache.has_state_changed:
            self.token_cache_path.write_text(cache.serialize(), encoding="utf-8")

    def build_app(self, cache: SerializableTokenCache) -> PublicClientApplication:
        return PublicClientApplication(
            client_id=self.client_id,
            authority=self.authority,
            token_cache=cache,
        )

    def acquire_token(self) -> tuple[str, bool]:
        self.validate()
        cache = self.load_cache()
        app = self.build_app(cache)
        accounts = app.get_accounts(username=self.username)
        result = None
        if accounts:
            result = app.acquire_token_silent(self.scopes, account=accounts[0])
        if not result:
            result = app.acquire_token_interactive(
                scopes=self.scopes,
                login_hint=self.username,
                port=self.redirect_port,
            )
        self.save_cache(cache)
        if "access_token" not in result:
            error = result.get("error", "unknown_error")
            description = result.get("error_description", "")
            raise RuntimeError(f"Microsoft sign-in failed: {error}. {description}".strip())
        return result["access_token"], bool(result.get("from_cache"))

    def get_access_token_silent(self) -> str:
        self.validate()
        cache = self.load_cache()
        app = self.build_app(cache)
        accounts = app.get_accounts(username=self.username)
        if not accounts:
            raise RuntimeError("No Microsoft account is signed in yet. Run the auth command first.")
        result = app.acquire_token_silent(self.scopes, account=accounts[0])
        if not result or "access_token" not in result:
            raise RuntimeError("No cached token available. Run the auth command again.")
        self.save_cache(cache)
        return result["access_token"]

    def describe(self) -> dict[str, str]:
        return {
            "client_id": self.client_id,
            "authority": self.authority,
            "username": self.username,
            "token_cache_path": str(self.token_cache_path),
            "scopes": " ".join(self.scopes),
            "redirect_port": str(self.redirect_port),
        }
