# Design

## The problem

You want media in a Matrix room — say, a busy bridged channel, or just a
room that gets a lot of photos/videos — to not accumulate forever, while
still keeping the actual conversation history intact. Ideally this should
work the same regardless of whether the room is a plain native Matrix room
or bridged from Telegram/WhatsApp/Signal/anything else, and it should be
settable per room, not as one global server setting.

## What already exists upstream, and why it doesn't quite cover this

**Synapse's `media_retention` config** (`local_media_lifetime`/
`remote_media_lifetime`) purges media that hasn't been accessed in a given
window. It's media-only (doesn't touch events), but it's **server-wide** —
one policy for the whole homeserver, no per-room granularity.

**Synapse's `m.room.retention` state event** lets a room's own moderators
set a retention lifetime for that room specifically. It's genuinely
per-room — but it purges whole **events**, message text included, not just
the media attachment. Using it to "retain media only" would also delete
captions and conversation history, which isn't what most people mean by
"media retention."

**Synapse's Admin API** (`DELETE /_synapse/admin/v1/media/<server>/<media_id>`,
or the bulk `POST /_synapse/admin/v1/media/delete?before_ts=...`) deletes
individual media files without touching events — but again, the bulk
variant is server-wide, and there's no room filter.

None of these three combine "media only" with "per room." That combination
is the actual gap.

## What does cover it: `matrix-media-repo`

[`matrix-media-repo`](https://github.com/t2bot/matrix-media-repo) is a
mature, actively maintained alternative media repository for Matrix
homeservers (used as a drop-in replacement/front for Synapse's own media
storage). Its own admin API has exactly the missing primitive:

```
POST /_matrix/media/unstable/admin/purge/room/<room_id>?before_ts=<ms>
```

This deletes all media known to that specific room, created/last-used
before the given timestamp, local or remote — while leaving the events
that referenced it untouched (they just end up pointing at media that's no
longer there, the same as any other expired/unavailable attachment).

It's already supported as a first-class, documented component of
`matrix-docker-ansible-deploy` (one of the most widely used Matrix
deployment tools) and other deployment stacks, pre-wired for Postgres/S3
backends. If your homeserver already runs it, there is no new
infrastructure to stand up — just this policy layer on top.

## What this plugin actually adds

Everything `matrix-media-repo` *doesn't* provide on its own:

1. **A per-room policy** (retain N / forever), settable by anyone with
   sufficient power level in that room, via an ordinary in-room command —
   not a server-admin-only operation. An operator/admin with a higher
   power level in a room uses the exact same command path, not a separate
   privileged one.
2. **A scheduler** that turns "room X should retain media for 30 days"
   into the right `before_ts` and calls the purge endpoint on a recurring
   basis, so the operator never has to run anything by hand.
3. **Authorization** based on the room's own `m.room.power_levels` — the
   one mechanism guaranteed to exist and mean the same thing in every
   Matrix room, bridged or not, which is what makes the whole thing
   bridge-agnostic without any bridge-specific integration code.
4. **A safety floor** on the shortest retention duration accepted
   (`minimum_retain_seconds`), so a unit typo can't turn into an
   accidental mass-deletion on the very next scheduler tick.
5. **An audit log** of every scheduler pass's outcome, and a **dry-run**
   mode to preview what a policy would do before it does it for real.

## Non-goals

- Deciding *whether* media should be uploaded to Matrix at all (vs. kept
  as a live reference back to the source app) is a different, complementary
  problem — solved differently, at ingest time, typically requiring a
  bridge-specific patch (Matrix's Direct Media / Bridge v2 mechanism). This
  plugin only governs media that has already landed in Matrix's storage,
  regardless of source.
- Message/event retention (`m.room.retention`) is untouched — this plugin
  never purges text.
- No graphical UI in this version — the in-room command surface is the
  only control surface.
- No global default retention applied to every room automatically — a
  room is only ever affected once the bot has been invited to it *and*
  someone with sufficient power level has explicitly set a policy there.
  This was a deliberate choice made explicit during design review: an
  opt-out model (every room gets a default retention window unless
  overridden) would be a materially larger blast radius (silently purging
  media in rooms nobody thought to configure) for a tool whose whole job
  is deleting real user data. If a server-wide, admin-configured default
  (independent of the bot, enumerating every room via the Synapse admin
  API) is ever wanted, it should be a distinct, explicitly-chosen mode,
  not this plugin's default behavior.
- Cross-room deduplication awareness: `matrix-media-repo` deduplicates
  identical files across rooms internally; if the same file is shared into
  two rooms with different policies, the more permissive one effectively
  wins for that shared blob. This is a known limitation of building on top
  of `matrix-media-repo`'s own model, not something this plugin attempts
  to solve independently.

## Acceptance criteria

1. `!media-retention retain <duration>` / `forever` / `off` / `help` work
   in any room the bot has joined, regardless of whether that room is
   bridged and to which network.
2. Authorization is enforced via the room's own Matrix power levels.
3. A room with `retain 30d` has media older than 30 days purged (file
   gone, event/text intact) within one scheduler interval of crossing
   that age.
4. A room with `forever`/`off`/no explicit policy is never queried by the
   purge scheduler.
5. Policy state, and the purge audit log, survive a plugin restart.
6. No Matrix bridge is patched, forked, or has its container image
   replaced by this plugin.
7. A `retain <duration>` below the configured `minimum_retain_seconds` is
   rejected with a clear error, and does not change the room's policy.
8. With `dry_run: true`, the scheduler records what it would have purged
   to the audit log for every `retain`-policy room, without ever calling
   matrix-media-repo's real purge endpoint or changing any room's
   last-purge summary fields.
