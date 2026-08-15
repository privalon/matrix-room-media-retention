"""The Matrix bot half of the plugin: listens for `!media-retention` commands
in any room it has been invited to. Auto-joins on invite (an operator adds
the bot to a room the same way they'd invite any other user/bot -- no
bridge-specific installation step).

Also handles a second, remote command surface (roadmap/041 §11): a trusted
sender can DM the bot `!media-retention <room_id> retain <dur>` (or
`forever`/`off`/no-args-to-view) to set a policy for a room the bot isn't a
member of at all, and `!media-retention list` to see every explicitly-
configured room's policy with its human-readable name. This needs the
bot's own account to be a Synapse server admin (see synapse_admin_client.py's
own docstring): both the room's power levels (to check the sender's
authorization) and its display name are read via Synapse's own admin API,
which needs no room membership at all -- no join/leave dance, no standing
presence in every room an operator wants a policy for.
"""

from __future__ import annotations

import logging

import nio

from .authorization import is_authorized
from .config import Config
from .duration import InvalidDurationError, format_duration_seconds, parse_duration_seconds
from .policy_store import PolicyStore
from .synapse_admin_client import SynapseAdminClient

logger = logging.getLogger(__name__)

_JOIN_GREETING = (
    "Hi, I'm the media retention bot. I manage this room's media retention "
    "policy -- media only, text and captions are never touched. Try "
    "'!media-retention help' for the full command list, or "
    "'!media-retention' to see the current policy."
)

# Recognized only as a friendly nudge in a DM-sized room whose message
# didn't match the real command prefix (see _on_message/_friendly_dm_reply)
# -- never in a real multi-member room, where treating every "hi" as
# directed at the bot would spam a room this bot was invited into purely
# to enforce a retention policy, not to chat.
_GREETING_WORDS = {"hi", "hello", "hey", "hiya", "howdy", "yo", "sup"}

_MATRIX_TO_PREFIX = "https://matrix.to/#/"

# DM-only shorthand for the full command_prefix, on the remote command
# surface specifically -- see _matched_prefix()'s own docstring for why
# this can never collide with a real `!room_id` argument.
_SHORT_REMOTE_PREFIX = "!"

_HELP_TEXT = (
    "Commands (in a room this bot has joined):\n"
    "!media-retention                  -- show current policy\n"
    "!media-retention retain <dur>     -- e.g. '30d', '6h', '2w' (min "
    "{minimum})\n"
    "!media-retention forever          -- keep everything (the default)\n"
    "!media-retention off              -- same as forever, explicit\n"
    "!media-retention help             -- this message\n"
    "\n"
    "Remote commands (DM this bot directly, trusted senders only) --\n"
    "<room> is a room ID (!opaque or !opaque:server) or a matrix.to link,\n"
    "e.g. https://matrix.to/#/!opaque:server?via=example.org:\n"
    "!media-retention <room>              -- show that room's policy\n"
    "!media-retention <room> retain <dur> -- set that room's policy\n"
    "!media-retention <room> forever      -- set that room's policy\n"
    "!media-retention list                -- every configured room + policy\n"
    "In a DM specifically, '!' alone also works as a shorthand for\n"
    "'!media-retention', e.g. '! <room> retain 30d'."
)


def _extract_room_id(token: str) -> str:
    """Unwraps a matrix.to link (e.g.
    `https://matrix.to/#/!room:server?via=example.org`, optionally with a
    trailing `/$event_id`) down to the bare room ID. A plain room ID
    passed in directly (with or without a `:server_name` suffix) is
    returned unchanged, since none of `https://matrix.to/#/`, `?`, or `/`
    ever appear in one."""
    if token.startswith(_MATRIX_TO_PREFIX):
        token = token[len(_MATRIX_TO_PREFIX):]
    token = token.split("?", 1)[0]
    token = token.split("/", 1)[0]
    return token


