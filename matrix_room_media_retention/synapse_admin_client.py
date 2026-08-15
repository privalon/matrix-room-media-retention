"""Thin client for the Synapse-admin-specific operations the plugin's
remote (DM-based) command surface needs (docs/roadmap/041 §11): force-
joining a room the bot isn't already a member of, and reading a room's own
display name without needing to be a member at all.

Requires the bot's own Matrix account to be a Synapse server admin.
Synapse has no separate "admin token" concept -- any valid access token
belonging to an admin-flagged account works for these endpoints too, so
this reuses the bot's own regular login token rather than a second
credential.
"""

from __future__ import annotations

import requests


class SynapseAdminError(RuntimeError):
    pass


class SynapseAdminClient:
    def __init__(self, *, homeserver_url: str, access_token: str, timeout_seconds: float = 30.0):
        self._base_url = homeserver_url.rstrip("/")
        self._access_token = access_token
        self._timeout_seconds = timeout_seconds

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._access_token}"}

    def force_join_room(self, *, room_id: str, user_id: str) -> None:
        """`POST /_synapse/admin/v1/join/<room_id>` -- joins `user_id`
        (any local user, not necessarily this admin account itself) to
        the given room without needing an invite (confirmed against
        Synapse's own admin API docs -- `user_id` is a required body
        param: Synapse's own admin API lets a server admin force-join
        *any* local user, so it never assumes "join myself"). Idempotent:
        joining an already-joined room is a no-op, not an error."""
        url = f"{self._base_url}/_synapse/admin/v1/join/{room_id}"
        response = requests.post(
            url, headers=self._headers(), json={"user_id": user_id}, timeout=self._timeout_seconds
        )
        if response.status_code != 200:
            raise SynapseAdminError(
                f"Failed to force-join room {room_id!r}: HTTP {response.status_code} {response.text!r}"
            )

    def get_room_name(self, room_id: str) -> str | None:
        """`GET /_synapse/admin/v1/rooms/<room_id>` -- room metadata,
        including its own display name, without needing to be a member at
        all (unlike the regular client-server room-state API). Returns
        `None` if the room has no name set, doesn't exist, or the lookup
        fails for any other reason -- a missing name is never worth
        failing the whole `!media-retention list` reply over."""
        url = f"{self._base_url}/_synapse/admin/v1/rooms/{room_id}"
        response = requests.get(url, headers=self._headers(), timeout=self._timeout_seconds)
        if response.status_code != 200:
            return None
        return response.json().get("name") or None
