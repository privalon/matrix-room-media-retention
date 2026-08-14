"""Entrypoint: wires the bot and the scheduler together, sharing one
asyncio event loop and one PolicyStore instance."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .bot import MediaRetentionBot
from .config import Config
from .policy_store import PolicyStore
from .purge_client import MediaRepoPurgeClient
from .scheduler import run_scheduler_loop


async def _run(config_path: str) -> None:
    config = Config.load(config_path)
    store = PolicyStore(config.db_path)
    purge_client = MediaRepoPurgeClient(
        base_url=config.media_repo_url,
        admin_access_token=config.media_repo_admin_access_token,
    )
    bot = MediaRetentionBot(config=config, store=store)

    if config.dry_run:
        logging.getLogger(__name__).warning(
            "dry_run is enabled -- no media will actually be purged, only logged/audited."
        )

    scheduler_task = asyncio.create_task(
        run_scheduler_loop(
            store=store,
            purge_client=purge_client,
            interval_seconds=config.scheduler_interval_seconds,
            dry_run=config.dry_run,
        )
    )
    try:
        await bot.login_and_sync_forever()
    finally:
        scheduler_task.cancel()
        store.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    args = parser.parse_args()

    try:
        asyncio.run(_run(args.config))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
