# matrix-room-media-retention

A genuinely matrix-wide, per-room, time-based media retention policy bot.

Set `!media-retention retain 30d` in any room — a native Matrix room, or one
bridged from Telegram, WhatsApp, Signal, or anything else — and media
uploaded to that room older than 30 days gets automatically purged.
**Text, captions, and every other event stay exactly where they are** —
only the underlying media files are removed.

## Why this exists

Matrix/Synapse has no native way to combine "media only" (not the whole
message) with "per room" (not the whole server) retention. This plugin
doesn't try to reinvent that from scratch either — it's a thin scheduling
and policy layer on top of
[`matrix-media-repo`](https://github.com/t2bot/matrix-media-repo)'s own
existing room-scoped purge API
(`POST /_matrix/media/unstable/admin/purge/room/<room_id>?before_ts=...`).
No bridge is patched, forked, or has its container image replaced. If your
homeserver already runs `matrix-media-repo` as its media backend (a
supported component of `matrix-docker-ansible-deploy` and several other
Matrix deployment tools), this plugin is the only piece you need to add.

See [`docs/design.md`](docs/design.md) for the full rationale, including
what was checked before deciding to build this (Synapse's own
`media_retention` config and `m.room.retention`, and why neither combines
per-room scoping with media-only deletion).

## How it works

- **A dedicated Matrix bot account.** Invite it to any room you want a
  retention policy in — it auto-joins. It never joins a room on its own,
  and posts a one-time greeting explaining itself right after joining.
- **A friendly reply to a bare "hi"/"help" sent directly in a DM** (no
  `!media-retention` prefix needed there) — but only in a DM-sized room
  (the bot plus at most one other person). A real multi-member room this
  bot was invited into purely to enforce a retention policy never gets a
  reply to ordinary conversation, only to the real command syntax below.
- **Commands, usable by anyone with at least moderator power level in that
  room** (checked against the room's own `m.room.power_levels`, not any
  bridge-specific permission system — an operator/admin with a higher
  power level in a room can use these exactly the same way, no separate
  admin-only path needed):

  ```
  !media-retention                  -- show current policy
  !media-retention retain 30d       -- purge media older than 30 days
  !media-retention retain 6h        -- ... or 6 hours, or 2w (subject to
                                        minimum_retain_seconds, see below)
  !media-retention forever          -- keep everything (the default)
  !media-retention off              -- same as forever, explicit
  !media-retention help             -- list these commands
  ```

  Viewing the current policy and `help` need no power level; only
  `retain`/`forever`/`off` are gated.

- **A background scheduler** (hourly by default) that, for every room with
  an explicit `retain` policy, calls `matrix-media-repo`'s own purge API
  with the right `before_ts`. Rooms left at `forever` (or never configured)
  are never queried.
- **A safety floor** on how short a `retain` duration can be
  (`minimum_retain_seconds`, default 1 day) — closes a real footgun where a
  typo like `retain 30m` (meaning `retain 30d`) would otherwise purge
  nearly everything in the room on the very next scheduler tick.
- **An audit log** of every scheduler pass's outcome per room (room, when,
  how many objects removed, whether it was a dry run) — a durable record
  separate from the bot's own "current status" reply, which only shows the
  most recent purge.
- **A dry-run mode** (`dry_run: true` in config): the scheduler still
  computes what each `retain`-policy room would purge and records it to
  the audit log, but never calls the real purge endpoint. Use this to
  validate a newly-set policy before trusting it with real deletions.
