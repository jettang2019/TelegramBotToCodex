from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional
from urllib import error, request


class TelegramApiError(RuntimeError):
    pass


class TelegramApiClient:
    API_ROOT = "https://api.telegram.org"

    async def get_me(self, token: str) -> Dict[str, Any]:
        response = await self._request(token, "getMe", {}, 15)
        result = response.get("result")
        if isinstance(result, dict):
            return result
        raise TelegramApiError("Telegram getMe response did not contain a result object")

    async def get_updates(
        self,
        token: str,
        offset: Optional[int],
        timeout_seconds: int,
    ) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "timeout": timeout_seconds,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        response = await self._request(token, "getUpdates", payload, timeout_seconds + 10)
        result = response.get("result")
        if isinstance(result, list):
            return result
        raise TelegramApiError("Telegram getUpdates response did not contain a result list")

    async def send_message(
        self,
        token: str,
        chat_id: int,
        text: str,
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        return await self._request(token, "sendMessage", payload, 20)

    async def edit_message_text(
        self,
        token: str,
        chat_id: int,
        message_id: int,
        text: str,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        return await self._request(token, "editMessageText", payload, 20)

    async def send_chat_action(self, token: str, chat_id: int, action: str = "typing") -> None:
        await self._request(token, "sendChatAction", {"chat_id": chat_id, "action": action}, 10)

    async def _request(
        self,
        token: str,
        method: str,
        payload: Optional[Dict[str, Any]],
        timeout_seconds: int,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self._request_sync,
            token,
            method,
            payload or {},
            timeout_seconds,
        )

    def _request_sync(
        self,
        token: str,
        method: str,
        payload: Dict[str, Any],
        timeout_seconds: int,
    ) -> Dict[str, Any]:
        url = f"{self.API_ROOT}/bot{token}/{method}"
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url=url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise TelegramApiError(f"Telegram HTTP {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise TelegramApiError(f"Telegram request failed: {exc.reason}") from exc

        if not data.get("ok"):
            description = data.get("description", "Telegram API returned ok=false")
            raise TelegramApiError(str(description))
        return data
