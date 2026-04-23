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


def _scan_payload(session: QRCodeSession) -> str:
    """Pick the string to encode in the QR.

    Prefer `qrcode_img_content` (the WeChat scan URL); fall back to the
    polling token only if the server omits the URL — that fallback won't
    actually log anyone in, but it still produces a renderable code for
    diagnostics.
    """
    return session.qrcode_img_content or session.qrcode


async def run() -> int:
    async with ClawBotClient() as client:
        session = await client.get_login_qrcode()
        scan_url = _scan_payload(session)

        print("Scan the QR code below in WeChat to log the bot in.")
        if scan_url.startswith("http"):
            print(f"(encoded payload: {scan_url})\n")
        else:
            print()
        try:
            _render_qr_terminal(scan_url)
        except Exception:  # noqa: BLE001
            print("(Could not render QR to terminal.)")

        png_path = Path("./qrcode.png")
        if _save_qr_png(scan_url, png_path):
            print(f"\nQR image also written to {png_path}.")

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
