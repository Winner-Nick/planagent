"""Protocol-layer tests. Fixture JSONs are realistic hand-authored payloads
(not mocks of HTTP); they mirror what /getupdates returns per the official
spec plus community SDK schemas cited in protocol.py.
"""

from __future__ import annotations

import base64

from planagent.wechat.protocol import (
    GetUpdatesResponse,
    InboundMessage,
    build_headers,
    group_id,
    is_at_bot,
    is_group_message,
    sender_id,
    text_content,
)

# ---------------------------------------------------------------------------
# Realistic inbound payload samples
# ---------------------------------------------------------------------------

DIRECT_TEXT = {
    "ret": 0,
    "get_updates_buf": "AAABCDEF==",
    "longpolling_timeout_ms": 35000,
    "msgs": [
        {
            "seq": 101,
            "message_id": 42,
            "from_user_id": "o9cq800kumabcd@im.wechat",
            "to_user_id": "e06c1ceea05e@im.bot",
            "message_type": 1,
            "message_state": 2,
            "context_token": "AARzJWAFAAABAAAAAAAp",
            "item_list": [
                {"type": 1, "text_item": {"text": "hello bot"}},
            ],
        }
    ],
}

DIRECT_IMAGE = {
    "from_user_id": "o9cq800kumabcd@im.wechat",
    "to_user_id": "e06c1ceea05e@im.bot",
    "message_type": 1,
    "context_token": "ctx-img-1",
    "item_list": [
        {
            "type": 2,
            "image_item": {
                "media": {
                    "encrypt_query_param": "abc",
                    "aes_key": "def",
                    "encrypt_type": 1,
                },
                "url": "https://cdn.example/pic.jpg",
            },
        }
    ],
}

GROUP_TEXT = {
    "from_user_id": "o9cq800kumuser1@im.wechat",
    "to_user_id": "e06c1ceea05e@im.bot",
    "group_id": "grp_20250423_001@im.chatroom",
    "session_id": "sess-xyz",
    "message_type": 1,
    "message_state": 2,
    "context_token": "ctx-grp-1",
    "item_list": [
        {"type": 1, "text_item": {"text": "@planagent remind me to ship PR-B"}},
    ],
}


# ---------------------------------------------------------------------------
# Direct messages
# ---------------------------------------------------------------------------

def test_parse_direct_text_message() -> None:
    resp = GetUpdatesResponse.model_validate(DIRECT_TEXT)
    assert resp.ret == 0
    assert resp.get_updates_buf == "AAABCDEF=="
    assert len(resp.msgs) == 1
    msg = resp.msgs[0]
    assert msg.context_token == "AARzJWAFAAABAAAAAAAp"
    assert text_content(msg) == "hello bot"
    assert is_group_message(msg) is False
    assert group_id(msg) is None
    assert sender_id(msg) == "o9cq800kumabcd@im.wechat"


def test_parse_image_has_no_text() -> None:
    msg = InboundMessage.model_validate(DIRECT_IMAGE)
    assert text_content(msg) is None
    # Extra payload fields preserved via extra="allow".
    assert msg.item_list[0].image_item is not None
    assert msg.item_list[0].image_item.url == "https://cdn.example/pic.jpg"


# ---------------------------------------------------------------------------
# Group messages
# ---------------------------------------------------------------------------

def test_parse_group_text_message() -> None:
    msg = InboundMessage.model_validate(GROUP_TEXT)
    assert is_group_message(msg) is True
    assert group_id(msg) == "grp_20250423_001@im.chatroom"
    # In groups, from_user_id is still the individual sender.
    assert sender_id(msg) == "o9cq800kumuser1@im.wechat"
    assert "@planagent" in (text_content(msg) or "")


def test_is_at_bot_by_leading_mention() -> None:
    msg = InboundMessage.model_validate(GROUP_TEXT)
    assert is_at_bot(msg, bot_user_id=None) is True
    assert is_at_bot(msg, bot_user_id="planagent") is True

    plain = InboundMessage.model_validate(
        {
            "from_user_id": "u@im.wechat",
            "message_type": 1,
            "item_list": [{"type": 1, "text_item": {"text": "just chatting"}}],
        }
    )
    assert is_at_bot(plain, bot_user_id="planagent") is False


def test_is_at_bot_false_when_mention_does_not_match_bot_id() -> None:
    """`@someone_else ...` in a group must NOT count as an at-bot mention
    once the bot's own user_id is known — otherwise we'd reply to other
    users' traffic.
    """
    other_mention = InboundMessage.model_validate(
        {
            "from_user_id": "user1@im.wechat",
            "group_id": "g1@im.chatroom",
            "message_type": 1,
            "item_list": [
                {"type": 1, "text_item": {"text": "@alice have you seen the PR?"}}
            ],
        }
    )
    assert is_at_bot(other_mention, bot_user_id="planagent") is False
    # Without a known bot_user_id we fall back to the permissive heuristic.
    assert is_at_bot(other_mention, bot_user_id=None) is True


def test_group_id_falls_back_to_candidate_field_names() -> None:
    """If the server ever renames to `chatroom_id`, our probe still finds it."""
    msg = InboundMessage.model_validate(
        {
            "from_user_id": "u@im.wechat",
            "message_type": 1,
            "chatroom_id": "legacy_room_1",
            "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
        }
    )
    assert is_group_message(msg) is True
    assert group_id(msg) == "legacy_room_1"


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------

def test_build_headers_structure_and_rotation() -> None:
    h1 = build_headers("tok-abc")
    assert h1["Content-Type"] == "application/json"
    assert h1["AuthorizationType"] == "ilink_bot_token"
    assert h1["Authorization"] == "Bearer tok-abc"

    uin1 = h1["X-WECHAT-UIN"]
    decoded = base64.b64decode(uin1)
    assert len(decoded) == 4  # 4 random bytes per spec

    # Vanishingly unlikely to repeat across two calls (2^32 space).
    rotations = {build_headers("tok-abc")["X-WECHAT-UIN"] for _ in range(4)}
    assert len(rotations) > 1


def test_build_headers_omits_authorization_when_token_empty() -> None:
    """Pre-login calls pass no bot_token. Emitting "Bearer " would be
    rejected by httpx as an illegal header value.
    """
    h = build_headers("")
    assert "Authorization" not in h
    assert h["AuthorizationType"] == "ilink_bot_token"
    assert "X-WECHAT-UIN" in h
