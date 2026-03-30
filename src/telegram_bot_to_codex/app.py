from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
from pathlib import Path
from typing import Optional

from .config import ConfigError, load_config
from .service import BridgeService
from .state import StateStore
from .telegram_api import TelegramApiClient, TelegramApiError


def main() -> int:
    parser = argparse.ArgumentParser(description="Bridge Telegram bots to Codex CLI")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="Path to the local TOML config file",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="Override log level for this run, for example DEBUG or INFO",
    )
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 1

    log_level_name = (args.log_level or config.app.log_level).upper()
    logging.basicConfig(
        level=getattr(logging, log_level_name, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )
    logger = logging.getLogger(__name__)
    logger.info("Starting Telegram Bot To Codex")
    logger.info("Using config file: %s", args.config.resolve())
    logger.info("Using state file: %s", config.app.state_path)
    logger.info("Configured bots: %s", len(config.bots))

    state = StateStore(config.app.state_path)
    try:
        asyncio.run(_run(config, state))
    except RuntimeError as exc:
        logger.error("Startup failed: %s", exc)
        return 1
    return 0


async def _run(config, state: StateStore) -> None:
    await _validate_startup(config)
    await state.load()
    logging.getLogger(__name__).info("Local state loaded successfully")
    service = BridgeService(config, state)
    logging.getLogger(__name__).info("Service is ready and polling Telegram")
    await service.run()


async def _validate_startup(config) -> None:
    await _validate_codex_binary(config.app.codex_bin)
    await _validate_telegram_bots(config)


async def _validate_codex_binary(codex_bin: str) -> None:
    resolved = shutil.which(codex_bin) if "/" not in codex_bin else codex_bin
    if not resolved:
        raise RuntimeError(f"codex binary not found: {codex_bin}")

    process = await asyncio.create_subprocess_exec(
        resolved,
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip() or "unknown error"
        raise RuntimeError(f"Failed to run '{codex_bin} --version': {detail}")

    version = stdout.decode("utf-8", errors="replace").strip() or resolved
    logging.getLogger(__name__).info("Using Codex CLI: %s", version)


async def _validate_telegram_bots(config) -> None:
    client = TelegramApiClient()
    logger = logging.getLogger(__name__)
    seen_bot_ids = set()

    for bot in config.bots:
        try:
            me = await client.get_me(bot.token)
        except TelegramApiError as exc:
            raise RuntimeError(f"Failed to validate Telegram bot '{bot.name}': {exc}") from exc

        bot_username = _format_bot_username(me.get("username"))
        bot_id = me.get("id")
        if bot_id in seen_bot_ids:
            logger.warning("Multiple config entries point at the same Telegram bot token: %s", bot.name)
        else:
            seen_bot_ids.add(bot_id)

        logger.info(
            "Validated Telegram bot '%s' as %s for workdir %s",
            bot.name,
            bot_username,
            bot.workdir,
        )


def _format_bot_username(username: Optional[object]) -> str:
    if isinstance(username, str) and username.strip():
        return f"@{username.lstrip('@')}"
    return "<unknown username>"
