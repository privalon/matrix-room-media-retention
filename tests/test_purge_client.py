from unittest import mock

import pytest

from matrix_room_media_retention.purge_client import MediaRepoPurgeClient, PurgeError


def _fake_response(status_code, json_body=None, text=""):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    resp.text = text
    return resp


class TestMediaRepoPurgeClient:
    def test_purge_room_success(self):
        client = MediaRepoPurgeClient(base_url="https://media.example.org", admin_access_token="tok")
        with mock.patch("matrix_room_media_retention.purge_client.requests.post") as post:
            post.return_value = _fake_response(200, {"num_removed": 7})
            result = client.purge_room(room_id="!room:example.org", before_ts_ms=1234567890)

        assert result.room_id == "!room:example.org"
        assert result.num_removed == 7

        call = post.call_args
        assert call.args[0] == "https://media.example.org/_matrix/media/unstable/admin/purge/room/!room:example.org"
        assert call.kwargs["params"] == {"before_ts": 1234567890}
        assert call.kwargs["headers"]["Authorization"] == "Bearer tok"

    def test_purge_room_missing_num_removed_defaults_to_zero(self):
        client = MediaRepoPurgeClient(base_url="https://media.example.org", admin_access_token="tok")
        with mock.patch("matrix_room_media_retention.purge_client.requests.post") as post:
            post.return_value = _fake_response(200, {})
            result = client.purge_room(room_id="!room:example.org", before_ts_ms=1)
        assert result.num_removed == 0

    def test_purge_room_non_200_raises(self):
        client = MediaRepoPurgeClient(base_url="https://media.example.org", admin_access_token="tok")
        with mock.patch("matrix_room_media_retention.purge_client.requests.post") as post:
            post.return_value = _fake_response(403, text="Forbidden")
            with pytest.raises(PurgeError):
                client.purge_room(room_id="!room:example.org", before_ts_ms=1)

    def test_base_url_trailing_slash_stripped(self):
        client = MediaRepoPurgeClient(base_url="https://media.example.org/", admin_access_token="tok")
        with mock.patch("matrix_room_media_retention.purge_client.requests.post") as post:
            post.return_value = _fake_response(200, {"num_removed": 0})
            client.purge_room(room_id="!room:example.org", before_ts_ms=1)
        url = post.call_args.args[0]
        assert "//_matrix" not in url
