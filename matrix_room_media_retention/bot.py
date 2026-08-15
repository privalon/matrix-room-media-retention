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
own docstring) -- it force-joins the target room just long enough to read
its real `power_levels` state and check the sender's authorization there,
then leaves again, rather than requiring a standing invite into every room
an operator wants a policy for.
"""

from __future__ import annotations

import logging

import nio

from .authorization import is_authorized
from .config import Config
from .duration import InvalidDurationError, format_duration_seconds, parse_duration_seconds
from .policy_store import PolicyStore
from .synapse_admin_client import SynapseAdminClient, SynapseAdminError

logger = logging.getLogger(__name__)

_HELP_TEXT = (
    "Commands (in a room this bot has joined):\n"
    "!media-retention                  -- show current policy\n"
    "!media-retention retain <dur>     -- e.g. '30d', '6h', '2w' (min "
    "{minimum})\n"
    "!media-retention forever          -- keep everything (the default)\n"
    "!media-retention off              -- same as forever, explicit\n"
    "!media-retention help             -- this message\n"
    "\n"
    "Remote commands (DM this bot directly, trusted senders only):\n"
    "!media-retention <room_id>              -- show that room's policy\n"
    "!media-retention <room_id> retain <dur> -- set that room's policy\n"
    "!media-retention <room_id> forever      -- set that room's policy\n"
    "!media-retention list                   -- every configured room + policy"
)


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
            homeserver_url=self._config.homeserver_url, access_token=login_response.access_token
        )
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
        if args and args[0] == "list":
            reply = await self._handle_list_command(sender=event.sender)
        elif args and args[0].startswith("!") and ":" in args[0]:
            # A Matrix room ID always starts with `!` and contains `:`
            # (server_name) -- unambiguous against every real subcommand
            # keyword, so no separate "is this a DM" detection is needed;
            # a room-id-shaped first argument always means "remote,
            # room-id-targeted command", regardless of which room (a DM or
            # otherwise) the message itself arrived in.
            reply = await self._handle_remote_command(sender=event.sender, args=args)
        else:
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

        assert self._synapse_admin is not None  # only called after login_and_sync_forever()
        try:
            self._synapse_admin.force_join_room(room_id)
        except SynapseAdminError as exc:
            return f"Could not access room {room_id}: {exc}"
        try:
            state = await self._client.room_get_state_event(room_id, "m.room.power_levels")
            if isinstance(state, nio.RoomGetStateEventError):
                return f"Could not read power levels for room {room_id}: {state}"
            authorized = is_authorized(
                power_levels_content=state.content,
                sender=sender,
                minimum_level=self._config.minimum_power_level,
            )
        finally:
            # Transient membership only, for the duration of this one
            # authorization check -- not a standing presence in every room
            # an operator manages remotely (the whole point of this
            # command surface, see this module's own docstring).
            await self._client.room_leave(room_id)

        if not authorized:
            return (
                f"Sorry, changing retention policy for {room_id} needs a power level of at least "
                f"{self._config.minimum_power_level} in that room."
            )
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
