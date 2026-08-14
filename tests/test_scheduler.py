import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

from matrix_room_media_retention.policy_store import PolicyStore
from matrix_room_media_retention.purge_client import PurgeError, PurgeResult
from matrix_room_media_retention.scheduler import run_scheduler_pass


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = PolicyStore(Path(tmp) / "test.sqlite3")
        yield s
        s.close()


class TestRunSchedulerPass:
    def test_purges_only_retain_rooms(self, store):
        store.set_retain("!a:example.org", 86400)
        store.set_forever("!b:example.org")

        purge_client = mock.Mock()
        purge_client.purge_room.return_value = PurgeResult(room_id="!a:example.org", num_removed=3)

        run_scheduler_pass(store=store, purge_client=purge_client)

        purge_client.purge_room.assert_called_once()
        called_room_id = purge_client.purge_room.call_args.kwargs["room_id"]
        assert called_room_id == "!a:example.org"

    def test_before_ts_reflects_retain_window(self, store):
        store.set_retain("!a:example.org", 86400)  # 1 day
        purge_client = mock.Mock()
        purge_client.purge_room.return_value = PurgeResult(room_id="!a:example.org", num_removed=0)

        before_call_ms = int(time.time() * 1000)
        run_scheduler_pass(store=store, purge_client=purge_client)
        after_call_ms = int(time.time() * 1000)

        before_ts_ms = purge_client.purge_room.call_args.kwargs["before_ts_ms"]
        # before_ts should be "now minus 1 day", give or take test execution time.
        assert before_call_ms - 86400_000 <= before_ts_ms <= after_call_ms - 86400_000

    def test_records_purge_result(self, store):
        store.set_retain("!a:example.org", 86400)
        purge_client = mock.Mock()
        purge_client.purge_room.return_value = PurgeResult(room_id="!a:example.org", num_removed=9)

        run_scheduler_pass(store=store, purge_client=purge_client)

        policy = store.get("!a:example.org")
        assert policy.last_purge_count == 9
        assert policy.last_purged_at is not None

    def test_one_room_failure_does_not_block_others(self, store):
        store.set_retain("!a:example.org", 86400)
        store.set_retain("!b:example.org", 86400)

        purge_client = mock.Mock()

        def side_effect(*, room_id, before_ts_ms):
            if room_id == "!a:example.org":
                raise PurgeError("boom")
            return PurgeResult(room_id=room_id, num_removed=1)

        purge_client.purge_room.side_effect = side_effect

        run_scheduler_pass(store=store, purge_client=purge_client)

        # !a failed and was not recorded; !b succeeded and was recorded.
        assert store.get("!a:example.org").last_purge_count is None
        assert store.get("!b:example.org").last_purge_count == 1

    def test_no_retain_rooms_calls_purge_zero_times(self, store):
        store.set_forever("!a:example.org")
        purge_client = mock.Mock()
        run_scheduler_pass(store=store, purge_client=purge_client)
        purge_client.purge_room.assert_not_called()

    def test_successful_purge_is_recorded_to_the_audit_log(self, store):
        store.set_retain("!a:example.org", 86400)
        purge_client = mock.Mock()
        purge_client.purge_room.return_value = PurgeResult(room_id="!a:example.org", num_removed=7)

        run_scheduler_pass(store=store, purge_client=purge_client)

        entries = store.list_audit_log(room_id="!a:example.org")
        assert len(entries) == 1
        assert entries[0].num_removed == 7
        assert entries[0].dry_run is False

    def test_failed_purge_is_not_recorded_to_the_audit_log(self, store):
        store.set_retain("!a:example.org", 86400)
        purge_client = mock.Mock()
        purge_client.purge_room.side_effect = PurgeError("boom")

        run_scheduler_pass(store=store, purge_client=purge_client)

        assert store.list_audit_log(room_id="!a:example.org") == []


class TestDryRun:
    """roadmap/041 §9 (second review pass): dry_run computes and logs what
    would be purged without ever calling matrix-media-repo's real endpoint."""

    def test_dry_run_never_calls_the_real_purge_endpoint(self, store):
        store.set_retain("!a:example.org", 86400)
        purge_client = mock.Mock()

        run_scheduler_pass(store=store, purge_client=purge_client, dry_run=True)

        purge_client.purge_room.assert_not_called()

    def test_dry_run_does_not_touch_the_per_room_summary_fields(self, store):
        store.set_retain("!a:example.org", 86400)
        purge_client = mock.Mock()

        run_scheduler_pass(store=store, purge_client=purge_client, dry_run=True)

        policy = store.get("!a:example.org")
        assert policy.last_purge_count is None
        assert policy.last_purged_at is None

    def test_dry_run_still_writes_to_the_audit_log_with_the_flag_set(self, store):
        store.set_retain("!a:example.org", 86400)
        purge_client = mock.Mock()

        run_scheduler_pass(store=store, purge_client=purge_client, dry_run=True)

        entries = store.list_audit_log(room_id="!a:example.org")
        assert len(entries) == 1
        assert entries[0].dry_run is True
        assert entries[0].num_removed == 0

    def test_dry_run_still_skips_forever_rooms(self, store):
        store.set_forever("!a:example.org")
        purge_client = mock.Mock()

        run_scheduler_pass(store=store, purge_client=purge_client, dry_run=True)

        assert store.list_audit_log() == []

    def test_dry_run_covers_every_retain_room_independently(self, store):
        store.set_retain("!a:example.org", 86400)
        store.set_retain("!b:example.org", 2 * 86400)
        purge_client = mock.Mock()

        run_scheduler_pass(store=store, purge_client=purge_client, dry_run=True)

        assert len(store.list_audit_log(room_id="!a:example.org")) == 1
        assert len(store.list_audit_log(room_id="!b:example.org")) == 1
        purge_client.purge_room.assert_not_called()
