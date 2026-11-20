"""Finding which cookie marks a finished login, from what one run set and where it went."""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from tokenbooth.store import PROVIDERS, Profile, jwt_expiry

NAME_HINT = re.compile(r"access|token|session|auth|jwt", re.IGNORECASE)
OPAQUE = re.compile(r"[A-Za-z0-9._~+/=-]{16,}")


@dataclass
class Seen:
    name: str
    domain: str
    path: str
    order: int
    before: bool
    present: bool = True
    http_only: bool = False
    secure: bool = False
    session: bool = True
    length: int = 0
    jwt: bool = False
    opaque: bool = False

    @property
    def score(self) -> int:
        return (4 * self.http_only + 2 * self.jwt + 2 * bool(NAME_HINT.search(self.name))
                + 2 * self.opaque + self.secure)


def site_of(host: str) -> str:
    """The start host's site: `www.` dropped, and its first label too when three or more remain,
    so my.example.com and auth.example.com belong to example.com."""
    host = host.lower().removeprefix("www.")
    labels = host.split(".")
    return ".".join(labels[1:]) if len(labels) >= 3 else host


def on_site(domain: str, site: str) -> bool:
    domain = domain.lstrip(".").lower()
    return domain == site or domain.endswith("." + site)


def looks_like_jwt(value: str) -> bool:
    return value.startswith("eyJ") and value.count(".") in (2, 4)


class Recorder:
    """Cookies and top-level navigations of one run; values are looked at, never kept."""

    def __init__(self):
        self.cookies: dict[tuple[str, str, str], Seen] = {}
        self.first_page: tuple[str, str] | None = None
        self.departed = False

    def navigated(self, host: str, path: str) -> None:
        if self.first_page is None:
            self.first_page = (host, path)
        elif (host, path) != self.first_page:
            self.departed = True

    def cookie_set(self, name: str, domain: str, path: str, value: str, *,
                   http_only: bool, secure: bool, session: bool) -> None:
        key = (name, domain, path)
        seen = self.cookies.get(key)
        if seen is None:
            seen = self.cookies[key] = Seen(name, domain, path, order=len(self.cookies), before=not self.departed)
        seen.present = True
        seen.http_only, seen.secure, seen.session = http_only, secure, session
        seen.length = len(value)
        seen.jwt = looks_like_jwt(value) or jwt_expiry(value) is not None
        seen.opaque = OPAQUE.fullmatch(value) is not None

    def cookie_removed(self, name: str, domain: str, path: str) -> None:
        if seen := self.cookies.get((name, domain, path)):
            seen.present = False

    def site_cookies(self, site: str) -> list[Seen]:
        return sorted((s for s in self.cookies.values() if on_site(s.domain, site)), key=lambda s: s.order)

    def candidates(self, site: str) -> list[Seen]:
        """Cookies on the app's site whose name first appeared after the run left the start page
        and that are still set: a cookie that existed before sign-in cannot be what marks it."""
        names_before = {s.name for s in self.site_cookies(site) if s.before}
        fresh = [s for s in self.site_cookies(site) if not s.before and s.present and s.name not in names_before]
        return sorted(fresh, key=lambda s: (-s.score, s.order))


def tied(candidates: list[Seen]) -> list[Seen]:
    return [s for s in candidates if s.score == candidates[0].score] if candidates else []


def describe(seen: Seen) -> str:
    flags = [f for f, on in (("HttpOnly", seen.http_only), ("Secure", seen.secure), ("JWT", seen.jwt),
                             ("opaque", seen.opaque), ("session", seen.session)) if on]
    return f"{seen.name}@{seen.domain}{seen.path}  {seen.length} chars  {' '.join(flags)}".rstrip()


def suggest_profile(profile: Profile, best: Seen) -> str:
    lines = [f'start_url     = "{profile.start_url}"',
             f'cookie        = "{best.name}"',
             f'cookie_domain = "{best.domain.lstrip(".")}"']
    from_providers = {h for v in profile.via for h in PROVIDERS[v]["hosts"]}
    own = list(dict.fromkeys(h for h in profile.allowed_hosts if h not in from_providers))
    base = (urlsplit(profile.start_url).hostname or "").removeprefix("www.")
    if own != [base, f"*.{base}"]:
        lines.append("allowed_hosts = [" + ", ".join(f'"{h}"' for h in own) + "]")
    if profile.via:
        lines.append("via           = [" + ", ".join(f'"{v}"' for v in profile.via) + "]")
    if profile.keep_cookies:
        lines.append("keep_cookies  = [" + ", ".join(f'"{k}"' for k in profile.keep_cookies) + "]")
    return "\n".join(lines) + "\n"
