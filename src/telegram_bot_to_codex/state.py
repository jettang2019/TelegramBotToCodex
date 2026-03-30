from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._data: Dict[str, Dict[str, Any]] = {"offsets": {}, "threads": {}}

    async def load(self) -> None:
        async with self._lock:
            if not self.path.exists():
                self._data = {"offsets": {}, "threads": {}}
                return

            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._data = {
                "offsets": dict(raw.get("offsets", {})),
                "threads": dict(raw.get("threads", {})),
            }

    async def get_offset(self, bot_name: str) -> Optional[int]:
        async with self._lock:
            value = self._data["offsets"].get(bot_name)
            if isinstance(value, int):
                return value
            return None

    async def set_offset(self, bot_name: str, offset: int) -> None:
        async with self._lock:
            self._data["offsets"][bot_name] = offset
            self._write_locked()

    async def get_thread(self, bot_name: str, chat_id: int, workdir: Path) -> Optional[str]:
        async with self._lock:
            record = self._data["threads"].get(self._thread_key(bot_name, chat_id))
            if not isinstance(record, dict):
                return None
            if record.get("workdir") != str(workdir):
                return None
            thread_id = record.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                return thread_id
            return None

    async def peek_thread(self, bot_name: str, chat_id: int) -> Optional[str]:
        async with self._lock:
            record = self._data["threads"].get(self._thread_key(bot_name, chat_id))
            if not isinstance(record, dict):
                return None
            thread_id = record.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                return thread_id
            return None

    async def set_thread(self, bot_name: str, chat_id: int, workdir: Path, thread_id: str) -> None:
        async with self._lock:
            self._data["threads"][self._thread_key(bot_name, chat_id)] = {
                "thread_id": thread_id,
                "workdir": str(workdir),
            }
            self._write_locked()

    async def clear_thread(self, bot_name: str, chat_id: int) -> None:
        async with self._lock:
            self._data["threads"].pop(self._thread_key(bot_name, chat_id), None)
            self._write_locked()

    def _thread_key(self, bot_name: str, chat_id: int) -> str:
        return f"{bot_name}:{chat_id}"

    def _write_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(self._data, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temp_path.replace(self.path)
