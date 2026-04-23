"""CLI: `python -m planagent.wechat.login` — QR scan login flow.

Renders the QR to the terminal (if possible) and saves a PNG fallback to
./qrcode.png. Persists the bot_token to ~/.planagent/credentials.json on
success.
"""

from __future__ import annotations

import asyncio
import base64
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


def _save_qr_png(session: QRCodeSession, path: Path) -> bool:
    """Try to write a PNG to disk. Returns True on success."""
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
        payload = session.qrcode

        print("Scan the QR code below in WeChat to log the bot in.\n")
        try:
            _render_qr_terminal(payload)
        except Exception:  # noqa: BLE001
            print("(Could not render QR to terminal.)")

        png_path = Path("./qrcode.png")
        if _save_qr_png(session, png_path):
            print(f"\nQR image also written to {png_path} (raw content from server).")

        print("\nWaiting for scan confirmation (up to 180s)…")
        try:
            logged_in = await client.poll_login(payload)
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
        # Silence pyflakes on unused helper when extending later.
        _ = base64
        return 0


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
