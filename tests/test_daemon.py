"""The daemon end to end over a real unix socket, against a fake model."""
import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from pathfinder.daemon import SOCKET, Daemon
from pathfinder.ledger import Ledger
from pathfinder.llm import LLMError


def fake_model(reply="Hello there."):
    def factory(cfg, cancel=None):
        factory.cancels.append(cancel)
        def step(messages):
            step.seen.append([dict(m) for m in messages])
            for word in reply.split(" "):
                yield ("text", word + " ")
            yield ("usage", {"prompt_tokens": 10, "completion_tokens": 3})
        step.seen = factory.seen
        return step
    factory.seen = []
    factory.cancels = []
    return factory


def blocked_model(cfg, cancel=None):
    def step(messages):
        raise LLMError(402, "this model is not included in your free usage")
        yield  # pragma: no cover - makes this a generator like the real adapter
    return step


class Client:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    @classmethod
    async def open(cls, home):
        return cls(*await asyncio.open_unix_connection(str(Path(home) / SOCKET)))

    def send(self, obj):
        self.writer.write((json.dumps(obj) + "\n").encode() if isinstance(obj, dict) else obj)

    async def recv(self):
        line = await asyncio.wait_for(self.reader.readline(), 5)
        assert line, "connection closed"
        return json.loads(line)

    async def until(self, type_):
        seen = []
        while True:
            msg = await self.recv()
            seen.append(msg)
            if msg["type"] == type_:
                return seen

    def close(self):
        self.writer.close()


class DaemonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_with(self, factory, scenario):
        async def main():
            daemon = Daemon(self.home, step_factory=factory)
            await daemon.start()
            try:
                return await scenario(daemon)
            finally:
                await daemon.close()
        return asyncio.run(main())

    def events(self):
        return list(Ledger(self.home).read())

    def test_daemon_round_trip(self):
        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            attached = await c.recv()
            c.send({"type": "say", "text": "hi"})
            seen = await c.until("turn.end")
            c.close()
            return attached, seen

        attached, seen = self.run_with(fake_model("Hello there."), scenario)
        self.assertEqual(attached["type"], "attached")
        self.assertFalse(attached["resumed"])
        self.assertEqual([m["type"] for m in seen], ["turn.start", "delta", "delta", "turn.end"])
        self.assertEqual("".join(m["text"] for m in seen if m["type"] == "delta"), "Hello there. ")
        self.assertEqual(seen[-1]["kind"], "answer")
        types = [e["type"] for e in self.events()]
        self.assertEqual(types, ["session_start", "user", "turn_start", "assistant", "turn_end"])
        user = self.events()[1]
        self.assertEqual((user["origin"], user["trust"], user["text"]), ("user", "user", "hi"))

    def test_second_turn_sees_the_first(self):
        factory = fake_model("ok")

        async def scenario(daemon):
            c = await Client.open(self.home)
            for text in ("first", "second"):
                c.send({"type": "say", "text": text})  # say without attach attaches implicitly
                await c.until("turn.end")
            c.close()

        self.run_with(factory, scenario)
        roles = [(m["role"], m["content"].strip()) for m in factory.seen[-1][1:]]
        self.assertEqual(roles, [("user", "first"), ("assistant", "ok"), ("user", "second")])

    def test_daemon_survives_model_402(self):
        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "hi"})
            first = await c.until("turn.end")
            daemon.step_factory = fake_model("back")  # credit returns; same process, same socket
            c.send({"type": "say", "text": "again"})
            second = await c.until("turn.end")
            c.close()
            return first, second

        first, second = self.run_with(blocked_model, scenario)
        self.assertEqual(first[-1]["kind"], "error")
        self.assertTrue(any(m["type"] == "error" and "free usage" in m["message"] for m in first))
        self.assertEqual(second[-1]["kind"], "answer")
        errors = [e for e in self.events() if e["type"] == "error"]
        self.assertEqual((len(errors), errors[0]["status"]), (1, 402))

    def test_unset_model_is_an_error_not_a_crash(self):
        """A fresh install has no model configured; the real step factory must say so."""
        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "hi"})
            seen = await c.until("turn.end")
            c.close()
            return seen

        seen = self.run_with(None, scenario)  # None = the default factory, no config.json
        self.assertEqual(seen[-1]["kind"], "error")
        self.assertTrue(any(m["type"] == "error" and "model" in m["message"].lower() for m in seen))

    def test_turn_gets_a_cancel_handle(self):
        """The seam partition 2 builds interrupt and steer on: every turn owns an Event that
        reaches the step factory (and, in the real factory, the HTTP stream)."""
        import threading
        factory = fake_model("ok")

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "hi"})
            await c.until("turn.end")
            c.close()
            return next(iter(daemon.sessions.values())).cancel

        held = self.run_with(factory, scenario)
        self.assertIsInstance(held, threading.Event)
        self.assertIs(factory.cancels[-1], held)

    def test_exhausted_turn_still_records_what_was_said(self):
        def talkative(cfg, cancel=None):
            def step(messages):
                yield ("text", "working ")
                yield ("tool_calls", [{"id": "c1", "name": "anything", "arguments": {}}])
            return step

        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "say", "text": "go"})
            seen = await c.until("turn.end")
            c.close()
            return seen

        seen = self.run_with(talkative, scenario)
        self.assertEqual(seen[-1]["kind"], "exhausted")
        said = [e for e in self.events() if e["type"] == "assistant"]
        self.assertEqual(len(said), 1)
        self.assertEqual(said[0]["text"], "working " * 8)  # the user saw 8 steps of it; history must too

    def test_attach_with_a_non_string_session_keeps_the_connection(self):
        async def scenario(daemon):
            c = await Client.open(self.home)
            c.send({"type": "attach", "session": {"x": 1}})
            attached = await c.recv()
            c.send({"type": "say", "text": "hi"})
            seen = await c.until("turn.end")
            c.close()
            return attached, seen

        attached, seen = self.run_with(fake_model("ok"), scenario)
        self.assertEqual(attached["type"], "attached")
        self.assertEqual(seen[-1]["kind"], "answer")

    def test_daemon_unknown_line_keeps_connection(self):
        async def scenario(daemon):
            c = await Client.open(self.home)
            replies = []
            for bad in (b"not json\n", b'{"type": "dance"}\n', b'["a list"]\n', b'{"type": "say", "text": " "}\n'):
                c.send(bad)
                replies.append(await c.recv())
            c.send({"type": "say", "text": "still here?"})
            seen = await c.until("turn.end")
            c.close()
            return replies, seen

        replies, seen = self.run_with(fake_model("yes"), scenario)
        self.assertEqual([r["type"] for r in replies], ["error"] * 4)
        self.assertEqual(seen[-1]["kind"], "answer")

    def test_socket_perms(self):
        async def scenario(daemon):
            return stat.S_IMODE(os.stat(self.home / SOCKET).st_mode)

        self.assertEqual(self.run_with(fake_model(), scenario), 0o600)

    def test_second_daemon_refuses_a_live_socket(self):
        async def scenario(daemon):
            with self.assertRaises(RuntimeError):
                await Daemon(self.home, step_factory=fake_model()).start()

        self.run_with(fake_model(), scenario)

    def test_daemon_resumes_after_state_deleted(self):
        async def talk(daemon):
            c = await Client.open(self.home)
            c.send({"type": "attach"})
            attached = await c.recv()
            c.send({"type": "say", "text": "remember the word plum"})
            await c.until("turn.end")
            c.close()
            return attached

        first = self.run_with(fake_model("noted"), talk)
        for leftover in self.home.glob("state.db*"):
            leftover.unlink()  # the derived state is gone; the ledger is all that is left

        factory = fake_model("plum")
        second = self.run_with(factory, talk)
        self.assertTrue(second["resumed"])
        self.assertEqual(second["session"], first["session"])
        self.assertEqual(second["turns"], 1)
        history = [m["content"].strip() for m in factory.seen[-1][1:]]
        self.assertEqual(history, ["remember the word plum", "noted", "remember the word plum"])


if __name__ == "__main__":
    unittest.main()
