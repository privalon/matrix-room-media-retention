"""Tests _handle_command() directly -- the pure decision logic behind
`!media-retention ...` -- without needing a real Matrix connection. bot.py's
own login_and_sync_forever()/_on_invite() are thin nio-wiring not covered
here; they're exercised by the compatibility/live-verification pass instead
(see README.md's testing section)."""

import asyncio
import tempfile
from pathlib import Path
from unittest import mock

import nio
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


def _make_bot(store, *, minimum_retain_seconds=0, trusted_remote_admin_user_ids=None):
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
        trusted_remote_admin_user_ids=trusted_remote_admin_user_ids or [],
    )
    with mock.patch("matrix_room_media_retention.bot.nio.AsyncClient"):
        bot = MediaRetentionBot(config=config, store=store)
    # login_and_sync_forever() normally constructs this after a real login
    # -- tests exercise _handle_remote_command()/_handle_list_command()
    # directly, so it needs to already exist, with its own HTTP calls
    # mocked per-test.
    bot._synapse_admin = mock.Mock()
    return bot


@pytest.fixture
def bot(store):
    return _make_bot(store)


def _fake_room(room_id="!room:example.org", *, moderator_users=None, member_count=10):
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
    # Defaults to a real multi-member room (10) -- tests exercising the
    # friendly-DM-reply path override this to <=2 explicitly, matching a
    # real bot+one-other-person DM.
    room.member_count = member_count
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


ADMIN = "@admin:example.org"


class TestRemoteCommands:
    """docs/roadmap/041 §11: `!media-retention <room_id> ...`, DM'd
    directly to the bot -- for a room it isn't a member of at all. Reads
    the target room's power levels via Synapse's own admin API (no join
    needed at all -- confirmed live 2026-08-15 that Synapse's admin JOIN
    api refuses a room the calling account has no prior relationship to,
    exactly the case a never-before-seen remote target room is)."""

    def test_untrusted_sender_is_rejected_before_any_matrix_call(self, store):
        bot = _make_bot(store, trusted_remote_admin_user_ids=[ADMIN])
        reply = asyncio.run(
            bot._handle_remote_command(sender="@rando:example.org", args=["!room:example.org", "retain", "30d"])
        )
        assert "trusted admin" in reply.lower()
        bot._synapse_admin.get_room_power_levels.assert_not_called()
        assert store.get("!room:example.org") is None

    def test_viewing_a_remote_rooms_status_needs_no_power_level_lookup(self, store):
        # Viewing was never gated by power level in-room either; remote
        # viewing is gated only by the sender allowlist, checked above.
        bot = _make_bot(store, trusted_remote_admin_user_ids=[ADMIN])
        reply = asyncio.run(bot._handle_remote_command(sender=ADMIN, args=["!room:example.org"]))
        assert "forever" in reply.lower()
        bot._synapse_admin.get_room_power_levels.assert_not_called()

    def test_retain_by_a_room_moderator_succeeds(self, store):
        bot = _make_bot(store, trusted_remote_admin_user_ids=[ADMIN])
        bot._synapse_admin.get_room_power_levels.return_value = {"users": {ADMIN: 50}, "users_default": 0}

        reply = asyncio.run(bot._handle_remote_command(sender=ADMIN, args=["!room:example.org", "retain", "30d"]))

        assert "30d" in reply
        assert store.get("!room:example.org").retain_seconds == 30 * 86400
        bot._synapse_admin.get_room_power_levels.assert_called_once_with("!room:example.org")

    def test_retain_by_a_non_moderator_is_rejected(self, store):
        bot = _make_bot(store, trusted_remote_admin_user_ids=[ADMIN])
        bot._synapse_admin.get_room_power_levels.return_value = {"users": {}, "users_default": 0}

        reply = asyncio.run(bot._handle_remote_command(sender=ADMIN, args=["!room:example.org", "retain", "30d"]))

        assert "power level" in reply.lower()
        assert store.get("!room:example.org") is None

    def test_unreadable_power_levels_is_reported_not_silently_authorized(self, store):
        # A missing/unreadable room must never be treated as "authorized
        # by default" -- fail closed.
        bot = _make_bot(store, trusted_remote_admin_user_ids=[ADMIN])
        bot._synapse_admin.get_room_power_levels.return_value = None

        reply = asyncio.run(bot._handle_remote_command(sender=ADMIN, args=["!room:example.org", "retain", "30d"]))

        assert "could not read power levels" in reply.lower()
        assert store.get("!room:example.org") is None

    def test_unrecognized_remote_subcommand_says_so(self, store):
        bot = _make_bot(store, trusted_remote_admin_user_ids=[ADMIN])
        reply = asyncio.run(bot._handle_remote_command(sender=ADMIN, args=["!room:example.org", "retian", "30d"]))
        assert "unrecognized" in reply.lower()
        bot._synapse_admin.get_room_power_levels.assert_not_called()

    def test_no_trusted_admins_configured_means_no_one_can_use_remote_commands(self, store):
        bot = _make_bot(store)  # trusted_remote_admin_user_ids defaults to []
        reply = asyncio.run(bot._handle_remote_command(sender=ADMIN, args=["!room:example.org", "retain", "30d"]))
        assert "trusted admin" in reply.lower()


