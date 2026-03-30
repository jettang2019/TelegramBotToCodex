import tempfile
import unittest
from pathlib import Path

from telegram_bot_to_codex.state import StateStore


class StateStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_thread_round_trip_respects_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            store = StateStore(state_path)
            await store.load()

            workdir_a = Path(temp_dir) / "repo-a"
            workdir_b = Path(temp_dir) / "repo-b"

            await store.set_thread("demo", 42, workdir_a, "thread-1")

            self.assertEqual(await store.get_thread("demo", 42, workdir_a), "thread-1")
            self.assertIsNone(await store.get_thread("demo", 42, workdir_b))

    async def test_offsets_persist_to_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            store = StateStore(state_path)
            await store.load()
            await store.set_offset("demo", 123)

            reloaded = StateStore(state_path)
            await reloaded.load()

            self.assertEqual(await reloaded.get_offset("demo"), 123)
