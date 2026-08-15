from unittest import mock

import pytest

from matrix_room_media_retention.synapse_admin_client import SynapseAdminClient, SynapseAdminError


def _fake_response(status_code, json_body=None, text=""):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    resp.text = text
    return resp


class TestForceJoinRoom:
    def test_success(self):
        client = SynapseAdminClient(homeserver_url="https://matrix.example.org", access_token="tok")
        with mock.patch("matrix_room_media_retention.synapse_admin_client.requests.post") as post:
            post.return_value = _fake_response(200, {})
            client.force_join_room("!room:example.org")
        call = post.call_args
        assert call.args[0] == "https://matrix.example.org/_synapse/admin/v1/join/!room:example.org"
        assert call.kwargs["headers"]["Authorization"] == "Bearer tok"

    def test_non_200_raises(self):
        client = SynapseAdminClient(homeserver_url="https://matrix.example.org", access_token="tok")
        with mock.patch("matrix_room_media_retention.synapse_admin_client.requests.post") as post:
            post.return_value = _fake_response(403, text="Forbidden")
            with pytest.raises(SynapseAdminError):
                client.force_join_room("!room:example.org")

    def test_base_url_trailing_slash_stripped(self):
        client = SynapseAdminClient(homeserver_url="https://matrix.example.org/", access_token="tok")
        with mock.patch("matrix_room_media_retention.synapse_admin_client.requests.post") as post:
            post.return_value = _fake_response(200, {})
            client.force_join_room("!room:example.org")
        url = post.call_args.args[0]
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
