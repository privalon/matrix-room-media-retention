"""Per-room retention policy persistence, plus an append-only purge audit
log (roadmap/041 §9, second review pass).

SQLite, not Postgres, deliberately: this plugin's entire durable state is a
couple of small tables (room_id, policy, timestamps; a purge history) -- a
single file is easier to back up/restore/inspect than requiring a shared
Postgres instance, and avoids coupling the plugin's own release cadence to
whatever Postgres client library version the Matrix deployment happens to
pin. An operator who already runs Postgres for everything else is free to
point this at a Postgres file via a future backend swap; nothing here
assumes SQLite's file format at the call-site level.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS room_policy (
    room_id TEXT PRIMARY KEY,
    policy TEXT NOT NULL,
    retain_seconds INTEGER,
    updated_at INTEGER NOT NULL,
    last_purged_at INTEGER,
    last_purge_count INTEGER
);

CREATE TABLE IF NOT EXISTS purge_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id TEXT NOT NULL,
    ran_at INTEGER NOT NULL,
    before_ts_ms INTEGER NOT NULL,
    num_removed INTEGER NOT NULL,
    dry_run INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class RoomPolicy:
    room_id: str
    policy: str  # "forever" | "retain"
    retain_seconds: int | None
    updated_at: int
    last_purged_at: int | None
    last_purge_count: int | None


@dataclass(frozen=True)
class AuditLogEntry:
    id: int
    room_id: str
    ran_at: int
    before_ts_ms: int
    num_removed: int
    dry_run: bool


class PolicyStore:
    """Not thread-safe by design -- the bot and scheduler are expected to
    share one asyncio event loop, never separate OS threads. A future
    multi-process deployment would need a real lock or a backend swap, not
    a bigger hammer applied here."""

    def __init__(self, db_path: str | Path):
        self._conn = sqlite3.connect(str(db_path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def set_retain(self, room_id: str, retain_seconds: int) -> None:
        now = int(time.time())
        self._conn.execute(
            """
            INSERT INTO room_policy (room_id, policy, retain_seconds, updated_at)
            VALUES (?, 'retain', ?, ?)
            ON CONFLICT(room_id) DO UPDATE SET
                policy = 'retain',
                retain_seconds = excluded.retain_seconds,
                updated_at = excluded.updated_at
            """,
            (room_id, retain_seconds, now),
        )
        self._conn.commit()

    def set_forever(self, room_id: str) -> None:
        now = int(time.time())
        self._conn.execute(
            """
            INSERT INTO room_policy (room_id, policy, retain_seconds, updated_at)
            VALUES (?, 'forever', NULL, ?)
            ON CONFLICT(room_id) DO UPDATE SET
                policy = 'forever',
                retain_seconds = NULL,
                updated_at = excluded.updated_at
            """,
            (room_id, now),
        )
        self._conn.commit()

    def get(self, room_id: str) -> RoomPolicy | None:
        row = self._conn.execute(
            "SELECT room_id, policy, retain_seconds, updated_at, last_purged_at, "
            "last_purge_count FROM room_policy WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        if row is None:
            return None
        return RoomPolicy(*row)

    def list_retain_policies(self) -> list[RoomPolicy]:
        """Only rooms with an explicit `retain` policy -- `forever`/absent
        rooms are never returned, so the scheduler never even considers
        them (not just "considers and skips"), matching the acceptance
        criterion that a forever-policy room is never queried."""
        rows = self._conn.execute(
            "SELECT room_id, policy, retain_seconds, updated_at, last_purged_at, "
            "last_purge_count FROM room_policy WHERE policy = 'retain'"
        ).fetchall()
        return [RoomPolicy(*row) for row in rows]

    def record_purge(self, room_id: str, media_purged_count: int) -> None:
        """Updates the per-room summary fields (what the bot's own
        `!media-retention` status reply reads) and appends one row to the
        audit log -- kept as two separate concerns (see record_purge_audit
        below) rather than folding the audit insert in here, so a dry run
        (which must NOT touch the per-room summary, since nothing was
        actually purged) can still be logged."""
        now = int(time.time())
        self._conn.execute(
            "UPDATE room_policy SET last_purged_at = ?, last_purge_count = ? WHERE room_id = ?",
            (now, media_purged_count, room_id),
        )
        self._conn.commit()

    def record_purge_audit(
        self, *, room_id: str, before_ts_ms: int, num_removed: int, dry_run: bool
    ) -> None:
        """Append-only history of every scheduler pass's outcome per room
        (roadmap/041 §9, informed by 037 §6's "audit log" as a core
        responsibility, not optional). Unlike record_purge()'s summary
        fields (overwritten every tick), this is never updated or
        deleted -- the durable answer to "what did this tool actually
        remove, and when" that an operator can review after the fact."""
        now = int(time.time())
        self._conn.execute(
            "INSERT INTO purge_audit_log (room_id, ran_at, before_ts_ms, num_removed, dry_run) "
            "VALUES (?, ?, ?, ?, ?)",
            (room_id, now, before_ts_ms, num_removed, int(dry_run)),
        )
        self._conn.commit()

    def list_audit_log(self, *, room_id: str | None = None, limit: int = 100) -> list[AuditLogEntry]:
        """Most recent entries first. Filtered to one room when `room_id`
        is given (e.g. for a future `!media-retention history` command),
        otherwise every room's history."""
        if room_id is not None:
            rows = self._conn.execute(
                "SELECT id, room_id, ran_at, before_ts_ms, num_removed, dry_run "
                "FROM purge_audit_log WHERE room_id = ? ORDER BY id DESC LIMIT ?",
                (room_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, room_id, ran_at, before_ts_ms, num_removed, dry_run "
                "FROM purge_audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            AuditLogEntry(
                id=row[0], room_id=row[1], ran_at=row[2], before_ts_ms=row[3],
                num_removed=row[4], dry_run=bool(row[5]),
            )
            for row in rows
        ]
