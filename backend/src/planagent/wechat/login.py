"""CLI: `python -m planagent.wechat.login` — QR scan login flow.

Renders the QR to the terminal and saves a PNG fallback to ./qrcode.png.
Persists the bot_token to ~/.planagent/credentials.json on success.

Important: the server's `qrcode` field is the *polling token*, not the
payload to encode in the QR image. The payload WeChat's scanner expects
is `qrcode_img_content`, which despite its name is typically a
`liteapp.weixin.qq.com/q/...` URL that opens the authorization page.
Encoding the polling token gives users a QR that WeChat reads as plain
text (they just see a hex string), which is why we use the URL.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import qrcode

from .client import ClawBotClient
from .credentials import save_credentials
from .protocol import QRCodeSession


def _redact(token: str) -> str:
    if not token:
        return "<empty>"
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}…{token[-4:]}"


def _render_qr_terminal(payload: str) -> None:
    qr = qrcode.QRCode(border=1)
    qr.add_data(payload)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def _save_qr_png(payload: str, path: Path) -> bool:
    """Write a fresh PNG of the scan URL to disk.

    PNG rendering requires Pillow; if unavailable we skip silently.
    """
    try:
        img = qrcode.make(payload)
        img.save(str(path))
        return True
    except Exception:  # noqa: BLE001 — PNG export is a nice-to-have
        return False


def _scan_payload(session: QRCodeSession) -> str | None:
    """Pick the string to encode in the QR.

    The server's `qrcode_img_content` can be one of two things depending
    on deployment/bot_type: a scannable URL (typically
    https://liteapp.weixin.qq.com/q/…?qrcode=…) or a base64-encoded PNG
    of a QR the server already rendered. Only the URL form can be
    re-encoded into a terminal QR code — base64 image bytes would just
    produce an un-scannable blob. We detect the URL case; otherwise
    signal None so callers fall back to the server-rendered PNG.
    """
    payload = session.qrcode_img_content
    if payload and payload.startswith(("http://", "https://")):
        return payload
    return None


def _save_server_png(session: QRCodeSession, path: Path) -> bool:
    """Write the server-provided QR PNG to disk when the payload is base64."""
    raw = session.qrcode_img_bytes
    if not raw:
        return False
    try:
        path.write_bytes(raw)
        return True
    except OSError:
        return False


async def run() -> int:
    async with ClawBotClient() as client:
        session = await client.get_login_qrcode()
        scan_url = _scan_payload(session)

        print("Scan the QR code below in WeChat to log the bot in.\n")
        if scan_url is not None:
            try:
                _render_qr_terminal(scan_url)
            except Exception:  # noqa: BLE001
                print("(Could not render QR to terminal.)")
            # Log the token shape without leaking the live scan credential:
            # anyone with the full URL in a shared log/CI console could
            # complete the auth instead of the operator.
            print(f"\n(scan URL length={len(scan_url)}; token redacted)")
            png_path = Path("./qrcode.png")
            if _save_qr_png(scan_url, png_path):
                print(f"QR image also written to {png_path}.")
        else:
            # The server gave us a base64 PNG; re-encoding it as a QR
            # would yield an un-scannable glyph. Save the server's image
            # and point the operator at it.
            png_path = Path("./qrcode.png")
            if _save_server_png(session, png_path):
                print(f"Server supplied a pre-rendered QR at {png_path} — open and scan.")
            else:
                print("Server returned no usable QR payload (neither URL nor image).")

        print("\nWaiting for scan confirmation (up to 180s)…")
        try:
            logged_in = await client.poll_login(session.qrcode)
        except Exception as exc:  # noqa: BLE001
            print(f"Login failed: {exc}", file=sys.stderr)
            return 1

        payload_out = {
            "bot_token": logged_in.bot_token,
            "baseurl": logged_in.baseurl,
            "bot_user_id": logged_in.bot_user_id,
        }
        save_credentials({k: v for k, v in payload_out.items() if v is not None})

        print("\nLogged in.")
        print(f"  bot_token : {_redact(logged_in.bot_token)}")
        print(f"  baseurl   : {logged_in.baseurl or '(default)'}")
        if logged_in.bot_user_id:
            print(f"  bot_user_id: {logged_in.bot_user_id}")
        return 0


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
