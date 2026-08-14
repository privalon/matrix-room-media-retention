"""Parsing for the `!media-retention retain <duration>` command argument.

Deliberately tiny and dependency-free -- this is the one piece of user input
the whole plugin trusts, so it fails loudly and specifically rather than
silently misinterpreting e.g. "30" as something other than what the operator
meant.
"""

from __future__ import annotations

import re

_UNIT_SECONDS = {
    "m": 60,
    "h": 60 * 60,
    "d": 60 * 60 * 24,
    "w": 60 * 60 * 24 * 7,
}

_PATTERN = re.compile(r"^(\d+)([mhdw])$")

# roadmap/041 §9 (second review pass, informed by 037 §7.1's own stated
# safety bar): a bare positive-integer check let a typo like "retain 30m"
# (meaning "30d") silently accept a 30-*minute* retention window, purging
# nearly everything in the room on the very next scheduler tick with no
# confirmation step at all. 037 §7.1 itself recommends a 1-day floor for
# exactly this reason ("deliberately not required... adds operational
# churn and has little value" for anything shorter). Enforced in
# parse_duration_seconds() below via its own `minimum_seconds` parameter,
# not hardcoded here, so an operator can still lower it via config if they
# have a genuine sub-day use case -- but the *default* (see config.py)
# refuses silently accepting one.
DEFAULT_MINIMUM_RETAIN_SECONDS = _UNIT_SECONDS["d"]


class InvalidDurationError(ValueError):
    pass


def parse_duration_seconds(text: str, *, minimum_seconds: int = DEFAULT_MINIMUM_RETAIN_SECONDS) -> int:
    """Parse a duration like "30d", "6h", "45m", "2w" into whole seconds.

    Deliberately requires an explicit unit (no bare numbers) -- an operator
    typing "!media-retention retain 30" almost certainly means days, but
    guessing that silently is exactly the kind of ambiguity that turns into
    an accidental 30-*second* retention window and a room's media getting
    purged within the next scheduler tick. Fail loudly instead.

    `minimum_seconds` rejects a duration below the configured safety floor
    (default 1 day, see DEFAULT_MINIMUM_RETAIN_SECONDS) with a specific
    error message, rather than accepting it and letting the first scheduler
    tick be the operator's first sign anything was wrong.
    """
    text = text.strip().lower()
    match = _PATTERN.match(text)
    if not match:
        raise InvalidDurationError(
            f"Could not parse duration {text!r} -- use a number followed by "
            "m (minutes), h (hours), d (days), or w (weeks), e.g. '30d' or '6h'."
        )
    amount = int(match.group(1))
    if amount <= 0:
        raise InvalidDurationError(f"Duration must be positive, got {text!r}.")
    unit = match.group(2)
    seconds = amount * _UNIT_SECONDS[unit]
    if seconds < minimum_seconds:
        raise InvalidDurationError(
            f"{text!r} ({format_duration_seconds(seconds)}) is below this deployment's minimum "
            f"retention of {format_duration_seconds(minimum_seconds)} -- if this is really what you "
            "want, ask an operator to lower minimum_retain_seconds in config.yaml."
        )
    return seconds


def format_duration_seconds(seconds: int) -> str:
    """Inverse of parse_duration_seconds, for human-readable status output.
    Picks the largest whole unit that divides evenly, falling back to days
    with a remainder note rather than fabricating false precision."""
    for unit, unit_seconds in (("w", _UNIT_SECONDS["w"]), ("d", _UNIT_SECONDS["d"])):
        if seconds % unit_seconds == 0:
            return f"{seconds // unit_seconds}{unit}"
    for unit in ("h", "m"):
        unit_seconds = _UNIT_SECONDS[unit]
        if seconds % unit_seconds == 0:
            return f"{seconds // unit_seconds}{unit}"
    return f"{seconds}s"
