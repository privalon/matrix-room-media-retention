"""Bridge-agnostic authorization: is this sender allowed to change this
room's retention policy?

Deliberately implemented against the room's own `m.room.power_levels` state
event (a core Matrix concept every room has, bridged or not) rather than any
bridge-specific permission model -- this is exactly what makes the command
surface work identically in a Telegram portal, a WhatsApp portal, or a plain
native room with no bridge involved at all.
"""

from __future__ import annotations

_DEFAULT_MODERATOR_LEVEL = 50


def is_authorized(
    *,
    power_levels_content: dict,
    sender: str,
    minimum_level: int = _DEFAULT_MODERATOR_LEVEL,
) -> bool:
    """`power_levels_content` is the raw `content` dict of the room's
    `m.room.power_levels` state event, exactly as returned by any Matrix
    client library -- no bridge-specific shape assumed.

    Falls back to the event's own `users_default` (or 0, matching the
    Matrix spec's own default) for a sender with no explicit entry in
    `users`, same resolution order the spec itself defines.
    """
    users = power_levels_content.get("users") or {}
    if sender in users:
        level = users[sender]
    else:
        level = power_levels_content.get("users_default", 0)
    return int(level) >= minimum_level
