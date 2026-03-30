import asyncio
import tempfile
import unittest
from pathlib import Path

from telegram_bot_to_codex.codex import CodexResult
from telegram_bot_to_codex.config import AppSettings, BotSettings, ServiceConfig
from telegram_bot_to_codex.service import BridgeService, _stream_event_status_text
from telegram_bot_to_codex.state import StateStore


class ServiceStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_flushes_completed_lines_only(self) -> None:
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
                        model=None,
                        effort=None,
                    ),
                ),
            )
            state = StateStore(config.app.state_path)
            await state.load()
            service = BridgeService(config, state)

            sent_messages = []
            edited_messages = []
            next_message_id = 9000

            async def fake_send_reply(bot, chat_id, text, reply_to_message_id):
                nonlocal next_message_id
                next_message_id += 1
                sent_messages.append((chat_id, text, reply_to_message_id))
                return next_message_id

            async def fake_edit_status_message(bot, chat_id, message_id, text):
                edited_messages.append((chat_id, message_id, text))

            async def fake_send_chat_action(token, chat_id, action="typing"):
                return None

            async def fake_run_prompt(bot, prompt, thread_id, event_callback=None):
                if event_callback is not None:
                    await event_callback({"type": "turn.started"})
                    await event_callback(
                        {
                            "type": "item.started",
                            "item": {
                                "type": "command_execution",
                                "command": "bash -lc ls",
                            },
                        }
                    )
                    await event_callback(
                        {
                            "type": "item.agent_message.delta",
                            "item_id": "item-1",
                            "delta": "Line 1\nLine",
                        }
                    )
                    await event_callback(
                        {
                            "type": "item.agent_message.delta",
                            "item_id": "item-1",
                            "delta": " 2",
                        }
                    )
                    await event_callback(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "id": "item-1",
                                "text": "Line 1\nLine 2",
                            },
                        }
                    )
                    await event_callback({"type": "turn.completed"})
                return CodexResult(
                    thread_id="thread-1",
                    reply="Line 1\nLine 2",
                    duration_seconds=0.25,
                )

            service._send_reply = fake_send_reply  # type: ignore[method-assign]
            service._edit_status_message = fake_edit_status_message  # type: ignore[method-assign]
            service.telegram.send_chat_action = fake_send_chat_action  # type: ignore[method-assign]
            service.codex.run_prompt = fake_run_prompt  # type: ignore[method-assign]

            await service._handle_authorized_message(
                config.bots[0],
                {
                    "chat": {"id": 42},
                    "text": "Please update the docs",
                    "message_id": 7,
                },
            )

            self.assertEqual(
                sent_messages,
                [
                    (42, "Message received. Codex is starting now.", 7),
                    (42, "Line 1", None),
                ],
            )
            self.assertEqual(
                edited_messages,
                [
                    (42, 9001, "Codex started working on your request."),
                    (42, 9001, "Running command: bash -lc ls"),
                    (42, 9002, "Line 1\nLine 2"),
                    (42, 9001, "Codex finished processing your request."),
                ],
            )
            self.assertEqual(await state.peek_thread("demo", 42), "thread-1")


class ServiceHelperTests(unittest.TestCase):
    def test_stream_event_status_text_is_english(self) -> None:
        self.assertEqual(
            _stream_event_status_text(
                {
                    "type": "item.started",
                    "item": {
                        "type": "command_execution",
                        "command": "bash -lc ls",
                    },
                }
            ),
            "Running command: bash -lc ls",
        )
