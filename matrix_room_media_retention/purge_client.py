"""Thin client for matrix-media-repo's own room-scoped purge admin API.

Confirmed against matrix-media-repo's own docs (docs/admin.md, t2bot/
matrix-media-repo): `POST /_matrix/media/unstable/admin/purge/room/<room_id>`
with a `before_ts` query param (milliseconds) deletes all media known to
that room created/last-used before that timestamp -- local or remote,
regardless of which bridge (if any) produced it -- while leaving the
referencing Matrix events themselves untouched. Auth is a normal Matrix
access token belonging to an account matrix-media-repo's own config lists
as an admin, not a separate API key system.

No native dry-run parameter exists on this endpoint (confirmed against the
same docs) -- roadmap/041 §9's dry-run mode is implemented one layer up, in
the scheduler, by simply not calling purge_room() at all when dry_run is
set, rather than by any flag this client sends to matrix-media-repo itself.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests


class PurgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class PurgeResult:
    room_id: str
    num_removed: int


class MediaRepoPurgeClient:
    def __init__(self, *, base_url: str, admin_access_token: str, timeout_seconds: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._access_token = admin_access_token
        self._timeout_seconds = timeout_seconds

    def purge_room(self, *, room_id: str, before_ts_ms: int) -> PurgeResult:
        url = f"{self._base_url}/_matrix/media/unstable/admin/purge/room/{room_id}"
        response = requests.post(
            url,
            params={"before_ts": before_ts_ms},
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=self._timeout_seconds,
        )
        if response.status_code != 200:
            raise PurgeError(
                f"matrix-media-repo purge failed for room {room_id!r}: "
                f"HTTP {response.status_code} {response.text!r}"
            )
        body = response.json()
        # matrix-media-repo's own response shape: {"num_removed": <int>}.
        # Tolerate a missing key rather than crashing the scheduler loop --
        # a malformed-but-200 response should be logged and treated as
        # "0 known", not fatal for every other configured room this tick.
        num_removed = int(body.get("num_removed", 0))
        return PurgeResult(room_id=room_id, num_removed=num_removed)

    def get_overall_usage_bytes(self, *, server_name: str) -> int:
        """`GET /_matrix/media/unstable/admin/usage/<server_name>` (docs/
        admin.md's "Per-server usage") -- total bytes across the whole
        server (media + thumbnails). Returns 0 on failure rather than
        raising -- used for the top-N-by-media-size report's own overall
        total line; a wrong-but-present report is still useful even if
        this one call fails."""
        url = f"{self._base_url}/_matrix/media/unstable/admin/usage/{server_name}"
        response = requests.get(
            url, headers={"Authorization": f"Bearer {self._access_token}"}, timeout=self._timeout_seconds
        )
        if response.status_code != 200:
            return 0
        return int(response.json().get("raw_bytes", {}).get("total", 0))

    def get_usage_for_mxcs(self, mxcs: list[str]) -> dict[str, dict]:
        """`GET /_matrix/media/unstable/admin/usage/<server_name>/uploads`
        with one or more repeated `?mxc=` query params (docs/admin.md's
        "Per-upload usage (batch of uploads / single upload)") -- per-MXC
        `size_bytes`/`created_ts`/etc., used to compute a room's total
        media size and oldest-file date from the MXC list
        `SynapseAdminClient.get_room_media_mxcs()` returns. The
        `<server_name>` path segment is a required part of the URL but
        does not scope the response -- confirmed against the same docs,
        the `?mxc=` params are what actually select which objects come
        back -- so any real server name this repository administrator
        account can query works; `_` is used here as a placeholder.
        Returns an empty dict for an empty `mxcs` list (no request made at
        all -- nothing to ask about) or on any failure."""
        if not mxcs:
            return {}
        url = f"{self._base_url}/_matrix/media/unstable/admin/usage/_/uploads"
        response = requests.get(
            url,
            params=[("mxc", mxc) for mxc in mxcs],
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=self._timeout_seconds,
        )
        if response.status_code != 200:
            return {}
        body = response.json()
        return body if isinstance(body, dict) else {}
