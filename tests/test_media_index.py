import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

from matrix_room_media_retention.media_index import MediaIndex
from matrix_room_media_retention.policy_store import PolicyStore
from matrix_room_media_retention.purge_client import MediaRepoPurgeClient, PurgeResult
from matrix_room_media_retention.scheduler import run_scheduler_pass


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = PolicyStore(Path(tmp) / "t.sqlite3")
        yield s
        s.close()


def _index(store, *, mxcs, usage, complete=True):
    synapse = mock.Mock()
    synapse.try_get_room_media_mxcs.side_effect = lambda room_id: mxcs
    purge = mock.Mock()
    purge.get_usage_for_mxcs_checked.side_effect = lambda m: ({k: usage[k] for k in m if k in usage}, complete)
    return MediaIndex(store=store, purge_client=purge, synapse_provider=lambda: synapse), purge, synapse


class TestMediaIndex:
    def test_only_unknown_files_are_asked_about(self, store):
        idx, purge, _ = _index(
            store,
            mxcs=["mxc://x/a", "mxc://x/b"],
            usage={"mxc://x/a": {"size_bytes": 10, "created_ts": 100}, "mxc://x/b": {"size_bytes": 20, "created_ts": 200}},
        )
        first = idx.refresh_room("!r:x")
        assert first.total_bytes == 30 and first.oldest_ts == 100
        purge.get_usage_for_mxcs_checked.reset_mock()
        second = idx.refresh_room("!r:x")
        assert second.total_bytes == 30
        purge.get_usage_for_mxcs_checked.assert_not_called()

    def test_new_file_is_the_only_one_requested(self, store):
        usage = {f"mxc://x/{c}": {"size_bytes": 1, "created_ts": 5} for c in "abc"}
        mxcs = ["mxc://x/a", "mxc://x/b"]
        idx, purge, synapse = _index(store, mxcs=mxcs, usage=usage)
        idx.refresh_room("!r:x")
        mxcs.append("mxc://x/c")
        purge.get_usage_for_mxcs_checked.reset_mock()
        idx.refresh_room("!r:x")
        purge.get_usage_for_mxcs_checked.assert_called_once_with(["mxc://x/c"])

    def test_returns_none_when_synapse_listing_fails(self, store):
        synapse = mock.Mock()
        synapse.try_get_room_media_mxcs.return_value = None
        idx = MediaIndex(store=store, purge_client=mock.Mock(), synapse_provider=lambda: synapse)
        assert idx.refresh_room("!r:x") is None

    def test_returns_none_before_login(self, store):
        idx = MediaIndex(store=store, purge_client=mock.Mock(), synapse_provider=lambda: None)
        assert idx.refresh_room("!r:x") is None

    def test_files_dropped_from_room_leave_the_cache(self, store):
        mxcs = ["mxc://x/a", "mxc://x/b"]
        usage = {m: {"size_bytes": 7, "created_ts": 1} for m in mxcs}
        idx, _, _ = _index(store, mxcs=mxcs, usage=usage)
        idx.refresh_room("!r:x")
        mxcs.remove("mxc://x/b")
        assert idx.refresh_room("!r:x").total_bytes == 7
        assert set(store.cache_get_room("!r:x")) == {"mxc://x/a"}


class TestSchedulerWithIndex:
    def _setup(self, store, created_ts, complete=True):
        store.set_retain("!r:x", 86400)
        idx, purge_usage, _ = _index(
            store, mxcs=["mxc://x/a"], usage={"mxc://x/a": {"size_bytes": 9, "created_ts": created_ts}}, complete=complete
        )
        purge_client = mock.Mock()
        purge_client.purge_room.return_value = PurgeResult(room_id="!r:x", num_removed=1)
        return idx, purge_client

    def test_skips_purge_when_nothing_is_old_enough(self, store):
        idx, purge_client = self._setup(store, created_ts=int(time.time() * 1000))
        run_scheduler_pass(store=store, purge_client=purge_client, media_index=idx)
        purge_client.purge_room.assert_not_called()

    def test_purges_when_something_is_expired(self, store):
        idx, purge_client = self._setup(store, created_ts=1000)
        run_scheduler_pass(store=store, purge_client=purge_client, media_index=idx)
        purge_client.purge_room.assert_called_once()

    def test_purges_when_picture_is_incomplete(self, store):
        idx, purge_client = self._setup(store, created_ts=int(time.time() * 1000), complete=False)
        run_scheduler_pass(store=store, purge_client=purge_client, media_index=idx)
        purge_client.purge_room.assert_called_once()

    def test_purges_when_index_cannot_read_room(self, store):
        store.set_retain("!r:x", 86400)
        idx = MediaIndex(store=store, purge_client=mock.Mock(), synapse_provider=lambda: None)
        purge_client = mock.Mock()
        purge_client.purge_room.return_value = PurgeResult(room_id="!r:x", num_removed=0)
        run_scheduler_pass(store=store, purge_client=purge_client, media_index=idx)
        purge_client.purge_room.assert_called_once()

    def test_post_purge_warns_when_files_remain(self, store, caplog):
        idx, purge_client = self._setup(store, created_ts=1000)
        with caplog.at_level("WARNING"):
            run_scheduler_pass(store=store, purge_client=purge_client, media_index=idx)
        assert "left 1 expired file" in caplog.text

    def test_post_purge_confirms_when_media_gone(self, store, caplog):
        idx, purge_client = self._setup(store, created_ts=1000)
        # After the purge matrix-media-repo no longer knows the file.
        def gone(room_id):
            idx._purge_client.get_usage_for_mxcs_checked.side_effect = lambda m: ({}, True)
            return PurgeResult(room_id=room_id, num_removed=1)
        purge_client.purge_room.side_effect = lambda room_id, before_ts_ms: gone(room_id)
        with caplog.at_level("INFO"):
            run_scheduler_pass(store=store, purge_client=purge_client, media_index=idx)
        assert "Verified: freed" in caplog.text


class TestThrottleAndBatching:
    def test_usage_requests_are_batched_and_paced(self):
        client = MediaRepoPurgeClient(base_url="https://m", admin_access_token="t", min_request_interval_seconds=0.05)
        mxcs = [f"mxc://x/{i}" for i in range(250)]
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {}
        with mock.patch("matrix_room_media_retention.purge_client.requests.get", return_value=resp) as get:
            start = time.monotonic()
            _, complete = client.get_usage_for_mxcs_checked(mxcs)
            elapsed = time.monotonic() - start
        assert complete and get.call_count == 3  # 100 + 100 + 50
        assert elapsed >= 0.09

    def test_failed_batch_marks_result_incomplete(self):
        client = MediaRepoPurgeClient(base_url="https://m", admin_access_token="t", min_request_interval_seconds=0)
        resp = mock.Mock(status_code=500)
        with mock.patch("matrix_room_media_retention.purge_client.requests.get", return_value=resp):
            result, complete = client.get_usage_for_mxcs_checked(["mxc://x/a"])
        assert result == {} and complete is False
