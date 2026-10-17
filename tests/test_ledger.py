import json
import os
import re
import shlex
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pathfinder import keychain as keychain_mod
from pathfinder import ledger as ledger_mod
from pathfinder import tools as tools_mod
from pathfinder.ledger import Ledger
from pathfinder.state import State


class LedgerClosedVocabTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_ledger_closed_vocab(self):
        with self.assertRaises(ValueError):
            self.ledger.append("not_a_type")
        with self.assertRaises(ValueError):
            self.ledger.append("user", origin="not_an_origin")
        with self.assertRaises(ValueError):
            self.ledger.append("user", trust="not_a_trust")

    def test_reserved_field_collision_rejected(self):
        with self.assertRaises(ValueError):
            self.ledger.append("user", seq=999)

    def test_v2_types_append_with_their_fields(self):
        """The partition 1 contract's Data section: every new type appends; every one carries `clone`."""
        fields = {
            "result": {"step": 1, "call_id": "c1", "tool": "read_file", "ok": True, "body": {"a": 1},
                       "cut": {"kept": 1, "of": 2}, "gate": 3},
            "gate": {"step": 1, "call_id": "c1", "tool": "write_file", "args": {"path": "x"}, "effect": "write",
                     "brief": "b"},
            "resume": {"gate": 3, "decision": "approved", "by": "socket"},
            "summary": {"first": 1, "upto": 2, "text": "t", "handles": ["r:1"], "model": "p:m", "usage": {}},
            "spawn": {"parent": "", "preset": "root", "brief": "", "model": "", "budget": 0},
            "progress": {"parent": "root", "text": "t"},
            "return": {"parent": "root", "status": "done", "text": "t", "handles": []},
            "thought": {"step": 1, "plan": "p", "of": 2}, "self_note": {"kind": "k", "text": "t", "evidence": []},
            "triage": {"signal_key": "k", "action": "a", "urgent": False, "line": "l", "model": "p:m"},
            "task_open": {"task": "t", "goal": "g", "trigger": "x", "budget": 1, "end": 0},
            "task_wake": {"task": "t", "why": "w"}, "task_update": {"task": "t", "state": "s"},
            "task_close": {"task": "t", "why": "w"},
        }
        for type_, data in fields.items():
            with self.subTest(type_):
                event = self.ledger.append(type_, clone="root", **data)
                self.assertEqual({k: event[k] for k in data}, data)
                self.assertEqual(event["clone"], "root")

    def test_ledger_closed_vocab_partition_3_types(self):
        """signal, decision, judgment, delivery, reaction - partition 3's new event types."""
        for type_ in ("signal", "decision", "judgment", "delivery", "reaction", "presence"):
            event = self.ledger.append(type_, origin="signal")
            self.assertEqual(event["type"], type_)


class PartitionRoundtripTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_partition_roundtrip(self):
        base = 1_700_000_000.0  # arbitrary fixed UTC instant
        days = iter(base + i * 86400 for i in range(13))
        ledger = Ledger(self.home)
        with mock.patch.object(ledger_mod.time, "time", lambda: next(days)):
            appended = [
                ledger.append("user", session="s", origin="user", trust="user", text=f"day{i}")
                for i in range(13)
            ]

        files = sorted((self.home / "events").glob("*.jsonl"))
        self.assertEqual(len(files), 13)  # one partition per day

        read_back = list(ledger.read())
        self.assertEqual([e["seq"] for e in read_back], [e["seq"] for e in appended])
        self.assertEqual([e["text"] for e in read_back], [f"day{i}" for i in range(13)])
        self.assertEqual(ledger.last_seq, 13)

        reopened = Ledger(self.home)
        self.assertEqual(reopened.last_seq, 13)  # last_seq survives reopening

        # hand-append a truncated half-line to the newest partition file, as a crash mid-write would
        with files[-1].open("a") as f:
            f.write('{"seq": 14, "type": "user", "trunc')  # no closing brace, no newline

        self.assertEqual(len(list(reopened.read())), 13)  # reading tolerates it

        healed = Ledger(self.home)
        self.assertEqual(healed.last_seq, 13)  # reopening tolerates it too
        next_event = healed.append("user", session="s", origin="user", trust="user", text="after")
        self.assertEqual(next_event["seq"], 14)  # and the next seq continues correctly


class LedgerConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_concurrent_appends_unique_and_valid(self):
        ledger = Ledger(self.home)
        results = []
        results_lock = threading.Lock()

        def worker():
            for _ in range(50):
                event = ledger.append("user", session="s", origin="user", trust="user", text="x")
                with results_lock:
                    results.append(event["seq"])

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 400)
        self.assertEqual(len(set(results)), 400)  # 400 unique seqs
        self.assertEqual(sorted(results), list(range(1, 401)))  # strictly increasing, no gaps

        lines = []
        for path in sorted((self.home / "events").glob("*.jsonl")):
            lines.extend(path.read_text().splitlines())
        self.assertEqual(len(lines), 400)
        for line in lines:
            json.loads(line)  # every line is valid JSON


class TwoLedgersTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_two_ledgers_never_duplicate_seq(self):
        # simulates two processes (e.g. the daemon and a `pathfinder config set` CLI call)
        # sharing one home directory, each with its own Ledger instance and in-memory last_seq
        a = Ledger(self.home)
        b = Ledger(self.home)
        seqs = []
        for i in range(20):
            src = a if i % 2 == 0 else b
            event = src.append("user", session="s", origin="user", trust="user", text=str(i))
            seqs.append(event["seq"])

        self.assertEqual(len(seqs), len(set(seqs)))
        self.assertEqual(seqs, list(range(1, 21)))  # unique and strictly increasing in write order

        self.assertEqual([e["seq"] for e in a.read()], list(range(1, 21)))
        self.assertEqual([e["seq"] for e in b.read()], list(range(1, 21)))

    def test_two_ledgers_interleaved_threads(self):
        """The sequential version never actually races; this one does."""
        ledgers = [Ledger(self.home) for _ in range(4)]

        def worker(led):
            for i in range(40):
                led.append("user", session="s", origin="user", trust="user", text=str(i))

        threads = [threading.Thread(target=worker, args=(led,)) for led in ledgers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([e["seq"] for e in Ledger(self.home).read()], list(range(1, 161)))


class CrashRecoveryTest(unittest.TestCase):
    """The predecessor died at a day boundary, silently, with every test green. These are
    the two ways this ledger could have done the same."""

    DAY = 86400

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _append_at(self, led, ts, text):
        with mock.patch.object(ledger_mod.time, "time", lambda: ts):
            return led.append("user", session="s", origin="user", trust="user", text=text)

    def test_last_seq_survives_an_empty_or_truncated_newest_partition(self):
        t0 = 1_700_000_000
        led = Ledger(self.home)
        for i in range(3):
            self._append_at(led, t0, f"a{i}")
        for leftover in ("", '{"seq": 4, "ts"'):  # crash on the first append after UTC midnight
            newest = self.home / "events" / f"{ledger_mod.partition(t0 + self.DAY)}.jsonl"
            newest.write_text(leftover)
            reopened = Ledger(self.home)
            self.assertEqual(reopened.last_seq, 3, f"newest partition holds {leftover!r}")
            event = self._append_at(reopened, t0 + self.DAY, "after the crash")
            self.assertEqual(event["seq"], 4)
            self.assertEqual([e["seq"] for e in Ledger(self.home).read()], [1, 2, 3, 4])
            newest.unlink()

    def test_append_after_a_truncated_line_in_the_same_file_is_readable(self):
        t0 = 1_700_000_000
        led = Ledger(self.home)
        for i in range(3):
            self._append_at(led, t0, f"a{i}")
        path = self.home / "events" / f"{ledger_mod.partition(t0)}.jsonl"
        with path.open("a") as f:
            f.write('{"seq": 4, "ts"')  # fragment without a newline, same partition
        reopened = Ledger(self.home)
        event = self._append_at(reopened, t0 + 60, "must not be glued to the fragment")
        self.assertEqual(event["seq"], 4)
        read_back = list(Ledger(self.home).read())
        self.assertEqual([e["seq"] for e in read_back], [1, 2, 3, 4])
        self.assertEqual(read_back[-1]["text"], "must not be glued to the fragment")


class SurrogateTest(unittest.TestCase):
    """A lone surrogate from a JSON client or a model's tool arguments made SQLite refuse the
    projection, so catch_up at every daemon start and reindex failed on that line forever."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _state(self, name="state.db"):
        from pathfinder.state import State
        state = State(self.home / name)
        self.addCleanup(state.close)
        return state

    def test_lone_surrogate_replaced_at_append(self):
        led = Ledger(self.home)
        led.append("session_start", session="s")
        event = led.append("user", session="s", origin="user", trust="user", text="hi \ud83d there",
                           args={"q\udc00": ["a\ude00", ("b\ud800",)]})
        on_disk = next((self.home / "events").glob("*.jsonl")).read_text()
        self.assertNotRegex(on_disk, r"\\ud[89a-f]")  # no surrogate escape reaches the file
        self.assertEqual(event["text"], "hi � there")  # exactly one per lone surrogate
        self.assertEqual(event["args"], {"q�": ["a�", ["b�"]]})
        state = self._state()
        state.catch_up(led)  # raised UnicodeEncodeError: surrogates not allowed
        self.assertEqual(state.history("s")[0]["content"], "hi � there")

    def test_old_ledger_with_a_lone_surrogate_catches_up_and_reindexes(self):
        (self.home / "events").mkdir()
        lines = [{"seq": 1, "ts": 1_700_000_000.0, "type": "session_start", "session": "s",
                  "turn": "", "origin": "system", "trust": "self"},
                 {"seq": 2, "ts": 1_700_000_001.0, "type": "user", "session": "s", "turn": "t",
                  "origin": "user", "trust": "user", "text": "hi \ud83d"}]
        raw = "".join(json.dumps(e) + "\n" for e in lines)  # written the old way, unclean
        self.assertIn('"hi \\ud83d"', raw)
        (self.home / "events" / f"{ledger_mod.partition(1_700_000_000)}.jsonl").write_text(raw)
        led = Ledger(self.home)
        state = self._state()
        self.assertEqual(state.catch_up(led), 2)
        self.assertRegex(state.history("s")[0]["content"], "^hi �+$")
        self.assertEqual(self._state("rebuilt.db").rebuild(led), 2)  # reindex

    def test_astral_characters_untouched(self):
        led = Ledger(self.home)
        led.append("user", session="s", origin="user", trust="user", text="ok \U0001f600")
        path = next((self.home / "events").glob("*.jsonl"))
        with path.open("a") as f:  # a valid pair written as two JSON escapes
            f.write(json.dumps({"seq": 2, "ts": 0, "type": "user", "text": "😀"}) + "\n")
        self.assertEqual([e["text"] for e in Ledger(self.home).read()], ["ok \U0001f600", "\U0001f600"])


class FileModeTest(unittest.TestCase):
    def test_partition_and_lock_born_0600_dir_0700(self):
        old = os.umask(0o022)  # the usual umask, under which a plain create is 0644 and 0755
        try:
            with tempfile.TemporaryDirectory() as root:
                led = Ledger(Path(root))
                event = led.append("session_start", session="s")
                self.assertEqual(led.dir.stat().st_mode & 0o777, 0o700)
                files = sorted(p.name for p in led.dir.iterdir())
                self.assertEqual(files, [".lock", f"{ledger_mod.partition(event['ts'])}.jsonl"])
                for name in files:
                    self.assertEqual((led.dir / name).stat().st_mode & 0o777, 0o600, name)
        finally:
            os.umask(old)


if __name__ == "__main__":
    unittest.main()


class PastedTokenTest(unittest.TestCase):
    """D53's backstop, with D66's names and scope. The Keychain is a dict here, faked at
    `store`/`delete`/`_gone` or at `subprocess.run`: no test touches the real one."""
    TOKEN = "ntn_" + "x" * 40  # built by concatenation, so the scanner never flags this file
    OTHER = "sk-" + "Y9" * 15

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.items = {}
        self.locked = False
        for name, fake in (("store", self._store), ("delete", self._delete), ("_gone", self._gone),
                           ("secret", lambda service, account: self.items.get((service, account)))):
            patch = mock.patch(f"pathfinder.keychain.{name}", side_effect=fake)
            patch.start()
            self.addCleanup(patch.stop)
        self.ledger = Ledger(self.home)
        self.state = State(self.home / "state.db")
        self.addCleanup(self.state.close)

    def _store(self, service, account, value):
        self.items[(service, account)] = value

    def _delete(self, service, account):
        if not self.locked:
            self.items.pop((service, account), None)
        return not self.locked

    def _gone(self, account):
        return not self.locked and ("pathfinder-pasted", account) not in self.items

    def _record(self, type, **kw):
        event = self.ledger.append(type, **kw)
        self.state.apply(event)
        return event

    def _say(self, text, **kw):  # what the owner typed
        return self._record("user", origin="user", trust="user", text=text, **kw)

    def _on_disk(self):
        return "".join(p.read_text() for p in (self.home / "events").glob("*.jsonl"))

    def _index(self):
        return json.loads((self.home / "pasted.json").read_text())

    def _security(self, argv, input=None, **kw):
        """A fake `security` over `self.items`, for the real secret/store/delete/_gone."""
        self.calls.append(argv[1])
        if argv[1] == "-i":  # add-generic-password -U -s "..." -a "..." -w "..."
            cmd = shlex.split(input)
            self.items[(cmd[cmd.index("-s") + 1], cmd[cmd.index("-a") + 1])] = cmd[cmd.index("-w") + 1]
            return mock.Mock(returncode=0, stdout="", stderr="")
        key = (argv[argv.index("-s") + 1], argv[argv.index("-a") + 1])
        if key not in self.items:
            return mock.Mock(returncode=44, stdout="", stderr="")
        value = self.items.pop(key) if argv[1] == "delete-generic-password" else self.items[key]
        return mock.Mock(returncode=0, stdout=value + "\n", stderr="")

    def test_paste_vaulted_and_handle_returned(self):
        self._record("session_start", session="s")
        event = self._say(f"use {self.TOKEN} then {self.TOKEN}", session="s", extra={"k": [self.OTHER]})
        seq = event["seq"]
        self.assertEqual(event["text"], f"use [secret:pasted-{seq}] then [secret:pasted-{seq}]")  # one handle
        self.assertEqual(event["extra"], {"k": [f"[secret:pasted-{seq}-2]"]})
        self.assertEqual(self.items, {("pathfinder-pasted", f"pasted-{seq}"): self.TOKEN,
                                      ("pathfinder-pasted", f"pasted-{seq}-2"): self.OTHER})
        disk = self._on_disk()
        self.assertNotIn(self.TOKEN, disk)
        self.assertNotIn(self.OTHER, disk)
        self.assertIn(f"[secret:pasted-{seq}]", disk)
        self.assertEqual(self.state.history("s"), [{"role": "user", "content": event["text"]}])
        self.assertEqual(sorted(self._index()["pasted"]), [f"pasted-{seq}", f"pasted-{seq}-2"])  # handles and times only
        self.assertNotIn(self.TOKEN, (self.home / "pasted.json").read_text())
        self.assertEqual((self.home / "pasted.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self._say("no token here", session="s")["text"], "no token here")
        self.assertEqual(len(self.items), 2)

    def test_handle_is_the_event_seq(self):
        first = self._say(f"one {self.TOKEN}")
        second = self._say(f"two {self.OTHER} and {self.TOKEN}")
        s1, s2 = first["seq"], second["seq"]
        self.assertEqual(first["text"], f"one [secret:pasted-{s1}]")
        self.assertEqual(second["text"], f"two [secret:pasted-{s2}] and [secret:pasted-{s2}-2]")
        self.assertEqual(sorted(a for _, a in self.items), [f"pasted-{s1}", f"pasted-{s2}", f"pasted-{s2}-2"])

    def test_a_lost_index_cannot_reuse_a_number(self):
        first = self._say(self.TOKEN)
        (self.home / "pasted.json").unlink()
        second = self._say(self.OTHER)
        self.assertNotEqual(first["text"], second["text"])
        self.assertEqual(self.items, {("pathfinder-pasted", f"pasted-{first['seq']}"): self.TOKEN,
                                      ("pathfinder-pasted", f"pasted-{second['seq']}"): self.OTHER})

    def _back_date(self, pasted):  # an index as partition 8 first wrote it, with `last`: still read
        (self.home / "pasted.json").write_text(json.dumps({"last": 2, "pasted": pasted}))

    def test_unbound_paste_expires_after_24h(self):
        now = time.time()
        self._back_date({"pasted-1": now - 25 * 3600, "pasted-2": now - 3600})
        self.items = {("pathfinder-pasted", "pasted-1"): "a", ("pathfinder-pasted", "pasted-2"): "b"}
        Ledger(self.home)  # opening a ledger sweeps nothing: only a daemon start and a paste do
        self.assertEqual(len(self.items), 2)
        keychain_mod.sweep(self.home / "pasted.json", time.time())  # what a daemon start runs
        self.assertEqual(list(self.items), [("pathfinder-pasted", "pasted-2")])
        self.assertEqual(self._index(), {"pasted": {"pasted-2": now - 3600}})

        self._back_date({"pasted-2": now - 25 * 3600})
        self.locked = True  # the entry stays for the next sweep
        keychain_mod.sweep(self.home / "pasted.json", time.time())
        self.assertEqual(list(self._index()["pasted"]), ["pasted-2"])
        self.locked = False
        seq = self._say(self.TOKEN)["seq"]  # a new paste sweeps too
        self.assertEqual(self.items, {("pathfinder-pasted", f"pasted-{seq}"): self.TOKEN})
        self.assertEqual(list(self._index()["pasted"]), [f"pasted-{seq}"])

    def test_a_handle_whose_item_is_gone_is_dropped(self):
        now = time.time()
        self._back_date({"pasted-1": now - 3600, "pasted-2": now - 3600})
        self.items = {("pathfinder-pasted", "pasted-2"): "b"}
        keychain_mod.sweep(self.home / "pasted.json", now)
        self.assertEqual(list(self._index()["pasted"]), ["pasted-2"])
        self._back_date({"pasted-1": now - 3600})
        self.items, self.locked = {}, True  # a locked keychain cannot say it is gone: kept
        keychain_mod.sweep(self.home / "pasted.json", now)
        self.assertEqual(list(self._index()["pasted"]), ["pasted-1"])

    def test_an_unusable_index_never_stops_an_append(self):
        index = self.home / "pasted.json"
        for state in (None, "", "not json{", "{}", "null", "[]", '{"last": 3}', '{"pasted": {}}'):
            with self.subTest(state):
                index.unlink(missing_ok=True)
                if state is not None:
                    index.write_text(state)
                ledger = Ledger(self.home)  # a daemon start, or any CLI command
                self.assertEqual(ledger.append("user", trust="user", text="plain")["text"], "plain")
                self.assertRegex(ledger.append("user", trust="user", text=self.TOKEN)["text"],
                                 r"^\[secret:pasted-\d+\]$")
        self.assertNotIn(self.TOKEN, self._on_disk())

    def test_an_unwritable_index_redacts_and_leaves_no_orphan(self):
        (self.home / "pasted.json").mkdir()  # nothing can be written there
        self.assertEqual(self._say(self.TOKEN)["text"], "[redacted]")
        self.assertEqual(self.items, {})

    def test_pem_without_end_is_redacted_whole(self):
        """The ledger's 2,000-character clip cuts a 4096-bit key's END line off: the header and
        the base64 after it go, as raw text or as JSON with `\\n` escapes."""
        body = "\n".join("MIIJ" + "Qk9vdHN0cmFw" * 5 for _ in range(48))  # about 3 KB, no END
        key = "-----BEGIN " + "RSA PRIVATE KEY-----\n" + body + "\n-----END " + "RSA PRIVATE KEY-----"
        for clipped in (tools_mod.clip_for_ledger(key), tools_mod.clip_for_ledger({"content": key})):
            event = self._record("tool_result", id="c1", name="read_file", ok=True, result=clipped)
            self.assertEqual(event["result"], clipped[:clipped.index("-----BEGIN")] + "[redacted]...(truncated)")
        self.assertIsNone(re.search(r"[A-Za-z0-9+/=]{20,}", "".join(json.loads(line)["result"]
                          for line in self._on_disk().splitlines())))
        self.assertEqual(self.items, {})

    def test_failed_read_back_leaves_no_orphan(self):
        def differs(service, account, value):  # stored, then read back as something else
            self.items[(service, account)] = value[:-1]
            raise RuntimeError("the Keychain gave back a different value")
        keychain_mod.store.side_effect = differs
        self.assertEqual(self._say(self.TOKEN)["text"], "[redacted]")
        self.assertEqual(self.items, {})
        self.assertFalse((self.home / "pasted.json").exists())

    def test_keychain_failure_falls_back_to_redaction(self):
        mock.patch.stopall()  # the real store(), over a `security` that is missing or refuses
        for failure in (FileNotFoundError(), mock.Mock(returncode=51, stdout="", stderr="")):
            with mock.patch("pathfinder.keychain.subprocess.run",
                            **({"side_effect": failure} if isinstance(failure, BaseException)
                               else {"return_value": failure})):
                event = self._say(f"key {self.TOKEN}")
            self.assertEqual(event["text"], "key [redacted]")
        self.assertNotIn(self.TOKEN, self._on_disk())
        self.assertEqual(self._on_disk().count("key [redacted]"), 2)  # the appends landed
        self.assertFalse((self.home / "pasted.json").exists())

    def test_assistant_blocks_written_verbatim(self):
        """Deviation 4: `assistant.blocks` replay byte for byte, so the mask skips them; the screen
        `text`, and a result body, stay masked. The Keychain is never asked."""
        mock.patch.stopall()
        blocks = [{"type": "thinking", "thinking": f"saw {self.OTHER}", "signature": self.TOKEN},
                  {"type": "tool_use", "id": "toolu_1", "name": "save", "input": {"key": self.OTHER}}]
        with mock.patch("pathfinder.keychain.subprocess.run") as run:
            event = self.ledger.append("assistant", origin="user", clone="root", step=1, model="p:m",
                                       blocks=blocks, stop_reason="tool_use", usage={}, text=f"key {self.OTHER}",
                                       final=False)
            result = self.ledger.append("result", origin="user", trust="third_party", clone="root", step=1,
                                        call_id="toolu_1", tool="save", ok=True, body=f"env {self.OTHER}")
        run.assert_not_called()
        self.assertEqual(event["blocks"], blocks)
        self.assertEqual((event["text"], result["body"]), ("key [redacted]", "env [redacted]"))
        on_disk = [json.loads(line) for line in self._on_disk().splitlines()]
        self.assertEqual(on_disk[0]["blocks"], blocks)
        self.assertEqual(next(self.ledger.read(event["seq"] - 1))["blocks"], blocks)

    def test_a_token_in_a_tool_result_is_redacted_without_the_keychain(self):
        mock.patch.stopall()
        with mock.patch("pathfinder.keychain.subprocess.run") as run:
            result = self._record("tool_result", id="c1", name="read_file", ok=True, result=f"env: {self.TOKEN}")
            signal = self.ledger.append("signal", origin="signal", trust="third_party", summary=f"mail {self.OTHER}")
        self.assertEqual((result["result"], signal["summary"]), ("env: [redacted]", "mail [redacted]"))
        self.assertEqual(run.call_count, 0)
        self.assertFalse((self.home / "pasted.json").exists())

    def test_a_token_in_the_owners_text_gets_a_handle(self):
        mock.patch.stopall()
        self.calls = []
        with mock.patch("pathfinder.keychain.subprocess.run", side_effect=self._security):
            event = self._say(f"key {self.TOKEN}")
        self.assertEqual(event["text"], f"key [secret:pasted-{event['seq']}]")
        self.assertEqual(self.items, {("pathfinder-pasted", f"pasted-{event['seq']}"): self.TOKEN})
        self.assertIn("-i", self.calls)

    def test_an_existing_item_is_never_replaced(self):
        """Every home shares the service, and a seq is unique only in its own ledger: a trial home
        or a reset `events/` reaches `pasted-1` again, and another home's live item is there."""
        mock.patch.stopall()
        self.calls, self.items = [], {("pathfinder-pasted", "pasted-1"): "another home's value"}
        with mock.patch("pathfinder.keychain.subprocess.run", side_effect=self._security):
            event = self._say(self.TOKEN)
        self.assertEqual((event["seq"], event["text"]), (1, "[redacted]"))
        self.assertEqual(self.items, {("pathfinder-pasted", "pasted-1"): "another home's value"})
        self.assertNotIn("-i", self.calls)
        self.assertNotIn("delete-generic-password", self.calls)

    def test_a_locked_keychain_drops_no_handle(self):
        """Only exit 44 says an item is gone: a locked keychain (exit 51) keeps every handle, and
        the paste is redacted without an add or a delete."""
        mock.patch.stopall()
        self._back_date({"pasted-9": time.time() - 3600})
        before = self._index()["pasted"]
        calls = []

        def locked(argv, input=None, **kw):
            calls.append(argv[1])
            return mock.Mock(returncode=51, stdout="", stderr="")
        with mock.patch("pathfinder.keychain.subprocess.run", side_effect=locked):
            self.assertEqual(self._say(self.TOKEN)["text"], "[redacted]")
        self.assertEqual(self._index(), {"last": 2, "pasted": before})  # not even rewritten
        self.assertNotIn("delete-generic-password", calls)
        self.assertNotIn("-i", calls)

    def test_pasted_token_reaches_the_record_as_a_handle(self):
        """The owner's paste becomes a handle in history; a call that passes the handle on is
        recorded with it, and a raw token the model wrote into a call is recorded redacted."""
        self._record("session_start", session="s")
        handle = self._say(f"save {self.TOKEN}", session="s", turn="t1")["text"].split()[-1]
        args = {"token": handle, "raw": self.OTHER}
        for type_, extra in (("tool_call", {"args": args}), ("tool_result", {"ok": True, "result": "done"})):
            self._record(type_, session="s", turn="t1", origin="user", id="c1", name="remember", **extra)
        (record,) = [m["content"] for m in self.state.history("s") if m["role"] == "system"]
        self.assertIn(f'remember {{"token":"{handle}","raw":"[redacted]"}} ok', record)
        self.assertNotIn(self.TOKEN, record)
        self.assertNotIn(self.OTHER, record)