class TestListCommand:
    """docs/roadmap/041 §11: `!media-retention list`."""

    def test_untrusted_sender_is_rejected(self, store):
        bot = _make_bot(store, trusted_remote_admin_user_ids=[ADMIN])
        reply = asyncio.run(bot._handle_list_command(sender="@rando:example.org"))
        assert "trusted admin" in reply.lower()

    def test_no_policies_configured(self, store):
        bot = _make_bot(store, trusted_remote_admin_user_ids=[ADMIN])
        reply = asyncio.run(bot._handle_list_command(sender=ADMIN))
        assert "no rooms" in reply.lower()

    def test_lists_every_configured_room_with_its_readable_name(self, store):
        store.set_retain("!a:example.org", 30 * 86400)
        store.set_forever("!b:example.org")
        bot = _make_bot(store, trusted_remote_admin_user_ids=[ADMIN])
        bot._synapse_admin.get_room_name.side_effect = lambda room_id: {
            "!a:example.org": "Project Chat",
            "!b:example.org": "Announcements",
        }[room_id]

        reply = asyncio.run(bot._handle_list_command(sender=ADMIN))

        assert "!a:example.org" in reply and "Project Chat" in reply and "30d" in reply
        assert "!b:example.org" in reply and "Announcements" in reply and "forever" in reply

    def test_falls_back_to_room_id_when_name_lookup_fails(self, store):
        store.set_retain("!a:example.org", 86400)
        bot = _make_bot(store, trusted_remote_admin_user_ids=[ADMIN])
        bot._synapse_admin.get_room_name.return_value = None

        reply = asyncio.run(bot._handle_list_command(sender=ADMIN))

        assert "!a:example.org" in reply
        assert "(no name)" in reply


class TestFriendlyDmReply:
    """Found live 2026-08-15: a bare "hi" or "help" (no `!media-retention`
    prefix) sent straight to the bot got no reply at all. Scoped to
    DM-sized rooms only -- a real multi-member retention-target room must
    never get a reply to ordinary conversation."""

    def test_greeting_in_a_dm_gets_a_friendly_reply(self, bot):
        room = _fake_room(member_count=2)
        reply = bot._friendly_dm_reply(room=room, body="hi")
        assert reply is not None
        assert "media retention" in reply.lower()
        assert bot._config.command_prefix in reply

    def test_bare_help_in_a_dm_shows_the_real_help_text(self, bot):
        room = _fake_room(member_count=2)
        reply = bot._friendly_dm_reply(room=room, body="help")
        for subcommand in ("retain", "forever", "off"):
            assert subcommand in reply

    def test_greeting_is_case_and_punctuation_insensitive(self, bot):
        room = _fake_room(member_count=2)
        assert bot._friendly_dm_reply(room=room, body="Hi!") is not None
        assert bot._friendly_dm_reply(room=room, body="HELLO") is not None

    def test_unrelated_text_in_a_dm_gets_no_reply(self, bot):
        room = _fake_room(member_count=2)
        assert bot._friendly_dm_reply(room=room, body="what's the weather like") is None

    def test_greeting_in_a_real_multi_member_room_gets_no_reply(self, bot):
        # The whole point of scoping this to DMs: a bridged/native room
        # this bot was invited into for policy enforcement must not start
        # replying to ordinary chat.
        room = _fake_room(member_count=10)
        assert bot._friendly_dm_reply(room=room, body="hi") is None
        assert bot._friendly_dm_reply(room=room, body="help") is None


def _fake_message_event(body, sender="@anyone:example.org"):
    event = mock.Mock(spec=nio.RoomMessageText)
    event.body = body
    event.sender = sender
    return event


