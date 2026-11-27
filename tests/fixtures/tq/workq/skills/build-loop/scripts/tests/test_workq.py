"""workq v2 pilot subprocess cases, with independent state fixtures."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[4] / "bin"


class WorkqCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("CLAUDE_CODE_SESSION", "WORKQ_", "T3CODE_THREAD", "XDG_"))}
        self.env.update(HOME=str(self.home), XDG_STATE_HOME=str(self.home / "state"),
                        XDG_CONFIG_HOME=str(self.home / "config"), WORKQ_SESSION="pilot")
        self.queue = self.home / "state/workq/pilot"

    def cli(self, *args, code=0, env=None):
        result = subprocess.run([sys.executable, "-I", str(BIN / "workq"), *args],
                                env=env or self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, code, result.stderr)
        return result.stdout

    def hook(self, prompt="Please build the queue " * 10, event="UserPromptSubmit", raw=None, **fields):
        payload = {"session_id": "pilot", "hook_event_name": event, "prompt": prompt, **fields}
        result = subprocess.run([sys.executable, "-I", str(BIN / "workq-hook")], env=self.env,
                                input=json.dumps(payload) if raw is None else raw,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        if not result.stdout:
            return ""
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], event)
        return output["additionalContext"]

    def records(self, name):
        return [json.loads(line) for line in (self.queue / name).read_text().splitlines()]


class WorkqCLI(WorkqCase):
    def test_unknown_session_refused_and_off_preserves_history(self):
        """WHY/CLAIM(opt-in): a typo must not create a second silent queue."""
        # catches: creating a queue implicitly on add
        self.cli("add", "lost", code=1)
        self.assertFalse(self.queue.exists())
        self.cli("on")
        self.cli("add", "retained")
        self.cli("off")
        self.assertEqual(self.hook(), "")
        self.assertIn("retained", self.cli("list"))
        self.cli("on")
        self.assertIn("retained", self.cli("list"))

    def test_multi_add_links_message_in_order(self):
        """WHY/CLAIM(filing): one owner message contains several ordered requests."""
        # catches: leaving linked messages unfiled or reversing add order
        self.cli("on")
        self.hook()
        self.assertEqual(self.cli("add", "--from", "m1", "first", "second").split(), ["1", "2"])
        listing = self.cli("list")
        self.assertLess(listing.index("first"), listing.index("second"))
        self.assertIn("(m1)", listing)
        self.assertNotIn("m1 (", listing)

    def test_status_next_reopen_move_and_reasons(self):
        """WHY/CLAIM(queue): status and ordering survive separate processes."""
        # catches: ignoring move or failing to persist status/reasons
        self.cli("on")
        self.cli("add", "first", "second", "third")
        self.cli("start", "2")
        self.assertIn("running", self.cli("list"))
        self.cli("move", "3", "--after", "1")
        self.assertIn("#3", self.cli("done", "1", "--note", "shipped"))
        self.cli("drop", "3", "--why", "owner deferred")
        self.assertNotIn("third", self.cli("list"))
        self.assertIn("dropped", self.cli("list", "--all"))
        self.cli("reopen", "1")
        self.assertLess(self.cli("list").index("first"), self.cli("list").index("second"))
        events = self.records("events.jsonl")
        self.assertTrue(any(e.get("note") == "shipped" for e in events))
        self.assertTrue(any(e.get("why") == "owner deferred" for e in events))

    def test_unfiled_and_nothing(self):
        """WHY/CLAIM(filing): messages without requests need an explicit resolution."""
        # catches: ignoring nothing events
        self.cli("on")
        self.hook()
        self.assertIn("m1 (", self.cli("list"))
        self.cli("nothing", "m1", "--why", "context only")
        self.assertNotIn("m1 (", self.cli("list"))
        self.assertEqual(self.records("events.jsonl")[-1]["why"], "context only")

    def test_fixture_age_bad_line_and_append_recovery(self):
        """WHY/CLAIM(replay): a broken row must not hide valid requests."""
        # catches: stopping replay at a malformed row or appending into a torn row
        self.cli("on")
        with (self.queue / "events.jsonl").open("a") as stream:
            stream.write(json.dumps({"t": time.time() - 7200, "op": "add", "n": 7,
                                     "text": "fixture", "m": None}) + "\n{partial")
        self.assertRegex(self.cli("list"), r"#7 \[open 2h\] fixture")
        self.assertEqual(self.cli("add", "after corruption").strip(), "8")
        self.assertIn("after corruption", self.cli("list"))

    def test_invalid_numbers_do_not_mutate(self):
        """WHY/CLAIM(numbers): bad references exit 2 without changing work."""
        # catches: accepting dangling item/message references
        self.cli("on")
        self.cli("add", "retained")
        before = (self.queue / "events.jsonl").read_bytes()
        for args in [("done", "99"), ("start", "bad"), ("move", "1", "--after", "99"),
                     ("add", "--from", "m9", "lost"), ("nothing", "m0", "--why", "bad")]:
            with self.subTest(args=args): self.cli(*args, code=2)
        self.assertEqual((self.queue / "events.jsonl").read_bytes(), before)

    def test_unreadable_state_exit_one(self):
        """WHY/CLAIM(errors): state errors differ from invalid references."""
        # catches: ignoring unreadable inbox state
        self.cli("on")
        (self.queue / "inbox.jsonl").mkdir()
        self.cli("list", code=1)

    def test_session_precedence_and_validation(self):
        """WHY/CLAIM(session): Claude wins; session ids never become arbitrary paths."""
        # catches: using fallback session before Claude session or allowing path traversal
        self.cli("-s", "explicit", "on", env=dict(self.env, CLAUDE_CODE_SESSION_ID="claude"))
        self.assertTrue((self.home / "state/workq/claude/events.jsonl").exists())
        self.assertFalse(self.queue.exists())
        self.cli("-s", "../escape", "on", code=2)
        self.cli("-s", "explicit", "on")
        self.assertTrue((self.home / "state/workq/explicit/events.jsonl").exists())


class WorkqHook(WorkqCase):
    def test_opted_out_and_delegates_silent(self):
        """WHY/CLAIM(opt-in): global hooks leave other sessions and delegates alone."""
        # catches: capturing default-off or delegate prompts
        self.assertEqual(self.hook(), "")
        self.assertFalse(self.queue.exists())
        self.env["T3CODE_THREAD_ID"] = "thread:delegated-task:pilot"
        self.assertEqual(self.hook("workq on"), "")
        self.assertFalse(self.queue.exists())
        self.env.pop("T3CODE_THREAD_ID")
        self.cli("on")
        self.env["T3CODE_THREAD_ID"] = "thread:delegated-task:pilot"
        self.assertEqual(self.hook(), "")
        self.assertFalse((self.queue / "inbox.jsonl").exists())

    def test_owner_capture_private_and_cli_parity(self):
        """WHY/CLAIM(capture): raw owner requests survive without agent cooperation."""
        # catches: dropping raw text or creating world-readable inbox files
        self.cli("on")
        prompt = "Build this.\nThen build that. " * 10
        context = self.hook(prompt)
        self.assertIn("m1 captured", context)
        self.assertIn("workq add --from m1", context)
        record = self.records("inbox.jsonl")[0]
        self.assertEqual((record["m"], record["chars"], record["text"]), ("m1", len(prompt), prompt))
        for path, mode in [(self.queue.parent, 0o700), (self.queue, 0o700),
                           (self.queue / "events.jsonl", 0o600), (self.queue / "inbox.jsonl", 0o600)]:
            self.assertEqual(path.stat().st_mode & 0o777, mode)
        self.assertIn("m1 (", self.cli("list"))

    def test_machine_prompts_not_captured(self):
        """WHY/CLAIM(owner-only): notifications and cross-session reports are not owner tasks."""
        # catches: capturing machine notifications as owner requests
        self.cli("on")
        for prompt in ["Delegated task test", "<task-notification>done", "<system-reminder>rules",
                       "[Cross-session message] hello", "From the demo-app orchestrator: report",
                       'Report from the demo-app orchestrator (thread "x")',
                       "FYI from the nav-guides session", "Follow-up from the nav-guides session",
                       "User-authorized addition from the active edge PR #669 task"]:
            with self.subTest(prompt=prompt): self.assertEqual(self.hook(prompt), "")
        self.assertFalse((self.queue / "inbox.jsonl").exists())

    def test_short_prompt_silent_until_unfiled_work(self):
        """WHY/CLAIM(nag): acknowledgements stay quiet until something needs filing."""
        # catches: always nagging or never showing older unfiled prompts
        self.cli("on")
        self.assertEqual(self.hook("thanks"), "")
        self.assertEqual(self.records("inbox.jsonl")[0]["text"], "thanks")
        self.assertIn("m1", self.hook("yes"))

    def test_exact_owner_on_off(self):
        """WHY/CLAIM(opt-in): opt-in works from the owner prompt without a shell."""
        # catches: failing to persist prompt-based on/off
        self.hook("workq on")
        self.assertIn("m1 captured", self.hook())
        self.assertEqual(self.hook("workq off"), "")
        self.assertEqual(self.hook(), "")
        self.assertEqual(len(self.records("inbox.jsonl")), 1)
        self.assertEqual(self.records("events.jsonl")[-1]["op"], "off")

    def test_prompt_budget_with_long_queue_and_older_unfiled(self):
        """WHY/CLAIM(bounded): a growing queue must not flood every owner turn."""
        # catches: dumping every item or omitting older-unfiled reminders
        self.cli("on")
        self.hook("old owner message " * 12)
        self.cli("add", "x" * 2000, *[f"task {n}" for n in range(9)])
        context = self.hook("new owner request " * 12)
        self.assertLessEqual(len(context.splitlines()), 6)
        self.assertLessEqual(len(context), 650)
        self.assertIn("#1", context)
        self.assertIn("m1", context)
        self.assertIn("more", context)

    def test_compact_verbatim_newest_first_and_filed_excluded(self):
        """WHY/CLAIM(compact): recover prompts the agent never filed after compaction."""
        # catches: losing raw messages, reversing inbox recency, or re-injecting filed messages
        self.cli("on")
        first, second = "first owner line\nsecond owner line", "newest raw owner message"
        self.hook(first)
        self.hook(second)
        self.cli("add", "open item")
        context = self.hook(event="SessionStart", source="compact")
        self.assertIn("this survived compaction; resume in order", context)
        self.assertIn(first, context)
        self.assertLess(context.index(second), context.index(first))
        self.assertIn("#1", context)
        self.cli("nothing", "m1", "--why", "background")
        self.cli("add", "--from", "m2", "filed")
        context = self.hook(event="SessionStart", source="compact")
        self.assertNotIn(first, context)
        self.assertNotIn(second, context)

    def test_compact_budget_marks_truncation(self):
        """WHY/CLAIM(bounded): huge raw prompts cannot overwhelm restored context."""
        # catches: forgetting the compact byte cap
        self.cli("on")
        self.hook("old " * 2000)
        self.hook("newest " * 2000)
        context = self.hook(event="SessionStart", source="compact")
        self.assertLessEqual(len(context.encode()), 6000)
        self.assertIn("newest", context)
        self.assertIn("truncated", context)

    def test_fragile_input_fails_open(self):
        """WHY/CLAIM(errors): malformed input must never break an owner turn."""
        # catches: letting malformed hook input escape or mutating on unknown events
        self.cli("on")
        for raw in ["garbage", "null", "[]", "{}", '{"session_id":"../bad"}',
                    '{"session_id":"pilot","hook_event_name":"UserPromptSubmit"}',
                    '{"session_id":"pilot","hook_event_name":"UserPromptSubmit","prompt":12}']:
            with self.subTest(raw=raw): self.assertEqual(self.hook(raw=raw), "")
        self.assertEqual(self.hook(event="Unknown"), "")
        self.assertEqual(self.hook(event="SessionStart", source="startup"), "")
        self.assertFalse((self.queue / "inbox.jsonl").exists())
        (self.queue / "inbox.jsonl").mkdir()
        self.assertEqual(self.hook(), "")


if __name__ == "__main__":
    unittest.main()
