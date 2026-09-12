from pathlib import Path
import tempfile
import unittest

from claude_native_bridge.client import NativeBridgeClient
from claude_native_bridge.settings import Settings


class FakeNative:
    instances = []

    def __init__(self, settings, home, model, effort, **kwargs):
        self.closed = False
        self.frames = []
        self.model = model
        self.effort = effort
        self.instances.append(self)

    def start(self):
        return self

    def exchange(self, content, request_id, **kwargs):
        self.frames.append(content)
        return {
            "sequence": len(self.frames),
            "request_id": request_id,
            "kind": "final",
            "text": "done",
        }

    def close(self):
        self.closed = True


def new_client(home):
    return NativeBridgeClient(
        hermes_home=home,
        settings=Settings(development_channels_accepted=True),
        native_factory=FakeNative,
    )


def request(messages, sid="test", **kwargs):
    return dict(
        model="claude-sonnet-5",
        messages=messages,
        extra_body={"hermes_session_id": sid} if sid else {},
        **kwargs,
    )


class ClientTests(unittest.TestCase):
    def setUp(self):
        FakeNative.instances = []
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.home = Path(self.folder.name)

    def test_discovery_is_lazy_and_sequential_history_reuses_native(self):
        c = new_client(self.home)
        self.addCleanup(c.close)
        self.assertEqual(FakeNative.instances, [])
        messages = [
            {"role": "system", "content": "fixture memory"},
            {"role": "user", "content": "first"},
        ]
        c.chat.completions.create(**request(messages))
        messages += [
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "second"},
        ]
        c.chat.completions.create(**request(messages))
        self.assertEqual(len(FakeNative.instances), 1)
        self.assertEqual(len(FakeNative.instances[0].frames), 2)
        self.assertIn("fixture memory", FakeNative.instances[0].frames[0])

    def test_same_session_id_in_different_clients_is_isolated(self):
        a = new_client(self.home)
        b = new_client(self.home)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        for c in (a, b):
            c.chat.completions.create(**request([{"role": "user", "content": "hello"}]))
        self.assertEqual(len(FakeNative.instances), 2)

    def test_auxiliary_calls_without_binding_are_one_shots(self):
        c = new_client(self.home)
        self.addCleanup(c.close)
        for _ in range(2):
            c.chat.completions.create(
                **request([{"role": "user", "content": "hello"}], None)
            )
        self.assertEqual(len(FakeNative.instances), 2)
        self.assertTrue(all(x.closed for x in FakeNative.instances))

    def test_changed_canonical_history_rebuilds_instead_of_appending(self):
        c = new_client(self.home)
        self.addCleanup(c.close)
        for text in ("old memory", "new memory"):
            c.chat.completions.create(
                **request(
                    [
                        {"role": "system", "content": text},
                        {"role": "user", "content": "hello"},
                    ]
                )
            )
        self.assertEqual(len(FakeNative.instances), 2)
        self.assertTrue(FakeNative.instances[0].closed)
        self.assertNotIn("old memory", FakeNative.instances[1].frames[0])

    def test_expired_native_binding_does_not_consume_session_capacity(self):
        c = new_client(self.home)
        self.addCleanup(c.close)
        for sid in ("a", "b"):
            c.chat.completions.create(
                **request([{"role": "user", "content": "hello"}], sid)
            )
        FakeNative.instances[0].close()
        c.chat.completions.create(**request([{"role": "user", "content": "new"}], "c"))
        self.assertEqual(len(FakeNative.instances), 3)

    def test_stream_is_buffered_once_and_usage_is_not_fabricated(self):
        c = new_client(self.home)
        self.addCleanup(c.close)
        stream = c.chat.completions.create(
            **request([{"role": "user", "content": "hello"}], stream=True)
        )
        chunks = list(stream)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].choices[0].delta.content, "done")
        self.assertIsNone(chunks[0].usage)
        c.close()
        c.close()
        self.assertTrue(c.is_closed)
        self.assertTrue(FakeNative.instances[0].closed)


class AsyncClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_auxiliary_async_facade_returns_awaitable_and_async_stream(self):
        FakeNative.instances = []
        with tempfile.TemporaryDirectory() as home:
            c = new_client(home)
            try:
                response = await c.chat.completions.create(
                    **request([{"role": "user", "content": "first"}], None)
                )
                self.assertEqual(response.choices[0].message.content, "done")
                stream = await c.chat.completions.create(
                    **request(
                        [{"role": "user", "content": "second"}], None, stream=True
                    )
                )
                self.assertEqual(
                    [x.choices[0].delta.content async for x in stream], ["done"]
                )
            finally:
                c.close()


if __name__ == "__main__":
    unittest.main()