class TestOnMessageRouting:
    """Exercises _on_message()'s own dispatch decision directly -- found
    live 2026-08-15 that this had zero test coverage at all, which is
    exactly how a real routing bug slipped through: room IDs on some
    homeservers (a newer room version's own ID format, confirmed live)
    have no `:server_name` suffix at all, unlike the classic
    `!opaque:server_name` shape -- a first version of the room-id
    detection here required a `:` and silently misrouted every remote
    command to "unrecognized" as a result."""

    def test_room_id_without_a_colon_suffix_still_routes_to_remote_handler(self, store):
        # The exact shape of the real bug: this homeserver's own room IDs
        # have no `:server_name` suffix at all.
        bot = _make_bot(store, trusted_remote_admin_user_ids=[ADMIN])
        bot._synapse_admin.get_room_power_levels.return_value = {"users": {ADMIN: 50}, "users_default": 0}
        bot._client.room_send = mock.AsyncMock()
        room = _fake_room("!anyroom:example.org")
        event = _fake_message_event("!media-retention !GaW2PwvaKrqplAmaq2buq8msVhLDeBU8Cnvqb5kmzMc retain 30d", sender=ADMIN)

        asyncio.run(bot._on_message(room, event))

        assert store.get("!GaW2PwvaKrqplAmaq2buq8msVhLDeBU8Cnvqb5kmzMc").retain_seconds == 30 * 86400
        bot._synapse_admin.get_room_power_levels.assert_called_once()

    def test_classic_room_id_with_a_colon_suffix_also_routes_to_remote_handler(self, store):
        bot = _make_bot(store, trusted_remote_admin_user_ids=[ADMIN])
        bot._synapse_admin.get_room_power_levels.return_value = {"users": {ADMIN: 50}, "users_default": 0}
        bot._client.room_send = mock.AsyncMock()
        room = _fake_room("!anyroom:example.org")
        event = _fake_message_event("!media-retention !target:example.org retain 30d", sender=ADMIN)

        asyncio.run(bot._on_message(room, event))

        assert store.get("!target:example.org").retain_seconds == 30 * 86400

    def test_list_routes_to_the_list_handler_not_the_in_room_handler(self, store):
        store.set_retain("!a:example.org", 86400)
        bot = _make_bot(store, trusted_remote_admin_user_ids=[ADMIN])
        bot._synapse_admin.get_room_name.return_value = "Some Room"
        bot._client.room_send = mock.AsyncMock()
        room = _fake_room("!anyroom:example.org")
        event = _fake_message_event("!media-retention list", sender=ADMIN)

        asyncio.run(bot._on_message(room, event))

        sent_body = bot._client.room_send.call_args.kwargs["content"]["body"]
        assert "!a:example.org" in sent_body
        assert "Some Room" in sent_body

    def test_a_plain_subcommand_still_routes_to_the_in_room_handler(self, store):
        bot = _make_bot(store, trusted_remote_admin_user_ids=[ADMIN])
        bot._client.room_send = mock.AsyncMock()
        room = _fake_room("!anyroom:example.org", moderator_users={ADMIN: 50})
        event = _fake_message_event("!media-retention retain 30d", sender=ADMIN)

        asyncio.run(bot._on_message(room, event))

        assert store.get("!anyroom:example.org").retain_seconds == 30 * 86400
        bot._synapse_admin.get_room_power_levels.assert_not_called()

    def test_unprefixed_greeting_in_a_dm_gets_a_reply_via_on_message(self, store):
        bot = _make_bot(store)
        bot._client.room_send = mock.AsyncMock()
        room = _fake_room("!dm:example.org", member_count=2)
        event = _fake_message_event("hi", sender="@someone:example.org")

        asyncio.run(bot._on_message(room, event))

        bot._client.room_send.assert_called_once()
        assert "media retention" in bot._client.room_send.call_args.kwargs["content"]["body"].lower()

    def test_unprefixed_greeting_in_a_group_room_gets_no_reply_via_on_message(self, store):
        bot = _make_bot(store)
        bot._client.room_send = mock.AsyncMock()
        room = _fake_room("!group:example.org", member_count=10)
        event = _fake_message_event("hi", sender="@someone:example.org")

        asyncio.run(bot._on_message(room, event))

        bot._client.room_send.assert_not_called()

    def test_bot_never_reacts_to_its_own_messages(self, store):
        bot = _make_bot(store)
        bot._client.room_send = mock.AsyncMock()
        room = _fake_room("!dm:example.org", member_count=2)
        event = _fake_message_event("hi", sender=bot._config.bot_user_id)

        asyncio.run(bot._on_message(room, event))

        bot._client.room_send.assert_not_called()
