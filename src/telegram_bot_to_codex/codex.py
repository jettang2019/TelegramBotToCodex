from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Dict, List, Optional, Tuple, Callable

from .config import BotSettings

LOGGER = logging.getLogger(__name__)


class CodexExecutionError(RuntimeError):
    pass


CodexEventCallback = Callable[[Dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class CodexResult:
    thread_id: Optional[str]
    reply: str
    duration_seconds: float


@dataclass
class _TurnState:
    thread_id: str
    started_at: float
    event_callback: Optional[CodexEventCallback]
    turn_id: Optional[str] = None
    completed: asyncio.Event = field(default_factory=asyncio.Event)
    last_message: Optional[str] = None
    message_buffers: Dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None
    failed: bool = False


class _JsonRpcError(RuntimeError):
    def __init__(self, message: str, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code


class _AppServerSession:
    def __init__(self, codex_bin: str, bot: BotSettings) -> None:
        self.codex_bin = codex_bin
        self.bot = bot
        self.process: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._pending_responses: Dict[int, asyncio.Future[Dict[str, Any]]] = {}
        self._request_id = 0
        self._write_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()
        self._active_turn: Optional[_TurnState] = None
        self._current_thread_id: Optional[str] = None
        self._initialized = False

    async def run_prompt(
        self,
        prompt: str,
        thread_id: Optional[str],
        event_callback: Optional[CodexEventCallback],
    ) -> CodexResult:
        async with self._run_lock:
            await self._ensure_started()
            active_thread_id = await self._ensure_thread(thread_id)
            turn_state = _TurnState(
                thread_id=active_thread_id,
                started_at=time.monotonic(),
                event_callback=event_callback,
            )
            self._active_turn = turn_state

            try:
                response = await self._send_request("turn/start", self._turn_start_params(active_thread_id, prompt))
                turn = response.get("turn", {})
                if isinstance(turn, dict):
                    candidate = turn.get("id")
                    if isinstance(candidate, str) and candidate:
                        turn_state.turn_id = candidate

                await turn_state.completed.wait()
                if turn_state.failed:
                    raise CodexExecutionError(turn_state.error or "Codex turn failed")

                if not turn_state.last_message:
                    turn_state.last_message = _last_non_empty_message(turn_state.message_buffers)
                if not turn_state.last_message:
                    raise CodexExecutionError("Codex completed without a final agent message")

                return CodexResult(
                    thread_id=active_thread_id,
                    reply=turn_state.last_message,
                    duration_seconds=time.monotonic() - turn_state.started_at,
                )
            finally:
                self._active_turn = None

    async def _ensure_started(self) -> None:
        if self._is_process_alive() and self._initialized:
            return

        await self._start_process()

    def _is_process_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def _start_process(self) -> None:
        await self._stop_process()
        self.process = await asyncio.create_subprocess_exec(
            self.codex_bin,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.bot.workdir),
        )
        self._reader_task = asyncio.create_task(self._read_stdout_loop())
        self._stderr_task = asyncio.create_task(self._read_stderr_loop())
        self._current_thread_id = None
        self._initialized = False

        await self._send_request(
            "initialize",
            {
                "clientInfo": {
                    "name": "telegram_bot_to_codex",
                    "title": "Telegram Bot To Codex",
                    "version": "0.1.0",
                }
            },
        )
        await self._send_notification("initialized", {})
        self._initialized = True

    async def _stop_process(self) -> None:
        self._initialized = False
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()

        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        self.process = None
        self._reader_task = None
        self._stderr_task = None
        self._fail_pending(_JsonRpcError("Codex app-server session stopped"))

    async def _ensure_thread(self, thread_id: Optional[str]) -> str:
        if thread_id:
            if self._current_thread_id == thread_id:
                return thread_id
            response = await self._send_request(
                "thread/resume",
                self._thread_resume_params(thread_id),
            )
        else:
            response = await self._send_request(
                "thread/start",
                self._thread_start_params(),
            )

        thread = response.get("thread", {})
        if not isinstance(thread, dict):
            raise CodexExecutionError("Codex app-server response did not include a thread object")

        next_thread_id = thread.get("id")
        if not isinstance(next_thread_id, str) or not next_thread_id:
            raise CodexExecutionError("Codex app-server response did not include a thread id")

        self._current_thread_id = next_thread_id
        return next_thread_id

    def _thread_start_params(self) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "cwd": str(self.bot.workdir),
            "serviceName": "telegram_bot_to_codex",
        }
        approval_policy, sandbox = _execution_mode_to_thread_settings(self.bot)
        if approval_policy is not None:
            params["approvalPolicy"] = approval_policy
        if sandbox is not None:
            params["sandbox"] = sandbox
        return params

    def _thread_resume_params(self, thread_id: str) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "threadId": thread_id,
            "cwd": str(self.bot.workdir),
        }
        approval_policy, sandbox = _execution_mode_to_thread_settings(self.bot)
        if approval_policy is not None:
            params["approvalPolicy"] = approval_policy
        if sandbox is not None:
            params["sandbox"] = sandbox
        return params

    def _turn_start_params(self, thread_id: str, prompt: str) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "cwd": str(self.bot.workdir),
        }
        if self.bot.model is not None:
            params["model"] = self.bot.model
        if self.bot.effort is not None:
            params["effort"] = self.bot.effort
        return params

    async def _send_request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        request_id = self._next_request_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Dict[str, Any]] = loop.create_future()
        self._pending_responses[request_id] = future
        await self._send_message({"id": request_id, "method": method, "params": params})
        return await future

    async def _send_notification(self, method: str, params: Dict[str, Any]) -> None:
        await self._send_message({"method": method, "params": params})

    async def _send_response(self, request_id: Any, payload: Dict[str, Any]) -> None:
        await self._send_message({"id": request_id, **payload})

    async def _send_message(self, payload: Dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None or self.process.returncode is not None:
            raise CodexExecutionError("Codex app-server is not running")

        encoded = (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")
        async with self._write_lock:
            self.process.stdin.write(encoded)
            await self.process.stdin.drain()

    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _read_stdout_loop(self) -> None:
        if self.process is None or self.process.stdout is None:
            return

        try:
            while True:
                raw_line = await self.process.stdout.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                await self._handle_stdout_line(line)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Codex app-server stdout reader failed for bot '%s'", self.bot.name)
        finally:
            self._initialized = False
            self._fail_pending(_JsonRpcError("Codex app-server connection closed"))
            if self._active_turn is not None and not self._active_turn.completed.is_set():
                self._active_turn.failed = True
                self._active_turn.error = "Codex app-server connection closed"
                self._active_turn.completed.set()

    async def _handle_stdout_line(self, line: str) -> None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            LOGGER.warning("Ignored non-JSON line from Codex app-server: %s", line)
            return

        if not isinstance(message, dict):
            return

        if "method" in message and "id" in message:
            await self._handle_server_request(message)
            return

        if "method" in message:
            await self._handle_notification(message)
            return

        if "id" in message:
            self._handle_response(message)

    async def _handle_server_request(self, message: Dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if not isinstance(method, str) or not isinstance(request_id, (int, str)):
            return

        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            decision = "accept" if _auto_accept_server_requests(self.bot) else "decline"
            await self._send_response(request_id, {"result": decision})
            return

        if method == "tool/requestUserInput":
            await self._send_response(
                request_id,
                {
                    "error": {
                        "code": -32000,
                        "message": "Interactive user input is not supported by telegram_bot_to_codex",
                    }
                },
            )
            return

        await self._send_response(
            request_id,
            {
                "error": {
                    "code": -32601,
                    "message": f"Unsupported Codex app-server request: {method}",
                }
            },
        )

    def _handle_response(self, message: Dict[str, Any]) -> None:
        request_id = message.get("id")
        if not isinstance(request_id, int):
            return
        future = self._pending_responses.pop(request_id, None)
        if future is None or future.done():
            return

        if "error" in message:
            error = message.get("error", {})
            if isinstance(error, dict):
                detail = error.get("message")
                code = error.get("code")
            else:
                detail = str(error)
                code = None
            future.set_exception(_JsonRpcError(str(detail or "Unknown app-server error"), code))
            return

        result = message.get("result")
        if isinstance(result, dict):
            future.set_result(result)
        else:
            future.set_result({})

    async def _handle_notification(self, message: Dict[str, Any]) -> None:
        if self._active_turn is None:
            return

        method = message.get("method")
        params = message.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            return

        normalized = _normalize_notification(method, params)
        if normalized is not None and self._active_turn.event_callback is not None:
            await self._active_turn.event_callback(normalized)

        if method == "turn/started":
            turn = params.get("turn", {})
            if isinstance(turn, dict):
                turn_id = turn.get("id")
                if isinstance(turn_id, str) and turn_id:
                    self._active_turn.turn_id = turn_id
            return

        thread_id = params.get("threadId")
        if thread_id != self._active_turn.thread_id:
            return

        if method == "item/agentMessage/delta":
            item_id = params.get("itemId")
            delta = params.get("delta")
            if isinstance(item_id, str) and isinstance(delta, str):
                current = self._active_turn.message_buffers.get(item_id, "")
                self._active_turn.message_buffers[item_id] = current + delta
            return

        if method == "item/completed":
            item = params.get("item", {})
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                item_id = item.get("id")
                text = item.get("text")
                if not isinstance(text, str) and isinstance(item_id, str):
                    text = self._active_turn.message_buffers.get(item_id)
                if isinstance(text, str) and text.strip():
                    self._active_turn.last_message = text.strip()
            return

        if method == "error":
            self._active_turn.failed = True
            self._active_turn.error = _format_turn_error(params.get("error"))
            return

        if method == "turn/completed":
            turn = params.get("turn", {})
            if isinstance(turn, dict):
                status = turn.get("status")
                if status == "failed":
                    self._active_turn.failed = True
                    self._active_turn.error = _format_turn_error(turn.get("error"))
            self._active_turn.completed.set()

    async def _read_stderr_loop(self) -> None:
        if self.process is None or self.process.stderr is None:
            return

        try:
            while True:
                raw_line = await self.process.stderr.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    LOGGER.warning("Codex app-server stderr for bot '%s': %s", self.bot.name, line)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Codex app-server stderr reader failed for bot '%s'", self.bot.name)

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending_responses.values():
            if not future.done():
                future.set_exception(exc)
        self._pending_responses.clear()


class CodexClient:
    def __init__(self, codex_bin: str) -> None:
        self.codex_bin = codex_bin
        self._sessions: Dict[str, _AppServerSession] = {}

    async def run_prompt(
        self,
        bot: BotSettings,
        prompt: str,
        thread_id: Optional[str],
        event_callback: Optional[CodexEventCallback] = None,
    ) -> CodexResult:
        session = self._sessions.setdefault(bot.name, _AppServerSession(self.codex_bin, bot))
        try:
            return await session.run_prompt(prompt, thread_id, event_callback)
        except _JsonRpcError as exc:
            raise CodexExecutionError(str(exc)) from exc


def _normalize_notification(method: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if method == "thread/started":
        thread = params.get("thread", {})
        if isinstance(thread, dict):
            thread_id = thread.get("id")
            if isinstance(thread_id, str) and thread_id:
                return {"type": "thread.started", "thread_id": thread_id}
        return None

    if method == "turn/started":
        return {"type": "turn.started"}

    if method == "turn/completed":
        turn = params.get("turn", {})
        status = turn.get("status") if isinstance(turn, dict) else None
        return {"type": "turn.completed", "status": status}

    if method == "item/started":
        item = params.get("item", {})
        if isinstance(item, dict):
            return {"type": "item.started", "item": _normalize_item(item)}
        return None

    if method == "item/completed":
        item = params.get("item", {})
        if isinstance(item, dict):
            return {"type": "item.completed", "item": _normalize_item(item)}
        return None

    if method == "item/agentMessage/delta":
        item_id = params.get("itemId")
        delta = params.get("delta")
        if isinstance(item_id, str) and isinstance(delta, str):
            return {
                "type": "item.agent_message.delta",
                "item_id": item_id,
                "delta": delta,
            }
        return None

    if method == "error":
        return {
            "type": "error",
            "error": _format_turn_error(params.get("error")),
            "will_retry": bool(params.get("willRetry")),
        }

    return None


def _normalize_item(item: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(item)
    item_type = normalized.get("type")
    if isinstance(item_type, str):
        normalized["type"] = {
            "agentMessage": "agent_message",
            "commandExecution": "command_execution",
            "fileChange": "file_change",
            "mcpToolCall": "mcp_tool_call",
            "webSearch": "web_search",
            "userMessage": "user_message",
            "contextCompaction": "context_compaction",
        }.get(item_type, _camel_to_snake(item_type))
    return normalized


def _camel_to_snake(value: str) -> str:
    chars: List[str] = []
    for index, char in enumerate(value):
        if char.isupper() and index > 0:
            chars.append("_")
        chars.append(char.lower())
    return "".join(chars)


def _format_turn_error(error: Any) -> str:
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        details = error.get("details")
        if isinstance(details, str) and details.strip():
            return details.strip()
    if isinstance(error, str) and error.strip():
        return error.strip()
    return "Codex turn failed"


def _last_non_empty_message(buffers: Dict[str, str]) -> Optional[str]:
    for value in reversed(list(buffers.values())):
        normalized = value.strip()
        if normalized:
            return normalized
    return None


def _execution_mode_to_thread_settings(bot: BotSettings) -> Tuple[Optional[str], Optional[str]]:
    if bot.codex_execution_mode == "full-auto":
        return "never", "workspace-write"
    if bot.codex_execution_mode == "danger-full-access":
        return "never", "danger-full-access"
    return None, None


def _auto_accept_server_requests(bot: BotSettings) -> bool:
    return bot.codex_execution_mode in {"full-auto", "danger-full-access"}
