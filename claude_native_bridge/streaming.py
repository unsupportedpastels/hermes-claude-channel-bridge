"""Bounded, correlated native MessageDisplay batches (not synthesized tokens)."""

import json

MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_BATCHES = 16384


class TextBatches:
    def __init__(
        self,
        session_id,
        request_id,
        on_text=None,
        previous_messages=(),
        *,
        expected_prompt_id=None,
    ):
        self.session_id = session_id
        self.request_id = request_id
        self.on_text = on_text
        self.previous_messages = previous_messages
        self.prompt_id = expected_prompt_id
        self.turn_id = None
        self.messages = {}
        self.parts = []
        self.bytes = 0
        self.count = 0
        self.offset = 0
        self.partial = False

    def add(self, record):
        if (
            record.get("session_id") != self.session_id
            or record.get("request_id") != self.request_id
        ):
            raise ValueError("Uncorrelated native text batch")
        prompt = record.get("prompt_id")
        turn, message = record.get("turn_id"), record.get("message_id")
        index, final, delta = (
            record.get("index"),
            record.get("final"),
            record.get("delta"),
        )
        if (
            not isinstance(prompt, str)
            or not prompt
            or not isinstance(turn, str)
            or not turn
            or not isinstance(message, str)
            or not message
            or type(index) is not int
            or index < 0
            or type(final) is not bool
            or not isinstance(delta, str)
        ):
            raise ValueError("Invalid native text batch")
        if self.prompt_id is not None and prompt != self.prompt_id:
            raise ValueError("Uncorrelated native text prompt")
        if self.turn_id is not None and turn != self.turn_id:
            raise ValueError("Uncorrelated native text turn")
        if message in self.previous_messages:
            raise ValueError("Stale native text message from an earlier request")
        self.prompt_id = prompt
        self.turn_id = turn
        batches = self.messages.setdefault(message, [])
        item = (delta, final)
        if index < len(batches):
            if batches[index] != item:
                raise ValueError("Conflicting duplicate native text batch")
            return
        if index != len(batches) or (batches and batches[-1][1]):
            raise ValueError("Missing or out-of-order native text batch")
        if any(
            values and not values[-1][1]
            for key, values in self.messages.items()
            if key != message
        ):
            raise ValueError("Overlapping native text messages")
        self.bytes += len(delta.encode("utf-8"))
        self.count += 1
        if self.bytes > MAX_TEXT_BYTES or self.count > MAX_BATCHES:
            raise ValueError("Native text limit exceeded")
        batches.append(item)
        self.parts.append(delta)
        if delta and self.on_text:
            self.on_text(delta)

    def drain(self, runtime):
        if (runtime / "native-attribution-error").exists():
            raise ValueError("Native hook attribution failed")
        if (runtime / "native-text-error").exists():
            raise ValueError("Native text capture failed or exceeded its limit")
        path = runtime / "native-text.jsonl"
        if not path.exists():
            return
        self.partial = False
        with path.open("rb") as source:
            source.seek(self.offset)
            while True:
                line = source.readline(MAX_TEXT_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_TEXT_BYTES:
                    raise ValueError("Native text record too large")
                if not line.endswith(b"\n"):
                    self.partial = True
                    break  # A concurrent hook has not finished its append yet.
                self.add(json.loads(line))
                self.offset = source.tell()
        if self.offset > MAX_TEXT_BYTES:
            raise ValueError("Native text journal limit exceeded")

    def awaiting_final(self):
        """True while this journal may still be missing its end-of-message marker.

        A debounced final display batch and a concurrent hook append (a partial
        trailing line) both leave the record incomplete for now, so the caller
        may wait briefly before deciding the journal is final.
        """
        return self.partial or any(
            not values or not values[-1][1] for values in self.messages.values()
        )

    def finish(self, final_text=None):
        if self.partial:
            raise ValueError("Incomplete native text journal record")
        if any(not values or not values[-1][1] for values in self.messages.values()):
            raise ValueError("Missing final native text batch")
        text = "".join(self.parts)
        if final_text is not None and not self.messages:
            # Some native clients emit only the already-correlated Stop final.
            # Return it for the API's final remainder path; do not synthesize a batch.
            return final_text
        # Native Stop strips terminal display whitespace; never alter emitted text.
        if final_text is not None and text.rstrip() != final_text.rstrip():
            raise ValueError("Native final text conflicts with captured batches")
        return text
