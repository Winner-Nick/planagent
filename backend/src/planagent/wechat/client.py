"""Async ClawBot HTTP client.

Covers: QR login handshake, long-poll for updates, and send_text. Retries
once on 5xx via tenacity; 4xx errors surface immediately.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .protocol import (
    CHANNEL_VERSION,
    ITEM_TYPE_TEXT,
    LONGPOLL_CLIENT_TIMEOUT_S,
    GetUpdatesResponse,
    LoggedInSession,
    OutboundItem,
    OutboundMessage,
    QRCodeSession,
    SendMessageResponse,
    TextItemPayload,
    build_headers,
    dump_outbound,
)

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"


class ClawBotError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"ClawBot HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


def _is_5xx(exc: BaseException) -> bool:
    return isinstance(exc, ClawBotError) and 500 <= exc.status < 600


class ClawBotClient:
    """Thin wrapper over `httpx.AsyncClient` pinned to the iLink base URL.

    The client is usable with or without a bot_token — login endpoints work
    with an empty Authorization header (server only checks token format).
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> ClawBotClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # --- Internal ---------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        headers = build_headers(token)

        async def _once() -> dict[str, Any]:
            resp = await self._http.request(
                method,
                self._url(path),
                headers=headers,
                params=params,
                json=json_body,
                timeout=timeout,
            )
            if resp.status_code >= 400:
                raise ClawBotError(resp.status_code, resp.text)
            ctype = resp.headers.get("content-type", "")
            if "application/json" not in ctype and not resp.text.startswith("{"):
                raise ClawBotError(resp.status_code, f"non-json reply: {resp.text[:200]}")
            return resp.json()

        async for attempt in AsyncRetrying(
            reraise=True,
            stop=stop_after_attempt(2),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=2.0),
            retry=retry_if_exception(_is_5xx),
        ):
            with attempt:
                return await _once()
        raise RuntimeError("unreachable")  # pragma: no cover

    # --- Public API -------------------------------------------------------

    async def get_login_qrcode(self) -> QRCodeSession:
        data = await self._request(
            "GET",
            "/ilink/bot/get_bot_qrcode",
            token="",
            params={"bot_type": 3},
        )
        return QRCodeSession.model_validate(data)

    async def _fetch_qrcode_status(self, qrcode_token: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/ilink/bot/get_qrcode_status",
            token="",
            params={"qrcode": qrcode_token},
        )

    async def poll_login(
        self,
        qrcode_token: str,
        timeout_s: int = 180,
        poll_interval_s: float = 2.0,
    ) -> LoggedInSession:
        deadline = asyncio.get_event_loop().time() + timeout_s
        while True:
            data = await self._fetch_qrcode_status(qrcode_token)
            status = data.get("status", "")
            if status == "confirmed":
                # bot_user_id is sometimes returned alongside the token,
                # sometimes only discoverable via the first /getupdates.
                return LoggedInSession.model_validate(
                    {
                        "bot_token": data.get("bot_token", ""),
                        "baseurl": data.get("baseurl"),
                        "bot_user_id": data.get("bot_user_id")
                        or data.get("user_id"),
                    }
                )
            if status in {"expired", "canceled", "cancelled"}:
                raise ClawBotError(0, f"qrcode {status}")
            if asyncio.get_event_loop().time() >= deadline:
                raise TimeoutError("QR login timed out")
            await asyncio.sleep(poll_interval_s)

    async def long_poll(self, bot_token: str, cursor: str = "") -> GetUpdatesResponse:
        body = {
            "get_updates_buf": cursor,
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        data = await self._request(
            "POST",
            "/ilink/bot/getupdates",
            token=bot_token,
            json_body=body,
            timeout=LONGPOLL_CLIENT_TIMEOUT_S,
        )
        return GetUpdatesResponse.model_validate(data)

    async def send_text(
        self,
        bot_token: str,
        *,
        to_user_id: str,
        text: str,
        context_token: str,
        group_id: str | None = None,
    ) -> SendMessageResponse:
        outbound = OutboundMessage(
            to_user_id=to_user_id,
            context_token=context_token,
            item_list=[OutboundItem(type=ITEM_TYPE_TEXT, text_item=TextItemPayload(text=text))],
            group_id=group_id,
        )
        body = {"msg": dump_outbound(outbound)}
        data = await self._request(
            "POST",
            "/ilink/bot/sendmessage",
            token=bot_token,
            json_body=body,
        )
        return SendMessageResponse.model_validate(data)
