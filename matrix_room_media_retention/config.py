"""Config loading -- a single YAML file, no environment-variable sprawl.

Kept intentionally small: this plugin has exactly one job, and its config
should be readable top to bottom in under a minute.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .duration import DEFAULT_MINIMUM_RETAIN_SECONDS


@dataclass(frozen=True)
class Config:
    homeserver_url: str
    bot_user_id: str
    bot_password: str
    media_repo_url: str
    media_repo_admin_access_token: str
    db_path: str
    scheduler_interval_seconds: int
    minimum_power_level: int
    command_prefix: str
    minimum_retain_seconds: int
    dry_run: bool
    trusted_remote_admin_user_ids: list[str] = field(default_factory=list)
    synapse_admin_url: str = ""

    @staticmethod
    def load(path: str | Path) -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        missing = [
            key
            for key in ("homeserver_url", "bot_user_id", "bot_password", "media_repo_admin_access_token")
            if not raw.get(key)
        ]
        if missing:
            raise ValueError(f"config.yaml is missing required field(s): {', '.join(missing)}")

        return Config(
            homeserver_url=raw["homeserver_url"],
            bot_user_id=raw["bot_user_id"],
            bot_password=raw["bot_password"],
            media_repo_url=raw.get("media_repo_url") or raw["homeserver_url"],
            media_repo_admin_access_token=raw["media_repo_admin_access_token"],
            db_path=raw.get("db_path", "media_retention.sqlite3"),
            scheduler_interval_seconds=int(raw.get("scheduler_interval_seconds", 3600)),
            minimum_power_level=int(raw.get("minimum_power_level", 50)),
            command_prefix=raw.get("command_prefix", "!media-retention"),
            # roadmap/041 §9: a safety floor below which `retain <duration>`
            # is rejected outright (see duration.py's own docstring for the
            # exact footgun this closes). Configurable, not hardcoded, for
            # an operator with a genuine sub-day use case -- but the
            # default refuses to guess that's what was meant.
            minimum_retain_seconds=int(raw.get("minimum_retain_seconds", DEFAULT_MINIMUM_RETAIN_SECONDS)),
            # roadmap/041 §9: when true, the scheduler computes and logs
            # what each retain-policy room *would* purge (audit log only)
            # without ever calling matrix-media-repo's real purge endpoint.
            dry_run=bool(raw.get("dry_run", False)),
            # roadmap/041 §11: sender allowlist for the remote (DM-based,
            # room-id-targeted) command surface -- `!media-retention
            # <room_id> retain <dur>` and `!media-retention list`.
            # Deliberately empty by default (the whole remote surface is
            # disabled until explicitly opted into), and deliberately a
            # SEPARATE gate from the in-room power-level check: it
            # restricts *who can even attempt* a remote, room-id-targeted
            # command at all, regardless of what power level they hold in
            # whatever room they end up targeting.
            trusted_remote_admin_user_ids=list(raw.get("trusted_remote_admin_user_ids") or []),
            # roadmap/041 §11: found live 2026-08-15 that a reverse-proxy
            # restriction on the homeserver's own admin API (a real,
            # independently-motivated hardening step -- see that finding's
            # own note) blocks THIS bot's admin calls too, since its own
            # network path to homeserver_url (through the public reverse
            # proxy) isn't guaranteed to originate from wherever that
            # restriction allows. Falls back to homeserver_url when unset,
            # for a deployment where the admin API genuinely has no such
            # restriction -- but a deployment restricting the admin API
            # (which it should) needs this pointed at whatever internal
            # address reaches it directly instead (e.g.
            # "http://matrix-synapse:8008" for a matrix-docker-ansible-
            # deploy-based homeserver, bypassing the reverse proxy
            # entirely, the same fix already used for import_synapse's own
            # -baseUrl).
            synapse_admin_url=(raw.get("synapse_admin_url") or raw["homeserver_url"]),
        )
