import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from telegram_bot_to_codex.codex import _AppServerSession
from telegram_bot_to_codex.config import BotSettings


class CodexClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_start_params_include_model_and_effort(self) -> None:
        bot = BotSettings(
            name="demo",
            token="123:abc",
            workdir=Path("/tmp"),
            telegram_username="@demo",
            telegram_user_id=None,
            skip_git_repo_check=True,
            codex_execution_mode="full-auto",
            model="gpt-5.4",
            effort="xhigh",
        )
        session = _AppServerSession("codex", bot)

        params = session._turn_start_params("thread-1", "Reply with OK")

        self.assertEqual(params["threadId"], "thread-1")
        self.assertEqual(params["model"], "gpt-5.4")
        self.assertEqual(params["effort"], "xhigh")


class _ChunkStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class CodexStreamReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_stdout_loop_handles_long_lines(self) -> None:
        bot = BotSettings(
            name="demo",
            token="123:abc",
            workdir=Path("/tmp"),
            telegram_username="@demo",
            telegram_user_id=None,
            skip_git_repo_check=True,
            codex_execution_mode="full-auto",
            model=None,
            effort=None,
        )
        session = _AppServerSession("codex", bot)
        long_line = "x" * 100_000

        session.process = SimpleNamespace(
            stdout=_ChunkStream(
                [
                    long_line[:25_000].encode("utf-8"),
                    long_line[25_000:80_000].encode("utf-8"),
                    long_line[80_000:].encode("utf-8") + b"\n",
                ]
            ),
            stderr=None,
            returncode=None,
        )
        session._handle_stdout_line = AsyncMock()  # type: ignore[method-assign]
        session._fail_pending = Mock()  # type: ignore[method-assign]

        await session._read_stdout_loop()

        session._handle_stdout_line.assert_awaited_once_with(long_line)
