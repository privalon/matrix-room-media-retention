import tempfile
from pathlib import Path

import pytest

from matrix_room_media_retention.config import Config


def _write_config(tmp_path, **overrides):
    base = {
        "homeserver_url": "https://matrix.example.org",
        "bot_user_id": "@bot:example.org",
        "bot_password": "pw",
        "media_repo_admin_access_token": "tok",
    }
    base.update(overrides)
    lines = []
    for key, value in base.items():
        if isinstance(value, list):
            lines.append(f"{key}: {value!r}")
        else:
            lines.append(f'{key}: "{value}"' if isinstance(value, str) else f"{key}: {value}")
    path = tmp_path / "config.yaml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class TestSynapseAdminUrl:
    def test_defaults_to_homeserver_url_when_unset(self, tmp_path):
        config = Config.load(_write_config(tmp_path))
        assert config.synapse_admin_url == "https://matrix.example.org"

    def test_uses_explicit_value_when_set(self, tmp_path):
        # roadmap/041 §11: found live that a reverse-proxy restriction on
        # the admin API also blocks this bot's own calls through the
        # public homeserver_url -- this must be overridable independently.
        config = Config.load(_write_config(tmp_path, synapse_admin_url="http://matrix-synapse:8008"))
        assert config.synapse_admin_url == "http://matrix-synapse:8008"


class TestRequiredFields:
    def test_missing_required_field_raises(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text('bot_user_id: "@bot:example.org"\n', encoding="utf-8")
        with pytest.raises(ValueError):
            Config.load(path)
