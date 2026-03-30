from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .config import BotSettings


class CodexExecutionError(RuntimeError):
    pass


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
    ) -> CodexResult:
        started_at = time.monotonic()
        command = self._build_command(bot, prompt, thread_id)
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(bot.workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        next_thread_id = thread_id
        last_message: Optional[str] = None
        logs: List[str] = []

        for raw_line in stdout_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logs.append(line)
                continue

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

        if process.returncode != 0:
            detail = stderr_text or "\n".join(logs) or "Unknown Codex CLI failure"
            raise CodexExecutionError(detail)

        if not last_message:
            raise CodexExecutionError("Codex completed without a final agent message")

        return CodexResult(
            thread_id=next_thread_id,
            reply=last_message,
            duration_seconds=time.monotonic() - started_at,
        )

    def _build_command(
        self,
        bot: BotSettings,
        prompt: str,
        thread_id: Optional[str],
    ) -> List[str]:
        if thread_id:
            command = [self.codex_bin, "exec", "resume", "--json"]
            if bot.skip_git_repo_check:
                command.append("--skip-git-repo-check")
            command.extend([thread_id, prompt])
            return command

        command = [self.codex_bin, "exec", "--json", "-C", str(bot.workdir)]
        if bot.skip_git_repo_check:
            command.append("--skip-git-repo-check")
        command.append(prompt)
        return command
