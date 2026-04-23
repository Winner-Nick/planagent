"""iLink ClawBot protocol — constants, Pydantic v2 models, field helpers.

Sources consulted (group-chat field layout is not covered by the official
markdown spec, so we pick names that appear in at least two community SDKs):

- Official spec (no group field names):
  https://github.com/hao-ji-xing/openclaw-weixin/blob/main/weixin-bot-api.md
  @ 4a853693e63c63e987302b939487c0edac100caf
- Go SDK (daemon365/weixin-clawbot) @ 5fc1f1cbabd4e09aaa87ab198fa0e009ecb49e99
  types.go: `WeixinMessage` has top-level `group_id` and `session_id`.
- Python SDK (nightsailer/wechat-clawbot) @ 80e2e11418d2e745bfa9b1d215c9a791bec8d4f7
  src/wechat_clawbot/api/types.py: identical `group_id` / `session_id`.
  docs/ilink-protocol.md: "group_id non-empty -> group message; from_user_id
  is the sender's personal id".
- Go mock server (openilink/openilink-hub) @ 8cf13da08563b5f9b798d64e3e0e4e4ec58d9435
  internal/provider/ilink/mockserver/types.go: confirms `group_id`,
  `session_id`.
- JS demo (x1ah/wechat-ilink-demo) @ 5e0507b13b24a3c042936b7ac8fd9615d441d728
  bot.mjs: uses `from_user_id`, treats `*@im.bot` as bot senders.

Conclusion: use `group_id` as the canonical group identifier. No SDK exposes
an explicit at-mention list — at-bot detection must be done on the text
content (e.g. leading `@<nickname>`), which is what we do in `is_at_bot`.
Helpers below probe a few candidate field names defensively so the code
keeps working if the server later adds siblings like `group_user_id` /
`chatroom_id`.
"""

from __future__ import annotations

import base64
import secrets
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# --- Constants ---------------------------------------------------------------

# message_type
MESSAGE_TYPE_NONE = 0
MESSAGE_TYPE_USER = 1
MESSAGE_TYPE_BOT = 2

# message_state
MESSAGE_STATE_NEW = 0
MESSAGE_STATE_GENERATING = 1
MESSAGE_STATE_FINISH = 2

# item type
ITEM_TYPE_TEXT = 1
ITEM_TYPE_IMAGE = 2
ITEM_TYPE_VOICE = 3
ITEM_TYPE_FILE = 4
ITEM_TYPE_VIDEO = 5

# Per spec: /getupdates holds for up to 35s; clients use a slightly higher
# transport timeout.
LONGPOLL_SERVER_TIMEOUT_S = 35
LONGPOLL_CLIENT_TIMEOUT_S = 40

CHANNEL_VERSION = "1.0.2"


# --- Headers -----------------------------------------------------------------

def build_headers(bot_token: str) -> dict[str, str]:
    """Construct the auth headers every bot endpoint expects.

    X-WECHAT-UIN: base64 of 4 random bytes. Regenerated on each call so that
    sequential requests don't share a UIN (server uses it for routing /
    rate-limiting heuristics).
    """
    uin = base64.b64encode(secrets.token_bytes(4)).decode("ascii")
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {bot_token}",
        "X-WECHAT-UIN": uin,
    }


# --- Item payloads (discriminated by `type`) ---------------------------------

class TextItemPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    text: str | None = None


class CDNMedia(BaseModel):
    model_config = ConfigDict(extra="allow")
    encrypt_query_param: str | None = None
    aes_key: str | None = None
    encrypt_type: int | None = None


class ImageItemPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    media: CDNMedia | None = None
    thumb_media: CDNMedia | None = None
    url: str | None = None


class VoiceItemPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    media: CDNMedia | None = None
    text: str | None = None  # transcription
    playtime: int | None = None


class FileItemPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    media: CDNMedia | None = None
    file_name: str | None = None


class VideoItemPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    media: CDNMedia | None = None
    play_length: int | None = None


class Item(BaseModel):
    """Single message item. `type` discriminates the payload sub-object.

    We permit all payload fields to coexist (rather than a strict union) so
    unknown/additional types don't fail parsing.
    """

    model_config = ConfigDict(extra="allow")

    type: int
    text_item: TextItemPayload | None = None
    image_item: ImageItemPayload | None = None
    voice_item: VoiceItemPayload | None = None
    file_item: FileItemPayload | None = None
    video_item: VideoItemPayload | None = None


# --- Messages ----------------------------------------------------------------

class InboundMessage(BaseModel):
    """A message pulled from /getupdates.

    Group-chat fields: per the SDK research above, `group_id` is the
    canonical group identifier (non-empty for group messages, empty/missing
    for 1:1). `session_id` is also present. We model both and expose
    candidate-probing helpers at module level.
    """

    model_config = ConfigDict(extra="allow")

    from_user_id: str | None = None
    to_user_id: str | None = None
    message_type: int = MESSAGE_TYPE_NONE
    message_state: int | None = None
    context_token: str | None = None
    item_list: list[Item] = Field(default_factory=list)

    # Group-aware optional fields (see module docstring for source cites).
    group_id: str | None = None
    session_id: str | None = None

    # Other commonly-observed top-level fields, kept optional.
    seq: int | None = None
    message_id: int | None = None
    client_id: str | None = None
    create_time_ms: int | None = None


class GetUpdatesResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ret: int = 0
    errcode: int | None = None
    errmsg: str | None = None
    msgs: list[InboundMessage] = Field(default_factory=list)
    get_updates_buf: str = ""
    longpolling_timeout_ms: int | None = None


class OutboundItem(BaseModel):
    """Minimal outbound item — currently only text is wired up."""

    model_config = ConfigDict(extra="allow")

    type: int = ITEM_TYPE_TEXT
    text_item: TextItemPayload | None = None


class OutboundMessage(BaseModel):
    """Body of POST /ilink/bot/sendmessage under the `msg` key.

    `group_id` is included only when replying into a group. `context_token`
    must echo the inbound message's token exactly, otherwise the reply
    doesn't thread in the client UI.
    """

    model_config = ConfigDict(extra="allow")

    to_user_id: str
    message_type: int = MESSAGE_TYPE_BOT
    message_state: int = MESSAGE_STATE_FINISH
    context_token: str
    item_list: list[OutboundItem]
    group_id: str | None = None


class SendMessageResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ret: int = 0
    errcode: int | None = None
    errmsg: str | None = None


class QRCodeSession(BaseModel):
    model_config = ConfigDict(extra="allow")

    qrcode: str
    # May be a base64-encoded PNG string. We also expose a decoded bytes view.
    qrcode_img_content: str | None = None

    @property
    def qrcode_img_bytes(self) -> bytes | None:
        if not self.qrcode_img_content:
            return None
        try:
            return base64.b64decode(self.qrcode_img_content)
        except (ValueError, TypeError):
            return self.qrcode_img_content.encode("latin-1")


class LoggedInSession(BaseModel):
    model_config = ConfigDict(extra="allow")

    bot_token: str
    baseurl: str | None = None
    bot_user_id: str | None = None


# --- Candidate-field helpers -------------------------------------------------
#
# These try several field names and fall back to None. The intent is that
# callers can write `group_id(msg)` without caring whether the server calls
# the field `group_id` or `chatroom_id` in a future version.

_GROUP_ID_CANDIDATES = ("group_id", "chatroom_id", "room_id")
_SENDER_CANDIDATES = ("from_user_id", "sender_id", "user_id")


def _probe(msg: InboundMessage, names: tuple[str, ...]) -> str | None:
    extras = msg.model_extra or {}
    for name in names:
        val = getattr(msg, name, None)
        if val is None:
            val = extras.get(name)
        if isinstance(val, str) and val:
            return val
    return None


def is_group_message(msg: InboundMessage) -> bool:
    """A non-empty `group_id` (or alias) marks a group message."""
    return _probe(msg, _GROUP_ID_CANDIDATES) is not None


def group_id(msg: InboundMessage) -> str | None:
    """Return the group id if present."""
    return _probe(msg, _GROUP_ID_CANDIDATES)


def sender_id(msg: InboundMessage) -> str | None:
    """Return the originating user id (in groups: the individual sender)."""
    return _probe(msg, _SENDER_CANDIDATES)


def text_content(msg: InboundMessage) -> str | None:
    """Concatenate text from all text items. None if no text item found."""
    parts: list[str] = []
    for item in msg.item_list:
        if item.type == ITEM_TYPE_TEXT and item.text_item and item.text_item.text:
            parts.append(item.text_item.text)
    if not parts:
        return None
    return "\n".join(parts)


def is_at_bot(msg: InboundMessage, bot_user_id: str | None) -> bool:
    """Heuristic: text starts with `@<name>` token.

    The protocol has no documented explicit at-list in community SDKs, so
    we match any leading `@…` mention. If `bot_user_id` resolves to a
    display nickname you want to match specifically, callers can do stricter
    matching on top of `text_content(msg)`.
    """
    text = text_content(msg)
    if not text:
        return False
    stripped = text.lstrip()
    if not stripped.startswith("@"):
        return False
    if bot_user_id:
        # Accept either the explicit id or the token immediately after '@'.
        head = stripped[1:].split(None, 1)[0] if len(stripped) > 1 else ""
        # When bot_user_id is known, a mismatched mention is NOT for us —
        # otherwise `@someone_else ...` would trigger unsolicited replies.
        return bot_user_id in stripped or head == bot_user_id
    # No bot_user_id given — treat any leading @mention as an at-bot signal.
    return True


def dump_outbound(msg: OutboundMessage) -> dict[str, Any]:
    """Serialize, dropping None `group_id` so 1:1 sends don't carry the key."""
    data = msg.model_dump(exclude_none=True)
    return data
