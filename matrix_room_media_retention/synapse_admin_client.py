"""Thin client for the Synapse-admin-specific operations the plugin's
remote (DM-based) command surface needs: reading a room's own current
power levels and display name (docs/roadmap/041 §11), enumerating every
room on the server, and resolving which media MXC URIs a given room's
timeline references (both for the top-N-by-media-size report) -- none of
which requires the bot to be a member of that room at all.

Requires the bot's own Matrix account to be a Synapse server admin.
Synapse has no separate "admin token" concept -- any valid access token
belonging to an admin-flagged account works for these endpoints too, so
this reuses the bot's own regular login token rather than a second
credential.

Deliberately does NOT force-join the room first (an earlier version of
this module did): confirmed live 2026-08-15 that Synapse's own admin
join API (`POST /_synapse/admin/v1/join/<room_id>`) refuses a room the
calling admin account has no prior relationship to at all ("... not in
room ...") when that account is also the target being joined -- the exact
case this plugin's own bot hits for a genuinely new, never-before-seen
room. The admin room-state endpoint below has no such restriction (it's
designed for exactly this "look at any room without joining it" admin
use case), so there's no join/leave dance needed here at all.
"""

from __future__ import annotations

import requests


class SynapseAdminClient:
    def __init__(self, *, homeserver_url: str, access_token: str, timeout_seconds: float = 30.0):
        self._base_url = homeserver_url.rstrip("/")
        self._access_token = access_token
        self._timeout_seconds = timeout_seconds

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._access_token}"}

    def get_room_power_levels(self, room_id: str) -> dict | None:
        """`GET /_synapse/admin/v1/rooms/<room_id>/state` -- every current
        state event in the room, admin-only, no membership required at
        all (confirmed live). Returns the raw `content` dict of the
        room's own `m.room.power_levels` event -- the exact shape
        `authorization.is_authorized()` already expects, matching any
        other Matrix client library's own state-event shape. `None` if
        the room doesn't exist, has no power_levels event (never true for
        a real room, but tolerated rather than assumed), or the lookup
        fails for any other reason."""
        url = f"{self._base_url}/_synapse/admin/v1/rooms/{room_id}/state"
        response = requests.get(url, headers=self._headers(), timeout=self._timeout_seconds)
        if response.status_code != 200:
            return None
        for event in response.json().get("state", []):
            if event.get("type") == "m.room.power_levels":
                return event.get("content")
        return None

    def get_room_name(self, room_id: str) -> str | None:
        """`GET /_synapse/admin/v1/rooms/<room_id>` -- room metadata,
        including its own display name, without needing to be a member at
        all. Returns `None` if the room has no name set, doesn't exist, or
        the lookup fails for any other reason -- a missing name is never
        worth failing the whole `!media-retention list` reply over."""
        url = f"{self._base_url}/_synapse/admin/v1/rooms/{room_id}"
        response = requests.get(url, headers=self._headers(), timeout=self._timeout_seconds)
        if response.status_code != 200:
            return None
        return response.json().get("name") or None

    def list_all_rooms(self) -> list[dict]:
        """`GET /_synapse/admin/v1/rooms` (paginated via `from`/`limit`,
        confirmed against Synapse's own docs/admin_api/rooms.md) -- every
        room on the server, each entry already including its own `name`
        (no separate `get_room_name()` call needed per room). Used by the
        top-N-by-media-size report to enumerate every room to check.
        Stops and returns whatever was collected so far on any non-200
        page rather than raising -- a partial report is more useful than
        none."""
        rooms: list[dict] = []
        offset = 0
        while True:
            url = f"{self._base_url}/_synapse/admin/v1/rooms"
            response = requests.get(
                url, headers=self._headers(), params={"from": offset, "limit": 100}, timeout=self._timeout_seconds
            )
            if response.status_code != 200:
                break
            body = response.json()
            rooms.extend(body.get("rooms", []))
            next_batch = body.get("next_batch")
            if next_batch is None or next_batch == offset:
                break
            offset = next_batch
        return rooms

    def get_room_media_mxcs(self, room_id: str) -> list[str]:
        """`GET /_synapse/admin/v1/room/<room_id>/media` -- every MXC URI
        (local and remote) ever referenced in that room's timeline.
        Confirmed directly against matrix-media-repo's own source
        (matrix/requests_admin.go's `ListMedia()`, called from
        `PurgeRoomMedia` in api/custom/purge.go) as the exact mechanism
        MMR itself uses to resolve "media in this room" for its own
        room-scoped purge endpoint -- MMR's own database has no room_id
        column at all, so this room-to-media mapping only ever exists on
        the homeserver side. Returns an empty list on any failure (missing
        room, no media, lookup error) rather than raising."""
        url = f"{self._base_url}/_synapse/admin/v1/room/{room_id}/media"
        response = requests.get(url, headers=self._headers(), timeout=self._timeout_seconds)
        if response.status_code != 200:
            return []
        body = response.json()
        return list(body.get("local", [])) + list(body.get("remote", []))
