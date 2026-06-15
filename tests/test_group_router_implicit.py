"""Router wiring/example tests for implicit addressing & spam (Task 7.7).

These exercise the *real* ``_handle_group_message`` / ``handle_user_message``
routing with a mocked aiogram ``Message`` and patched hot-path dependencies
(mirroring ``tests/test_group_routing.py`` style — no real LLM, network, or DB).
They pin the router's decision order, the single-write buffer invariant across
the explicit/implicit/ambient paths, unchanged DM and explicit behavior, the
implicit-reply logging, defensive fallthrough, observe-on-every-path, and the
burst reply-to-bot survival rule.

**Validates: Requirements 1.1, 1.6, 3.2, 3.4, 5.1, 5.2, 5.3, 5.4, 9.6, 10.3, 10.7, 10.14**
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from loguru import logger

import app.handlers.messages as messages_module


_BOT_IDENTITY = {"id": 999, "username": "ThinkMateBot", "name": "ThinkMate"}


def _make_group_message(
    text: str,
    *,
    chat_type: str = "supergroup",
    chat_id: int = -100,
    user_id: int = 111,
    full_name: str = "Alice",
    reply_to_message=None,
    entities=None,
) -> MagicMock:
    """Build a mocked aiogram Message for a group/supergroup update."""
    message = MagicMock()
    message.from_user = MagicMock()
    message.from_user.id = user_id
    message.from_user.full_name = full_name
    message.chat = MagicMock()
    message.chat.id = chat_id
    message.chat.type = chat_type
    message.text = text
    message.entities = entities
    message.reply_to_message = reply_to_message
    message.answer = AsyncMock()
    message.bot = MagicMock()
    # Real-message defaults so the non-conversational guard (forward/channel) is inert.
    message.sender_chat = None
    message.forward_origin = None
    message.forward_date = None
    message.is_automatic_forward = False
    return message


def _make_dm_message(text: str, *, user_id: int = 222) -> MagicMock:
    message = _make_group_message(
        text, chat_type="private", chat_id=user_id, user_id=user_id
    )
    return message


def _reply_from(user_id: int) -> MagicMock:
    """A reply_to_message whose author has the given id."""
    reply = MagicMock()
    reply.from_user = MagicMock()
    reply.from_user.id = user_id
    return reply


def _patch_common(implicit_gate, spam_burst_detector):
    """Patchers for enqueue/buffer/affinity/identity plus injected gate singletons."""
    return [
        patch.object(
            messages_module.user_task_manager,
            "enqueue_message",
            new_callable=AsyncMock,
        ),
        patch.object(
            messages_module.models, "add_message_to_buffer", new_callable=AsyncMock
        ),
        patch.object(messages_module.affinity_cache, "bump", new_callable=AsyncMock),
        patch.object(
            messages_module.affinity_cache,
            "get",
            new_callable=AsyncMock,
            return_value={"affinity": 0.5, "mode": "auto"},
        ),
        patch.object(
            messages_module,
            "_get_bot_identity",
            new_callable=AsyncMock,
            return_value=dict(_BOT_IDENTITY),
        ),
        patch.object(messages_module, "implicit_gate", implicit_gate),
        patch.object(messages_module, "spam_burst_detector", spam_burst_detector),
    ]


def _fresh_gate(*, decide_return=(False, "out_of_window"), cooldown=True):
    """A MagicMock ImplicitAddressGate with controllable decide/cooldown."""
    gate = MagicMock()
    gate.decide.return_value = decide_return
    gate.cooldown_elapsed.return_value = cooldown
    return gate


def _fresh_burst(*, observe_return=False):
    det = MagicMock()
    det.observe.return_value = observe_return
    return det


# ===========================================================================
# Decision order — implicit detector consulted before the ambient gate (Req 1.1)
# ===========================================================================

@pytest.mark.asyncio
async def test_non_explicit_consults_implicit_before_ambient():
    """A non-explicit message hits the implicit detector before the ambient gate."""
    db = MagicMock()
    message = _make_group_message("just chatting about lunch")

    gate = _fresh_gate(decide_return=(False, "out_of_window"))
    det = _fresh_burst()
    order = []
    gate.decide.side_effect = lambda *a, **k: (order.append("decide"), (False, "x"))[1]

    (p_enq, p_buf, p_bump, p_get, p_id, p_gate, p_det) = _patch_common(gate, det)
    with p_enq, p_buf, p_bump, p_get, p_id, p_gate, p_det, \
            patch.object(
                messages_module, "_maybe_ambient_chime", new_callable=AsyncMock
            ) as mock_chime:
        mock_chime.side_effect = lambda *a, **k: order.append("chime")
        await messages_module.handle_user_message(message, db)

        assert gate.decide.called, "implicit detector must be consulted"
        assert mock_chime.called, "non-implicit message must fall to the ambient gate"
        assert order == ["decide", "chime"], "decide must run before the ambient gate"
        # observe runs on every group message (Req 10.3).
        assert det.observe.called


# ===========================================================================
# Implicit reply — enqueue with no buffer write, cooldown commit, logging
# ===========================================================================

@pytest.mark.asyncio
async def test_implicit_reply_enqueues_no_buffer_marks_cooldown_and_logs():
    """An implicit reply enqueues reason=reply, writes no buffer, and logs chat id."""
    db = MagicMock()
    message = _make_group_message("yeah i think so too", chat_id=-100777)

    gate = _fresh_gate(decide_return=(True, "implicit"), cooldown=True)
    det = _fresh_burst()

    sink = []
    sink_id = logger.add(lambda m: sink.append(str(m)), level="DEBUG")
    try:
        (p_enq, p_buf, p_bump, p_get, p_id, p_gate, p_det) = _patch_common(gate, det)
        with p_enq as mock_enq, p_buf as mock_buf, p_bump, p_get, p_id, p_gate, p_det:
            await messages_module.handle_user_message(message, db)

            # Enqueued exactly once as a direct reply.
            assert mock_enq.call_count == 1
            _, kwargs = mock_enq.call_args
            assert kwargs["reason"] == "reply"
            # Single-write invariant: no buffer write on the implicit-reply path.
            assert not mock_buf.called, "implicit reply must not write the buffer (Req 3.4)"
            # Cooldown committed BEFORE enqueue (Req 3.3).
            assert gate.mark_implicit_reply.called
            # Counter advanced after the decision.
            assert gate.note_human_message.called
    finally:
        logger.remove(sink_id)

    # Implicit-reply decision logged with the chat id (Req 3.2).
    assert any("-100777" in line and "implicit" in line.lower() for line in sink), (
        "implicit-reply decision must be logged with the chat id"
    )


@pytest.mark.asyncio
async def test_implicit_classified_but_cooldown_not_elapsed_falls_to_ambient():
    """Req 4.1: implicit but cooldown not elapsed → hand off to the ambient gate."""
    db = MagicMock()
    message = _make_group_message("another follow up")

    gate = _fresh_gate(decide_return=(True, "implicit"), cooldown=False)
    det = _fresh_burst()

    (p_enq, p_buf, p_bump, p_get, p_id, p_gate, p_det) = _patch_common(gate, det)
    with p_enq as mock_enq, p_buf, p_bump, p_get, p_id, p_gate, p_det, \
            patch.object(
                messages_module, "_maybe_ambient_chime", new_callable=AsyncMock
            ) as mock_chime:
        await messages_module.handle_user_message(message, db)

        reply_calls = [
            c for c in mock_enq.call_args_list if c.kwargs.get("reason") == "reply"
        ]
        assert not reply_calls, "cooldown-blocked implicit must not direct-reply"
        assert not gate.mark_implicit_reply.called
        assert mock_chime.called, "must fall through to the ambient gate"


# ===========================================================================
# Explicit path unchanged (Req 5.2) + observe-on-every-path (Req 10.3)
# ===========================================================================

@pytest.mark.asyncio
async def test_explicit_path_enqueues_reply_no_buffer_no_implicit():
    """An explicit @mention enqueues a reply, bumps affinity, skips the detector."""
    db = MagicMock()
    message = _make_group_message("hey @ThinkMateBot how are you")

    gate = _fresh_gate()
    det = _fresh_burst()

    (p_enq, p_buf, p_bump, p_get, p_id, p_gate, p_det) = _patch_common(gate, det)
    with p_enq as mock_enq, p_buf as mock_buf, p_bump as mock_bump, p_get, p_id, \
            p_gate, p_det:
        await messages_module.handle_user_message(message, db)

        assert mock_enq.call_count == 1
        _, kwargs = mock_enq.call_args
        assert kwargs["reason"] == "reply"
        assert not mock_buf.called, "explicit path must not double-write the buffer"
        assert mock_bump.called, "explicit path keeps the +0.05 affinity bump"
        # Explicit returns before the implicit detector is consulted (Req 5.2).
        assert not gate.decide.called
        # observe still runs on the explicit path (Req 10.3).
        assert det.observe.called
        # Counter advanced on the explicit path too.
        assert gate.note_human_message.called


# ===========================================================================
# Ambient fallthrough — single-write invariant (Req 5.3, 5.4)
# ===========================================================================

@pytest.mark.asyncio
async def test_ambient_drop_writes_buffer_exactly_once():
    """A non-addressed dropped message is buffered exactly once by the gate."""
    db = MagicMock()
    message = _make_group_message("nothing special here")

    gate = _fresh_gate(decide_return=(False, "out_of_window"))
    det = _fresh_burst()

    (p_enq, p_buf, p_bump, p_get, p_id, p_gate, p_det) = _patch_common(gate, det)
    with p_enq as mock_enq, p_buf as mock_buf, p_bump, p_get, p_id, p_gate, p_det, \
            patch.object(
                messages_module.ambient_gate,
                "decide",
                return_value=(False, "no_trigger"),
            ):
        await messages_module.handle_user_message(message, db)

        # Drop path: buffer written exactly once, no direct reply enqueued.
        assert mock_buf.call_count == 1, "ambient drop must write the buffer once (Req 5.4)"
        reply_calls = [
            c for c in mock_enq.call_args_list if c.kwargs.get("reason") == "reply"
        ]
        assert not reply_calls


@pytest.mark.asyncio
async def test_ambient_pass_writes_via_enqueue_not_buffer():
    """A non-addressed passing message is enqueued (reason=ambient), not buffered."""
    db = MagicMock()
    message = _make_group_message("happy birthday everyone!!")

    gate = _fresh_gate(decide_return=(False, "out_of_window"))
    det = _fresh_burst()

    (p_enq, p_buf, p_bump, p_get, p_id, p_gate, p_det) = _patch_common(gate, det)
    with p_enq as mock_enq, p_buf as mock_buf, p_bump, p_get, p_id, p_gate, p_det, \
            patch.object(
                messages_module.ambient_gate, "decide", return_value=(True, "pass")
            ), \
            patch.object(messages_module.ambient_gate, "mark_chimed"):
        await messages_module.handle_user_message(message, db)

        ambient_calls = [
            c for c in mock_enq.call_args_list if c.kwargs.get("reason") == "ambient"
        ]
        assert len(ambient_calls) == 1, "ambient pass must enqueue once (reason=ambient)"
        assert not mock_buf.called, "ambient pass must not write the buffer (enqueue does)"


# ===========================================================================
# DM unchanged — no detector invoked (Req 5.1)
# ===========================================================================

@pytest.mark.asyncio
async def test_dm_message_skips_detectors_entirely():
    """A DM uses the private path: no spam scan, no implicit detector."""
    db = MagicMock()
    message = _make_dm_message("hey what's up")

    gate = _fresh_gate()
    det = _fresh_burst()

    (p_enq, p_buf, p_bump, p_get, p_id, p_gate, p_det) = _patch_common(gate, det)
    with p_enq as mock_enq, p_buf, p_bump, p_get, p_id, p_gate, p_det:
        await messages_module.handle_user_message(message, db)

        # DM enqueues positionally with the user id; no detectors consulted.
        assert mock_enq.call_count == 1
        args, _ = mock_enq.call_args
        assert args[1] == 222, "DM enqueues with the user id"
        assert not gate.decide.called, "DM must not invoke the implicit detector (Req 5.1)"
        assert not det.observe.called, "DM must not invoke the spam burst detector"


# ===========================================================================
# Defensive fallthrough — classifier/decide errors reach the ambient path
# ===========================================================================

@pytest.mark.asyncio
async def test_decide_raises_degrades_to_ambient():
    """Req 1.6: an exception from decide degrades to the ambient path."""
    db = MagicMock()
    message = _make_group_message("some normal chatter")

    gate = _fresh_gate()
    gate.decide.side_effect = RuntimeError("boom")
    det = _fresh_burst()

    (p_enq, p_buf, p_bump, p_get, p_id, p_gate, p_det) = _patch_common(gate, det)
    with p_enq, p_buf, p_bump, p_get, p_id, p_gate, p_det, \
            patch.object(
                messages_module, "_maybe_ambient_chime", new_callable=AsyncMock
            ) as mock_chime:
        await messages_module.handle_user_message(message, db)
        assert mock_chime.called, "decide failure must fall through to the ambient gate"


@pytest.mark.asyncio
async def test_burst_observe_raises_degrades_and_continues():
    """Req 10.14: a burst-detector error degrades to 'not burst' and routing continues."""
    db = MagicMock()
    message = _make_group_message("hello there friends")

    gate = _fresh_gate(decide_return=(False, "out_of_window"))
    det = _fresh_burst()
    det.observe.side_effect = RuntimeError("burst boom")

    (p_enq, p_buf, p_bump, p_get, p_id, p_gate, p_det) = _patch_common(gate, det)
    with p_enq, p_buf, p_bump, p_get, p_id, p_gate, p_det, \
            patch.object(
                messages_module, "_maybe_ambient_chime", new_callable=AsyncMock
            ) as mock_chime:
        await messages_module.handle_user_message(message, db)
        # Routing continues to the implicit/ambient path despite the observe error.
        assert gate.decide.called
        assert mock_chime.called


@pytest.mark.asyncio
async def test_mass_tag_scan_raises_degrades_to_not_spam():
    """Req 9.6: a mass-tag-spam scan error degrades to 'not spam' and routing continues."""
    db = MagicMock()
    message = _make_group_message("plain message no mentions")

    gate = _fresh_gate(decide_return=(False, "out_of_window"))
    det = _fresh_burst()

    (p_enq, p_buf, p_bump, p_get, p_id, p_gate, p_det) = _patch_common(gate, det)
    with p_enq, p_buf, p_bump, p_get, p_id, p_gate, p_det, \
            patch.object(
                messages_module, "is_mass_tag_spam", side_effect=RuntimeError("scan boom")
            ), \
            patch.object(
                messages_module, "_maybe_ambient_chime", new_callable=AsyncMock
            ) as mock_chime:
        await messages_module.handle_user_message(message, db)
        # decide receives is_spam=False (degraded) and routing reaches the gate.
        assert gate.decide.called
        _, kwargs = gate.decide.call_args
        assert kwargs["is_spam"] is False
        assert mock_chime.called


# ===========================================================================
# Burst-classified reply-to-bot still routes through the explicit path (Req 10.7)
# ===========================================================================

@pytest.mark.asyncio
async def test_burst_reply_to_bot_still_explicit():
    """Req 10.7: a burst message replying to the bot is still an Explicit_Address."""
    db = MagicMock()
    message = _make_group_message(
        "hi", reply_to_message=_reply_from(_BOT_IDENTITY["id"])
    )

    # Classify as a burst — yet the deliberate reply-to-bot must win.
    gate = _fresh_gate()
    det = _fresh_burst(observe_return=True)

    (p_enq, p_buf, p_bump, p_get, p_id, p_gate, p_det) = _patch_common(gate, det)
    with p_enq as mock_enq, p_buf as mock_buf, p_bump as mock_bump, p_get, p_id, \
            p_gate, p_det:
        await messages_module.handle_user_message(message, db)

        assert mock_enq.call_count == 1
        _, kwargs = mock_enq.call_args
        assert kwargs["reason"] == "reply", "reply-to-bot survives the burst (Req 10.7)"
        assert not mock_buf.called
        assert mock_bump.called, "explicit path keeps the affinity bump"
        # Explicit path returns before the implicit detector.
        assert not gate.decide.called
