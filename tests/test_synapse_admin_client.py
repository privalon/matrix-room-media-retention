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
            client.force_join_room(room_id="!room:example.org", user_id="@bot:example.org")
        call = post.call_args
        assert call.args[0] == "https://matrix.example.org/_synapse/admin/v1/join/!room:example.org"
        assert call.kwargs["headers"]["Authorization"] == "Bearer tok"
        # Required by Synapse's own admin API -- it can force-join *any*
        # local user, so it never assumes "join myself".
        assert call.kwargs["json"] == {"user_id": "@bot:example.org"}

    def test_non_200_raises(self):
        client = SynapseAdminClient(homeserver_url="https://matrix.example.org", access_token="tok")
        with mock.patch("matrix_room_media_retention.synapse_admin_client.requests.post") as post:
            post.return_value = _fake_response(403, text="Forbidden")
            with pytest.raises(SynapseAdminError):
                client.force_join_room(room_id="!room:example.org", user_id="@bot:example.org")

    def test_already_in_the_room_is_treated_as_success_not_an_error(self):
        # Found live 2026-08-15: Synapse's own admin join API returns 403
        # M_FORBIDDEN rather than silently succeeding when the target user
        # is already a member -- the caller only cares about the end state
        # (a member), not whether this call is what achieved it.
        client = SynapseAdminClient(homeserver_url="https://matrix.example.org", access_token="tok")
        with mock.patch("matrix_room_media_retention.synapse_admin_client.requests.post") as post:
            post.return_value = _fake_response(
                403, text='{"errcode":"M_FORBIDDEN","error":"@bot:example.org is already in the room."}'
            )
            client.force_join_room(room_id="!room:example.org", user_id="@bot:example.org")  # must not raise

    def test_a_different_403_still_raises(self):
        # Only the specific "already in the room" 403 is tolerated -- any
        # other forbidden reason (e.g. a genuinely non-admin token) must
        # still surface as a real failure.
        client = SynapseAdminClient(homeserver_url="https://matrix.example.org", access_token="tok")
        with mock.patch("matrix_room_media_retention.synapse_admin_client.requests.post") as post:
            post.return_value = _fake_response(403, text='{"errcode":"M_FORBIDDEN","error":"You are not a server admin."}')
            with pytest.raises(SynapseAdminError):
                client.force_join_room(room_id="!room:example.org", user_id="@bot:example.org")

    def test_base_url_trailing_slash_stripped(self):
        client = SynapseAdminClient(homeserver_url="https://matrix.example.org/", access_token="tok")
        with mock.patch("matrix_room_media_retention.synapse_admin_client.requests.post") as post:
            post.return_value = _fake_response(200, {})
            client.force_join_room(room_id="!room:example.org", user_id="@bot:example.org")
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
