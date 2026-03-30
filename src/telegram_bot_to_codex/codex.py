from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .config import BotSettings


class CodexExecutionError(RuntimeError):
    pass


CodexEventCallback = Callable[[Dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class CodexResult:
    thread_id: Optional[str]
    reply: str
    duration_seconds: float


class CodexClient:
    def __init__(self, codex_bin: str) -> None:
        self.codex_bin = codex_bin

    async def run_prompt(
        self,
        bot: BotSettings,
        prompt: str,
        thread_id: Optional[str],
        event_callback: Optional[CodexEventCallback] = None,
    ) -> CodexResult:
        started_at = time.monotonic()
        command = self._build_command(bot, prompt, thread_id)
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(bot.workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stderr_task = asyncio.create_task(self._read_stream_lines(process.stderr))
        next_thread_id = thread_id
        last_message: Optional[str] = None
        logs: List[str] = []

        assert process.stdout is not None
        while True:
            raw_line = await process.stdout.readline()
            if not raw_line:
                break

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logs.append(line)
                continue

            if event_callback is not None:
                await event_callback(event)

            next_thread_id, last_message = self._apply_event(
                event,
                next_thread_id=next_thread_id,
                last_message=last_message,
            )

        return_code = await process.wait()
        stderr_lines = await stderr_task
        stderr_text = "\n".join(stderr_lines).strip()

        if return_code != 0:
            detail = stderr_text or "\n".join(logs) or "Unknown Codex CLI failure"
            raise CodexExecutionError(detail)

        if not last_message:
            raise CodexExecutionError("Codex completed without a final agent message")

        return CodexResult(
            thread_id=next_thread_id,
            reply=last_message,
            duration_seconds=time.monotonic() - started_at,
        )

    async def _read_stream_lines(
        self,
        stream: Optional[asyncio.StreamReader],
    ) -> List[str]:
        if stream is None:
            return []

        lines: List[str] = []
        while True:
            raw_line = await stream.readline()
            if not raw_line:
                return lines
            lines.append(raw_line.decode("utf-8", errors="replace").rstrip())

    def _apply_event(
        self,
        event: Dict[str, Any],
        next_thread_id: Optional[str],
        last_message: Optional[str],
    ) -> tuple[Optional[str], Optional[str]]:
        event_type = event.get("type")
        if event_type == "thread.started":
            candidate = event.get("thread_id")
            if isinstance(candidate, str) and candidate:
                next_thread_id = candidate
        elif event_type == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    last_message = text.strip()
        return next_thread_id, last_message

    def _build_command(
        self,
        bot: BotSettings,
        prompt: str,
        thread_id: Optional[str],
    ) -> List[str]:
        if thread_id:
            command = [self.codex_bin, "exec", "resume", "--json"]
            command.extend(self._execution_mode_args(bot))
            if bot.skip_git_repo_check:
                command.append("--skip-git-repo-check")
            command.extend([thread_id, prompt])
            return command

        command = [self.codex_bin, "exec", "--json", "-C", str(bot.workdir)]
        command.extend(self._execution_mode_args(bot))
        if bot.skip_git_repo_check:
            command.append("--skip-git-repo-check")
        command.append(prompt)
        return command

    def _execution_mode_args(self, bot: BotSettings) -> List[str]:
        if bot.codex_execution_mode == "full-auto":
            return ["--full-auto"]
        if bot.codex_execution_mode == "danger-full-access":
            return ["--dangerously-bypass-approvals-and-sandbox"]
        return []