- **An optional remote command surface** for trusted senders
  (`trusted_remote_admin_user_ids` in config, empty/disabled by default):
  DM the bot directly, no invite into the target room required —

  ```
  !media-retention !roomid:example.org                -- show that room's policy
  !media-retention !roomid:example.org retain 30d      -- set that room's policy
  !media-retention !roomid:example.org forever         -- set that room's policy
  !media-retention list                                -- every configured room + policy
  !media-retention top [N]                             -- top N rooms (default 10, capped
                                                            at 200) by media storage size,
                                                            each with its room ID, human
                                                            name, size, current retention
                                                            policy, and oldest media file's
                                                            date -- plus the server's
                                                            overall total storage used
  ```

  `top`'s room-to-media mapping has no shortcut: matrix-media-repo's own
  database has no room_id column at all (confirmed directly against its
  own source, the same `ListMedia()`/`PurgeRoomMedia` mechanism its
  room-scoped purge endpoint itself uses). Building this report walks
  every room on the server, asking Synapse's own admin API which media
  each one's timeline references, then matrix-media-repo's own admin API
  for those objects' sizes — an O(rooms) series of HTTP calls, not a
  single cheap query. Fine for an explicit admin command and a monthly
  background job; not something to run more often than that.

  The same report is also sent proactively once a month to every
  `trusted_remote_admin_user_ids` recipient (top 100 rooms), no command
  needed — tracked via a persisted last-sent timestamp (survives bot
  restarts without ever double-sending or resetting the clock).

  The room can also be pasted as a matrix.to link (e.g.
  `https://matrix.to/#/!roomid:example.org?via=example.org`, with or
  without the `?via=` part, and with or without a trailing `/$event_id`)
  instead of a bare room ID — copy-pasted straight out of a client's own
  "Copy link" action on a room, no manual editing needed.

  In a DM specifically, a bare `!` also works as a shorthand for
  `!media-retention` on this remote surface, e.g.
  `! !roomid:example.org retain 30d` — faster to type when managing
  several rooms in a row. This shorthand only applies in a DM-sized room
  (this bot plus at most one other person); a real multi-member room
  never treats a bare `!` as the command prefix.

  Lets an operator manage every room's retention policy from one place
  instead of inviting the bot into each one individually. Authorization
  here is the `trusted_remote_admin_user_ids` allowlist alone — the
  target room's own power levels are read via Synapse's admin API only to
  confirm the room actually exists, not to re-gate authorization (found
  live 2026-08-15: a bridged portal room commonly grants every member,
  including its real owner, power level 0 with no elevated user at all —
  re-checking room-level power on top of the allowlist made every such
  room unmanageable through this surface for no security benefit, since
  the allowlist is already the stricter, identity-based gate). **Requires
  the bot's own account to be a Synapse server admin** (the room-lookup
  call needs no membership at all — no join, no standing presence) — see
  the security note below before enabling this; it's a real privilege
  escalation, not a free convenience.
  - If your homeserver's admin API is itself restricted to a trusted
    network at the reverse proxy (it should be), this bot's own admin
    calls need a way to reach it directly too — see `synapse_admin_url` in
    `config.example.yaml`.

## Finding the bot and messaging it directly (DM)

The bot has no identity beyond a normal Matrix account — whatever
`bot_user_id` is set to in `config.yaml` (e.g.
`@media-retention-bot:example.org`) is its real, full Matrix ID, usable
anywhere a Matrix ID is accepted.

**Finding it**, if you didn't set this deployment up yourself:
- Ask whoever configured it what `bot_user_id` is set to.
- Or, if the bot is already a member of any room you're in (any room with
  an explicit retention policy), open that room's member list — its full
  ID is right there, and its display name generally makes its purpose
  obvious.
- Or, if you have Synapse admin access, look it up via the account list
  (`/_synapse/admin/v2/users`, or the Synapse Admin web UI if installed)
  for an account matching this deployment's chosen name.

**Messaging it directly**, once you have that ID: every command under
"How it works" above — both the in-room ones and, if enabled, the remote
room-id-targeted ones — also works in a plain direct message, not just
inside a room the bot has been invited to.

1. In your Matrix client, start a new direct message the same way you
   would with any person — e.g. in Element, the "+" next to
   "People"/"Direct Messages" → "Start new chat", or "New message".
2. Enter the bot's full Matrix ID from above, not just a display name —
   e.g. `@media-retention-bot:example.org`.
3. Send a command as a normal message in that DM, e.g.
   `!media-retention help` or `!media-retention list`.

The bot auto-accepts the DM the moment your client creates it — same
auto-join behaviour as being invited into a room (see "How it works"
above) — so there is no separate "accept" step on its side; your first
message goes straight through as soon as the room shows as joined in your
client.

