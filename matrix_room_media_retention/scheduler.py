"""The purge scheduler loop.

Deliberately dumb: wake up on an interval, ask the policy store for every
room with an explicit `retain` policy (never the `forever`/absent ones --
see PolicyStore.list_retain_policies's own docstring), purge each, log the
result, sleep again. No backoff/retry sophistication -- a failed purge for
one room this tick just gets tried again next tick, and a scheduler-level
failure should be loud (logged with the room id) rather than silently
swallowed, since a silently-failing purge is exactly how a room the
operator believes is being kept small quietly fills up with permanent media.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .policy_store import PolicyStore
from .purge_client import MediaRepoPurgeClient, PurgeError

logger = logging.getLogger(__name__)


async def run_scheduler_loop(
    *,
    store: PolicyStore,
    purge_client: MediaRepoPurgeClient,
    interval_seconds: int,
    dry_run: bool = False,
    stop_event: asyncio.Event | None = None,
) -> None:
    while True:
        run_scheduler_pass(store=store, purge_client=purge_client, dry_run=dry_run)
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
                return  # stop_event was set -- exit instead of looping again
            except asyncio.TimeoutError:
                continue
        else:
            await asyncio.sleep(interval_seconds)


def run_scheduler_pass(*, store: PolicyStore, purge_client: MediaRepoPurgeClient, dry_run: bool = False) -> None:
    """One scheduler tick, factored out from the sleep loop so tests can
    call it directly without waiting on real time.

    `dry_run` (roadmap/041 §9): when true, computes the same `before_ts`
    and records the same audit-log entry as a real pass, but never calls
    `purge_client.purge_room()` at all -- matrix-media-repo's own purge
    endpoint has no native dry-run parameter (checked against its own
    admin API docs), so this is the only way to answer "what would this
    policy actually remove" without removing anything. A dry run never
    touches `record_purge()`'s per-room summary fields either (nothing was
    actually purged, so `last_purge_count` would be misleading) -- only
    the audit log, which explicitly records dry_run=True per entry so a
    reviewer isn't misled either.
    """
    now_ms = int(time.time() * 1000)
    for policy in store.list_retain_policies():
        before_ts_ms = now_ms - (policy.retain_seconds * 1000)

        if dry_run:
            logger.info(
                "[dry-run] Would purge media older than %s from room %s (retain=%ds) -- no request sent",
                before_ts_ms,
                policy.room_id,
                policy.retain_seconds,
            )
            store.record_purge_audit(
                room_id=policy.room_id, before_ts_ms=before_ts_ms, num_removed=0, dry_run=True
            )
            continue

        try:
            result = purge_client.purge_room(room_id=policy.room_id, before_ts_ms=before_ts_ms)
        except PurgeError:
            logger.exception("Purge failed for room %s -- will retry next tick", policy.room_id)
            continue
        store.record_purge(policy.room_id, result.num_removed)
        store.record_purge_audit(
            room_id=policy.room_id, before_ts_ms=before_ts_ms, num_removed=result.num_removed, dry_run=False
        )
        logger.info(
            "Purged %d media object(s) from room %s (retain=%ds)",
            result.num_removed,
            policy.room_id,
            policy.retain_seconds,
        )
