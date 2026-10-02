"""A native MCP read can split one request into multiple assistant messages."""
import pytest
from claude_native_bridge.streaming import TextBatches


def record(message, text, *, final=True, index=0):
    return {"session_id": "s", "request_id": "r", "prompt_id": "p", "turn_id": "t",
            "message_id": message, "index": index, "final": final, "delta": text}


def test_stop_matches_last_message_after_native_image_read_without_losing_preamble():
    emitted = []
    batches = TextBatches("s", "r", emitted.append)
    batches.add(record("before-reader", "I will view the image. "))
    batches.add(record("answer", "IMAGE-123"))
    assert batches.finish("IMAGE-123") == "I will view the image. IMAGE-123"
    assert "".join(emitted) == batches.finish("IMAGE-123")


def test_stop_cannot_match_an_earlier_message_or_arbitrary_suffix():
    for wrong in ("I will view the image. ", "123", "different"):
        batches = TextBatches("s", "r")
        batches.add(record("before-reader", "I will view the image. "))
        batches.add(record("answer", "IMAGE-123"))
        with pytest.raises(ValueError, match="conflicts"):
            batches.finish(wrong)


def test_multiple_messages_still_require_complete_final_batches():
    batches = TextBatches("s", "r")
    batches.add(record("before-reader", "I will view the image. "))
    batches.add(record("answer", "IMAGE-123", final=False))
    with pytest.raises(ValueError, match="Missing final"):
        batches.finish("IMAGE-123")
