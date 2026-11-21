"""The browser engines a booth can drive. Each module has
`run(profile, keep, log, recorder, hangup, offscreen) -> (Outcome, keep-set)`."""

import importlib
from dataclasses import dataclass
from types import ModuleType

ENGINES = ("chrome", "qt")


@dataclass
class Outcome:
    token: str | None = None
    expires: float | None = None
    timed_out: bool = False
    confirmed: bool = False


def load(name: str) -> ModuleType:
    if name not in ENGINES:
        raise ValueError(f"unknown engine {name!r} (known: {', '.join(ENGINES)})")
    return importlib.import_module(f"tokenbooth.engines.{name}")
