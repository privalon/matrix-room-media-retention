"""Builds the top-N-rooms-by-media-storage-size report -- shared, single
source of truth for both the on-demand `!media-retention top <N>` command
(bot.py) and the proactive monthly report (bot.py's
`_maybe_send_monthly_report()`). Kept as a pure function (no Matrix
messaging, no scheduling) so both call sites format identically and this
is independently unit-testable with fake clients.

Room-to-media resolution has no shortcut: matrix-media-repo's own database
has no room_id column at all (confirmed directly against its own source,
`matrix/requests_admin.go`'s `ListMedia()`, called from
`api/custom/purge.go`'s `PurgeRoomMedia` -- the exact mechanism MMR's own
room-scoped purge endpoint uses to resolve "media in this room"). Building
this report means, for every room on the server: ask Synapse's own admin
API which MXC URIs that room's timeline references
(`SynapseAdminClient.get_room_media_mxcs()`), then ask matrix-media-repo's
own admin API for those specific MXCs' sizes/timestamps in one batched
call per room (`MediaRepoPurgeClient.get_usage_for_mxcs()`). This is
O(rooms) HTTP round-trips, not a single cheap query -- acceptable for an
explicit admin-triggered command and a monthly background job, not
something to run more often than that.
"""

from __future__ import annotations

import datetime

from .duration import format_duration_seconds
from .policy_store import PolicyStore
from .purge_client import MediaRepoPurgeClient
from .synapse_admin_client import SynapseAdminClient


def _format_bytes(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _format_oldest(oldest_ts_ms: int | None) -> str:
    if oldest_ts_ms is None:
        return "unknown"
    return datetime.datetime.fromtimestamp(oldest_ts_ms / 1000, tz=datetime.timezone.utc).strftime("%Y-%m-%d")


def _policy_text(store: PolicyStore, room_id: str) -> str:
    policy = store.get(room_id)
    if policy is None or policy.policy == "forever":
        return "forever"
    return f"retain {format_duration_seconds(policy.retain_seconds)}"


def build_top_rooms_report(
    *,
    synapse_admin: SynapseAdminClient,
    purge_client: MediaRepoPurgeClient,
    store: PolicyStore,
    server_name: str,
    top_n: int,
) -> str:
    """Returns the full report text: one line per room (room ID, human
    name, total media size, current retention policy, oldest media file's
    date), sorted largest-first, then an overall total-storage line.
    Rooms with zero resolvable media are skipped entirely (nothing useful
    to show), so `top_n` is a cap, not a guarantee of exactly that many
    lines."""
    rooms = synapse_admin.list_all_rooms()

    room_sizes = []
    for room in rooms:
        room_id = room.get("room_id")
        if not room_id:
            continue
        mxcs = synapse_admin.get_room_media_mxcs(room_id)
        if not mxcs:
            continue
        usage = purge_client.get_usage_for_mxcs(mxcs)
        if not usage:
            continue
        total_bytes = sum(int(v.get("size_bytes", 0)) for v in usage.values())
        if total_bytes <= 0:
            continue
        creation_timestamps = [int(v["created_ts"]) for v in usage.values() if v.get("created_ts")]
        oldest_ts = min(creation_timestamps) if creation_timestamps else None
        room_sizes.append(
            {
                "room_id": room_id,
                "name": room.get("name") or "(no name)",
                "total_bytes": total_bytes,
                "oldest_ts": oldest_ts,
            }
        )

    room_sizes.sort(key=lambda r: r["total_bytes"], reverse=True)
    top = room_sizes[:top_n]

    lines = [f"Top {len(top)} room(s) by media storage size:"]
    for r in top:
        lines.append(
            f"{r['room_id']} ({r['name']}): {_format_bytes(r['total_bytes'])}, "
            f"policy: {_policy_text(store, r['room_id'])}, "
            f"oldest media: {_format_oldest(r['oldest_ts'])}"
        )
    if not top:
        lines.append("(no rooms with resolvable media found)")

    overall_bytes = purge_client.get_overall_usage_bytes(server_name=server_name)
    lines.append(f"\nOverall media storage used: {_format_bytes(overall_bytes)}")
    return "\n".join(lines)
