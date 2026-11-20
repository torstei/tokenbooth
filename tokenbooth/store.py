"""Profiles and cached tokens under the tokenbooth home directory."""

import base64
import json
import os
import re
import shutil
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

EXPIRY_MARGIN = 60

FIREFOX_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"

PROVIDERS = {
    # Google refuses sign-in from an embedded Chromium that calls itself Chrome, but not from one
    # calling itself Firefox; after sign-in it bounces the page through YouTube to set cookies there.
    "google": {
        "hosts": ["accounts.google.*", "accounts.google.co.*", "accounts.google.com.*", "accounts.youtube.com"],
        "user_agent": FIREFOX_UA,
    },
}


def host_matches(host: str, pattern: str) -> bool:
    """`*` is one DNS label; a leading `*.` is any number of them."""
    host, pattern = host.lower(), pattern.lower()
    prefix = ""
    if pattern.startswith("*."):
        prefix, pattern = r"(?:[^.]+\.)+", pattern[2:]
    body = r"[^.]+".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(prefix + body, host) is not None


class UsageError(Exception):
    pass


def default_home() -> Path:
    if env := os.environ.get("TOKENBOOTH_HOME"):
        return Path(env)
    data = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(data) / "tokenbooth"


def private_dir(path: Path) -> Path:
    for directory in reversed([path, *path.parents]):
        if not directory.exists():
            directory.mkdir(mode=0o700)
    return path


@dataclass
class Profile:
    name: str
    start_url: str
    cookie: str
    cookie_domain: str
    allowed_hosts: list[str] = field(default_factory=list)
    done_url: str = ""
    persist_session: bool = False
    user_agent: str = ""
    timeout: int = 600
    via: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, home: Path, name: str, overrides: dict, extra_hosts: list[str] = (), via: list[str] = ()) -> "Profile":
        if not name or "/" in name or name.startswith("."):
            raise UsageError(f"bad profile name: {name!r}")
        path = home / "profiles" / f"{name}.toml"
        values = {}
        if path.exists():
            with path.open("rb") as f:
                values = tomllib.load(f)
        values.update({k: v for k, v in overrides.items() if v is not None})
        unknown = set(values) - (set(cls.__dataclass_fields__) - {"name"})
        if unknown:
            raise UsageError(f"{path}: unknown key(s): {', '.join(sorted(unknown))}")
        if not values.get("start_url"):
            raise UsageError(f"no start_url for {name!r}: give --url or create {path}")
        if not values.get("cookie"):
            raise UsageError(f"no cookie to capture for {name!r}: give --cookie or set it in {path}")
        host = urlsplit(values["start_url"]).hostname or ""
        if urlsplit(values["start_url"]).scheme != "https" or not host:
            raise UsageError(f"start_url must be an https URL: {values['start_url']}")
        base = host.removeprefix("www.")
        values.setdefault("allowed_hosts", [base, f"*.{base}"])
        values["allowed_hosts"] = list(values["allowed_hosts"]) + list(extra_hosts)
        values["via"] = list(dict.fromkeys(list(values.get("via", [])) + list(via)))
        for provider in values["via"]:
            if provider not in PROVIDERS:
                raise UsageError(f"unknown identity provider {provider!r} (known: {', '.join(PROVIDERS)})")
            values["allowed_hosts"] += PROVIDERS[provider]["hosts"]
            if not values.get("user_agent"):
                values["user_agent"] = PROVIDERS[provider]["user_agent"]
        values.setdefault("cookie_domain", base)
        return cls(name=name, **values)


def token_path(home: Path, name: str) -> Path:
    return home / "tokens" / f"{name}.json"


def session_path(home: Path, name: str) -> Path:
    return home / "sessions" / name


def jwt_expiry(token: str) -> float | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        return float(payload["exp"])
    except (ValueError, KeyError, TypeError):
        return None


def cached_token(home: Path, name: str) -> str | None:
    try:
        entry = json.loads(token_path(home, name).read_text())
    except (OSError, ValueError):
        return None
    expires = entry.get("expires")
    if not expires or expires < time.time() + EXPIRY_MARGIN:
        return None
    return entry.get("token")


def save_token(home: Path, name: str, token: str, cookie_expires: float | None) -> float | None:
    candidates = [e for e in (cookie_expires, jwt_expiry(token)) if e]
    expires = min(candidates) if candidates else None
    path = token_path(home, name)
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"token": token, "expires": expires, "saved": time.time()}, f)
    return expires


def forget(home: Path, name: str) -> list[Path]:
    removed = []
    token = token_path(home, name)
    if token.exists():
        token.unlink()
        removed.append(token)
    session = session_path(home, name)
    if session.exists():
        shutil.rmtree(session)
        removed.append(session)
    return removed
