import asyncio
import tempfile
import unittest
from pathlib import Path

from telegram_bot_to_codex.config import AppSettings, BotSettings, ServiceConfig
from telegram_bot_to_codex.service import BridgeService, _processing_status_text
from telegram_bot_to_codex.state import StateStore


class ServiceHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_processing_updates_send_periodic_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workdir = root / "repo"
            workdir.mkdir()

            config = ServiceConfig(
                app=AppSettings(
                    codex_bin="codex",
                    state_path=root / "state.json",
                    poll_timeout_seconds=30,
                    log_level="INFO",
                ),
                bots=(
                    BotSettings(
                        name="demo",
                        token="123:abc",
                        workdir=workdir,
                        telegram_username="@demo",
                        telegram_user_id=None,
                        skip_git_repo_check=True,
                        codex_execution_mode="full-auto",
                    ),
                ),
            )
            state = StateStore(config.app.state_path)
            await state.load()
            service = BridgeService(config, state)

            sent_messages = []

            async def fake_send_reply(bot, chat_id, text, reply_to_message_id):
                sent_messages.append((chat_id, text, reply_to_message_id))

            service._send_reply = fake_send_reply  # type: ignore[method-assign]

            stop_event = asyncio.Event()
            task = asyncio.create_task(
                service._send_processing_updates(
                    bot=config.bots[0],
                    chat_id=42,
                    stop_event=stop_event,
                    interval_seconds=0.01,
                )
            )
            await asyncio.sleep(0.025)
            stop_event.set()
            await task

            self.assertGreaterEqual(len(sent_messages), 2)
            self.assertEqual(sent_messages[0][1], _processing_status_text(1))
            self.assertEqual(sent_messages[1][1], _processing_status_text(1))


class ServiceHelperTests(unittest.TestCase):
    def test_processing_status_text_is_english(self) -> None:
        self.assertEqual(
            _processing_status_text(20),
            "Still processing your request. Codex has been working for about 20 seconds.",
        )
