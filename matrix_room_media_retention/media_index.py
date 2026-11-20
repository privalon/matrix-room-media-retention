"""Per-room media index backed by the PolicyStore's `media_cache` table.

Files in matrix-media-repo are immutable, so a file's size and upload time
never change once learned. Asking the media repo about every file of every
room on every report/scheduler pass (the previous behaviour) caused hundreds
of requests per run and, in turn, HTTP 429 rate limiting. This index only
asks about files it has not seen before; everything else comes from SQLite.

The same index lets the scheduler skip a room entirely when none of its
known files is old enough to purge, instead of calling the purge endpoint
every hour for every room with a policy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from .policy_store import PolicyStore
from .purge_client import MediaRepoPurgeClient
from .synapse_admin_client import SynapseAdminClient

logger = logging.getLogger(__name__)


@dataclass
class RoomMediaSummary:
    room_id: str
    # mxc -> (size_bytes, created_ts_ms | None)
    entries: dict[str, tuple[int, int | None]] = field(default_factory=dict)
    # False when any lookup failed, i.e. the picture may be missing files.
    complete: bool = True

    @property
    def total_bytes(self) -> int:
        return sum(size for size, _ in self.entries.values())

    @property
    def oldest_ts(self) -> int | None:
        stamps = [created for _, created in self.entries.values() if created]
        return min(stamps) if stamps else None

    def older_than(self, before_ts_ms: int) -> tuple[int, int]:
        """(file count, total bytes) of files uploaded before `before_ts_ms`."""
        old = [size for size, created in self.entries.values() if created and created < before_ts_ms]
        return len(old), sum(old)


class MediaIndex:
    def __init__(
        self,
        *,
        store: PolicyStore,
        purge_client: MediaRepoPurgeClient,
        synapse_provider: Callable[[], SynapseAdminClient | None],
    ):
        self._store = store
        self._purge_client = purge_client
        self._synapse_provider = synapse_provider

    def refresh_room(self, room_id: str, mxcs: list[str] | None = None) -> RoomMediaSummary | None:
        """Brings the cache for one room up to date and returns its summary.
        Returns None when the room's file list can't be read at all (callers
        must then NOT conclude "nothing to purge")."""
        if mxcs is None:
            synapse = self._synapse_provider()
            if synapse is None:
                return None
            mxcs = synapse.try_get_room_media_mxcs(room_id)
            if mxcs is None:
                return None

        cached = self._store.cache_get_room(room_id)
        current = set(mxcs)
        # Files no longer referenced by the room (or purged) leave the cache.
        self._store.cache_drop(room_id, [m for m in cached if m not in current])

        unknown = [m for m in mxcs if m not in cached]
        complete = True
        if unknown:
            usage, complete = self._purge_client.get_usage_for_mxcs_checked(unknown)
            fresh = {
                mxc: (int(v.get("size_bytes", 0)), int(v["created_ts"]) if v.get("created_ts") else None)
                for mxc, v in usage.items()
            }
            self._store.cache_put(room_id, fresh)
            cached.update(fresh)

        return RoomMediaSummary(
            room_id=room_id,
            entries={m: cached[m] for m in mxcs if m in cached},
            complete=complete,
        )