class MediaRetentionBot:
    def __init__(self, *, config: Config, store: PolicyStore):
        self._config = config
        self._store = store
        self._client = nio.AsyncClient(config.homeserver_url, config.bot_user_id)
        self._client.add_event_callback(self._on_invite, nio.InviteEvent)
        self._client.add_event_callback(self._on_message, nio.RoomMessageText)
        # Only usable once login_and_sync_forever() has actually logged in
        # (Synapse admin API calls reuse the bot's own regular access
        # token -- see synapse_admin_client.py's own docstring for why
        # there's no separate admin credential).
        self._synapse_admin: SynapseAdminClient | None = None

    async def login_and_sync_forever(self) -> None:
        login_response = await self._client.login(self._config.bot_password)
        if isinstance(login_response, nio.LoginError):
            raise RuntimeError(f"Failed to log in as {self._config.bot_user_id!r}: {login_response}")
        logger.info("Logged in as %s", self._config.bot_user_id)
        self._synapse_admin = SynapseAdminClient(
            homeserver_url=self._config.synapse_admin_url, access_token=login_response.access_token
        )
        await self._client.sync_forever(timeout=30000, full_state=True)

    async def _on_invite(self, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
        # Auto-join is the whole "installation" step for this plugin in any
        # given room -- deliberately no allowlist/approval flow in v1,
        # matching this plugin's own minimal-v1 scope. An operator who only
        # wants it in specific rooms simply only invites it there.
        await self._client.join(room.room_id)
        logger.info("Joined room %s after invite", room.room_id)
        # A single one-time message, not per-message noise -- tells anyone
        # in the room (DM or a real retention-target room alike) what this
        # bot is and how to reach it, without needing external docs.
        await self._client.room_send(
            room_id=room.room_id,
            message_type="m.room.message",
            content={"msgtype": "m.notice", "body": _JOIN_GREETING},
        )

    async def _on_message(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
        if event.sender == self._config.bot_user_id:
            return  # never react to its own messages
        body = (event.body or "").strip()
        matched_prefix = self._matched_prefix(room=room, body=body)
        if matched_prefix is None:
            reply = self._friendly_dm_reply(room=room, body=body)
            if reply is not None:
                await self._client.room_send(
                    room_id=room.room_id,
                    message_type="m.room.message",
                    content={"msgtype": "m.notice", "body": reply},
                )
            return

        args = body[len(matched_prefix):].strip().split()
        if args and args[0] == "list":
            reply = await self._handle_list_command(sender=event.sender)
        elif args and (args[0].startswith("!") or args[0].startswith(_MATRIX_TO_PREFIX)):
            # The `!` sigil is the one part of Matrix's room ID grammar
            # guaranteed not to change across room versions -- confirmed
            # live 2026-08-15 against this exact homeserver that the
            # `:server_name` suffix classic room IDs have is NOT present
            # on this homeserver's own newly-created rooms (a newer room
            # version's own ID format), so checking for `:` too would
            # have made every real remote command silently fall through
            # to "unrecognized". `!` alone is unambiguous against every
            # real subcommand keyword (none of retain/forever/off/help/
            # list start with it), so no `:` check is needed anyway. A
            # matrix.to link is recognized the same way and unwrapped to
            # the bare room ID before routing.
            normalized_args = [_extract_room_id(args[0]), *args[1:]]
            reply = await self._handle_remote_command(sender=event.sender, args=normalized_args)
        else:
            reply = self._handle_command(room=room, sender=event.sender, args=args)
        await self._client.room_send(
            room_id=room.room_id,
            message_type="m.room.message",
            content={"msgtype": "m.notice", "body": reply},
        )

    def _matched_prefix(self, *, room: nio.MatrixRoom, body: str) -> str | None:
        """Returns the exact prefix string `body` used (so the caller
        strips precisely that many characters), or None if it matches
        neither. The real `command_prefix` always works; a bare "!" is
        also accepted, but only in a DM-sized room (bot plus at most one
        other member -- same detection as _friendly_dm_reply), as a
        shorthand for the remote command surface specifically. This can
        never collide with a real `!room_id` argument (e.g. "!media
        -retention !roomid:server retain 30d" typed as just
        "!roomid:server retain 30d"): a bare room ID is never followed by
        a space right after its own leading "!", so it never matches
        `_SHORT_REMOTE_PREFIX + " "` in the first place."""
        prefix = self._config.command_prefix
        if body == prefix or body.startswith(prefix + " "):
            return prefix
        if room.member_count <= 2 and (
            body == _SHORT_REMOTE_PREFIX or body.startswith(_SHORT_REMOTE_PREFIX + " ")
        ):
            return _SHORT_REMOTE_PREFIX
        return None

    def _handle_command(self, *, room: nio.MatrixRoom, sender: str, args: list[str]) -> str:
        if not args:
            return self._status_text(room.room_id)

        if args[0] == "help":
            return _HELP_TEXT.format(minimum=format_duration_seconds(self._config.minimum_retain_seconds))

        mutating_subcommands = {"retain", "forever", "off"}
        if args[0] in mutating_subcommands:
            power_levels = room.power_levels
            authorized = is_authorized(
                power_levels_content={
                    "users": power_levels.users,
                    # nio's own PowerLevels dataclass nests this under
                    # `.defaults` (a DefaultLevels object), not a flat
                    # `.users_default` attribute -- confirmed directly
                    # against nio 0.26.0's own source (nio/rooms.py).
                    "users_default": power_levels.defaults.users_default,
                },
                sender=sender,
                minimum_level=self._config.minimum_power_level,
            )
            if not authorized:
                return (
                    f"Sorry, changing this room's retention policy needs a power level of "
                    f"at least {self._config.minimum_power_level} (you can still view it with "
                    f"'{self._config.command_prefix}')."
                )
            return self._apply_mutating_subcommand(room_id=room.room_id, args=args)

        # Genuinely unrecognized input (e.g. a typo) -- surface that, and
        # point at `help`, rather than silently falling back to the same
        # reply as no-args-at-all and hiding the mistake (roadmap/041 §9).
        return (
            f"Unrecognized command {args[0]!r}. Try '{self._config.command_prefix} help' "
            "for the list of commands."
        )

    def _friendly_dm_reply(self, *, room: nio.MatrixRoom, body: str) -> str | None:
        """Called only for a message that didn't match the real command
        prefix. Deliberately scoped to DM-sized rooms (bot + at most one
        other member) -- a real multi-member room (native or bridged) this
        bot was invited into purely to enforce a retention policy must
        never get a reply to ordinary conversation, or every "hi" in that
        room would spam it. A bare greeting or "help" sent straight to the
        bot with no prefix is a very reasonable first thing for someone to
        try (found live 2026-08-15: neither got any reply at all), so
        answer them instead of silently doing nothing."""
        if room.member_count > 2:
            return None
        word = body.strip().lower().rstrip("!.?")
        if word == "help":
            return _HELP_TEXT.format(minimum=format_duration_seconds(self._config.minimum_retain_seconds))
        if word in _GREETING_WORDS:
            return (
                "Hi! I'm the media retention bot -- I manage per-room media "
                f"purging policies. Try '{self._config.command_prefix} help' "
                "to see what I can do."
            )
        return None

    async def _handle_remote_command(self, *, sender: str, args: list[str]) -> str:
        # Separate, sender-identity allowlist -- deliberately independent
        # of whatever power level `sender` might hold in the target room:
        # restricts *who can even attempt* a remote, room-id-targeted
        # command at all, not just what they can do once attempted.
        if sender not in self._config.trusted_remote_admin_user_ids:
            return "Sorry, remote room-targeted commands are only available to this bot's configured trusted admins."

        room_id = args[0]
        sub_args = args[1:]
        if not sub_args:
            return self._status_text(room_id)
        if sub_args[0] == "help":
            return _HELP_TEXT.format(minimum=format_duration_seconds(self._config.minimum_retain_seconds))

        mutating_subcommands = {"retain", "forever", "off"}
        if sub_args[0] not in mutating_subcommands:
            return (
                f"Unrecognized command {sub_args[0]!r}. Try '{self._config.command_prefix} help' "
                "for the list of commands."
            )

        # No room-power-level check here (unlike _handle_command's in-room
        # path) -- found live 2026-08-15: mautrix-whatsapp's own bridged
        # portal rooms commonly grant every member power level 0 with no
        # elevated user at all (`"users": {}`, `"users_default": 0` --
        # confirmed directly against two real portal rooms), which made
        # the real room owner unable to ever pass a >=50 check there. The
        # `trusted_remote_admin_user_ids` allowlist just above is already a
        # stricter, identity-based gate than a room-specific power level
        # (that's the whole point of this remote surface -- "manage every
        # room's policy from one place" implies the target room's own
        # internal power structure shouldn't matter), so re-checking room
        # power level on top of it was redundant for every native room and
        # actively broken for a whole class of bridged ones.
        assert self._synapse_admin is not None  # only called after login_and_sync_forever()
        if self._synapse_admin.get_room_power_levels(room_id) is None:
            return f"Could not read room {room_id} -- does it exist?"
        return self._apply_mutating_subcommand(room_id=room_id, args=sub_args)

    async def _handle_list_command(self, *, sender: str) -> str:
        if sender not in self._config.trusted_remote_admin_user_ids:
            return "Sorry, listing configured policies is only available to this bot's configured trusted admins."
        policies = self._store.list_all_policies()
        if not policies:
            return "No rooms have an explicit retention policy configured."

        assert self._synapse_admin is not None
        lines = []
        for policy in policies:
            # A missing/unreachable name is never worth failing the whole
            # reply over -- falls back to just the room ID.
            name = self._synapse_admin.get_room_name(policy.room_id) or "(no name)"
            if policy.policy == "forever":
                lines.append(f"{policy.room_id} ({name}): forever")
            else:
                lines.append(f"{policy.room_id} ({name}): retain {format_duration_seconds(policy.retain_seconds)}")
        return "Configured retention policies:\n" + "\n".join(lines)

    def _apply_mutating_subcommand(self, *, room_id: str, args: list[str]) -> str:
        """Shared by both the in-room and remote command paths, once
        authorization for `room_id` has already been checked by the
        caller -- `args[0]` is one of retain/forever/off."""
        if args[0] == "retain":
            if len(args) != 2:
                return f"Usage: {self._config.command_prefix} retain <duration>, e.g. '30d' or '6h'."
            try:
                seconds = parse_duration_seconds(args[1], minimum_seconds=self._config.minimum_retain_seconds)
            except InvalidDurationError as exc:
                return str(exc)
            self._store.set_retain(room_id, seconds)
            return f"Retention policy set: media older than {format_duration_seconds(seconds)} will be purged (text/captions are never affected)."

        # args[0] in ("forever", "off")
        self._store.set_forever(room_id)
        return "Retention policy set: media is kept forever (no automatic purging)."

    def _status_text(self, room_id: str) -> str:
        policy = self._store.get(room_id)
        if policy is None or policy.policy == "forever":
            return "Current policy: forever (no automatic purging)."
        last_purge = (
            f"; last purge removed {policy.last_purge_count} object(s)"
            if policy.last_purge_count is not None
            else "; no purge has run yet"
        )
        return (
            f"Current policy: retain {format_duration_seconds(policy.retain_seconds)} "
            f"(media older than this is purged; text/captions are never affected){last_purge}."
        )
