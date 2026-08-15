from unittest import mock

from matrix_room_media_retention.synapse_admin_client import SynapseAdminClient


def _fake_response(status_code, json_body=None, text=""):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    resp.text = text
    return resp


def _state_response(*events):
    return _fake_response(200, {"state": list(events)})


class TestGetRoomPowerLevels:
    """docs/roadmap/041 §11: reads a room's power_levels via Synapse's own
    admin room-state endpoint, no membership required at all -- confirmed
    live 2026-08-15 this is genuinely membership-independent, unlike the
    admin JOIN endpoint an earlier version of this client used instead
    (which refused a room the calling account had no prior relationship
    to, exactly the case for a never-before-seen remote target room)."""

    def test_returns_the_power_levels_content(self):
        client = SynapseAdminClient(homeserver_url="https://matrix.example.org", access_token="tok")
        power_levels = {"users": {"@mod:example.org": 50}, "users_default": 0}
        with mock.patch("matrix_room_media_retention.synapse_admin_client.requests.get") as get:
            get.return_value = _state_response(
                {"type": "m.room.create", "content": {}},
                {"type": "m.room.power_levels", "content": power_levels},
            )
            result = client.get_room_power_levels("!room:example.org")
        assert result == power_levels
        call = get.call_args
        assert call.args[0] == "https://matrix.example.org/_synapse/admin/v1/rooms/!room:example.org/state"
        assert call.kwargs["headers"]["Authorization"] == "Bearer tok"

    def test_returns_none_when_no_power_levels_event_present(self):
        client = SynapseAdminClient(homeserver_url="https://matrix.example.org", access_token="tok")
        with mock.patch("matrix_room_media_retention.synapse_admin_client.requests.get") as get:
            get.return_value = _state_response({"type": "m.room.create", "content": {}})
            assert client.get_room_power_levels("!room:example.org") is None

    def test_returns_none_on_non_200_rather_than_raising(self):
        # A room that doesn't exist (or the call otherwise failing) must
        # never be silently treated as "authorized" -- the caller
        # (bot.py's own _handle_remote_command) fails closed on None.
        client = SynapseAdminClient(homeserver_url="https://matrix.example.org", access_token="tok")
        with mock.patch("matrix_room_media_retention.synapse_admin_client.requests.get") as get:
            get.return_value = _fake_response(404, text="Not found")
            assert client.get_room_power_levels("!room:example.org") is None

    def test_base_url_trailing_slash_stripped(self):
        client = SynapseAdminClient(homeserver_url="https://matrix.example.org/", access_token="tok")
        with mock.patch("matrix_room_media_retention.synapse_admin_client.requests.get") as get:
            get.return_value = _state_response()
            client.get_room_power_levels("!room:example.org")
        url = get.call_args.args[0]
        assert "//_synapse" not in url


class TestGetRoomName:
    def test_returns_the_rooms_name(self):
        client = SynapseAdminClient(homeserver_url="https://matrix.example.org", access_token="tok")
        with mock.patch("matrix_room_media_retention.synapse_admin_client.requests.get") as get:
            get.return_value = _fake_response(200, {"name": "General Chat"})
            name = client.get_room_name("!room:example.org")
        assert name == "General Chat"
        call = get.call_args
        assert call.args[0] == "https://matrix.example.org/_synapse/admin/v1/rooms/!room:example.org"

    def test_returns_none_when_no_name_set(self):
        client = SynapseAdminClient(homeserver_url="https://matrix.example.org", access_token="tok")
        with mock.patch("matrix_room_media_retention.synapse_admin_client.requests.get") as get:
            get.return_value = _fake_response(200, {})
            assert client.get_room_name("!room:example.org") is None

    def test_returns_none_on_non_200_rather_than_raising(self):
        # A missing/unreachable name is never worth failing the whole
        # `!media-retention list` reply over.
        client = SynapseAdminClient(homeserver_url="https://matrix.example.org", access_token="tok")
        with mock.patch("matrix_room_media_retention.synapse_admin_client.requests.get") as get:
            get.return_value = _fake_response(404, text="Not found")
            assert client.get_room_name("!room:example.org") is None


class TestListAllRooms:
    def test_single_page_returns_every_room(self):
        client = SynapseAdminClient(homeserver_url="https://matrix.example.org", access_token="tok")
        with mock.patch("matrix_room_media_retention.synapse_admin_client.requests.get") as get:
            get.return_value = _fake_response(
                200,
                {
                    "rooms": [{"room_id": "!a:example.org", "name": "A"}, {"room_id": "!b:example.org", "name": "B"}],
                    "next_batch": None,
                },
            )
            rooms = client.list_all_rooms()
        assert rooms == [{"room_id": "!a:example.org", "name": "A"}, {"room_id": "!b:example.org", "name": "B"}]
        call = get.call_args
        assert call.args[0] == "https://matrix.example.org/_synapse/admin/v1/rooms"
        assert call.kwargs["params"] == {"from": 0, "limit": 100}

    def test_paginates_across_multiple_pages(self):
        client = SynapseAdminClient(homeserver_url="https://matrix.example.org", access_token="tok")
        page1 = _fake_response(200, {"rooms": [{"room_id": "!a:example.org", "name": "A"}], "next_batch": 1})
        page2 = _fake_response(200, {"rooms": [{"room_id": "!b:example.org", "name": "B"}], "next_batch": None})
        with mock.patch("matrix_room_media_retention.synapse_admin_client.requests.get") as get:
            get.side_effect = [page1, page2]
            rooms = client.list_all_rooms()
        assert [r["room_id"] for r in rooms] == ["!a:example.org", "!b:example.org"]
        assert get.call_count == 2
        assert get.call_args_list[1].kwargs["params"] == {"from": 1, "limit": 100}

    def test_non_200_page_stops_and_returns_whatever_was_collected(self):
        client = SynapseAdminClient(homeserver_url="https://matrix.example.org", access_token="tok")
        page1 = _fake_response(200, {"rooms": [{"room_id": "!a:example.org", "name": "A"}], "next_batch": 1})
        page2 = _fake_response(500, text="oops")
        with mock.patch("matrix_room_media_retention.synapse_admin_client.requests.get") as get:
            get.side_effect = [page1, page2]
            rooms = client.list_all_rooms()
        assert [r["room_id"] for r in rooms] == ["!a:example.org"]


class TestGetRoomMediaMxcs:
    def test_returns_local_and_remote_mxcs_combined(self):
        client = SynapseAdminClient(homeserver_url="https://matrix.example.org", access_token="tok")
        with mock.patch("matrix_room_media_retention.synapse_admin_client.requests.get") as get:
            get.return_value = _fake_response(
                200, {"local": ["mxc://example.org/aaa"], "remote": ["mxc://other.org/bbb"]}
            )
            mxcs = client.get_room_media_mxcs("!room:example.org")
        assert mxcs == ["mxc://example.org/aaa", "mxc://other.org/bbb"]
        call = get.call_args
        assert call.args[0] == "https://matrix.example.org/_synapse/admin/v1/room/!room:example.org/media"

    def test_returns_empty_list_on_non_200_rather_than_raising(self):
        client = SynapseAdminClient(homeserver_url="https://matrix.example.org", access_token="tok")
        with mock.patch("matrix_room_media_retention.synapse_admin_client.requests.get") as get:
            get.return_value = _fake_response(404, text="Not found")
            assert client.get_room_media_mxcs("!room:example.org") == []
