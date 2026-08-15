"""Tests build_top_rooms_report() -- the shared report-building logic
behind both the on-demand `!media-retention top <N>` command and the
monthly proactive report, using fake synapse_admin/purge_client objects
(no real Matrix connection needed)."""

import tempfile
from pathlib import Path
from unittest import mock

import pytest

from matrix_room_media_retention.media_size_report import build_top_rooms_report
from matrix_room_media_retention.policy_store import PolicyStore


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = PolicyStore(Path(tmp) / "test.sqlite3")
        yield s
        s.close()


def _fake_synapse_admin(*, rooms, media_by_room):
    admin = mock.Mock()
    admin.list_all_rooms.return_value = rooms
    admin.get_room_media_mxcs.side_effect = lambda room_id: media_by_room.get(room_id, [])
    return admin


def _fake_purge_client(*, usage_by_mxc, overall_bytes=0):
    client = mock.Mock()

    def _get_usage_for_mxcs(mxcs):
        return {mxc: usage_by_mxc[mxc] for mxc in mxcs if mxc in usage_by_mxc}

    client.get_usage_for_mxcs.side_effect = _get_usage_for_mxcs
    client.get_overall_usage_bytes.return_value = overall_bytes
    return client


class TestBuildTopRoomsReport:
    def test_sorts_rooms_largest_first(self, store):
        synapse_admin = _fake_synapse_admin(
            rooms=[
                {"room_id": "!small:example.org", "name": "Small Room"},
                {"room_id": "!big:example.org", "name": "Big Room"},
            ],
            media_by_room={
                "!small:example.org": ["mxc://example.org/a"],
                "!big:example.org": ["mxc://example.org/b"],
            },
        )
        purge_client = _fake_purge_client(
            usage_by_mxc={
                "mxc://example.org/a": {"size_bytes": 100, "created_ts": 1000},
                "mxc://example.org/b": {"size_bytes": 100000, "created_ts": 2000},
            }
        )
        report = build_top_rooms_report(
            synapse_admin=synapse_admin, purge_client=purge_client, store=store,
            server_name="example.org", top_n=10,
        )
        big_index = report.index("!big:example.org")
        small_index = report.index("!small:example.org")
        assert big_index < small_index

    def test_includes_room_id_and_human_name(self, store):
        synapse_admin = _fake_synapse_admin(
            rooms=[{"room_id": "!abc:example.org", "name": "Project Chat"}],
            media_by_room={"!abc:example.org": ["mxc://example.org/x"]},
        )
        purge_client = _fake_purge_client(usage_by_mxc={"mxc://example.org/x": {"size_bytes": 500, "created_ts": 1000}})
        report = build_top_rooms_report(
            synapse_admin=synapse_admin, purge_client=purge_client, store=store,
            server_name="example.org", top_n=10,
        )
        assert "!abc:example.org" in report
        assert "Project Chat" in report

    def test_falls_back_to_no_name_placeholder(self, store):
        synapse_admin = _fake_synapse_admin(
            rooms=[{"room_id": "!abc:example.org", "name": None}],
            media_by_room={"!abc:example.org": ["mxc://example.org/x"]},
        )
        purge_client = _fake_purge_client(usage_by_mxc={"mxc://example.org/x": {"size_bytes": 500, "created_ts": 1000}})
        report = build_top_rooms_report(
            synapse_admin=synapse_admin, purge_client=purge_client, store=store,
            server_name="example.org", top_n=10,
        )
        assert "(no name)" in report

    def test_shows_the_rooms_current_retention_policy(self, store):
        store.set_retain("!abc:example.org", 30 * 86400)
        synapse_admin = _fake_synapse_admin(
            rooms=[{"room_id": "!abc:example.org", "name": "R"}],
            media_by_room={"!abc:example.org": ["mxc://example.org/x"]},
        )
        purge_client = _fake_purge_client(usage_by_mxc={"mxc://example.org/x": {"size_bytes": 500, "created_ts": 1000}})
        report = build_top_rooms_report(
            synapse_admin=synapse_admin, purge_client=purge_client, store=store,
            server_name="example.org", top_n=10,
        )
        assert "30d" in report

    def test_defaults_to_forever_when_no_policy_set(self, store):
        synapse_admin = _fake_synapse_admin(
            rooms=[{"room_id": "!abc:example.org", "name": "R"}],
            media_by_room={"!abc:example.org": ["mxc://example.org/x"]},
        )
        purge_client = _fake_purge_client(usage_by_mxc={"mxc://example.org/x": {"size_bytes": 500, "created_ts": 1000}})
        report = build_top_rooms_report(
            synapse_admin=synapse_admin, purge_client=purge_client, store=store,
            server_name="example.org", top_n=10,
        )
        assert "forever" in report

    def test_shows_the_oldest_media_files_date(self, store):
        synapse_admin = _fake_synapse_admin(
            rooms=[{"room_id": "!abc:example.org", "name": "R"}],
            media_by_room={"!abc:example.org": ["mxc://example.org/old", "mxc://example.org/new"]},
        )
        # 2020-01-01T00:00:00Z in epoch milliseconds.
        purge_client = _fake_purge_client(
            usage_by_mxc={
                "mxc://example.org/old": {"size_bytes": 500, "created_ts": 1577836800000},
                "mxc://example.org/new": {"size_bytes": 500, "created_ts": 1700000000000},
            }
        )
        report = build_top_rooms_report(
            synapse_admin=synapse_admin, purge_client=purge_client, store=store,
            server_name="example.org", top_n=10,
        )
        assert "2020-01-01" in report

    def test_respects_top_n_cap(self, store):
        rooms = [{"room_id": f"!r{i}:example.org", "name": f"Room {i}"} for i in range(5)]
        media_by_room = {r["room_id"]: [f"mxc://example.org/{r['room_id']}"] for r in rooms}
        usage_by_mxc = {f"mxc://example.org/{r['room_id']}": {"size_bytes": 100, "created_ts": 1000} for r in rooms}
        synapse_admin = _fake_synapse_admin(rooms=rooms, media_by_room=media_by_room)
        purge_client = _fake_purge_client(usage_by_mxc=usage_by_mxc)
        report = build_top_rooms_report(
            synapse_admin=synapse_admin, purge_client=purge_client, store=store,
            server_name="example.org", top_n=2,
        )
        assert report.count("policy: forever") == 2

    def test_rooms_with_no_resolvable_media_are_skipped_entirely(self, store):
        synapse_admin = _fake_synapse_admin(
            rooms=[{"room_id": "!empty:example.org", "name": "Empty"}],
            media_by_room={},
        )
        purge_client = _fake_purge_client(usage_by_mxc={})
        report = build_top_rooms_report(
            synapse_admin=synapse_admin, purge_client=purge_client, store=store,
            server_name="example.org", top_n=10,
        )
        assert "!empty:example.org" not in report
        assert "no rooms with resolvable media" in report.lower()

    def test_includes_the_overall_total_line(self, store):
        synapse_admin = _fake_synapse_admin(rooms=[], media_by_room={})
        purge_client = _fake_purge_client(usage_by_mxc={}, overall_bytes=5 * 1024 * 1024)
        report = build_top_rooms_report(
            synapse_admin=synapse_admin, purge_client=purge_client, store=store,
            server_name="example.org", top_n=10,
        )
        assert "Overall media storage used" in report
        assert "5.0 MB" in report
