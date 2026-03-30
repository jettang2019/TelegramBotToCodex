from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .codex import CodexClient, CodexExecutionError
from .config import BotSettings, ServiceConfig, normalize_username
from .state import StateStore
from .telegram_api import TelegramApiClient, TelegramApiError

LOGGER = logging.getLogger(__name__)
_STREAM_EDIT_INTERVAL_SECONDS = 0.75


@dataclass
class _StreamProgressState:
    status_message_id: Optional[int]
    last_status_text: Optional[str] = None
    last_streamed_reply: Optional[str] = None
    active_stream_item_id: Optional[str] = None
    active_stream_text: str = ""
    active_stream_message_ids: List[int] = field(default_factory=list)
    active_stream_chunks: List[str] = field(default_factory=list)
    last_stream_flush_at: float = 0.0


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

        initial_status = "Message received. Codex is starting now."
        status_message_id = await self._send_reply(
            bot,
            chat_id,
            initial_status,
            reply_to_message_id=message_id,
        )
        stream_state = _StreamProgressState(
            status_message_id=status_message_id,
            last_status_text=initial_status if status_message_id is not None else None,
        )

        async def handle_codex_event(event: Dict[str, Any]) -> None:
            event_type = event.get("type")
            item = event.get("item", {})
            if event_type == "item.agent_message.delta":
                item_id = event.get("item_id")
                delta = event.get("delta")
                if isinstance(item_id, str) and isinstance(delta, str):
                    self._prepare_stream_item(stream_state, item_id)
                    stream_state.active_stream_text += delta
                    await self._flush_streamed_reply(
                        bot,
                        chat_id,
                        stream_state,
                        force=False,
                    )

            if (
                event_type == "item.completed"
                and isinstance(item, dict)
                and item.get("type") == "agent_message"
            ):
                item_id = item.get("id")
                if isinstance(item_id, str):
                    self._prepare_stream_item(stream_state, item_id)
                streamed_text = item.get("text")
                if isinstance(streamed_text, str):
                    stream_state.active_stream_text = streamed_text
                normalized_text = stream_state.active_stream_text.strip()
                if normalized_text:
                    stream_state.last_streamed_reply = normalized_text
                    await self._flush_streamed_reply(
                        bot,
                        chat_id,
                        stream_state,
                        force=True,
                    )

            status_text = _stream_event_status_text(event)
            if not status_text or status_text == stream_state.last_status_text:
                return
            if stream_state.status_message_id is None:
                return

            stream_state.last_status_text = status_text
            await self._edit_status_message(
                bot,
                chat_id,
                stream_state.status_message_id,
                status_text,
            )

        result: Optional[Any] = None
        try:
            LOGGER.info(
                "Dispatching prompt to Codex for bot '%s' chat %s thread=%s",
                bot.name,
                chat_id,
                thread_id or "<new>",
            )
            result = await self.codex.run_prompt(
                bot,
                text,
                thread_id,
                event_callback=handle_codex_event,
            )
        except CodexExecutionError as exc:
            if thread_id:
                LOGGER.warning(
                    "Stored thread id failed for bot '%s'; clearing state and retrying once",
                    bot.name,
                )
                if stream_state.status_message_id is not None:
                    retry_status = "Stored Codex thread failed. Retrying with a fresh thread."
                    stream_state.last_status_text = retry_status
                    await self._edit_status_message(
                        bot,
                        chat_id,
                        stream_state.status_message_id,
                        retry_status,
                    )
                await self.state.clear_thread(bot.name, chat_id)
                try:
                    result = await self.codex.run_prompt(
                        bot,
                        text,
                        None,
                        event_callback=handle_codex_event,
                    )
                except CodexExecutionError as retry_exc:
                    LOGGER.exception("Codex retry failed for bot '%s'", bot.name)
                    await self._update_failed_status(bot, chat_id, stream_state, "Codex execution failed.")
                    await self._send_reply(
                        bot,
                        chat_id,
                        f"Codex execution failed:\n{retry_exc}",
                        reply_to_message_id=message_id,
                    )
                    return
            else:
                LOGGER.exception("Codex execution failed for bot '%s'", bot.name)
                await self._update_failed_status(bot, chat_id, stream_state, "Codex execution failed.")
                await self._send_reply(
                    bot,
                    chat_id,
                    f"Codex execution failed:\n{exc}",
                    reply_to_message_id=None,
                )
                return

        if result.thread_id and result.thread_id != thread_id:
            await self.state.set_thread(bot.name, chat_id, bot.workdir, result.thread_id)
            LOGGER.info(
                "Saved Codex thread '%s' for bot '%s' chat %s",
                result.thread_id,
                bot.name,
                chat_id,
            )

        if not stream_state.last_streamed_reply:
            active_stream_text = stream_state.active_stream_text.strip()
            if active_stream_text == result.reply:
                stream_state.last_streamed_reply = active_stream_text

        if stream_state.status_message_id is not None:
            final_status = "Codex finished processing your request."
            if stream_state.last_status_text != final_status:
                stream_state.last_status_text = final_status
                await self._edit_status_message(
                    bot,
                    chat_id,
                    stream_state.status_message_id,
                    final_status,
                )

        if stream_state.last_streamed_reply == result.reply:
            LOGGER.info(
                "Final reply for bot '%s' chat %s was already streamed; skipping duplicate send",
                bot.name,
                chat_id,
            )
            return

        LOGGER.info(
            "Sending Codex reply for bot '%s' chat %s (%s chars, %.2fs)",
            bot.name,
            chat_id,
            len(result.reply),
            result.duration_seconds,
        )
        await self._send_reply(bot, chat_id, result.reply, reply_to_message_id=None)

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
    ) -> Optional[int]:
        sent_message_id: Optional[int] = None
        for chunk in _split_telegram_message(text):
            try:
                response = await self.telegram.send_message(
                    token=bot.token,
                    chat_id=chat_id,
                    text=chunk,
                    reply_to_message_id=reply_to_message_id,
                )
            except TelegramApiError:
                LOGGER.exception("Failed to send Telegram reply for bot '%s'", bot.name)
                return sent_message_id
            if sent_message_id is None:
                sent_message_id = _extract_message_id(response)
            reply_to_message_id = None
        return sent_message_id

    async def _edit_status_message(
        self,
        bot: BotSettings,
        chat_id: int,
        message_id: int,
        text: str,
    ) -> None:
        try:
            await self.telegram.edit_message_text(
                token=bot.token,
                chat_id=chat_id,
                message_id=message_id,
                text=text,
            )
        except TelegramApiError:
            LOGGER.exception("Failed to edit Telegram status message for bot '%s'", bot.name)

    async def _update_failed_status(
        self,
        bot: BotSettings,
        chat_id: int,
        stream_state: _StreamProgressState,
        text: str,
    ) -> None:
        if stream_state.status_message_id is None:
            return
        stream_state.last_status_text = text
        await self._edit_status_message(
            bot,
            chat_id,
            stream_state.status_message_id,
            text,
        )

    def _prepare_stream_item(self, stream_state: _StreamProgressState, item_id: str) -> None:
        if stream_state.active_stream_item_id == item_id:
            return
        stream_state.active_stream_item_id = item_id
        stream_state.active_stream_text = ""
        stream_state.active_stream_message_ids = []
        stream_state.active_stream_chunks = []
        stream_state.last_stream_flush_at = 0.0

    async def _flush_streamed_reply(
        self,
        bot: BotSettings,
        chat_id: int,
        stream_state: _StreamProgressState,
        *,
        force: bool,
    ) -> None:
        normalized_text = stream_state.active_stream_text.strip()
        if not normalized_text:
            return

        now = time.monotonic()
        if (
            not force
            and stream_state.active_stream_message_ids
            and now - stream_state.last_stream_flush_at < _STREAM_EDIT_INTERVAL_SECONDS
        ):
            return

        desired_chunks = _split_telegram_message(normalized_text)
        for index, chunk in enumerate(desired_chunks):
            if index < len(stream_state.active_stream_message_ids):
                if index < len(stream_state.active_stream_chunks) and stream_state.active_stream_chunks[index] == chunk:
                    continue
                await self._edit_status_message(
                    bot,
                    chat_id,
                    stream_state.active_stream_message_ids[index],
                    chunk,
                )
            else:
                message_id = await self._send_reply(
                    bot,
                    chat_id,
                    chunk,
                    reply_to_message_id=None,
                )
                if message_id is None:
                    return
                stream_state.active_stream_message_ids.append(message_id)

        stream_state.active_stream_chunks = desired_chunks
        stream_state.last_stream_flush_at = now


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


