from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from .codex import CodexClient, CodexExecutionError
from .config import BotSettings, ServiceConfig, normalize_username
from .state import StateStore
from .telegram_api import TelegramApiClient, TelegramApiError

LOGGER = logging.getLogger(__name__)


class BridgeService:
    def __init__(self, config: ServiceConfig, state: StateStore) -> None:
        self.config = config
        self.state = state
        self.codex = CodexClient(config.app.codex_bin)
        self.telegram = TelegramApiClient()
        self._locks: Dict[Tuple[str, int], asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    async def run(self) -> None:
        runners = [asyncio.create_task(self._poll_bot(bot)) for bot in self.config.bots]
        await asyncio.gather(*runners)

    async def _poll_bot(self, bot: BotSettings) -> None:
        LOGGER.info("Polling bot '%s' for workdir %s", bot.name, bot.workdir)
        offset = await self.state.get_offset(bot.name)
        if offset is not None:
            LOGGER.info("Resuming Telegram update offset %s for bot '%s'", offset, bot.name)

        while True:
            try:
                updates = await self.telegram.get_updates(
                    token=bot.token,
                    offset=offset,
                    timeout_seconds=self.config.app.poll_timeout_seconds,
                )
                if updates:
                    LOGGER.info("Received %s update(s) for bot '%s'", len(updates), bot.name)
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
                        await self.state.set_offset(bot.name, offset)
                    task = asyncio.create_task(self._handle_update(bot, update))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
            except TelegramApiError:
                LOGGER.exception("Telegram polling failed for bot '%s'", bot.name)
                await asyncio.sleep(3)
            except Exception:
                LOGGER.exception("Unexpected polling failure for bot '%s'", bot.name)
                await asyncio.sleep(3)

    async def _handle_update(self, bot: BotSettings, update: Dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return

        chat = message.get("chat", {})
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return
        if chat.get("type") != "private":
            return

        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return

        if text.startswith("/whoami"):
            LOGGER.info("Handling /whoami for bot '%s' chat %s", bot.name, chat_id)
            await self._send_reply(
                bot,
                chat_id,
                self._format_identity(message),
                reply_to_message_id=message.get("message_id"),
            )
            return

        if not self._is_authorized(bot, message):
            sender = message.get("from", {})
            LOGGER.warning(
                "Unauthorized access attempt for bot '%s' from user_id=%s username=%s",
                bot.name,
                sender.get("id"),
                sender.get("username"),
            )
            await self._send_reply(
                bot,
                chat_id,
                "This bot is not authorized for your Telegram account.",
                reply_to_message_id=message.get("message_id"),
            )
            return

        lock = self._locks.setdefault((bot.name, chat_id), asyncio.Lock())
        async with lock:
            await self._handle_authorized_message(bot, message)

    async def _handle_authorized_message(self, bot: BotSettings, message: Dict[str, Any]) -> None:
        chat_id = int(message["chat"]["id"])
        text = str(message["text"]).strip()
        message_id = message.get("message_id")
        LOGGER.info(
            "Authorized message for bot '%s' chat %s: %s",
            bot.name,
            chat_id,
            _preview_text(text),
        )

        if text.startswith("/start") or text.startswith("/help"):
            LOGGER.info("Handling help command for bot '%s' chat %s", bot.name, chat_id)
            await self._send_reply(bot, chat_id, self._help_text(bot), reply_to_message_id=message_id)
            return

        if text.startswith("/status"):
            thread_id = await self.state.peek_thread(bot.name, chat_id)
            LOGGER.info("Handling /status for bot '%s' chat %s thread=%s", bot.name, chat_id, thread_id)
            status = (
                f"Current thread id: {thread_id}"
                if thread_id
                else "No saved Codex thread yet."
            )
            await self._send_reply(bot, chat_id, status, reply_to_message_id=message_id)
            return

        if text.startswith("/reset"):
            await self.state.clear_thread(bot.name, chat_id)
            LOGGER.info("Cleared saved thread for bot '%s' chat %s", bot.name, chat_id)
            await self._send_reply(
                bot,
                chat_id,
                "Saved Codex thread cleared. The next message will start a fresh context.",
                reply_to_message_id=message_id,
            )
            return

        thread_id = await self.state.get_thread(bot.name, chat_id, bot.workdir)

        try:
            await self.telegram.send_chat_action(bot.token, chat_id, action="typing")
        except TelegramApiError:
            LOGGER.warning("Failed to send typing action for bot '%s'", bot.name, exc_info=True)

        await self._send_reply(
            bot,
            chat_id,
            "Message received. Codex is processing it now. I will send the final reply when it is ready.",
            reply_to_message_id=message_id,
        )

        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._send_processing_updates(
                bot=bot,
                chat_id=chat_id,
                stop_event=heartbeat_stop,
            )
        )

        result: Optional[Any] = None
        started_at = time.monotonic()
        try:
            LOGGER.info(
                "Dispatching prompt to Codex for bot '%s' chat %s thread=%s",
                bot.name,
                chat_id,
                thread_id or "<new>",
            )
            result = await self.codex.run_prompt(bot, text, thread_id)
        except CodexExecutionError as exc:
            if thread_id:
                LOGGER.warning(
                    "Stored thread id failed for bot '%s'; clearing state and retrying once",
                    bot.name,
                )
                await self.state.clear_thread(bot.name, chat_id)
                try:
                    result = await self.codex.run_prompt(bot, text, None)
                except CodexExecutionError as retry_exc:
                    LOGGER.exception("Codex retry failed for bot '%s'", bot.name)
                    await self._send_reply(
                        bot,
                        chat_id,
                        f"Codex execution failed:\n{retry_exc}",
                        reply_to_message_id=message_id,
                    )
                    return
            else:
                LOGGER.exception("Codex execution failed for bot '%s'", bot.name)
                await self._send_reply(
                    bot,
                    chat_id,
                    f"Codex execution failed:\n{exc}",
                    reply_to_message_id=None,
                )
                return
        finally:
            heartbeat_stop.set()
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

        if result.thread_id and result.thread_id != thread_id:
            await self.state.set_thread(bot.name, chat_id, bot.workdir, result.thread_id)
            LOGGER.info(
                "Saved Codex thread '%s' for bot '%s' chat %s",
                result.thread_id,
                bot.name,
                chat_id,
            )

        LOGGER.info(
            "Sending Codex reply for bot '%s' chat %s (%s chars, %.2fs)",
            bot.name,
            chat_id,
            len(result.reply),
            result.duration_seconds or (time.monotonic() - started_at),
        )
        await self._send_reply(bot, chat_id, result.reply, reply_to_message_id=None)

    async def _send_processing_updates(
        self,
        bot: BotSettings,
        chat_id: int,
        stop_event: asyncio.Event,
        interval_seconds: float = 10.0,
    ) -> None:
        elapsed_intervals = 0
        while True:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
                return
            except asyncio.TimeoutError:
                elapsed_intervals += 1
                elapsed_seconds = max(1, int(round(elapsed_intervals * interval_seconds)))
                LOGGER.info(
                    "Sending processing heartbeat for bot '%s' chat %s after %ss",
                    bot.name,
                    chat_id,
                    elapsed_seconds,
                )
                await self._send_reply(
                    bot,
                    chat_id,
                    _processing_status_text(elapsed_seconds),
                    reply_to_message_id=None,
                )

    def _is_authorized(self, bot: BotSettings, message: Dict[str, Any]) -> bool:
        sender = message.get("from", {})
        username = sender.get("username")
        if not isinstance(username, str):
            return False
        if normalize_username(username) != bot.normalized_username:
            return False
        if bot.telegram_user_id is not None and sender.get("id") != bot.telegram_user_id:
            return False
        return True

    def _help_text(self, bot: BotSettings) -> str:
        return (
            f"Bot: {bot.name}\n"
            f"Workdir: {bot.workdir}\n\n"
            "Commands:\n"
            "/status - show the saved Codex thread id\n"
            "/reset - clear the saved Codex thread id\n"
            "/whoami - show your Telegram username and user id\n\n"
            "Any other text message will be sent to Codex."
        )

    def _format_identity(self, message: Dict[str, Any]) -> str:
        sender = message.get("from", {})
        raw_username = sender.get("username")
        if isinstance(raw_username, str) and raw_username.strip():
            username = f"@{raw_username.lstrip('@')}"
        else:
            username = "<no username>"
        user_id = sender.get("id", "<unknown>")
        return f"username: {username}\nuser_id: {user_id}"

    async def _send_reply(
        self,
        bot: BotSettings,
        chat_id: int,
        text: str,
        reply_to_message_id: Optional[int],
    ) -> None:
        for chunk in _split_telegram_message(text):
            try:
                await self.telegram.send_message(
                    token=bot.token,
                    chat_id=chat_id,
                    text=chunk,
                    reply_to_message_id=reply_to_message_id,
                )
            except TelegramApiError:
                LOGGER.exception("Failed to send Telegram reply for bot '%s'", bot.name)
                return
            reply_to_message_id = None


def _split_telegram_message(text: str, limit: int = 4000) -> List[str]:
    normalized = text.strip()
    if not normalized:
        return ["(empty response)"]
    if len(normalized) <= limit:
        return [normalized]

    chunks: List[str] = []
    current = ""
    for line in normalized.splitlines(keepends=True):
        if len(line) > limit:
            if current:
                chunks.append(current.rstrip())
                current = ""
            chunks.extend(_split_long_line(line, limit))
            continue
        if len(current) + len(line) > limit:
            chunks.append(current.rstrip())
            current = line
        else:
            current += line
    if current:
        chunks.append(current.rstrip())
    return chunks


def _split_long_line(line: str, limit: int) -> List[str]:
    chunks: List[str] = []
    start = 0
    while start < len(line):
        chunks.append(line[start : start + limit].rstrip())
        start += limit
    return [chunk or "(empty response)" for chunk in chunks]


def _preview_text(text: str, limit: int = 120) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 3]}..."


def _processing_status_text(elapsed_seconds: int) -> str:
    return (
        f"Still processing your request. Codex has been working for about {elapsed_seconds} seconds."
    )
