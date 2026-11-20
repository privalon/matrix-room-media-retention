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

from .media_index import MediaIndex
from .policy_store import PolicyStore
from .purge_client import MediaRepoPurgeClient, PurgeError

logger = logging.getLogger(__name__)


async def run_scheduler_loop(
    *,
    store: PolicyStore,
    purge_client: MediaRepoPurgeClient,
    interval_seconds: int,
    dry_run: bool = False,
    media_index: MediaIndex | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    while True:
        run_scheduler_pass(store=store, purge_client=purge_client, dry_run=dry_run, media_index=media_index)
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
                return  # stop_event was set -- exit instead of looping again
            except asyncio.TimeoutError:
                continue
        else:
            await asyncio.sleep(interval_seconds)


def run_scheduler_pass(
    *,
    store: PolicyStore,
    purge_client: MediaRepoPurgeClient,
    dry_run: bool = False,
    media_index: MediaIndex | None = None,
) -> None:
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

        expired_before = (0, 0)
        if media_index is not None:
            # Only files not seen before cost a request to matrix-media-repo.
            # If we positively know nothing in this room is old enough, skip
            # the purge call entirely. Any doubt (lookup failed, incomplete
            # picture) falls through to purging as before.
            try:
                summary = media_index.refresh_room(policy.room_id)
            except Exception:
                logger.exception("Media index refresh failed for room %s -- purging without it", policy.room_id)
                summary = None
            if summary is not None:
                expired_before = summary.older_than(before_ts_ms)
                if summary.complete and expired_before[0] == 0:
                    logger.info(
                        "Nothing to purge in room %s (%d file(s), %d byte(s) tracked, none older than retention)",
                        policy.room_id,
                        len(summary.entries),
                        summary.total_bytes,
                    )
                    continue

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
        except Exception:
            # Any other failure must not kill the scheduler task (an uncaught
            # exception here would silently stop ALL future purges).
            logger.exception("Unexpected error purging room %s -- will retry next tick", policy.room_id)
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
        if media_index is not None:
            _verify_purge(media_index, store, policy.room_id, before_ts_ms, expired_before)


def _verify_purge(
    media_index: MediaIndex, store: PolicyStore, room_id: str, before_ts_ms: int, expected: tuple[int, int]
) -> None:
    """After a purge: forget the expired files, re-ask about them and warn if
    the media repo still reports any (the purge did not really remove them)."""
    try:
        store.cache_drop_older_than(room_id, before_ts_ms)
        summary = media_index.refresh_room(room_id)
        if summary is None:
            return
        remaining_count, remaining_bytes = summary.older_than(before_ts_ms)
        if remaining_count:
            logger.warning(
                "Purge of room %s left %d expired file(s) (%d byte(s)) behind; expected to free %d file(s) / %d byte(s)",
                room_id,
                remaining_count,
                remaining_bytes,
                expected[0],
                expected[1],
            )
        else:
            logger.info("Verified: freed ~%d file(s) / %d byte(s) in room %s", expected[0], expected[1], room_id)
    except Exception:
        logger.exception("Post-purge verification failed for room %s", room_id)
