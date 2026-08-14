"""The Matrix bot half of the plugin: listens for `!media-retention` commands
in any room it has been invited to. Auto-joins on invite (an operator adds
the bot to a room the same way they'd invite any other user/bot -- no
bridge-specific installation step).
"""

from __future__ import annotations

import logging

import nio

from .authorization import is_authorized
from .config import Config
from .duration import InvalidDurationError, format_duration_seconds, parse_duration_seconds
from .policy_store import PolicyStore

logger = logging.getLogger(__name__)

_HELP_TEXT = (
    "Commands:\n"
    "!media-retention                  -- show current policy\n"
    "!media-retention retain <dur>     -- e.g. '30d', '6h', '2w' (min "
    "{minimum})\n"
    "!media-retention forever          -- keep everything (the default)\n"
    "!media-retention off              -- same as forever, explicit\n"
    "!media-retention help             -- this message"
)


class MediaRetentionBot:
    def __init__(self, *, config: Config, store: PolicyStore):
        self._config = config
        self._store = store
        self._client = nio.AsyncClient(config.homeserver_url, config.bot_user_id)
        self._client.add_event_callback(self._on_invite, nio.InviteEvent)
        self._client.add_event_callback(self._on_message, nio.RoomMessageText)

    async def login_and_sync_forever(self) -> None:
        login_response = await self._client.login(self._config.bot_password)
        if isinstance(login_response, nio.LoginError):
            raise RuntimeError(f"Failed to log in as {self._config.bot_user_id!r}: {login_response}")
        logger.info("Logged in as %s", self._config.bot_user_id)
        await self._client.sync_forever(timeout=30000, full_state=True)

    async def _on_invite(self, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
        # Auto-join is the whole "installation" step for this plugin in any
        # given room -- deliberately no allowlist/approval flow in v1,
        # matching this plugin's own minimal-v1 scope. An operator who only
        # wants it in specific rooms simply only invites it there.
        await self._client.join(room.room_id)
        logger.info("Joined room %s after invite", room.room_id)

    async def _on_message(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
        prefix = self._config.command_prefix
        body = (event.body or "").strip()
        if not (body == prefix or body.startswith(prefix + " ")):
            return

        args = body[len(prefix):].strip().split()
        reply = self._handle_command(room=room, sender=event.sender, args=args)
        await self._client.room_send(
            room_id=room.room_id,
            message_type="m.room.message",
            content={"msgtype": "m.notice", "body": reply},
        )

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
                    "users_default": power_levels.users_default,
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

        if args[0] == "retain":
            if len(args) != 2:
                return f"Usage: {self._config.command_prefix} retain <duration>, e.g. '30d' or '6h'."
            try:
                seconds = parse_duration_seconds(args[1], minimum_seconds=self._config.minimum_retain_seconds)
            except InvalidDurationError as exc:
                return str(exc)
            self._store.set_retain(room.room_id, seconds)
            return f"Retention policy set: media older than {format_duration_seconds(seconds)} will be purged (text/captions are never affected)."

        if args[0] in ("forever", "off"):
            self._store.set_forever(room.room_id)
            return "Retention policy set: media is kept forever (no automatic purging)."

        # Genuinely unrecognized input (e.g. a typo) -- surface that, and
        # point at `help`, rather than silently falling back to the same
        # reply as no-args-at-all and hiding the mistake (roadmap/041 §9).
        return (
            f"Unrecognized command {args[0]!r}. Try '{self._config.command_prefix} help' "
            "for the list of commands."
        )

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