One thing worth knowing once you're in the DM: commands **without** a
room ID (`!media-retention help`, `!media-retention list`) work for
anyone who can reach the bot; commands **with** a room ID
(`!media-retention !roomid:example.org retain 30d`) are the remote
command surface, gated by `trusted_remote_admin_user_ids` — see the
security note below.

## Requirements

- A Synapse homeserver with [`matrix-media-repo`](https://github.com/t2bot/matrix-media-repo)
  configured as its media backend, with the bot's own account (or a
  separate dedicated admin account) listed in `matrix-media-repo`'s own
  `admins:` config.
- Python 3.11+.
- **Only if `trusted_remote_admin_user_ids` is set** (the remote command
  surface above): the bot's own account also needs Synapse's server-admin
  flag (`register_new_matrix_user ... --admin`, or an equivalent admin API
  call for an already-registered account). Not required for the in-room
  command surface at all — leave `trusted_remote_admin_user_ids` empty and
  skip this entirely if you don't need it.

## Security note: the remote command surface and server-admin scope

Synapse's admin flag is binary (all-or-nothing) — there is no way to grant
"just enough" admin scope for the read-power-levels/read-room-name calls
this plugin's remote surface needs. An account with this flag can do anything
any Synapse server admin can do (reset any user's password, deactivate
accounts, read any room), not just what this plugin actually uses it for.

Before enabling `trusted_remote_admin_user_ids`, make sure:
- The homeserver's raw admin REST API (`/_synapse/admin/*`, not just an
  admin UI's own path) is not reachable from the public internet — a
  reverse-proxy IP allowlist restricting it to your own trusted network is
  the minimum bar, independent of whether this plugin uses the flag at
  all.
- You've weighed the deployment's own topology: if this bot runs
  co-located with Synapse itself (the common case — same host, same
  Docker network), a full host compromise already implies full Synapse
  compromise regardless of this flag. The scope this flag genuinely
  widens is a narrower one: a vulnerability specific to *this plugin's own
  code* (a dependency bug, a parsing bug) that leaks its token or achieves
  code execution *without* a full host compromise. Decide based on that
  narrower risk, not "is the host secure" alone.

## Setup

```bash
cp config.example.yaml config.yaml   # then fill in real values
pip install -r requirements.txt
python3 -m matrix_room_media_retention.main --config config.yaml
```

Or via Docker:

```bash
docker build -t matrix-room-media-retention -f docker/Dockerfile .
docker run -v $(pwd)/data:/data matrix-room-media-retention
```

(`config.yaml` and the SQLite policy database both live under `/data` —
mount it as a volume so both survive a container recreate.)

## What this plugin does NOT do

- Does not patch, fork, or modify any Matrix bridge (`mautrix-telegram`,
  `-whatsapp`, `-signal`, etc.) or the Synapse/Matrix spec itself.
- Does not touch Matrix event/message retention (`m.room.retention`) —
  text and captions are never purged by this tool, only already-uploaded
  media files.
- Does not decide *whether* media gets uploaded to Matrix in the first
  place (that's a different, complementary problem — see your own
  archive-vs-reference design if you want that decision made at ingest
  time for a specific bridge). This plugin only decides how long media
  that *has* been uploaded (from any source) sticks around.
- Does not provide a graphical UI — the in-room command surface is the
  only control surface in this version.
- Does not enumerate or apply a policy to every room on the server by
  default — a room only ever gets a policy once the bot is invited to it
  and someone explicitly sets one; there is no global default retention
  window applied automatically.
- Does not deduplicate awareness across rooms: `matrix-media-repo` itself
  deduplicates identical files internally, so if the same file is shared
  into two rooms with different retention policies, the more permissive
  policy effectively wins for that shared blob. This is a known,
  documented limitation, not something this plugin attempts to solve.

## Testing

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

All policy resolution, duration parsing (including the safety floor),
authorization, the `matrix-media-repo` API client, the audit log, dry-run
mode, and the scheduler loop are covered by pure unit tests (no live
homeserver needed). The Matrix bot's own connection/sync handling
(`bot.py`'s `login_and_sync_forever`/`_on_invite`) is thin `nio` wiring,
exercised by a live/compatibility pass against a real test homeserver
instead — see `docs/design.md`'s acceptance criteria for what that pass
must confirm before a real deploy.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
