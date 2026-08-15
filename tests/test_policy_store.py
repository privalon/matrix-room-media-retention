import tempfile
from pathlib import Path

import pytest

from matrix_room_media_retention.policy_store import PolicyStore


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = PolicyStore(Path(tmp) / "test.sqlite3")
        yield s
        s.close()


class TestPolicyStore:
    def test_unknown_room_returns_none(self, store):
        assert store.get("!unknown:example.org") is None

    def test_set_retain_then_get(self, store):
        store.set_retain("!room:example.org", 30 * 86400)
        policy = store.get("!room:example.org")
        assert policy.policy == "retain"
        assert policy.retain_seconds == 30 * 86400

    def test_set_forever_then_get(self, store):
        store.set_forever("!room:example.org")
        policy = store.get("!room:example.org")
        assert policy.policy == "forever"
        assert policy.retain_seconds is None

    def test_retain_then_forever_overwrites(self, store):
        store.set_retain("!room:example.org", 86400)
        store.set_forever("!room:example.org")
        policy = store.get("!room:example.org")
        assert policy.policy == "forever"
        assert policy.retain_seconds is None

    def test_forever_then_retain_overwrites(self, store):
        store.set_forever("!room:example.org")
        store.set_retain("!room:example.org", 86400)
        policy = store.get("!room:example.org")
        assert policy.policy == "retain"
        assert policy.retain_seconds == 86400

    def test_list_retain_policies_excludes_forever_rooms(self, store):
        store.set_retain("!a:example.org", 86400)
        store.set_forever("!b:example.org")
        rooms = {p.room_id for p in store.list_retain_policies()}
        assert rooms == {"!a:example.org"}

    def test_list_retain_policies_excludes_unconfigured_rooms(self, store):
        store.set_retain("!a:example.org", 86400)
        # !unconfigured:example.org was never set at all.
        rooms = {p.room_id for p in store.list_retain_policies()}
        assert rooms == {"!a:example.org"}

    def test_list_all_policies_includes_forever_rooms(self, store):
        # Unlike list_retain_policies() (which the scheduler uses), the
        # remote `!media-retention list` command wants everything an
        # operator has explicitly configured, forever included.
        store.set_retain("!a:example.org", 86400)
        store.set_forever("!b:example.org")
        rooms = {p.room_id for p in store.list_all_policies()}
        assert rooms == {"!a:example.org", "!b:example.org"}

    def test_list_all_policies_excludes_unconfigured_rooms(self, store):
        store.set_retain("!a:example.org", 86400)
        rooms = {p.room_id for p in store.list_all_policies()}
        assert rooms == {"!a:example.org"}

    def test_list_all_policies_empty_when_nothing_configured(self, store):
        assert store.list_all_policies() == []

    def test_record_purge_updates_last_purge_fields(self, store):
        store.set_retain("!room:example.org", 86400)
        store.record_purge("!room:example.org", 5)
        policy = store.get("!room:example.org")
        assert policy.last_purge_count == 5
        assert policy.last_purged_at is not None

    def test_persists_across_reconnect(self, tmp_path):
        db_path = tmp_path / "persist.sqlite3"
        s1 = PolicyStore(db_path)
        s1.set_retain("!room:example.org", 86400)
        s1.close()

        s2 = PolicyStore(db_path)
        policy = s2.get("!room:example.org")
        s2.close()
        assert policy.policy == "retain"
        assert policy.retain_seconds == 86400


class TestPurgeAuditLog:
    """roadmap/041 §9 (second review pass): an append-only history of every
    scheduler pass's outcome per room, separate from the per-room summary
    fields (which get overwritten every tick)."""

    def test_empty_log_for_a_room_with_no_purges_yet(self, store):
        assert store.list_audit_log(room_id="!room:example.org") == []

    def test_record_purge_audit_appends_an_entry(self, store):
        store.record_purge_audit(
            room_id="!room:example.org", before_ts_ms=1000, num_removed=3, dry_run=False
        )
        entries = store.list_audit_log(room_id="!room:example.org")
        assert len(entries) == 1
        entry = entries[0]
        assert entry.room_id == "!room:example.org"
        assert entry.before_ts_ms == 1000
        assert entry.num_removed == 3
        assert entry.dry_run is False
        assert entry.ran_at is not None

    def test_multiple_entries_do_not_overwrite_each_other(self, store):
        store.record_purge_audit(room_id="!room:example.org", before_ts_ms=1000, num_removed=1, dry_run=False)
        store.record_purge_audit(room_id="!room:example.org", before_ts_ms=2000, num_removed=2, dry_run=False)
        entries = store.list_audit_log(room_id="!room:example.org")
        assert len(entries) == 2

    def test_most_recent_entry_first(self, store):
        store.record_purge_audit(room_id="!room:example.org", before_ts_ms=1000, num_removed=1, dry_run=False)
        store.record_purge_audit(room_id="!room:example.org", before_ts_ms=2000, num_removed=2, dry_run=False)
        entries = store.list_audit_log(room_id="!room:example.org")
        assert entries[0].before_ts_ms == 2000
        assert entries[1].before_ts_ms == 1000

    def test_dry_run_flag_is_recorded_and_readable(self, store):
        store.record_purge_audit(room_id="!room:example.org", before_ts_ms=1000, num_removed=0, dry_run=True)
        entries = store.list_audit_log(room_id="!room:example.org")
        assert entries[0].dry_run is True

    def test_filtering_by_room_excludes_other_rooms(self, store):
        store.record_purge_audit(room_id="!a:example.org", before_ts_ms=1000, num_removed=1, dry_run=False)
        store.record_purge_audit(room_id="!b:example.org", before_ts_ms=1000, num_removed=2, dry_run=False)
        entries = store.list_audit_log(room_id="!a:example.org")
        assert len(entries) == 1
        assert entries[0].room_id == "!a:example.org"

    def test_no_room_id_filter_returns_every_rooms_history(self, store):
        store.record_purge_audit(room_id="!a:example.org", before_ts_ms=1000, num_removed=1, dry_run=False)
        store.record_purge_audit(room_id="!b:example.org", before_ts_ms=1000, num_removed=2, dry_run=False)
        entries = store.list_audit_log()
        assert {e.room_id for e in entries} == {"!a:example.org", "!b:example.org"}

    def test_record_purge_does_not_itself_write_to_the_audit_log(self, store):
        # record_purge() only updates the per-room summary fields --
        # callers (the scheduler) are responsible for also calling
        # record_purge_audit() themselves, so a dry run can log without
        # touching the summary at all.
        store.set_retain("!room:example.org", 86400)
        store.record_purge("!room:example.org", 5)
        assert store.list_audit_log(room_id="!room:example.org") == []

    def test_persists_across_reconnect(self, tmp_path):
        db_path = tmp_path / "persist.sqlite3"
        s1 = PolicyStore(db_path)
        s1.record_purge_audit(room_id="!room:example.org", before_ts_ms=1000, num_removed=4, dry_run=False)
        s1.close()

        s2 = PolicyStore(db_path)
        entries = s2.list_audit_log(room_id="!room:example.org")
        s2.close()
        assert len(entries) == 1
        assert entries[0].num_removed == 4
