"""Profiles and cached tokens under the tokenbooth home directory."""

import base64
import datetime
import fnmatch
import json
import os
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

EXPIRY_MARGIN = 60

FIREFOX_KNOWN = (158, datetime.date(2026, 11, 20))
FIREFOX_CYCLE_DAYS = 28


def firefox_ua(today: datetime.date | None = None) -> str:
    """The current Firefox release, counted from a known one in whole release cycles so it is
    never ahead of the real one; Google refuses sign-in from a Firefox it considers outdated."""
    known, since = FIREFOX_KNOWN
    version = known + max(0, ((today or datetime.date.today()) - since).days // FIREFOX_CYCLE_DAYS)
    return f"Mozilla/5.0 (X11; Linux x86_64; rv:{version}.0) Gecko/20100101 Firefox/{version}.0"


PROVIDERS = {
    # Google refuses sign-in from an embedded Chromium that calls itself Chrome, but not from one
    # calling itself Firefox; after sign-in it bounces the page through YouTube to set cookies there.
    "google": {
        "hosts": ["accounts.google.*", "accounts.google.co.*", "accounts.google.com.*", "accounts.youtube.com"],
        "user_agent": firefox_ua,
    },
    # Entra accepts the embedded engine as it is; personal accounts go through login.live.com.
    "microsoft": {
        "hosts": ["login.microsoftonline.com", "login.microsoft.com", "login.live.com"],
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


def keep_pattern(pattern: str) -> tuple[str, str]:
    name, at, domain = pattern.partition("@")
    if not at or not name or not domain:
        raise UsageError(f"keep_cookies entry must be NAME@DOMAIN: {pattern!r}")
    return name, domain


def keep_matches(patterns: list[str], name: str, domain: str) -> bool:
    """NAME is a glob on the cookie name, DOMAIN a host glob on its domain without the leading dot."""
    domain = domain.lstrip(".")
    return any(fnmatch.fnmatchcase(name, n) and host_matches(domain, d) for n, d in map(keep_pattern, patterns))


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
    keep_cookies: list[str] = field(default_factory=list)
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
        if "persist_session" in values:
            raise UsageError(f"{path}: persist_session is gone; name the cookies to keep with keep_cookies")
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
            if not values.get("user_agent") and "user_agent" in PROVIDERS[provider]:
                values["user_agent"] = PROVIDERS[provider]["user_agent"]()
        values.setdefault("cookie_domain", base)
        values["keep_cookies"] = list(values.get("keep_cookies", []))
        for pattern in values["keep_cookies"]:
            keep_pattern(pattern)
        return cls(name=name, **values)


def token_path(home: Path, name: str) -> Path:
    return home / "tokens" / f"{name}.json"


def keep_path(home: Path, name: str) -> Path:
    return home / "keep" / f"{name}.json"


def load_keep(home: Path, name: str) -> list[dict]:
    """Cookies kept from earlier runs: name, domain, path, expires (None for a session cookie)
    and raw, the base64 of the full Set-Cookie form. Expired ones are dropped."""
    try:
        entries = json.loads(keep_path(home, name).read_text())
    except (OSError, ValueError):
        return []
    now = time.time()
    return [e for e in entries if not e.get("expires") or e["expires"] > now]


def save_keep(home: Path, name: str, entries: list[dict]) -> None:
    path = keep_path(home, name)
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(entries, f)


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
    keep = keep_path(home, name)
    if keep.exists():
        keep.unlink()
        removed.append(keep)
    return removed
