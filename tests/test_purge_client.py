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


class TestGetOverallUsageBytes:
    def test_returns_the_total_from_raw_bytes(self):
        client = MediaRepoPurgeClient(base_url="https://media.example.org", admin_access_token="tok")
        with mock.patch("matrix_room_media_retention.purge_client.requests.get") as get:
            get.return_value = _fake_response(200, {"raw_bytes": {"total": 123456, "media": 100000, "thumbnails": 23456}})
            total = client.get_overall_usage_bytes(server_name="example.org")
        assert total == 123456
        call = get.call_args
        assert call.args[0] == "https://media.example.org/_matrix/media/unstable/admin/usage/example.org"
        assert call.kwargs["headers"]["Authorization"] == "Bearer tok"

    def test_returns_zero_on_non_200_rather_than_raising(self):
        client = MediaRepoPurgeClient(base_url="https://media.example.org", admin_access_token="tok")
        with mock.patch("matrix_room_media_retention.purge_client.requests.get") as get:
            get.return_value = _fake_response(500, text="oops")
            assert client.get_overall_usage_bytes(server_name="example.org") == 0


class TestGetUsageForMxcs:
    def test_returns_empty_dict_for_empty_list_without_making_a_request(self):
        client = MediaRepoPurgeClient(base_url="https://media.example.org", admin_access_token="tok")
        with mock.patch("matrix_room_media_retention.purge_client.requests.get") as get:
            result = client.get_usage_for_mxcs([])
        assert result == {}
        get.assert_not_called()

    def test_sends_one_repeated_mxc_param_per_uri(self):
        client = MediaRepoPurgeClient(base_url="https://media.example.org", admin_access_token="tok")
        body = {
            "mxc://example.org/aaa": {"size_bytes": 100, "created_ts": 1000},
            "mxc://example.org/bbb": {"size_bytes": 200, "created_ts": 2000},
        }
        with mock.patch("matrix_room_media_retention.purge_client.requests.get") as get:
            get.return_value = _fake_response(200, body)
            result = client.get_usage_for_mxcs(["mxc://example.org/aaa", "mxc://example.org/bbb"])
        assert result == body
        call = get.call_args
        assert call.kwargs["params"] == [("mxc", "mxc://example.org/aaa"), ("mxc", "mxc://example.org/bbb")]

    def test_returns_empty_dict_on_non_200_rather_than_raising(self):
        client = MediaRepoPurgeClient(base_url="https://media.example.org", admin_access_token="tok")
        with mock.patch("matrix_room_media_retention.purge_client.requests.get") as get:
            get.return_value = _fake_response(500, text="oops")
            assert client.get_usage_for_mxcs(["mxc://example.org/aaa"]) == {}