def _extract_message_id(response: Dict[str, Any]) -> Optional[int]:
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    message_id = result.get("message_id")
    if isinstance(message_id, int):
        return message_id
    return None


def _stream_event_status_text(event: Dict[str, Any]) -> Optional[str]:
    event_type = event.get("type")
    if event_type == "turn.started":
        return "Codex started working on your request."
    if event_type == "turn.completed":
        return "Codex finished processing your request."
    if event_type not in {"item.started", "item.completed"}:
        return None

    item = event.get("item")
    if not isinstance(item, dict):
        return None

    item_type = item.get("type")
    if item_type == "agent_message":
        return None

    if item_type == "command_execution":
        command = item.get("command")
        verb = "Running" if event_type == "item.started" else "Finished"
        if isinstance(command, str) and command.strip():
            return f"{verb} command: {_preview_text(command, limit=100)}"
        return f"Codex {verb.lower()} a command."

    if item_type == "reasoning":
        return "Codex is analyzing the request."
    if item_type == "web_search":
        return "Codex is searching the web."
    if item_type == "mcp_tool_call":
        return "Codex is calling an external tool."
    if item_type in {"file_change", "file_changes"}:
        if event_type == "item.started":
            return "Codex is preparing file changes."
        return "Codex finished applying file changes."
    if item_type == "plan_update":
        return "Codex updated its plan."
    return None
