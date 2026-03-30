import unittest
from pathlib import Path

from telegram_bot_to_codex.codex import _AppServerSession
from telegram_bot_to_codex.config import BotSettings


class CodexClientTests(unittest.TestCase):
    def test_turn_start_params_include_model_and_effort(self) -> None:
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

