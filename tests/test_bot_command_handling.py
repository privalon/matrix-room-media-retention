"""Tests _handle_command() directly -- the pure decision logic behind
`!media-retention ...` -- without needing a real Matrix connection. bot.py's
own login_and_sync_forever()/_on_invite() are thin nio-wiring not covered
here; they're exercised by the compatibility/live-verification pass instead
(see README.md's testing section)."""

import tempfile
from pathlib import Path
from unittest import mock

import pytest

from matrix_room_media_retention.bot import MediaRetentionBot
from matrix_room_media_retention.config import Config
from matrix_room_media_retention.policy_store import PolicyStore


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = PolicyStore(Path(tmp) / "test.sqlite3")
        yield s
        s.close()


def _make_bot(store, *, minimum_retain_seconds=0):
    # minimum_retain_seconds=0 by default here so existing tests using
    # sub-day durations ("6h" etc.) keep exercising the rest of the command
    # logic without also being about the floor -- TestMinimumRetainFloor
    # below tests the floor itself with a real (non-zero) value.
    config = Config(
        homeserver_url="https://matrix.example.org",
        bot_user_id="@retention-bot:example.org",
        bot_password="unused",
        media_repo_url="https://matrix.example.org",
        media_repo_admin_access_token="unused",
        db_path=":memory:",
        scheduler_interval_seconds=3600,
        minimum_power_level=50,
        command_prefix="!media-retention",
        minimum_retain_seconds=minimum_retain_seconds,
        dry_run=False,
    )
    with mock.patch("matrix_room_media_retention.bot.nio.AsyncClient"):
        return MediaRetentionBot(config=config, store=store)


@pytest.fixture
def bot(store):
    return _make_bot(store)


def _fake_room(room_id="!room:example.org", *, moderator_users=None):
    # Shaped to match nio's real PowerLevels dataclass (nio/rooms.py) --
    # `users_default` nests under `.defaults` (a DefaultLevels object), not
    # a flat attribute. A prior version of this fixture used a flat mock
    # attribute here, which masked a real bot.py bug (AttributeError,
    # found live 2026-08-15) that no test caught until then.
    room = mock.Mock()
    room.room_id = room_id
    power_levels = mock.Mock()
    power_levels.users = moderator_users or {}
    power_levels.defaults = mock.Mock()
    power_levels.defaults.users_default = 0
    room.power_levels = power_levels
    return room


class TestHandleCommand:
    def test_no_args_shows_forever_by_default(self, bot):
        reply = bot._handle_command(room=_fake_room(), sender="@anyone:example.org", args=[])
        assert "forever" in reply

    def test_retain_by_moderator_succeeds(self, bot, store):
        room = _fake_room(moderator_users={"@mod:example.org": 50})
        reply = bot._handle_command(room=room, sender="@mod:example.org", args=["retain", "30d"])
        assert "30d" in reply
        assert store.get(room.room_id).retain_seconds == 30 * 86400

    def test_retain_by_non_moderator_rejected(self, bot, store):
        room = _fake_room(moderator_users={})
        reply = bot._handle_command(room=room, sender="@rando:example.org", args=["retain", "30d"])
        assert "power level" in reply.lower()
        assert store.get(room.room_id) is None

    def test_forever_by_moderator_succeeds(self, bot, store):
        room = _fake_room(moderator_users={"@mod:example.org": 50})
        store.set_retain(room.room_id, 86400)
        reply = bot._handle_command(room=room, sender="@mod:example.org", args=["forever"])
        assert "forever" in reply.lower()
        assert store.get(room.room_id).policy == "forever"

    def test_off_is_an_alias_for_forever(self, bot, store):
        room = _fake_room(moderator_users={"@mod:example.org": 50})
        bot._handle_command(room=room, sender="@mod:example.org", args=["off"])
        assert store.get(room.room_id).policy == "forever"

    def test_status_after_retain_shows_current_policy(self, bot):
        room = _fake_room(moderator_users={"@mod:example.org": 50})
        bot._handle_command(room=room, sender="@mod:example.org", args=["retain", "6h"])
        reply = bot._handle_command(room=room, sender="@anyone:example.org", args=[])
        assert "6h" in reply
        assert "no purge has run yet" in reply

    def test_invalid_duration_reports_error_without_changing_policy(self, bot, store):
        room = _fake_room(moderator_users={"@mod:example.org": 50})
        reply = bot._handle_command(room=room, sender="@mod:example.org", args=["retain", "notaduration"])
        assert "could not parse" in reply.lower()
        assert store.get(room.room_id) is None

    def test_viewing_status_needs_no_power_level(self, bot):
        # A regular room member (not moderator) can still view the policy --
        # only *changing* it is gated.
        room = _fake_room(moderator_users={})
        reply = bot._handle_command(room=room, sender="@rando:example.org", args=[])
        assert "power level" not in reply.lower()

    def test_help_lists_the_real_subcommands(self, bot):
        reply = bot._handle_command(room=_fake_room(), sender="@anyone:example.org", args=["help"])
        for subcommand in ("retain", "forever", "off", "help"):
            assert subcommand in reply

    def test_help_needs_no_power_level(self, bot):
        # Same reasoning as viewing status -- help is read-only.
        room = _fake_room(moderator_users={})
        reply = bot._handle_command(room=room, sender="@rando:example.org", args=["help"])
        assert "power level" not in reply.lower()

    def test_unrecognized_subcommand_says_so_instead_of_silently_showing_status(self, bot):
        # roadmap/041 §9: a genuine typo must not be silently treated the
        # same as no-args-at-all (the status reply) -- that would hide the
        # mistake from the sender instead of surfacing it.
        room = _fake_room(moderator_users={"@mod:example.org": 50})
        reply = bot._handle_command(room=room, sender="@mod:example.org", args=["retian", "30d"])
        assert "unrecognized" in reply.lower()
        assert "help" in reply.lower()


class TestMinimumRetainFloor:
    """roadmap/041 §9: a duration below the configured floor is rejected at
    the bot layer too (not just duration.py's own unit tests), with the
    room's policy left unchanged."""

    def test_below_floor_rejected_without_changing_policy(self, store):
        bot = _make_bot(store, minimum_retain_seconds=86400)
        room = _fake_room(moderator_users={"@mod:example.org": 50})
        reply = bot._handle_command(room=room, sender="@mod:example.org", args=["retain", "30m"])
        assert "minimum" in reply.lower()
        assert store.get(room.room_id) is None

    def test_at_or_above_floor_accepted(self, store):
        bot = _make_bot(store, minimum_retain_seconds=86400)
        room = _fake_room(moderator_users={"@mod:example.org": 50})
        reply = bot._handle_command(room=room, sender="@mod:example.org", args=["retain", "1d"])
        assert "1d" in reply
        assert store.get(room.room_id).retain_seconds == 86400
