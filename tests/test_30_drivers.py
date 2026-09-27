# -*- coding: utf-8 -*-
"""Both driver families, against the fake CLI stub. ⭐The criteria are all behavioral: the echo[n] in the answer,
whether the process really is gone, whether the gate fires right on time."""
import ast
import contextlib
import inspect
import io
import json
import os
import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers

import scv  # noqa: E402

HOME = ""
CFG = {}
NL = chr(10)
_REAL_HEAD = scv.cli_head


def setUpModule():
    global HOME
    HOME = helpers.fresh_home("drivers", unittest.addModuleCleanup)
    scv.cli_head = helpers.fake_head


def tearDownModule():
    scv.cli_head = _REAL_HEAD


def workdir(tag):
    p = Path(HOME) / "work" / tag
    p.mkdir(parents=True, exist_ok=True)
    return p


def mode(m, **extra):
    os.environ["FAKE_MODE"] = m
    for k, v in extra.items():
        os.environ[k] = v


def gone(pid, within=8):
    """Wait for this pid to really disappear from the system (`""` = does not exist; `None` = could not tell,
    never counted as gone). ⚠️Asking for a birth id once is microsecond-scale on win32 (ctypes), and one `ps` call
    on POSIX ⇒ never treat it as a busy-wait anyway."""
    end = time.time() + within
    while time.time() < end and scv.proc_start_id(pid) != "":
        time.sleep(0.2)
    return scv.proc_start_id(pid) == ""


def log_lines_during(fn):
    """The new lines that really land on disk in `bridge.log` during this block. Never the number of calls to a
    mocked scv.log: what needs to be measured is the bytes."""
    p = scv.spath("bridge.log")
    before = p.read_text(encoding="utf-8") if p.exists() else ""
    with contextlib.redirect_stderr(io.StringIO()):
        fn()
    after = p.read_text(encoding="utf-8") if p.exists() else ""
    return after[len(before):].splitlines()


def src_tree():
    with io.open(scv.__file__, encoding="utf-8") as f:
        return ast.parse(f.read())


class _Staged(unittest.TestCase):
    """⭐Every test case restores `FAKE_*` on the way in and out, and defaults back to `ok`.

    Never rely on the tacit agreement that "every setUp remembers to reset FAKE_MODE": the next person who adds a
    test case and forgets to set the mode will have it run silently in the mode left behind by the previous test
    case — that kind of green means nothing, and no one can tell just by looking."""

    def setUp(self):
        saved = {k: v for k, v in os.environ.items() if k.startswith("FAKE_")}
        self.addCleanup(self._restore, saved)
        mode("ok")

    @staticmethod
    def _restore(saved):
        for k in [k for k in os.environ if k.startswith("FAKE_")]:
            if k not in saved:
                del os.environ[k]
        os.environ.update(saved)


class Claude(_Staged):
    def test_two_turns_stay_in_one_process(self):
        mark = len(helpers.read_fake_log())
        d = scv.ClaudeDriver(CFG, "haiku", None, "你是甲", workdir("c1"))
        got = []
        try:
            r1 = d.turn("第一问", got.append, 20, 10, None)
            r2 = d.turn("第二问", got.append, 20, 10, None)
        finally:
            d.close()
        self.assertEqual((r1["text"], r2["text"]), ("echo[1]: 第一问", "echo[2]: 第二问"))
        self.assertEqual("".join(got), "echo[1]: 第一问echo[2]: 第二问")
        self.assertIsNotNone(r1["ttfc"])
        self.assertEqual(r2["usage"]["input_tokens"], 20)
        boots = [x for x in helpers.fake_log_since(mark) if x.get("family") == "claude"]   # never `[-1]`: see helpers
        self.assertEqual(len(boots), 1, boots)
        boot = boots[0]
        self.assertEqual(boot["system"], "你是甲")
        self.assertIn("--safe-mode", boot["argv"])
        self.assertEqual(Path(boot["cwd"]).resolve(), workdir("c1").resolve())

    def test_first_token_gate_fires_while_heartbeats_flow(self):
        """There are heartbeats but no content: the stall gate will never fire, it has to rely on the first-token
        gate. If the gate were only written into the `queue.Empty` branch, this test would sit and wait for the
        whole turn to time out.

        ⭐Both bounds are needed: with only the upper bound, the whole "gate fires early" family of mutations
        (`content_at` missing `+ first_token`, the unit written as milliseconds, `- 0.01` written as `- 10`) would
        all be green — and in production that is silent: a normal, slow answer gets killed as a timeout, and the
        log righteously states "45s with no first byte of content out". The lower bound leaves a margin of 0.9;
        what it guards against is "early", never "late"."""
        mode("heartbeat_only")
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("c2"))
        t0 = time.time()
        with self.assertRaises(scv.BridgeError) as cm:
            d.turn("x", lambda s: None, 30, 1.0, None)
        took = time.time() - t0
        self.assertLess(took, 6)
        self.assertGreater(took, 0.9, "the gate is set at 1.0s, firing at %.3fs = it fired early" % took)
        self.assertEqual(cm.exception.klass, "timeout")
        self.assertIn("no first byte of content out", cm.exception.raw)
        self.assertFalse(d.alive())

    def test_an_empty_text_delta_is_not_the_first_token(self):
        """⭐The second half of the third layer of the three-layer criterion (`and d.get("text")`) originally had
        zero coverage: the stub had no frame that was "`text_delta` but `text` is empty" ⇒ deleting just that half
        left the whole thing all green.

        ⚠️This frame is adversarial, never a measured shape — no one has measured whether a real CLI ever emits an
          empty `text`. Precisely because it cannot be measured, that half is a defensive criterion; a defensive
          criterion still needs to be pinned by someone, or it is just a line of dead code."""
        mode("empty_text_first")
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("c10"))
        t0 = time.time()
        with self.assertRaises(scv.BridgeError) as cm:
            d.turn("x", lambda s: None, 30, 1.0, None)
        took = time.time() - t0
        self.assertEqual(cm.exception.klass, "timeout")
        self.assertIn("no first byte of content out", cm.exception.raw)
        self.assertGreater(took, 0.9, "an empty text was treated as the first token (with the gate removed entirely it would not fire at 1s)")
        self.assertLess(took, 6)

    def test_stall_gate(self):
        mode("hang")
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("c3"))
        d.stall = 1.0
        t0 = time.time()
        with self.assertRaises(scv.BridgeError) as cm:
            d.turn("x", lambda s: None, 30, None, None)
        took = time.time() - t0
        self.assertIn("not a single byte out", cm.exception.raw)
        # ⚠️The `%gs` printed in the wording is the configured value, never how long it actually waited ⇒ looking
        #   only at the wording, this test cannot tell whether the gate fired at 0.01s or at 29s. The time must be
        #   measured separately.
        self.assertGreater(took, 0.9, "the gate is set at 1.0s, firing at %.3fs = it fired early" % took)
        self.assertLess(took, 6, "the gate is set at 1.0s, it fired after waiting %.3fs" % took)
        self.assertFalse(d.alive())

    def test_crash_carries_stderr_verbatim(self):
        mode("crash")
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("c4"))
        with self.assertRaises(scv.BridgeError) as cm:
            d.turn("x", lambda s: None, 20, 10, None)
        self.assertEqual(cm.exception.klass, "crashed")
        self.assertIn("boom: fake crash", cm.exception.raw)

    def test_auth_error_is_classified_and_verbatim(self):
        mode("auth")
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("c5"))
        try:
            with self.assertRaises(scv.BridgeError) as cm:
                d.turn("x", lambda s: None, 20, 10, None)
        finally:
            d.kill()
        self.assertEqual((cm.exception.klass, cm.exception.raw, cm.exception.family),
                         ("auth_required", "Not logged in · Please run /login", "claude"))

    def test_cancel_really_kills(self):
        mode("hang")
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("c6"))
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        t0 = time.time()
        with self.assertRaises(scv.BridgeError) as cm:
            d.turn("x", lambda s: None, 60, None, cancel)
        took = time.time() - t0
        self.assertEqual(cm.exception.klass, "cancelled")
        self.assertLess(took, 5)
        # The lower bound follows the same logic: never treat it as cancelled before it was actually cancelled
        # (the family of bugs where `cancel.is_set()` gets written as `cancel is not None`)
        self.assertGreater(took, 0.4, "the cancel only happened at 0.5s, coming back at %.3fs = it did not wait for that event" % took)
        self.assertFalse(d.alive())

    def test_kill_takes_the_grandchild_too(self):
        mode("grandchild")
        mark = len(helpers.read_fake_log())
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("c7"))
        d.turn("x", lambda s: None, 20, 10, None)
        gcs = [x["grandchild"] for x in helpers.fake_log_since(mark) if "grandchild" in x]
        self.assertEqual(len(gcs), 1, gcs)
        gc = gcs[0]
        self.assertTrue(helpers.born_alive(scv.proc_start_id(gc)), "positive control: the grandchild process must be alive before the kill (could not tell doesn't count either)")
        d.kill()
        self.assertTrue(gone(gc), "the grandchild process was leaked")
        self.assertEqual([c for c in scv.children() if c["pid"] == d.pid], [])


class WhichHalfCarriesTheGate(_Staged):
    """🔴Two gates, each held up by two separate mechanisms: (1) the gate check sits at the top of the loop
    (2) `caps` is computed from the gate (the wait cap on `_q.get`).
    Measured on 2026-09-22 which one is doing what work — both carry real weight, neither is decorative, but they
    carry different cells:

    - **`caps` computed from the gate** carries "the CLI is completely silent": when not a single byte is coming
      out, the top of the loop never even gets a second turn, only the wait cap can wake `get` up. Remove it
      (`caps=[deadline]`) ⇒ `test_stall_gate` goes red.
    - **The gate at the top of the loop** carries "the CLI arrives faster than the gate": the queue always has
      something in it ⇒ `queue.Empty` never fires, and a gate written only into that branch gets pushed back
      indefinitely. The test below is what pins this one down.

    ⚠️So the sentence in the engine/brief comments that "`Empty` will never fire even once" is wrong for today's
      code (NC-1a is green): when `caps` is computed from the gate, the wait cap shrinks down to the 0.01s scale,
      and `Empty` still fires. The correct statement is the two points above."""

    def test_the_gate_still_fires_when_the_queue_never_runs_dry(self):
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("h1"))
        fake = _AlwaysHasOne(20)
        d.pipe._q = fake
        d.pipe.send({"type": "user", "message": {"role": "user", "content": "x"}})
        # 🔴Pin down first that the pump thread is still alive, never just that the assertion below has passed:
        #   `_pump_out` re-checks `self._q` on every iteration ⇒ once the queue is swapped here, if the fake queue
        #   is missing one `put`, the pump thread dies on the spot with an `AttributeError` on `self._q.put(raw)`,
        #   and it dies inside a background thread that `redirect_stderr` swallows completely clean ⇒ the gate
        #   below would still be all green (it never even needs the real queue) — from then on that gate is
        #   testing a world where data is no longer being pumped, and no one can see it.
        #   The criterion uses the real data flow (whether the frames the stub emits get pushed through), never
        #   "is the thread object still there".
        end = time.time() + 10
        while time.time() < end and not fake.fed:
            time.sleep(0.05)
        self.assertTrue(fake.fed, "the pump thread did not push a single frame through ⇒ it has most likely died silently (is the fake queue missing put?)")
        t0 = time.time()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(scv.BridgeError) as cm:
                d.pipe.read_until(lambda ev: False, 30, 1.0, 1.0, lambda: False, None)
        took = time.time() - t0
        self.assertEqual(cm.exception.klass, "timeout")
        self.assertIn("no first byte of content out", cm.exception.raw)
        self.assertGreater(took, 0.9)
        self.assertLess(took, 6, "if the gate were only written into the queue.Empty branch, it would only fire here after the queue runs dry in 20s")
        self.assertFalse(d.alive())
        # The other end: after the gate fires, stdout gets an EOF, and the pump thread has to deliver that `None`
        # sentinel ⇒ it is alive from start to finish, never "alive at the start, quietly dead in the middle".
        end = time.time() + 10
        while time.time() < end and None not in fake.fed:
            time.sleep(0.05)
        self.assertIn(None, fake.fed, "the pump thread never sent the EOF sentinel ⇒ it died partway through this test")


class _AlwaysHasOne:
    """A queue that always has something in it: `get()` returns a heartbeat frame immediately, and it never
    throws `queue.Empty` before `until`.

    ⭐What this models is "the CLI arrives faster than the gate" — that is the state the queue is in when a real
      CLI is flooding the screen.
    ⚠️The stub cannot produce this cell (so the only option is to feed the queue directly): the stub is written
      in Python, and its `dumps` + `write` + `flush` is slower than our own `decode` + `json.loads` ⇒ the queue
      can never build up, and `queue.Empty` always fires. "Say so when a deterministic window cannot be built" —
      this is that statement, said out loud.
    🔴`put` is never optional: it stands in for an object that both sides use — `get` on our side, `put` on the
      pump thread's side. Without it the pump thread dies on the spot with an `AttributeError` in the background,
      without a sound. `fed` is kept around precisely so that "it is still alive" becomes an assertable
      statement."""

    BEAT = json.dumps({"type": "stream_event",
                       "event": {"type": "content_block_delta",
                                 "delta": {"type": "thinking_delta", "thinking": ""}}}).encode("utf-8")

    def __init__(self, seconds):
        self.until = time.time() + seconds
        self.fed = []

    def get(self, timeout=None):
        if time.time() > self.until:
            raise queue.Empty
        return self.BEAT

    def put(self, item):
        self.fed.append(item)



class Constants(_Staged):
    def test_the_four_windows_are_the_numbers_the_plan_fixed(self):
        """⭐What this pins down is the four numbers the plan fixed, never "what the code happens to say right
        now": if they quietly drifted, every timing test above would still be all green (the tests carry their
        own stall/first_token parameters).
        ⭐`CODEX_STALL_S`'s consumer was already wired up by Task 7 (`Codex.test_a_fresh_codex_driver_starts_on_the_codex_window`
          watches whether the driver picked up this number, `test_the_stall_window_this_driver_carries_is_really_used`
          watches that it is really used) ⇒ from here on this line only has to guard the value itself from
          drifting. `CODEX_HANDSHAKE_S` does not belong in this sentence: it is not a number the plan fixed."""
        self.assertEqual((scv.CLAUDE_STALL_S, scv.CODEX_STALL_S, scv.FIRST_TOKEN_S, scv.CLOSE_GRACE_S),
                         (30, 90, 45, 10))

    def test_a_fresh_claude_driver_starts_on_the_claude_window(self):
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("k1"))
        try:
            self.assertEqual((d.stall, d.family), (scv.CLAUDE_STALL_S, "claude"))
        finally:
            d.close()


class ApiRetry(_Staged):
    """The CLI itself is backing off and retrying (`system/api_retry` carries `retry_delay_ms`, zero output during
    the backoff) ⇒ the window is extended by that much, and it must never be killed as stuck. ⭐The positive
    control is welded into a second test case: the same length of silence, but with the backoff duration written
    as 0 ⇒ it must be judged stuck — otherwise "the extension really took effect" and "the threshold was simply
    wide enough already" look identical on the test bench."""

    SILENCE = "2.0"

    def test_backoff_extends_the_stall_window(self):
        mode("api_retry", FAKE_RETRY_MS="5000", FAKE_RETRY_SILENCE=self.SILENCE)
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("c8"))
        d.stall = 0.5
        try:
            r = d.turn("x", lambda s: None, 30, 10, None)
        finally:
            d.close()
        self.assertEqual(r["text"], "echo[1]: x")

    def test_positive_control_the_same_silence_without_a_delay_is_judged_stalled(self):
        mode("api_retry", FAKE_RETRY_MS="0", FAKE_RETRY_SILENCE=self.SILENCE)
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("c9"))
        d.stall = 0.5
        with self.assertRaises(scv.BridgeError) as cm:
            d.turn("x", lambda s: None, 30, 10, None)
        self.assertEqual(cm.exception.klass, "timeout")
        self.assertIn("not a single byte out", cm.exception.raw)
        self.assertFalse(d.alive())

    def test_the_extension_survives_a_frame_that_lands_right_after_the_announcement(self):
        """🔴The extension gets wiped out by any frame that comes right after it: every incoming line
        unconditionally sets `quiet_until = now + stall`, and the extension is only layered on top of that
        afterward ⇒ the shape "announce backoff → emit one frame → long silence" still gets mistakenly killed.
        With a 529 backing off for tens of seconds and `CLAUDE_STALL_S=30`, this is a real window, never a
        contrived one."""
        mode("api_retry", FAKE_RETRY_MS="5000", FAKE_RETRY_SILENCE=self.SILENCE, FAKE_RETRY_TAIL="1")
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("c11"))
        d.stall = 0.5
        try:
            r = d.turn("x", lambda s: None, 30, 10, None)
        finally:
            d.close()
        self.assertEqual(r["text"], "echo[1]: x")


class Registry(_Staged):
    """⭐`child_add` returning False means this process never made it into the registry, the bridge cannot manage
    it ⇒ the caller must clean up after itself (its own contract). Never copy `run_cli`'s "loud but keeps
    running": that one is handling a second-scale probe, this one is a long-lived process that can stay alive for
    an entire session."""

    def test_a_process_that_cannot_be_registered_is_killed_not_ignored(self):
        seen = []

        def refuse(pid, family):
            seen.append(pid)
            return False

        err = io.StringIO()
        with mock.patch.object(scv, "child_add", refuse), \
                mock.patch.object(scv, "kill_tree", wraps=scv.kill_tree) as gun, \
                mock.patch.object(scv, "child_remove", wraps=scv.child_remove) as bury, \
                contextlib.redirect_stderr(err):
            with self.assertRaises(scv.BridgeError) as cm:
                scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("r1"))
        self.assertEqual(cm.exception.klass, "crashed")
        self.assertEqual(len(seen), 1)
        # Assert the correct value: the process really is gone, never "the constructor threw an exception"
        self.assertTrue(gone(seen[0]), "could not register yet still alive = an orphan the bridge cannot manage, exactly the shape the registry is meant to prevent")
        self.assertIn(str(seen[0]), err.getvalue())   # it was loud, and it named exactly which pid
        # 🔴The mechanism half must be pinned on its own. Actually hit on 2026-09-22: delete the `kill_tree` line
        #   and the three assertions above are still all green — the stub is well-behaved, and the moment
        #   `pipe.close()` closes stdin right after, it exits with EOF on its own ⇒ the behavioral criterion
        #   cannot tell "the whole tree was really killed" apart from "stdin was merely closed". And the CLI that
        #   is genuinely dangerous on this path is exactly the ill-behaved kind (it keeps lingering even after
        #   stdin is closed, and it has a grandchild in tow) ⇒ the shot fired must be aimed at the whole tree:
        #   `kill_tree` (taskkill /T on Windows, killpg on POSIX), never "it'll leave on its own eventually".
        self.assertEqual([c.args[0].pid for c in gun.call_args_list], [seen[0]])
        # ⭐The other half is equally a design decision, and equally something no assertion ever pinned before:
        #   this entry was never registered in the first place ⇒ it must never be un-registered (un-registering
        #   an entry that never existed shouts "it was not in the registry to begin with", burying the real cause
        #   under a misleading warning). Without this line, swapping `kill_tree(self.popen)` for `self.kill()`
        #   would leave the test all green either way.
        self.assertEqual(bury.call_count, 0, "the registration was refused ⇒ there must be no un-registering here")

    def test_the_refusal_is_not_what_normally_happens(self):
        """Zero-input control: take away that False and walk the same path again — it must be able to start and
        make it into the table. Without this line, the test above cannot be told apart from "ClaudeDriver simply
        cannot start at all"."""
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("r2"))
        try:
            self.assertEqual([c["family"] for c in scv.children() if c["pid"] == d.pid], ["claude"])
        finally:
            d.close()
        self.assertEqual([c for c in scv.children() if c["pid"] == d.pid], [])


class NoDoubleUnlist(_Staged):
    """⭐`child_remove` shouts "it was not in the registry to begin with" for a pid that is not in the table, and
    what that sentence means (`child_remove`'s own comment says so) is "either this is un-registering something a
    second time, or a registration was refused and nobody noticed" — it is the only signal for the latter.

    🔴A failed turn has already called `kill()` internally (all three of EOF/timeout/cancel do), and if the
    caller then does `finally: close()`, that un-registers a second time ⇒ turning that signal into routine noise
    that fires on every single failed turn, so that on the day something really goes wrong, no one can see it.
    ⚠️Not one test case today has ever exercised "failure ⇒ close() again", and that is exactly the most natural
    way to write the wiring layer."""

    WARN = "it was not in the registry to begin with"
    # ⭐All three paths must be covered: the review named exactly these three — EOF/timeout/cancel — and all three
    #   have already called kill() internally. Covering only one means that if either of the other two regresses
    #   into a double un-registration, this test would still be all green.
    PATHS = (("u1", "EOF (the CLI crashed)", "crash", None, 10, False),
             ("u2", "timeout (the stall gate)", "hang", 1.0, None, False),
             ("u3", "cancel (the caller cancelled)", "hang", None, None, True))

    def test_closing_after_any_failed_turn_adds_no_misleading_line(self):
        for tag, name, m, stall, first_token, cancelling in self.PATHS:
            with self.subTest(path=name):
                mode(m)
                d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir(tag))
                if stall is not None:
                    d.stall = stall
                ev = threading.Event()
                if cancelling:
                    threading.Timer(0.5, ev.set).start()

                def go():
                    with contextlib.suppress(scv.BridgeError):
                        d.turn("x", lambda s: None, 30, first_token, ev if cancelling else None)

                first = log_lines_during(go)
                self.assertEqual(len(first), 1, first)                 # the failure line still lands as usual
                self.assertNotIn(self.WARN, first[0])
                self.assertEqual(log_lines_during(d.close), [], name)  # closing again ⇒ never a single extra line

    def test_only_two_guarded_doors_call_child_remove(self):
        """⭐The controller's criterion: after the fix, that sentence appearing again must always mean something
        really has gone wrong.
        ⇒ un-registering is only ever allowed through two doors, each carrying its own gate: `_unlist` (the
        `_listed` flag) and `run_cli` (the `registered` flag). A third call site would turn that signal back into
        routine noise, and on the day that happens, the behavioral test above would still be all green (it only
        exercises today's three paths)."""
        def callers(tree):
            out = set()
            for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
                for c in ast.walk(fn):
                    if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "child_remove":
                        out.add(fn.name)
            return out

        self.assertEqual(callers(src_tree()), {"_unlist", "run_cli"})
        # the ruler is not blind: feed it a synthetic sample with a third door, and it must call it out (never let
        # an empty scan pass itself off as all green)
        probe = ast.parse("def somewhere_new(pid):" + NL + "    child_remove(pid)" + NL)
        self.assertEqual(callers(probe), {"somewhere_new"})

    def test_the_signal_itself_is_not_muted(self):
        """Zero-input control: that warning was never muted by me — un-registering an entry that really does not
        exist still shouts. Without this line, the test above cannot be told apart from "`child_remove` was
        disabled entirely"."""
        lines = log_lines_during(lambda: scv.child_remove(9999999))
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(self.WARN, lines[0])

    def test_closing_after_a_good_turn_is_silent_too(self):
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("u2"))
        d.turn("x", lambda s: None, 20, 10, None)
        self.assertEqual(log_lines_during(d.close), [])


class WorkdirPrepFailure(_Staged):
    """⭐The two `write_text` calls in `__init__` are a real failure path (workdir not writable / disk full / path
    too long). It used to throw a bare `OSError`: never a `BridgeError`, and not a single line landed on disk ⇒
    the wiring layer can only fall back to a generic 500/unknown, with no way to get the one sentence that could
    have told someone how to fix it — "writing system.txt failed".

    ⚠️The structural gate `NoSilentFailurePath` only scans `ast.Raise`, and is naturally blind to "an exception
    that was never wrapped" ⇒ this can only be pinned behaviorally."""

    def test_a_workdir_that_cannot_be_written_becomes_a_classified_bridge_error(self):
        box = {}

        def go():
            try:
                scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("w1"))
            except scv.BridgeError as exc:
                box["e"] = exc

        with mock.patch.object(Path, "write_text", side_effect=OSError("盘满了：写不进去")):
            lines = log_lines_during(go)
        self.assertIn("e", box, "it still threw a bare OSError ⇒ the caller got no class, and nothing was logged")
        self.assertEqual((box["e"].klass, box["e"].family), ("crashed", "claude"))
        self.assertIn("盘满了：写不进去", box["e"].raw)     # the OS's own words, not a character changed
        self.assertIn("system.txt", box["e"].raw)          # spells out which step it was, never just "could not start"
        self.assertEqual(len(lines), 1, lines)

    def test_the_same_path_works_when_the_disk_does(self):
        """Zero-input control: take away that OSError and walk the same path again — it must be able to start.
        Without this line, the test above cannot be told apart from "ClaudeDriver always throws BridgeError"."""
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("w2"))
        try:
            self.assertTrue((workdir("w2") / "system.txt").exists())
        finally:
            d.close()

    def test_a_workdir_with_a_null_byte_is_caught_too(self):
        """🔴Another cell in the same family, and this one needs no mock: a NUL byte in the workdir ⇒ what
        `write_text` throws is a `ValueError`, never `OSError` (measured 2026-09-22: `embedded null character`) ⇒
        catching only `OSError` still lets it escape bare.
        ⭐Which classes an `except` should catch has to be measured, never written to match "what I assumed it
          would throw" — get it wrong and that except is a line of dead code that never executes, while a test
          that mocks the same wrong assumption is all green."""
        box = {}

        def go():
            try:
                scv.ClaudeDriver(CFG, "haiku", None, "s", Path(str(workdir("w3")) + chr(0)))
            except scv.BridgeError as exc:
                box["e"] = exc

        lines = log_lines_during(go)
        self.assertIn("e", box, "it still threw a bare ValueError ⇒ the caller got no class, and nothing was logged")
        self.assertEqual(box["e"].klass, "crashed")
        self.assertIn("embedded null character", box["e"].raw)   # Python's own words, not a character changed
        self.assertEqual(len(lines), 1, lines)

    def test_a_popen_that_python_refuses_is_classified_too(self):
        """🔴What `Popen` throws for bad input is not only `OSError`. Measured 2026-09-22 (py3.12.10/win32):

            NUL in argv -> ValueError      NUL in cwd -> ValueError      NUL in an env value -> ValueError
            argv is an empty list -> OSError          executable does not exist -> FileNotFoundError (a subclass of OSError)
            an int mixed into argv -> TypeError

        ⇒ catching only `OSError` lets the three ValueError cells escape bare: never a `BridgeError`, and not a
        single line lands on disk.
        ⚠️Before Task 13c this was reachable: codex's `CODEX_HOME` came from config.json, and a user writing a NUL
          in there would land here. 13c removed that path (env comes as-is from the process environment, and the
          OS does not allow a NUL inside it) ⇒ today this is defense in depth, ⏳other foreign inputs have not yet
          been surveyed.
        ⛔`TypeError` is deliberately not caught: a non-string mixed into argv is our own bug (models go through a
          closed set, no external input can get in) — wrapping it as a retryable `crashed` would be passing off a
          bug as a retryable failure."""
        box = {}

        def go():
            try:
                scv._Pipe([sys.executable, "-c", "pass"], workdir("w4"), {"X": "a" + chr(0) + "b"}, "claude")
            except scv.BridgeError as exc:
                box["e"] = exc

        lines = log_lines_during(go)
        self.assertIn("e", box, "it still threw a bare ValueError ⇒ the caller got no class, and nothing was logged")
        self.assertEqual((box["e"].klass, box["e"].family), ("crashed", "claude"))
        self.assertIn("embedded null character", box["e"].raw)
        self.assertEqual(len(lines), 1, lines)


class RegistryWriteFailure(_Staged):
    """🔴The `_children_save` call inside `child_add` swallows not a single exception (disk full / permissions /
    `SCV_HOME` deleted). Letting it pass through bare means the process has already started, is not registered,
    and no one can kill it — the caller cannot even get the pid — exactly the shape the registry is meant to
    prevent, and not a single line lands on disk either.

    ⭐`run_cli` specifically guards against this in the same spot (its own comment spells out the reason word for
      word), `_Pipe` originally did not ⇒ the same piece of reasoning was only half carried out (the second time
      this is the same mistake as I-1).
    ⚠️`run_cli` chooses "loud but keeps running", this one chooses "kill it": that one is handling a second-scale
    probe, this one is a long-lived process that can stay alive for an entire session."""

    def test_a_table_write_that_blows_up_kills_the_process_and_is_classified(self):
        seen = []

        def explode(pid, family):
            seen.append(pid)
            raise OSError("children.json 写不进去：磁盘满了")

        box = {}

        def go():
            try:
                scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("t1"))
            except scv.BridgeError as exc:
                box["e"] = exc

        with mock.patch.object(scv, "child_add", explode), \
                mock.patch.object(scv, "kill_tree", wraps=scv.kill_tree) as gun:
            lines = log_lines_during(go)
        self.assertIn("e", box, "it still threw a bare OSError ⇒ the process started, was never registered, and no one could kill it")
        self.assertEqual(box["e"].klass, "crashed")
        self.assertIn("磁盘满了", box["e"].raw)                                  # the OS's own words, not a character changed
        self.assertEqual([c.args[0].pid for c in gun.call_args_list], seen)      # that shot is aimed at this tree
        self.assertTrue(gone(seen[0]), "never registered and never killed ⇒ exactly the kind of orphan the bridge cannot manage")
        self.assertEqual(len(lines), 1, lines)


class SpawnWindow(_Staged):
    """🔴`Popen` has already started, and this `_Pipe` has not been handed to anyone yet — any exception escaping
    through this stretch means a resident CLI that no one recognizes and no one can kill (the window is real:
    `child_add` has to ask for a birth id and write the registry table, and either step can throw. Before
    switching to ctypes, just the birth id alone took roughly 0.8s on win32, now it's microseconds — ⚠️the window
    narrowed, it never disappeared: this gate is about any exception, regardless of how wide the window is).

    ⭐The gate is built to the shape of the bug: the shape is "throwing an exception leaks it", never "throwing
      `OSError` leaks it". The `RegistryWriteFailure` test above only pins the `OSError` cell, because the
      original code only did `except OSError` — but `child_add`'s contract only promises the two shapes
      "returns `False`" and "throws `OSError`", and this stretch can still let someone else's exception fly out
      (a `RuntimeError` from a thread that fails to start, `KeyboardInterrupt`, `MemoryError`).
    ⭐The two injection points each pin down one half, never the same thing twice:
      - blows up before registration ⇒ the entry was never recorded ⇒ just kill the process + close the fds,
        and never un-register an entry that never existed;
      - blows up after registration ⇒ the entry was recorded ⇒ besides killing the process, it must also be
        un-registered, or `children.json` is left with a dead pid.
      Testing only the first would leave this test all green even if the second one were missed."""

    def _spy_popen(self):
        """Record the Popen of that CLI child process. ⚠️Record only the one whose argv carries the fake CLI: the
        reaping path's `taskkill` (tree-kill on win32) / `ps` (asking for a birth id on POSIX) also go through
        `subprocess` ⇒ without filtering, those would get recorded instead."""
        made = []
        real = scv.subprocess.Popen

        def spy(*a, **kw):
            p = real(*a, **kw)
            if helpers.FAKE in " ".join(str(x) for x in (a[0] if a else kw.get("args") or [])):
                made.append(p)
            return p

        return made, spy

    def test_any_exception_after_spawn_buries_the_child(self):
        for where, patch_target, boom in (
                ("before registration", "child_add", ValueError("child_add itself blew up (not part of its contract)")),
                ("after registration", "queue", RuntimeError("could not start"))):
            with self.subTest(where=where):
                made, spy = self._spy_popen()
                rows_before = len(scv.children())
                if patch_target == "child_add":
                    inject = mock.patch.object(scv, "child_add", side_effect=boom)
                else:
                    # registration already succeeded, and it blows up on the very next line (`self._q = queue.Queue()`)
                    inject = mock.patch.object(scv.queue, "Queue", side_effect=boom)
                with mock.patch.object(scv.subprocess, "Popen", spy), inject, \
                        contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(type(boom)):
                        scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("sw-" + where))
                self.assertEqual(len(made), 1, "never landed inside that window, this run tested nothing")
                pid = made[0].pid
                # (1) the process really is gone (never "the constructor threw an exception")
                self.assertTrue(gone(pid), "started but no one killed it = an orphan the bridge cannot manage, exactly the shape the registry is meant to prevent")
                # (2) the registry table is back to how it was: blew up before registration = there was never a
                #     row; blew up after registration = that row must be un-registered
                self.assertEqual(len(scv.children()), rows_before, scv.children())
                # (3) the three fds on the parent side must be closed too (A8: on Linux, RLIMIT_NOFILE is hit
                #     first, never memory)
                self.assertEqual([p.closed for p in (made[0].stdin, made[0].stdout, made[0].stderr)],
                                 [True, True, True])

    def test_a_clean_spawn_leaves_all_three_intact(self):
        """Zero-input control: take away the injection and walk the same path again — the process is alive, it's
        in the table, and all three fds are open. Without this line, the test above cannot be told apart from
        "ClaudeDriver simply cannot start at all"."""
        made, spy = self._spy_popen()
        with mock.patch.object(scv.subprocess, "Popen", spy):
            d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("sw-ok"))
        try:
            self.assertEqual([c["pid"] for c in scv.children() if c["pid"] == d.pid], [d.pid])
            self.assertEqual([p.closed for p in (made[0].stdin, made[0].stdout, made[0].stderr)],
                             [False, False, False])
        finally:
            d.close()


class StderrTailIsSerialised(_Staged):
    """`stderr_tail()` (reads) and the pump thread (writes) touch the same deque ⇒ both doors must use the same
    lock.

    🔴But the mechanism review gave cannot be reproduced on this machine, I measured it (2026-09-22, py3.12.10, 2s
    per arm):

        arm=join  RuntimeError=0       read=2,876,325  write=9,232,786     <- the production line `"".join(deque)`
        arm=list  RuntimeError=0       read=5,438,712  write=9,281,069
        arm=iter  RuntimeError=48      read=3,781,250  write=9,180,109     <- `for x in dq: pass`
        (pushed switchinterval down to 1e-6 and ran another round: join/list are still 0, iter climbed to 213,487)

      ⇒ `deque mutated during iteration` can only ever catch iteration at the Python layer; `str.join(deque)` runs
        the whole statement in C, without releasing the GIL midway ⇒ a concurrent append cannot squeeze in. My
        original test ("hammer it for a second and see if it blows up") stayed green three times in a row on
        lock-free code — it was a blind ruler, and a blind ruler is worse than no ruler at all (it looks like
        coverage). ⇒ Replaced with the test below.

    ⭐The lock stays, but for a real reason: (1) under a free-threaded (no-GIL) build that join is no longer
      atomic, and the bridge runs on the player's own machine; (2) the moment the next person writes `stderr_tail`
      as `[x for x in self._err]` or adds a third door, the race condition becomes real immediately (the
      `arm=iter` row is exactly what that looks like). ⇒ what really carries the weight in this cell is the
      structural gate; the behavioral test here only pins "the same lock"."""

    def test_both_doors_are_blocked_by_the_same_lock(self):
        """The mechanism half, deterministic: hold `_err_lock`, and both doors must be blocked outside it; release
        it and they must get through immediately. Never look only at whether the source has a `with` in it —
        each door taking its own separate lock, or only one door being inside the lock, both still have a
        `with`."""
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("e1"))
        try:
            for name, door in (("_err_add", lambda: d.pipe._err_add("one line")),
                               ("stderr_tail", d.pipe.stderr_tail), ("mark_ok", d.pipe.mark_ok)):
                with self.subTest(door=name):
                    started, done = threading.Event(), threading.Event()

                    def knock(door=door, started=started, done=done):
                        started.set()      # ⭐raise the flag before going through the door
                        door()
                        done.set()

                    t = threading.Thread(target=knock, daemon=True)
                    with d.pipe._err_lock:
                        t.start()
                        # ⚠️Using `blocked = not done.wait(0.5)` alone has a narrow crack for a false green: when
                        #   the machine is extremely bogged down, even a door with no lock on it at all might not
                        #   get scheduled within 0.5s ⇒ that instant would be judged "blocked".
                        #   ⇒ prove it really did start running first, then judge whether it's blocked; that way
                        #   `blocked` says "it hit the lock", never "it just hasn't had its turn yet". (This crack
                        #   cannot actually be produced — it would require stalling the scheduler for 0.5s — so it
                        #   has no negative control; this trades away an extremely low-probability false green for
                        #   determinism, it does not fix a bug that was ever observed.)
                        self.assertTrue(started.wait(5), name + " that thread was never scheduled to run at all")
                        blocked = not done.wait(0.5)
                    ok = done.wait(5)
                    t.join(timeout=5)
                    self.assertTrue(blocked, name + " was not blocked by this lock ⇒ either it has no lock, or it's holding a different one")
                    self.assertTrue(ok, name + " still could not get through after the lock was released ⇒ deadlock")   # assert the correct value, never just "blocked is enough"
        finally:
            d.kill()

    def test_only_the_known_doors_touch_the_deque_and_each_takes_the_lock(self):
        """The structural half, and this is the one that really carries the weight in this cell. ⭐The test above
        only proves "today's doors use the same lock"; only this gate can stop the next person who adds another
        place that touches `_err` (especially one written as Python-layer iteration).
        ⭐Task 12 added a third door, `mark_ok` (the watermark: clear it on a clear success, carry-forward D17) —
          it was explicitly added into both of these sets, and it was also added to the behavioral gate's door
          list above (it too must be blocked while the lock is held)."""
        cls = [n for n in ast.walk(src_tree()) if isinstance(n, ast.ClassDef) and n.name == "_Pipe"][0]
        touch, locked = set(), set()
        for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
            if any(isinstance(n, ast.Attribute) and n.attr == "_err" for n in ast.walk(fn)):
                touch.add(fn.name)
            for w in [n for n in ast.walk(fn) if isinstance(n, ast.With)]:
                inside = list(ast.walk(w))
                if any(isinstance(n, ast.Attribute) and n.attr == "_err_lock" for n in inside) \
                        and any(isinstance(n, ast.Attribute) and n.attr == "_err" for n in inside):
                    locked.add(fn.name)
        # There is no second thread yet when `__init__` builds it ⇒ that spot needs no lock; only these doors are
        # allowed to touch it.
        self.assertEqual(touch, {"__init__", "_err_add", "stderr_tail", "mark_ok"})
        self.assertEqual(locked, {"_err_add", "stderr_tail", "mark_ok"})


class ClassifyIsReallyConsumed(_Staged):
    """⭐As of this batch, `classify()`'s production-side hit count is 0 (only tests call it) ⇒ this is its first
    real consumer.
    Three cells side by side: one for each of the two recognizable categories, plus the cell for "when it cannot
    be recognized, use the caller's default"."""

    def test_quota_wording_from_the_cli_becomes_quota(self):
        mode("quota")
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("q1"))
        try:
            with self.assertRaises(scv.BridgeError) as cm:
                d.turn("x", lambda s: None, 20, 10, None)
        finally:
            d.kill()
        self.assertEqual((cm.exception.klass, cm.exception.raw),
                         ("quota", "You've hit your session limit · resets 3pm"))

    def test_an_unreadable_crash_takes_the_default_we_passed_in(self):
        """⭐What this pins down is "where `crashed` comes from": first prove this real original text is not
        recognized by either group of pattern strings (otherwise it might just be a lucky guess), then prove the
        driver received exactly the default we passed in. Never guessing wildly when it cannot be recognized is
        exactly what this gate is protecting against."""
        mode("crash")
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("q2"))
        with self.assertRaises(scv.BridgeError) as cm:
            d.turn("x", lambda s: None, 20, 10, None)
        # the ruler is not blind: ask it about the actual original words it received, and it really cannot read
        # them (never a phrase I hand-picked)
        self.assertEqual(scv.classify(cm.exception.raw), "unknown")
        self.assertEqual(cm.exception.klass, "crashed")


class ShoutsOnce(_Staged):
    """Errors must be loud (a global constraint): every failure path lands one line in bridge.log and also
    returns to the caller.
    ⭐The logging point was chosen at `_fail()` — the only door in the driver layer that constructs a
      `BridgeError` ⇒ one line per failure, never one line per request, and any new failure path added later gets
      recorded automatically too (the structural half is in `NoSilentFailurePath`)."""

    CASES = (("crash", "crashed"), ("auth", "auth_required"), ("quota", "quota"))

    def test_every_failure_lands_exactly_one_line_carrying_class_and_raw(self):
        for i, (m, klass) in enumerate(self.CASES):
            with self.subTest(mode=m):
                mode(m)
                d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("g%d" % i))
                box = {}

                def go():
                    try:
                        d.turn("x", lambda s: None, 20, 10, None)
                    except scv.BridgeError as exc:
                        box["e"] = exc

                lines = log_lines_during(go)
                if d.alive():
                    d.kill()
                self.assertIn("e", box, "this turn never actually failed, the lines below are pointless")
                self.assertEqual(len(lines), 1, lines)
                self.assertIn(klass, lines[0])                              # the class (the only signal once the pattern strings go stale)
                self.assertIn(scv._one_line(box["e"].raw), lines[0])        # the CLI's original words, not a character changed

    def test_a_turn_that_works_lands_nothing(self):
        """Zero-input control: take away "failure" and measure again. Without this line, the test above cannot be
        told apart from "it writes a line on every single turn"."""
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("g9"))
        try:
            lines = log_lines_during(lambda: d.turn("x", lambda s: None, 20, 10, None))
        finally:
            d.close()
        self.assertEqual(lines, [])

    def test_a_multi_line_original_wording_folds_into_one_line_but_loses_nothing(self):
        """⚠️Foreign text must be folded into one line before it reaches `log()` (Task 4's rule) — folded, never
        truncated: in node's original crash text, the sentence that actually tells someone how to fix it is in
        the later lines, and `splitlines()[0]` throws exactly that away.
        On the other end, B21 requires "the original words, not a character changed" ⇒ `e.raw` must keep its
        multiple lines byte for byte."""
        raw = "node:internal/modules/cjs/loader:1234" + NL + "      throw err;" + NL \
            + "Error: Cannot find module 'yoga-wasm-web'"
        box = {}
        lines = log_lines_during(lambda: box.__setitem__("e", scv._fail("crashed", raw, "claude")))
        self.assertEqual(box["e"].raw, raw)                                  # what's handed to the caller: the original words, byte for byte
        self.assertEqual(len(lines), 1, lines)                               # what lands on disk: one line
        self.assertIn("Cannot find module", lines[0])                        # the last line (the real reason) was not thrown away
        self.assertIn("loader:1234", lines[0])                               # the first line is still there too

    def test_the_same_thing_end_to_end_through_a_real_cli_stderr(self):
        """⭐The test above feeds it a synthetic string (calling `_fail` directly) ⇒ it cannot prove "this still
        holds all the way from the CLI's real stderr". This test goes through the stub's real multi-line crash
        text.
        ⚠️`stderr_tail()`'s `[-500:]` truncates the head of the original text ⇒ B21's "byte for byte" only holds
        within those 500 characters."""
        mode("crash")
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("m1"))
        box = {}

        def go():
            try:
                d.turn("x", lambda s: None, 20, 10, None)
            except scv.BridgeError as exc:
                box["e"] = exc

        lines = log_lines_during(go)
        self.assertIn(NL, box["e"].raw, "the stub's crash text was not multi-line ⇒ this test tested nothing")
        self.assertIn("boom: fake crash", box["e"].raw)            # the first line
        self.assertIn("Cannot find module", box["e"].raw)          # the last line is the real reason
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("Cannot find module", lines[0])


class NoSilentFailurePath(_Staged):
    """⭐A structural gate, built to the shape of the bug: what it's missing is never today's raise statements,
    it's the one added in the future — the moment it directly writes `raise BridgeError(...)`, it bypasses the
    log, and all three tables stay green.
    (The behavioral half is in `ShoutsOnce`, and it can only cover the failure paths that can be counted today.)

    🔴Choose the scanning surface by shape, never by name: it used to hard-code `("_Pipe", "ClaudeDriver")` ⇒ Task
    7's `CodexDriver` would be silently skipped, and the lower bound on the "ruler is not blind" total count was
    already satisfied by the `_Pipe` class alone ⇒ the gate quietly stopped covering it, and all three tables
    stayed green.
    "A gate that misses things is worse than no gate at all"."""

    @staticmethod
    def _is_driver(node):
        return isinstance(node, ast.ClassDef) and (node.name == "_Pipe" or node.name.endswith("Driver"))

    @classmethod
    def _raises(cls, tree):
        """[(class name, line number, whether it went through _fail)]: every raise in the body of every driver
        class."""
        found = []
        for node in [n for n in ast.walk(tree) if cls._is_driver(n)]:
            for r in [n for n in ast.walk(node) if isinstance(n, ast.Raise)]:
                door = isinstance(r.exc, ast.Call) and getattr(r.exc.func, "id", "") == "_fail"
                found.append((node.name, r.lineno, door))
        return found

    def test_every_raise_in_the_drivers_goes_through_the_logging_door(self):
        """🔴🔴This gate's boundary: it being green never means the rule is being followed everywhere (the settled
        idiom entry exists precisely because of it, and was retired for that reason):
        the scope is the driver layer (`SessionManager`'s few `raise` statements are a deliberate, ruled-on
        exception: a bad request / a caller cancelling are both normal control flow, and writing a disk line for
        every single one would turn `bridge.log` into background noise);
        the scanning surface is by class name (`_is_driver`: `_Pipe` or `*Driver`).
        ⇒ Whatever new classes Task 10/11 add (an HTTP handler, a remote leg), as long as they are doing the job
        of "translating a CLI/network failure for the layer above", that makes them play the driver-layer role ⇒
        they must be added to this gate's scanning surface, otherwise the rule is retired while the gate still
        cannot see it."""
        tree = src_tree()
        rows = self._raises(tree)
        self.assertEqual(["%s@%d" % (c, ln) for c, ln, door in rows if not door], [])
        # ⭐"The ruler is not blind" is counted per class, never as one grand total: with a grand total, the
        #   `_Pipe` class alone could satisfy the lower bound, and it would still be green even if `ClaudeDriver`
        #   (or a future `CodexDriver`) were missed entirely.
        seen = {c for c, _ln, _d in rows}
        found = {n.name for n in ast.walk(tree) if self._is_driver(n)}
        self.assertGreaterEqual(len(found), 2, found)
        self.assertEqual(seen, found,
                         "every driver class should have at least one raise scanned; if some family genuinely "
                         "has none, write it into this sentence and say why, never let it silently sit outside "
                         "the scanning surface")

    def test_the_scanner_is_not_blind(self):
        """Positive control: feed synthetic samples to the missed-detection surface, never rely only on shapes
        that happen to exist in the real file. A cell is laid down for the false-alarm side too."""
        def parsed(name, body):
            return ast.parse("class " + name + ":" + NL + "    def f(self):" + NL + "        " + body + NL)

        self.assertEqual(self._raises(parsed("_Pipe", "raise BridgeError('x', 'y', 'z')")),
                         [("_Pipe", 3, False)])
        self.assertEqual(self._raises(parsed("_Pipe", "raise _fail('x', 'y', 'z')")),
                         [("_Pipe", 3, True)])
        # 🔴the family the next batch adds: never let it be silently skipped just because it's not named ClaudeDriver
        self.assertEqual(self._raises(parsed("CodexDriver", "raise BridgeError('a', 'b')")),
                         [("CodexDriver", 3, False)])
        # the false-alarm side: a raise that is not in a driver class must never be called out
        self.assertEqual(self._raises(parsed("Elsewhere", "raise BridgeError('a', 'b')")), [])


class ReapsItsDead(_Staged):
    """⭐"Whoever calls Popen reaps the child" — `scv.py` writes this rule itself, in `proc_rss_kb`'s docstring.

    🔴The path where the CLI crashes (stdout EOF) used to only un-register, never reap. This cell is nearly
      invisible on Windows, but the bridge is meant to deploy on Linux: on POSIX, a dead child that no one reaps
      is a zombie. ⏳Whether a zombie can fool the two rulers `proc_rss_kb`/`proc_start_id` was not measured this
      round (only run on Windows), so it's an inference, never a measurement — but "whoever calls Popen reaps the
      child" already holds regardless.
    ⚠️The criterion is `returncode` (the exit code received), never `alive()`: `alive()` goes through `poll()`,
      which reaps the child as a side effect on its own ⇒ using it as the criterion would leave this gate green
      even on code that reaps no one (a false negative)."""

    def test_a_crashed_cli_is_reaped_not_left_as_a_zombie(self):
        mode("crash")
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("z1"))
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(scv.BridgeError):
                d.turn("x", lambda s: None, 20, 10, None)
        self.assertEqual(d.pipe.popen.returncode, 3)   # assert the correct value: the 3 from the stub's sys.exit(3) really was received


class ReadFailureIsNotAnEof(_Staged):
    """🔴The bridge's own read failures must never be laundered into "the CLI finished talking".

    The `except (OSError, ValueError)` in both pumps was originally meant to catch just one thing —
    `_close_pipes()` closing a pipe that `readline()` is in the middle of reading. But the whole `OSError` family
    comes along for free: an fd closed by someone else, EMFILE, a driver-layer I/O error all throw it ⇒ a real
    read failure also gets swallowed into a quiet sentinel, walks down the EOF path, kills a perfectly healthy
    child process, and then runs `classify()` over a stale stderr tail.

    ⭐So the criterion here is never "did it throw an exception", it's "what did it say":
      (1) the original text must contain "read...blew up" (never a conclusion about the upstream guessed from
          stderr);
      (2) positive control: swap the stderr banner for a quota phrase, and `klass` must still be `crashed` —
          otherwise a bridge I/O failure would get reported as "you hit your quota, retryable" (`retryable=True`).
    ⚠️The injection point is chosen at `readline` itself, never at mocking out `_pump_out`: that's exactly where
      an OS read failure really arrives."""

    class _Boom:
        """Wraps the real stdout: the nth call to `readline()` throws OSError, and it behaves normally afterward.
        Never touches the child process; it stays alive the whole time."""

        def __init__(self, real, at):
            self._real, self._left = real, at

        def readline(self, *a):
            self._left -= 1
            if self._left == 0:
                raise OSError(5, "Input/output error")
            return self._real.readline(*a)

        def __getattr__(self, name):
            return getattr(self._real, name)

    def _blow_up_on_read(self, banner):
        mode("hang")                      # the child process is alive and has no intention of saying anything
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("rf" + str(abs(hash(banner)) % 999)))
        d.pipe._err_add(banner)           # goes through the one write entry point, simulating a stray stderr line the CLI printed earlier
        d.pipe.popen.stdout = self._Boom(d.pipe.popen.stdout, 1)
        # the pump is already stuck in the real stdout's readline ⇒ start a new one against the wrapped object, a
        # shape consistent with a real OS read failure
        threading.Thread(target=d.pipe._pump_out, daemon=True).start()
        try:
            with self.assertRaises(scv.BridgeError) as cm:
                d.turn("x", lambda s: None, 25, None, None)
        finally:
            d.kill()
        return cm.exception

    def test_a_read_failure_says_so_instead_of_guessing_from_stale_stderr(self):
        e = self._blow_up_on_read("CLI is perfectly healthy")
        self.assertEqual(e.klass, "crashed")
        self.assertIn("blew up", e.raw)
        self.assertIn("Input/output error", e.raw)       # assert the correct value: the real errno carries through as-is (B21)

    def test_a_read_failure_is_not_reclassified_as_an_upstream_quota(self):
        """⭐Positive control: the exact same run, only the stderr banner is swapped for a quota phrase. If the
        criterion were broken (fell back to `classify(tail)`), this test would get `quota` + `retryable=True` —
        a bridge I/O failure of its own reported as "you hit your quota, wait for it to recover"."""
        e = self._blow_up_on_read("You've hit your usage limit, resets 3pm")
        self.assertEqual(scv.classify("You've hit your usage limit, resets 3pm"), "quota")   # the ruler is not blind
        self.assertEqual(e.klass, "crashed")
        self.assertNotIn(e.klass, scv.RETRYABLE[2:3])    # never allowed to turn into a retryable quota
        self.assertIn("blew up", e.raw)

    def test_the_stderr_pump_shouts_too(self):
        """⚠️The stderr pump originally had no test at all — measured by NC-19b: strip out its bookkeeping line
        and the two tests above are still all green (they go through the stdout path). And it's more silent than
        the stdout one: when the stderr pump blows up, the tail is simply a little shorter, and that tail is
        `classify()`'s only input ⇒ the statement "stderr is empty" could itself be false.
        ⇒ The criterion is whether it shouted or not (the line on disk names stderr word for word, plus the real
        errno)."""
        mode("hang")
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("rf8"))
        d.pipe.popen.stderr = self._Boom(d.pipe.popen.stderr, 1)
        try:
            lines = log_lines_during(lambda: d.pipe._pump_err())
        finally:
            d.kill()
        hit = [x for x in lines if "blew up" in x]
        self.assertEqual(len(hit), 1, lines)
        self.assertIn("stderr", hit[0])                       # assert the correct value: names exactly which pump
        self.assertIn("Input/output error", hit[0])
        self.assertIn("stderr", d.pipe._read_error or "")

    def test_the_second_pump_failure_is_not_logged_as_the_first(self):
        """🔴`_read_error` only records the first one (whatever comes after is mostly its knock-on effect), but
        what lands on disk must be this failure's own line. It used to print `self._read_error` in both places ⇒
        when the two pumps blow up one after the other, the two lines on disk are identical, both saying stdout,
        while "the stderr pump blew up too" has not a single character on disk — yet another error message that
        would be lying.
        ⚠️The tests above, which each blow up only one pump, cannot catch this cell (`_read_error` is None ⇒ what
        gets printed happens to be exactly this failure), so this test has to make two blow up one after the
        other."""
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("rf12"))
        try:
            lines = log_lines_during(lambda: (d.pipe._read_failed("stdout", OSError(5, "boom-a")),
                                              d.pipe._read_failed("stderr", OSError(5, "boom-b"))))
        finally:
            d.kill()
        hits = [x for x in lines if "blew up" in x]
        self.assertEqual(len(hits), 2, lines)
        self.assertIn("stdout", hits[0])
        self.assertIn("stderr", hits[1])                 # assert the correct value, never just "the two lines differ"
        self.assertIn("boom-b", hits[1])
        self.assertIn("stdout", d.pipe._read_error)      # what's handed to the layer above is still the first failure

    def test_close_pipes_is_bounded_and_names_the_fd_it_gave_up_on(self):
        """`_close_pipes()` has an upper bound, and any fd it gives up on must be named.

        🔴Never read this test as "the test for the `_closing` gate" — it is not. The plain truth, written down
          here: `_closing` has zero tests today (turning the whole gate into `if False:` leaves the full run with
          not a single red, measured twice during review). The reason is not that no one wrote it, it's that the
          window cannot be produced today: `_close_pipes()` now never closes a pipe that still has a pump alive on
          it, and even if it did close one on win32, what throws is not the reading thread (it's the closing side
          that gets stuck, measured at 24.04s). ⇒ That flag is only a fallback for a POSIX path that has not been
          measured, at a cost of four lines. Never write "cannot do it" as if it were "done".

        🔴I got this window wrong twice; writing it down here so the next person does not have to redo it:
          (1) going through `close()`'s EOF path ⇒ the child process reads stdin EOF and exits on its own, and
              neither pump throws at all;
          (2) switching to `hang` mode and then `close()` still does not throw — `close()`'s timeout calls
              `kill()`, and once the child process dies, `readline()` returns `b""` (measured this round) ⇒ the
              pump exits cleanly.
          ⇒ The only real window is `grandchild` + killing only the parent (the grandchild process holds onto the
          write end), which leaves the pump waiting for an EOF that never comes.

        The criterion is the upper bound: before the fix, this call was blocked until the grandchild process
        finished sleeping (measured at 121s; a bare `close()` call measured at 24.04s; the zero-input control at
        0.000s)."""
        # 🔴Producing this window is harder than it looks, I got it wrong twice, writing it down here:
        #   (1) `close()` going through the normal EOF path ⇒ neither pump throws at all (the version review
        #       pointed out);
        #   (2) switching to `hang` mode and then `close()` still does not throw either — `close()`'s timeout
        #       calls `kill()`, and once the child process dies, `readline()` returns `b""`, never an exception
        #       (measured this round) ⇒ the pump exits cleanly, join succeeds, and no one is reading when the fds
        #       are closed. This version was only caught because NC-23 stayed green.
        #   ⇒ There is only one real window: the instant the fd is closed, the pump is still stuck inside
        #     `readline()`. To produce it, the write end has to still be in someone else's hands ⇒ use
        #     `grandchild` (the grandchild process inherits our stdout/stderr), then kill only the parent process
        #     (never a tree-kill) ⇒ the pump waits forever for an EOF that never comes ⇒ join times out ⇒ closing
        #     the fd lands right on the read.
        mode("grandchild")
        mark = len(helpers.read_fake_log())
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("rf9"))
        d.turn("x", lambda s: None, 20, 10, None)
        gcs = [x["grandchild"] for x in helpers.fake_log_since(mark) if "grandchild" in x]   # never `[-1]`: see helpers
        try:
            self.assertEqual(len(gcs), 1, gcs)
            d.pipe.popen.kill()                      # kill only the parent: the grandchild process still holds our pipes
            d.pipe.popen.wait(timeout=10)
            t0 = time.time()
            lines = log_lines_during(d.pipe._close_pipes)
            took = time.time() - t0
            # ⭐This is the real criterion: `_close_pipes()` has an upper bound. Before the fix it was blocked here
            #   until the grandchild process finished sleeping (measured at 121s; a bare `close()` call measured
            #   at 24.04s; the zero-input control at 0.000s).
            self.assertLess(took, 8, "_close_pipes() was blocked for %.1fs ⇒ kill()'s time budget does not hold at all" % took)
            self.assertGreater(took, 1.5, "neither of the two join(timeout=1) calls actually waited ⇒ %.2fs is too fast" % took)
            # missing an fd must shout, and when both pumps are stuck together it must be possible to tell which
            # one (never two identical lines)
            skipped = [x for x in lines if "did not get closed" in x]
            self.assertEqual(len(skipped), 2, lines)
            self.assertEqual(sorted("stdout" in x for x in skipped), [False, True])
            self.assertEqual(len([x for x in skipped if "stderr" in x]), 1, skipped)
            # the pump reads an EOF, never an exception (measured on win32) ⇒ not a single "blew up" is allowed on this path
            self.assertEqual([x for x in lines if "blew up" in x], [], lines)
            self.assertIsNone(d.pipe._read_error)
        finally:
            for gc_pid in gcs:
                scv.kill_pid_tree(gc_pid)
            d.kill()

    def test_a_real_read_failure_during_close_is_still_loud(self):
        """🔴Whether the flag is raised before or after the join is two different things. Raised before ⇒ every
        real read failure during the whole join stretch gets swallowed, and `read_until`'s EOF path is exactly
        this order: `join` -> `kill()` (raises the flag) -> read `_read_error` ⇒ measured during review: the
        stderr pump blows up at 3s, `_read_error` stays `None`, zero lines on disk, and what that turn hands back
        is `crashed: CLI process exited (exit=…), stderr is empty` — and "stderr is empty" is exactly the
        sentence `_pump_err`'s own comment says could be false.
        ⭐The criterion: during the join stretch we have not closed a single fd yet ⇒ whatever blows up in there
        must be a real error, and it must shout."""
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("rf11"))
        d.turn("x", lambda s: None, 20, 10, None)

        def boom_during_join():
            # the pump blows up inside `_close_pipes()`'s join window ⇒ the flag should not be raised yet at that point
            d.pipe._read_failed("stderr", OSError(5, "Input/output error"))

        real_join = d.pipe._out_thread.join
        d.pipe._out_thread.join = lambda *a, **kw: (boom_during_join(), real_join(*a, **kw))[1]
        try:
            lines = log_lines_during(d.close)
        finally:
            d.kill()
        self.assertEqual(len([x for x in lines if "blew up" in x]), 1, lines)
        self.assertIn("stderr", d.pipe._read_error or "")


class ParentSidePipes(_Staged):
    """⭐The three pipe fds on the parent side must be reclaimed by closing them, never by letting reference
    counting take `Popen` down with it.

    🔴`close()` used to close only stdin, and `kill()` closed none at all (the failure path in `__init__` does
      close all three — the same piece of reasoning was only one-third carried out) ⇒ that string of
      `ResourceWarning: unclosed file` in the full run is exactly this.
    ⭐Why this cell deserves its own gate: an fd is one of the hardest ceilings among "resources counted per
      session" — process count is limited by memory, an fd is limited by `RLIMIT_NOFILE` (1024 by default on
      Linux), and measured at 15 handles per session on win32 ⇒ once deployed to Linux, fds get hit before memory
      does. And the reference chain that fences it in today (`_sessions` -> `_Session` -> driver -> `_Pipe` ->
      `Popen`) breaks the moment it is snagged by any exception's traceback or a single debugging reference.
    ⚠️One test per family, never writing only one: today both families delegate `close()`/`kill()` straight to
      the same `_Pipe`, but that is today's implementation; these two tests pin down the interface contract, and
      the moment anyone pulls a family out to implement it on its own, this goes red immediately."""

    def _both(self, tag):
        return (("claude", lambda: scv.ClaudeDriver(CFG, "haiku", None, "s", workdir(tag + "c"))),
                ("codex", lambda: scv.CodexDriver(CFG, Codex.MODEL, "low", "s", workdir(tag + "x"))))

    def test_close_shuts_all_three_parent_side_pipes(self):
        for family, make in self._both("fd1"):
            with self.subTest(family=family):
                d = make()
                p = d.pipe.popen
                # zero-input control: first prove they were open to begin with (otherwise the line below might be
                # trivially true from the start)
                self.assertEqual([x.closed for x in (p.stdin, p.stdout, p.stderr)], [False, False, False])
                self.assertEqual(d.close(), "exited")
                self.assertEqual([x.closed for x in (p.stdin, p.stdout, p.stderr)], [True, True, True])

    def test_kill_shuts_all_three_parent_side_pipes(self):
        for family, make in self._both("fd2"):
            with self.subTest(family=family):
                d = make()
                p = d.pipe.popen
                self.assertEqual([x.closed for x in (p.stdin, p.stdout, p.stderr)], [False, False, False])
                d.kill()
                self.assertEqual([x.closed for x in (p.stdin, p.stdout, p.stderr)], [True, True, True])
                self.assertFalse(d.alive())


class Close(_Staged):
    """`close()`'s two return values (`exited`/`killed`) are part of the interface ⇒ both sides need someone
    watching them, otherwise if the "graceful close" path breaks, no one would know — it breaks silently: the
    process is leaked, and the return value is still a perfectly good-looking string."""

    def _turns_seen(self):
        return len([x for x in helpers.read_fake_log() if "turn" in x])

    def test_a_well_behaved_cli_exits_when_stdin_closes(self):
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("x1"))
        d.turn("x", lambda s: None, 20, 10, None)
        self.assertEqual(d.close(), "exited")
        self.assertFalse(d.alive())
        self.assertEqual([c for c in scv.children() if c["pid"] == d.pid], [])

    def test_a_cli_that_will_not_leave_gets_killed_and_says_so(self):
        mode("hang")
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("x2"))
        was = self._turns_seen()
        d.pipe.send({"type": "user", "message": {"role": "user", "content": "x"}})
        # ⭐The sync point is a record the stub keeps of itself (it writes it before the sleep), never a sleep
        #   statement: if stdin is closed before it enters that sleep, it exits normally via EOF ⇒ this test would
        #   no longer be testing "lingers and refuses to leave".
        end = time.time() + 10
        while time.time() < end and self._turns_seen() == was:
            time.sleep(0.05)
        self.assertGreater(self._turns_seen(), was, "the stub has not received this question yet, so close cannot test 'lingers and refuses to leave'")
        pid = d.pid
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(d.pipe.close(grace=1.0), "killed")
        self.assertFalse(d.alive())
        self.assertEqual([c for c in scv.children() if c["pid"] == pid], [])


class Codex(_Staged):
    """The codex family: `app-server` is resident, JSON-RPC over stdio.

    ⭐Written to the same shape as `ClaudeDriver`, but never assume the two families are the same: over there,
      sending one `user` message is the whole story; here `__init__` has a whole extra stretch of handshaking
      (`initialize` -> `initialized` -> `thread/start`), an entire extra family of failure paths."""

    MODEL = "gpt-6-luna"     # real CLI probes always use the cheap tier

    def test_two_turns_stay_on_one_thread(self):
        mark = len(helpers.read_fake_log())
        d = scv.CodexDriver(CFG, self.MODEL, "low", "你是乙", workdir("cx1"))
        got = []
        try:
            r1 = d.turn("第一问", got.append, 20, 10, None)
            r2 = d.turn("第二问", got.append, 20, 10, None)
        finally:
            d.close()
        # ⭐`echo[2]` can only be counted this way within the same process (a new process would restart counting
        #   from 1) ⇒ this line is the criterion for "resident" in and of itself
        self.assertEqual((r1["text"], r2["text"]), ("echo[1]: 第一问", "echo[2]: 第二问"))
        self.assertEqual("".join(got), "echo[1]: 第一问echo[2]: 第二问")
        self.assertIsNotNone(r1["ttfc"])
        # ⚠️This turn's usage reads `last`, never `total` (`total` is context occupancy); the stub emits both, and
        # reading the wrong cell gets you 300
        self.assertEqual(r2["usage"]["input_tokens"], 20)
        self.assertEqual(r2["usage"]["cached_input_tokens"], 0)
        # ⭐take it by window (never `[-1]`/`[-2:]`: see helpers.fake_log_since) — especially the effort statement
        #   at the end: the two `low` entries left over from a previous test case would trivially satisfy "the
        #   last two are low", so this cell would still be green even without carrying effort
        log = helpers.fake_log_since(mark)
        starts = [x for x in log if "thread_params" in x]
        self.assertEqual(len(starts), 1, starts)
        start = starts[0]
        self.assertEqual(start["system"], "你是乙")
        self.assertEqual((start["thread_params"]["sandbox"], start["thread_params"]["ephemeral"]),
                         ("read-only", True))
        self.assertEqual(start["thread_params"]["model"], self.MODEL)
        self.assertEqual(Path(start["thread_params"]["cwd"]).resolve(), workdir("cx1").resolve())
        boots = [x for x in log if x.get("family") == "codex"]
        self.assertEqual(len(boots), 1, boots)
        self.assertEqual(Path(boots[0]["cwd"]).resolve(), workdir("cx1").resolve())
        # ⭐effort is a field on every single `turn/start` (it's not in `thread/start`) ⇒ it must be carried on
        # both turns
        self.assertEqual([x.get("effort") for x in log if "effort" in x], ["low", "low"])

    def test_ttfc_ignores_the_user_echo(self):
        """A real app-server echoes back our own user message in 0.006s; treating "the first event" as the first
        token ⇒ the reading would always be 0 and all three tables would be green.

        ⭐Both bounds are needed: the lower bound guards against "treating the echo as the first token", the upper
        bound guards against "getting the origin point wrong" (say, counting from process start)."""
        mode("slow_first", FAKE_DELAY="1.2")
        d = scv.CodexDriver(CFG, self.MODEL, "low", "s", workdir("cx2"))
        try:
            r = d.turn("x", lambda s: None, 20, 10, None)
        finally:
            d.close()
        self.assertEqual(r["text"], "echo[1]: x")
        self.assertGreaterEqual(r["ttfc"], 1.0, "the echo was treated as the first token (the stub only sleeps 1.2s after the echo)")
        self.assertLess(r["ttfc"], 5, "the first-token latency is far greater than the stub's 1.2s sleep ⇒ the origin point was most likely taken wrong")

    def test_an_empty_delta_is_not_the_first_token(self):
        """⭐The `if piece:` half originally had zero coverage: the stub had no frame where "delta is an empty
        string" ⇒ deleting it left the whole thing all green.
        ⚠️This frame is adversarial, never a measured shape — precisely because it cannot be measured, that half
        is a defensive criterion."""
        mode("empty_delta_first")
        d = scv.CodexDriver(CFG, self.MODEL, "low", "s", workdir("cx3"))
        t0 = time.time()
        with self.assertRaises(scv.BridgeError) as cm:
            d.turn("x", lambda s: None, 20, 1.0, None)
        took = time.time() - t0
        self.assertEqual(cm.exception.klass, "timeout")
        self.assertIn("no first byte of content out", cm.exception.raw)
        self.assertGreater(took, 0.9, "an empty delta was treated as the first token (with the gate removed entirely it would not fire at 1s)")
        self.assertLess(took, 6, "the gate is set at 1.0s, it fired after waiting %.3fs" % took)
        self.assertFalse(d.alive())

    def test_the_stall_window_this_driver_carries_is_really_used(self):
        """codex emits zero events while it's thinking ⇒ the stall gate is the only one watching "is it still
        there" for this family. If `turn()` forgot to pass `self.stall` through, this test would sit and wait for
        the whole turn to time out (30s) before going red."""
        mode("heartbeat_only")
        d = scv.CodexDriver(CFG, self.MODEL, "low", "s", workdir("cx4"))
        d.stall = 1.0
        t0 = time.time()
        with self.assertRaises(scv.BridgeError) as cm:
            d.turn("x", lambda s: None, 30, None, None)
        took = time.time() - t0
        self.assertIn("not a single byte out", cm.exception.raw)
        self.assertGreater(took, 0.9, "the gate is set at 1.0s, firing at %.3fs = it fired early" % took)
        self.assertLess(took, 6, "the gate is set at 1.0s, it fired after waiting %.3fs" % took)
        self.assertFalse(d.alive())

    def test_cancel_really_kills(self):
        """If `turn()` missed the `cancel` cell, no one would be watching once the cancel event arrives ⇒ it would
        sit and wait for the whole turn to time out."""
        mode("heartbeat_only")
        d = scv.CodexDriver(CFG, self.MODEL, "low", "s", workdir("cx5"))
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        t0 = time.time()
        with self.assertRaises(scv.BridgeError) as cm:
            d.turn("x", lambda s: None, 60, None, cancel)
        took = time.time() - t0
        self.assertEqual(cm.exception.klass, "cancelled")
        self.assertLess(took, 5)
        self.assertGreater(took, 0.4, "the cancel only happened at 0.5s, coming back at %.3fs = it did not wait for that event" % took)
        self.assertFalse(d.alive())

    def test_auth_error_is_classified_and_verbatim(self):
        mode("auth")
        d = scv.CodexDriver(CFG, self.MODEL, "low", "s", workdir("cx6"))
        try:
            with self.assertRaises(scv.BridgeError) as cm:
                d.turn("x", lambda s: None, 20, 10, None)
        finally:
            d.kill()
        self.assertEqual((cm.exception.klass, cm.exception.family), ("auth_required", "codex"))
        self.assertIn("401 Unauthorized", cm.exception.raw)

    def test_quota_wording_from_this_family_becomes_quota_too(self):
        """⭐The two families' failures travel through different fields: claude uses `result.is_error`, codex uses
        `turn.error.message` ⇒ "classify is really being consumed" has to be pinned again on this family's own
        path."""
        mode("quota")
        d = scv.CodexDriver(CFG, self.MODEL, "low", "s", workdir("cx7"))
        try:
            with self.assertRaises(scv.BridgeError) as cm:
                d.turn("x", lambda s: None, 20, 10, None)
        finally:
            d.kill()
        self.assertEqual((cm.exception.klass, cm.exception.raw),
                         ("quota", "You've hit your session limit · resets 3pm"))

    def test_a_turn_that_completes_without_any_text_is_loud(self):
        """`turn/completed` says it succeeded, yet there is not a single sentence of content ⇒ an empty string
        must never be handed up as the answer."""
        mode("no_answer")
        d = scv.CodexDriver(CFG, self.MODEL, "low", "s", workdir("cx8"))
        try:
            with self.assertRaises(scv.BridgeError) as cm:
                d.turn("x", lambda s: None, 20, 10, None)
        finally:
            d.close()
        self.assertEqual((cm.exception.klass, cm.exception.family), ("unknown", "codex"))
        self.assertIn("no content", cm.exception.raw)

    def test_a_turn_error_that_is_a_bare_string_does_not_escape_unclassified(self):
        """⚠️An adversarial cell: `turn.error` is a bare string, never an object (the protocol drifted / a
        different implementation).

        ⭐What needs guarding against is not "reading the wrong field", it's a bare escape: the `AttributeError`
          that `err.get("message")` throws against a str is never a `BridgeError`, not a single line lands on
          disk, and it specifically blows up right on the path where something has already gone wrong (wiping out
          the CLI's original words) — the same family as Task 6's three cells (the ValueError from `write_text` /
          the ValueError from `Popen` / the OSError from `child_add`). Never read this as "a matter of the stub's
          taste"."""
        mode("auth", FAKE_ERR_STR="1")
        d = scv.CodexDriver(CFG, self.MODEL, "low", "s", workdir("cx17"))
        box = {}

        def go():
            try:
                d.turn("x", lambda s: None, 20, 10, None)
            except scv.BridgeError as exc:
                box["e"] = exc

        lines = log_lines_during(go)
        if d.alive():
            d.kill()
        self.assertIn("e", box, "it still threw a bare AttributeError ⇒ the caller got no class, and nothing was logged")
        self.assertEqual((box["e"].klass, box["e"].family), ("auth_required", "codex"))
        self.assertIn("401 Unauthorized", box["e"].raw)    # the original words came through without a character changed
        self.assertEqual(len(lines), 1, lines)

    def test_two_agent_messages_in_one_turn_are_both_kept(self):
        """🔴The criterion is an invariant: what streamed out must equal what got handed back.

        `on_delta` has already streamed every character of every agentMessage to the caller ⇒ if the `item/completed`
        cell were assigned in an overwriting way, the second message would silently wipe out the first: the
        caller would see A+B in the stream, yet the `text` handed back would only have B left, and all three
        tables would be green.
        ⚠️"Two agentMessages in one turn" is an adversarial construction, never a measured shape (that run's probe
          only went as far as the handshake, it never sent a turn; ⚠️the handshake itself does make codex warm up
          once, at zero cost — 13c Fix 1b, NOTES.md::codex-user-home) — but this inconsistency can be read
          statically, it does not need a real CLI to hold.
        Never add a separator while accumulating: a separator that never went through `on_delta` would break the
        invariant above the moment it's added."""
        mode("two_messages")
        d = scv.CodexDriver(CFG, self.MODEL, "low", "s", workdir("cx18"))
        got = []
        try:
            r = d.turn("x", got.append, 20, 10, None)
        finally:
            d.close()
        self.assertEqual(r["text"], "".join(got))                    # the invariant itself
        # assert the correct value: both are there, in the right order, and the leading/trailing whitespace is
        # there byte for byte (never read this as "some spaces were casually added": the stub carries whitespace
        # precisely so the invariant above gets genuinely checked once — a fixture without whitespace would
        # satisfy it merely by luck)
        self.assertEqual(r["text"], "  echo[1]: x-A" + NL + "  echo[1]: x-B" + NL)

    def test_an_empty_tail_message_does_not_wipe_what_was_already_said(self):
        """The other half of the same cell: the trailing agentMessage's `text` is empty ⇒ the content already
        accumulated must never be wiped out. Wiping it out means a turn that genuinely finished answering gets
        reported as "no content" — this branch is loud, but it points the blame at the wrong place."""
        mode("empty_tail_message")
        d = scv.CodexDriver(CFG, self.MODEL, "low", "s", workdir("cx19"))
        got = []
        try:
            r = d.turn("x", got.append, 20, 10, None)
        finally:
            d.close()
        self.assertEqual(r["text"], "  echo[1]: x" + NL)
        self.assertEqual(r["text"], "".join(got))

    def test_a_turn_whose_only_answer_is_whitespace_is_still_loud(self):
        """⭐After `.strip()` moved from the accumulation step to the final-judgment step, this is the cell it
        guards: a turn that only emitted whitespace must never be treated as a success. Without this test, not a
        single test would go red if `not box["text"].strip()` regressed to `not box["text"]`, and the symptom
        would be handing a piece of whitespace to the player as the answer."""
        mode("blank_answer")
        d = scv.CodexDriver(CFG, self.MODEL, "low", "s", workdir("cx20"))
        try:
            with self.assertRaises(scv.BridgeError) as cm:
                d.turn("x", lambda s: None, 20, 10, None)
        finally:
            d.close()
        self.assertEqual((cm.exception.klass, cm.exception.family), ("unknown", "codex"))
        self.assertIn("no content", cm.exception.raw)

    def test_a_rejected_turn_hands_back_the_wording_not_a_python_repr(self):
        """🔴The second scene of the I-1 bug (the previous round only carried the line number over): `turn/start`
        is refused outright by the app-server (an expired threadId / an unrecognized model / drifted parameters)
        ⇒ what comes back is `{"id": rid, "error": {...}}`, and the turn side used to do `str()` on the whole
        object ⇒ the user still ended up seeing a Python dict's repr.
        ⚠️This path originally had zero tests (the review broke it along with three other cells, and not one of
        the 329 tests in the full run went red)."""
        mode("turn_rejected")
        d = scv.CodexDriver(CFG, self.MODEL, "low", "s", workdir("cx21"))
        try:
            with self.assertRaises(scv.BridgeError) as cm:
                d.turn("x", lambda s: None, 20, 10, None)
        finally:
            d.kill()
        self.assertEqual(cm.exception.raw, "unknown threadId (code=-32602)")
        self.assertEqual((cm.exception.klass, cm.exception.family), ("unknown", "codex"))

    def test_a_failed_turn_keeps_the_code_too(self):
        """🔴The same family of error must never carry `code` during the handshake and then lose it during a turn
        (the previous round's two implementations each gave a different sentence). ⚠️Whether `turn.error` actually
          has a `code` has never been measured, this cell is adversarial (the stub keeps it off by default) — but
          "the same input on two different paths must produce the same sentence" holds regardless of a real CLI."""
        mode("quota", FAKE_ERR_CODE="1")
        d = scv.CodexDriver(CFG, self.MODEL, "low", "s", workdir("cx22"))
        try:
            with self.assertRaises(scv.BridgeError) as cm:
                d.turn("x", lambda s: None, 20, 10, None)
        finally:
            d.kill()
        self.assertEqual(cm.exception.raw,
                         "You've hit your session limit · resets 3pm (code=usage_limit_reached)")
        self.assertEqual(cm.exception.klass, "quota")     # the original words are still there ⇒ classification still recognizes it

    def test_a_crashed_cli_carries_its_stderr_verbatim(self):
        mode("crash")
        d = scv.CodexDriver(CFG, self.MODEL, "low", "s", workdir("cx9"))
        with self.assertRaises(scv.BridgeError) as cm:
            d.turn("x", lambda s: None, 20, 10, None)
        self.assertEqual((cm.exception.klass, cm.exception.family), ("crashed", "codex"))
        self.assertIn("boom: fake crash", cm.exception.raw)
        self.assertIn("Cannot find module", cm.exception.raw)     # in the multi-line original text, the last line is the real reason

    def test_a_well_behaved_cli_exits_and_leaves_no_row_behind(self):
        d = scv.CodexDriver(CFG, self.MODEL, "low", "s", workdir("cx10"))
        d.turn("x", lambda s: None, 20, 10, None)
        self.assertEqual([c["family"] for c in scv.children() if c["pid"] == d.pid], ["codex"])
        self.assertEqual(d.close(), "exited")
        self.assertFalse(d.alive())
        self.assertEqual([c for c in scv.children() if c["pid"] == d.pid], [])

    def test_a_fresh_codex_driver_starts_on_the_codex_window(self):
        d = scv.CodexDriver(CFG, self.MODEL, "low", "s", workdir("cx11"))
        try:
            self.assertEqual((d.stall, d.family), (scv.CODEX_STALL_S, "codex"))
        finally:
            d.close()

    def test_the_players_own_codex_home_reaches_the_child(self):
        """Task 13c: the CODEX_HOME the resident path (the one that does the real work) gets is exactly the one in
        the player's own environment, as-is (an escape hatch, zero code). ⚠️An old install's `codex_home` written
        in the config must never be allowed to override it either (that key is no longer read since 13c)."""
        home = str(Path(HOME) / "codex-home-x12")
        Path(home).mkdir(exist_ok=True)          # his home is already there (a real codex cannot start against a home that doesn't exist, and the stub plays along)
        mark = len(helpers.read_fake_log())
        with mock.patch.dict(os.environ, {"CODEX_HOME": home}):
            d = scv.CodexDriver({"codex_home": str(Path(HOME) / "old-install-home")}, self.MODEL, "low", "s",
                                workdir("cx12"))
            d.close()
        boots = [x for x in helpers.fake_log_since(mark) if x.get("family") == "codex"]   # never `[-1]`: see helpers
        self.assertEqual([b["codex_home"] for b in boots], [home])
        self.assertFalse((Path(HOME) / "old-install-home").exists())

    def test_without_one_the_child_is_given_none(self):
        """Zero-input control: take away the CODEX_HOME in his environment and walk the same path again — never
        allowed to invent one for the child out of thin air (before 13c this would stuff in scv's own dedicated
        home, exactly the source of "having to log in a second time").
        ⚠️Take it by window: any boot record left over from an earlier test case would trivially satisfy this line
        (`[-1]` reading it would be a false green)."""
        mark = len(helpers.read_fake_log())
        saved = os.environ.pop("CODEX_HOME", None)
        self.addCleanup(lambda: saved is None or os.environ.__setitem__("CODEX_HOME", saved))
        # ⭐Feed it the production copy (`load_config()`, the one SessionManager holds): before 13c this would fill
        #   in a dedicated home on its own
        d = scv.CodexDriver(scv.load_config(), self.MODEL, "low", "s", workdir("cx13"))
        d.close()
        boots = [x for x in helpers.fake_log_since(mark) if x.get("family") == "codex"]
        self.assertEqual([b["codex_home"] for b in boots], [""])

    def test_factory(self):
        d = scv.make_driver(CFG, "codex", self.MODEL, "low", "s", workdir("cx14"))
        try:
            self.assertEqual((d.family, type(d).__name__), ("codex", "CodexDriver"))
        finally:
            d.close()
        c = scv.make_driver(CFG, "claude", "haiku", None, "s", workdir("cx15"))
        try:
            self.assertEqual((c.family, type(c).__name__), ("claude", "ClaudeDriver"))
        finally:
            c.close()

    def test_an_unknown_family_is_refused_out_loud(self):
        """⭐Failure paths must be loud too (a global constraint): a bare `raise BridgeError` does not land on
        disk, and "why does this bridge not have gemini" is exactly the kind of question someone would go look up
        in `bridge.log`."""
        box = {}

        def go():
            try:
                scv.make_driver(CFG, "gemini", "m", None, "s", workdir("cx16"))
            except scv.BridgeError as exc:
                box["e"] = exc

        lines = log_lines_during(go)
        self.assertIn("e", box)
        self.assertEqual(box["e"].klass, "bad_request")
        self.assertIn("gemini", box["e"].raw)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("bad_request", lines[0])


class CodexRetry401(_Staged):
    """15c: when credentials fail, codex recognizes a 401 in two shapes — the object variant's `httpStatusCode`
    and the string enum `unauthorized` — carried by either of two vehicles: the `error` notification sent while
    retrying, or `turn/completed`'s `turn.error`. The final cell's original text might still not be recognizable
    on its own (`codexErrorInfo: "other"`, no 401 in the wording) — this cell used to fall into unknown/502, with
    no next step (a silent error).
    ⇒ Once a structured 401 has been seen in either shape/vehicle, and the final original text does not itself
    read as quota/auth_required ⇒ classify it as `auth_required`; the original text is not changed by a character
    (B21); never cut it off early (codex might be using that 401 to refresh credentials right now, and the next
    attempt could succeed — ⏳until "never succeeds after a 401" has been measured, never cut it off). This class
    has seven cells: the one that was red before the fix, the quota control arm, the zero-input control, and four
    review probes (S2/S3/S4/S6) covering the two shapes times two vehicles plus the "succeeds despite a 401"
    case."""

    MODEL = "gpt-6-luna"

    def turn_fails(self, tag, **extra):
        mode("retry_then_fail", **extra)
        d = scv.CodexDriver(CFG, self.MODEL, "low", "s", workdir(tag))
        try:
            with self.assertRaises(scv.BridgeError) as cm:
                d.turn("x", lambda s: None, 20, 10, None)
        finally:
            d.kill()
        return cm.exception

    def test_a_401_seen_while_retrying_makes_an_unreadable_final_error_auth_required(self):
        e = self.turn_fails("r401")
        self.assertEqual((e.klass, e.family), ("auth_required", "codex"))
        self.assertEqual(e.raw, "stream disconnected before completion: tls handshake eof")     # B21: the original text, not a character changed

    def test_a_final_error_that_says_quota_stays_quota(self):
        """Control arm: the final original text itself is clear that it's a quota limit ⇒ it must never be
        overridden by a 401 seen while retrying."""
        e = self.turn_fails("r401q", FAKE_FINAL="You've hit your usage limit · resets 3pm")
        self.assertEqual(e.klass, "quota")

    def test_without_a_401_the_same_final_error_stays_unknown(self):
        """Zero-input control: the retry notification exists, but there is no 401 (`httpStatusCode: null`) ⇒ it
        still cannot be recognized (unknown), never treat seeing an `error` notification by itself as a login
        problem."""
        e = self.turn_fails("r401n", FAKE_RETRY_STATUS="none")
        self.assertEqual(e.klass, "unknown")

    # ━━ 15c review I3: the schema has two shapes for a 401 (the object variant carrying httpStatusCode / the
    #   string enum `unauthorized`) x two vehicles (the `error` notification / `turn/completed`'s `turn.error`) —
    #   originally only the "object variant x notification" cell was recognized. Review probes S2/S3/S4, one cell
    #   each.
    def test_s2_the_enum_unauthorized_in_a_retry_notice_counts(self):
        e = self.turn_fails("r401s2", FAKE_RETRY_INFO='"unauthorized"')
        self.assertEqual((e.klass, e.raw), ("auth_required", "stream disconnected before completion: tls handshake eof"))

    def test_s3_a_401_on_the_final_turn_error_alone_counts(self):
        """No retry notification, only the final state `turn.error.codexErrorInfo = {responseTooManyFailedAttempts: {401}}`,
        and the original text is not recognizable."""
        e = self.turn_fails("r401s3", FAKE_RETRIES="0", FAKE_FINAL="exceeded retry limit",
                            FAKE_FINAL_INFO='{"responseTooManyFailedAttempts": {"httpStatusCode": 401}}')
        self.assertEqual((e.klass, e.raw), ("auth_required", "exceeded retry limit"))

    def test_s4_the_enum_unauthorized_on_the_final_turn_error_counts(self):
        """No retry notification, the final state codexErrorInfo is the enum `unauthorized`, and the original text
        is only one word (`AUTH_PAT` only recognizes `401 unauthorized`)."""
        e = self.turn_fails("r401s4", FAKE_RETRIES="0", FAKE_FINAL="Unauthorized", FAKE_FINAL_INFO='"unauthorized"')
        self.assertEqual((e.klass, e.raw), ("auth_required", "Unauthorized"))

    def test_s6_a_401_while_retrying_then_a_blank_but_successful_turn_is_not_a_login_problem(self):
        """15c review M7: a 401 was seen while retrying, and this final turn succeeded (the credentials did
        refresh), it's just that the content is entirely blank ⇒ never send someone to log in again; it's still
        the "this turn has no content" cell (unknown)."""
        e = self.turn_fails("r401s6", FAKE_FINAL_OK="1")
        self.assertEqual((e.klass, e.raw), ("unknown", "codex gave no content this turn"))


class CodexUserStuffIsTurnedOff(_Staged):
    """Task 13c: codex runs inside the player's own CODEX_HOME ⇒ the MCP servers he configured and the skills he
    installed all come along by default. `-c` cannot block either of them; the only approach measured to actually
    work is turning them off one by one inside thread/start's `config` — the names/paths are asked from codex
    itself first (`config/read`, `skills/list`); the skill manifest cell also has to be written into that same
    table (Fix 1, measured: the moment thread/start's `config` carries the `skills` table,
    `-c skills.include_instructions=false` stops taking effect).
    Behavioral readings (zero quota: a fake Responses endpoint receives the real request codex sends out) 📎
    NOTES.md::codex-user-home inside scv.py: drop just "MCP turned off by name" ⇒ the canary server gets started
    and MCP's nested tools come back into the tool descriptions; drop just "skills turned off by path" ⇒ writing
    `$skill-name` in the prompt puts that SKILL.md's full text into the request.
    ⚠️This test only proves "every name/path was passed into thread/start without missing one, and not a single
    value was carried away with it"; that "codex really turned it off because of this" can only be proven by a
    real CLI (the runs above)."""

    SECRETS = ("mcp-secret", "srv-alpha", "his own words", "desc of")    # values the stub puts in the reply (not one may ever be carried out)

    def _start(self, tag, **env):
        mode("ok", **env)
        mark = len(helpers.read_fake_log())
        d = scv.CodexDriver(CFG, Codex.MODEL, "low", "s", workdir(tag))
        d.close()
        return [x for x in helpers.fake_log_since(mark) if "config_read" in x or "skills_list" in x
                or "thread_params" in x]

    def test_every_mcp_server_and_every_skill_is_turned_off_by_name(self):
        rows = self._start("uo1", FAKE_MCP="alpha,beta", FAKE_SKILLS="s2,s1")
        self.assertEqual([next(iter(r)) for r in rows], ["config_read", "skills_list", "system"])   # ask first, then open the session
        where = str(workdir("uo1"))
        self.assertEqual((rows[0]["config_read"], rows[1]["skills_list"]), ({"cwd": where}, {"cwds": [where]}))
        sk = lambda n: os.path.join("fake-skills", n, "SKILL.md")
        self.assertEqual(rows[2]["thread_params"]["config"], {
            "mcp_servers": {"alpha": {"enabled": False}, "beta": {"enabled": False}},
            "skills": {"include_instructions": False,
                       "config": [{"path": sk("s1"), "enabled": False}, {"path": sk("s2"), "enabled": False}]}})

    def test_nothing_configured_means_nothing_to_turn_off(self):
        """Zero-input control: he configured nothing at all (a real codex would reply with an empty table/empty
        list) ⇒ what gets passed along is empty too, never invent names out of thin air, and never refuse to open
        the session because of this.
        ⚠️The skill manifest cell is still suppressed regardless (it doesn't check whether he installed anything:
        what's installed is up to codex to say, and this manifest cell is only a fallback)."""
        rows = self._start("uo2")
        self.assertEqual(rows[-1]["thread_params"]["config"],
                         {"mcp_servers": {}, "skills": {"include_instructions": False, "config": []}})

    def test_none_of_his_values_leave_the_reply(self):
        """The reply is his whole effective configuration and every skill's description (the stub puts a "secret
        password" in the MCP env, his own standing instruction in the config, and a description on the skill) ⇒
        only the key names / paths may be taken: they must never be passed back along with thread/start, and they
        must never go into bridge.log."""
        box = {}
        lines = log_lines_during(lambda: box.__setitem__("rows", self._start("uo3", FAKE_MCP="alpha", FAKE_SKILLS="s1")))
        blob = json.dumps(box["rows"][-1]["thread_params"], ensure_ascii=False) + NL.join(lines)
        self.assertIn("alpha", blob)                               # the ruler is not blind: the name really was passed along
        self.assertIn(os.path.join("fake-skills", "s1", "SKILL.md").replace(chr(92), chr(92) * 2), blob)   # the path was passed along too
        for secret in self.SECRETS:
            self.assertNotIn(secret, blob)

    def test_a_reply_it_cannot_read_means_no_session_not_a_session_with_his_tools(self):
        """When the reply's shape is wrong (a key got renamed / a whole group came back missing) or the method
        does not exist (an old version) ⇒ if it cannot be turned off, it must never pretend it was: this session
        must never start, the resident process gets killed, and one line lands in the log.
        ⭐Every cell needs: (1) to say clearly which reply this was, why it did not start, and what to do next
          (13c review M1); (2) a class that is never retryable (it reproduces every single time); (3) never let
          the original text or that log line carry any value from the reply (13c review I1: the stub moved the
          password-carrying value to a different spot in the reply, never removed it entirely — the old assertion
          `assertNotIn("mcp-secret", raw)` was also green on an implementation that stuffed the whole reply into
          the original text); codex's own original words must be carried along (B21)."""
        cases = (({"FAKE_CONFIG_READ": "nokey"}, "mcp_servers"), ({"FAKE_SKILLS_LIST": "noskills"}, "skills/list"),
                 ({"FAKE_SKILLS_LIST": "nogroups"}, "skills/list"),
                 ({"FAKE_CONFIG_READ": "error"}, "Method not found: config/read"))
        for i, (env, mark) in enumerate(cases):
            with self.subTest(env=env):
                # ⭐Clear the previous cell's knobs first: if this were left at the end, one red cell (the
                #   assertion throwing) would skip the cleanup, and the next cell would run in the environment it
                #   left behind, going red in a different way (13c Fix 1's mutation X1 actually hit this: the
                #   last two cells both went red as "skills/list not found")
                os.environ.pop("FAKE_CONFIG_READ", None)
                os.environ.pop("FAKE_SKILLS_LIST", None)
                mode("ok", FAKE_MCP="alpha", FAKE_SKILLS="s1", **env)
                box = {}

                def go():
                    try:
                        scv.CodexDriver(CFG, Codex.MODEL, "low", "s", workdir("uo4-%d" % i))
                    except scv.BridgeError as exc:
                        box["e"] = exc

                with mock.patch.object(scv, "kill_tree", wraps=scv.kill_tree) as gun:
                    lines = log_lines_during(go)
                self.assertIn("e", box, "the session started anyway ⇒ his MCP servers/skills might not have been turned off at all")
                self.assertEqual((box["e"].klass, box["e"].family), ("unknown", "codex"))
                self.assertNotIn(box["e"].klass, scv.RETRYABLE)
                self.assertIn(mark, box["e"].raw)
                self.assertIn("this session does not start", box["e"].raw)
                self.assertIn("upgrade Codex CLI", box["e"].raw)       # the next step
                self.assertEqual(len(lines), 1, lines)
                for secret in self.SECRETS:
                    self.assertNotIn(secret, box["e"].raw)
                    self.assertNotIn(secret, lines[0])
                pids = [c.args[0].pid for c in gun.call_args_list]
                self.assertEqual(len(pids), 1, pids)
                self.assertTrue(gone(pids[0]), "it still lingered after the session was refused ⇒ the bridge can no longer reach this resident process")


class CodexHandshake(_Staged):
    """The handshake is an entire extra surface of failure this family has (over on claude's side, `__init__` is
    done the moment the process starts).

    🔴Two cells, two different shapes: (1) the other side replies with a JSON-RPC `error`; (2) it replies with a
      `result` but `thread.id` is missing (the protocol drifted / a version changed).
    ⭐Both must kill that resident process: once `__init__` throws, the caller cannot even get `self`, and after
      that no one can manage that process anymore — exactly the shape the registry is meant to prevent.
    ⚠️The stub lingers and refuses to leave in both of these modes (a real JSON-RPC server stays alive too after
      replying with an error): otherwise "the driver killed it" and "it exited on its own via EOF" would look
      identical on the test bench (Task 6 actually hit this on the very same shape)."""

    # ⭐The third cell = the class: a reply with no thread.id reproduces every single time (the protocol changed)
    #   ⇒ never a retryable crashed (13c review M1, same settled idiom as `_serve`)
    CASES = (("handshake_error", "Invalid request", "crashed"), ("no_thread_id", "did not give a threadId", "unknown"))

    def test_a_failed_handshake_kills_the_process_and_lands_exactly_one_line(self):
        for i, (m, mark, klass) in enumerate(self.CASES):
            with self.subTest(mode=m):
                mode(m)
                box = {}

                def go():
                    try:
                        scv.CodexDriver(CFG, Codex.MODEL, "low", "s", workdir("hs%d" % i))
                    except scv.BridgeError as exc:
                        box["e"] = exc

                with mock.patch.object(scv, "kill_tree", wraps=scv.kill_tree) as gun:
                    lines = log_lines_during(go)
                self.assertIn("e", box, "the handshake never actually failed, the lines below are pointless")
                self.assertEqual((box["e"].klass, box["e"].family), (klass, "codex"))
                self.assertIn(mark, box["e"].raw)
                self.assertEqual(len(lines), 1, lines)
                # 🔴A value from the reply (the stub puts an instructionSources that looks like the player's
                #   private path into the no_thread_id cell) must never travel out with the original text:
                #   the original text goes into bridge.log, and also into the API error body (the remote leg
                #   hands that to the dispatcher). It used to be `"…did not give a threadId: %s" % (ev,)` (13c
                #   review I1)
                self.assertNotIn("secret-path", box["e"].raw + NL.join(lines))
                pids = [c.args[0].pid for c in gun.call_args_list]
                self.assertEqual(len(pids), 1, pids)     # that shot was fired only once, and it's aimed at the whole tree
                self.assertTrue(gone(pids[0]), "the handshake blew up and it still lingered ⇒ the bridge can no longer reach this resident process")
                self.assertEqual([c for c in scv.children() if c["pid"] == pids[0]], [])

    def test_the_error_comes_back_as_the_clis_wording_not_a_python_repr(self):
        """🔴The fifth place carrying the same piece of reasoning: `turn()` just specifically added the
        `isinstance` cell for "`error` is not guaranteed to be an object", while `_call()`, in that very same
        class, still does `str()` on the whole error object ⇒ the user sees a Python dict's repr, never the CLI's
        original words (B21's "an error is the original text" is discounted on this cell).

        ⚠️It does not affect classification (`classify` does substring matching, and the original text is still
          inside the repr) ⇒ all three tables are green, which is exactly why it deserves to be pinned.
        `code` must never be lost: it's the machine-readable half of this error."""
        mode("handshake_error")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(scv.BridgeError) as cm:
                scv.CodexDriver(CFG, Codex.MODEL, "low", "s", workdir("hs7"))
        # assert the correct value: the original text is there word for word, code is on the same line, and there
        # are no dict-style braces or quotes
        self.assertEqual(cm.exception.raw, "Invalid request: unknown method (code=-32600)")

    def test_a_non_bridge_error_in_the_handshake_still_takes_the_process_down(self):
        """🔴The extra half that a `finally` shape covers compared to `except BridgeError`: what blows up in the
        handshake is not one of the exception families we recognize.

        ⭐Measured during review: switching the implementation back to the brief's
          `except BridgeError: kill; raise` shape and running all 89 tests, only the AST gate
          `NoSilentFailurePath` went red, and every behavioral test was green ⇒ this cell originally had no
          behavioral gate. And the reason that gate exists is "there's a raise that doesn't go through `_fail`",
          never "there's a cell where a resident process can leak" — the gate and the bug are not the same shape,
          and the day the gate is relaxed to allow a bare `raise` (a very natural piece of evolution), the gap
          comes back silently.
        ⚠️This run of the stub uses `deaf_after_handshake` (stops reading stdin once the handshake is answered),
          but never record it as "this test only carries weight because of that": review reran it twice and
          proved that switching back to `ok` mode leaves this gate red just the same — this test case stores
          that RuntimeError in `box["e"]`, and the exception's traceback pins `__init__`'s stack frame, which
          pins `self.pipe` ⇒ that stdin can never wait for the GC to close it, and the `ok`-mode stub lingers the
          same way.
          ⇒ This mode does not carry a single test today; it's kept only so the correctness of this gate is
          never staked on GC timing (the day someone changes `box["e"]` to not store the exception, the `ok`-mode
          path would instantly become a gamble on GC luck).
        ⚠️Landing 0 lines is correct: `finally` was never meant to call `_fail`, that RuntimeError is our own bug,
          it should pass straight through to the caller as-is, and it must never be wrapped into a retryable
          `crashed`."""
        mode("deaf_after_handshake")
        real_send, box = scv._Pipe.send, {}

        def boom(pipe, obj):
            if obj.get("method") == "initialized":
                raise RuntimeError("not a BridgeError")
            return real_send(pipe, obj)

        def go():
            with mock.patch.object(scv._Pipe, "send", boom):
                try:
                    scv.CodexDriver(CFG, Codex.MODEL, "low", "s", workdir("hs8"))
                except RuntimeError as exc:
                    box["e"] = exc

        with mock.patch.object(scv, "child_add", wraps=scv.child_add) as reg, \
                mock.patch.object(scv, "kill_tree", wraps=scv.kill_tree) as gun:
            lines = log_lines_during(go)
        self.assertIn("e", box, "what got thrown was not that RuntimeError ⇒ this test is not testing the half it means to")
        self.assertEqual(lines, [], "our own bug must never land as a CLI failure")
        pid = reg.call_args_list[0].args[0]      # ⭐the pid comes from elsewhere ⇒ this assertion can still get an answer even when that shot was never fired
        self.assertTrue(gone(pid), "the handshake blowing up on something other than BridgeError leaked a resident process that could have lived an entire session")
        self.assertEqual([c.args[0].pid for c in gun.call_args_list], [pid])   # that shot is aimed at the whole tree
        self.assertEqual([c for c in scv.children() if c["pid"] == pid], [])

    def test_an_unreadable_handshake_error_takes_the_default_we_passed_in(self):
        """⭐What this pins down is "where `crashed` comes from": first prove this real original text is not
        recognized by either group of pattern strings, then prove the driver received exactly the default we
        passed in."""
        mode("handshake_error")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(scv.BridgeError) as cm:
                scv.CodexDriver(CFG, Codex.MODEL, "low", "s", workdir("hs2"))
        self.assertEqual(scv.classify(cm.exception.raw), "unknown")   # the ruler is not blind
        self.assertEqual(cm.exception.klass, "crashed")

    def test_a_readable_handshake_error_is_classified_by_its_words(self):
        """Positive control: same path, swap in a piece of original text that can be recognized ⇒ it must become
        `auth_required`. Without this line, "classify really read the text" cannot be told apart from "it always
        returns the default"."""
        mode("handshake_error", FAKE_RPC_ERROR="auth")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(scv.BridgeError) as cm:
                scv.CodexDriver(CFG, Codex.MODEL, "low", "s", workdir("hs3"))
        self.assertEqual(cm.exception.klass, "auth_required")
        self.assertIn("401 Unauthorized", cm.exception.raw)

    def test_an_unsolicited_notification_is_not_mistaken_for_the_answer(self):
        """🔴A real app-server (codex-cli 0.155.0-alpha.9.2, measured on this machine 2026-09-22) inserts a
        notification of its own between two exchanges that no one asked for (`remoteControl/status/changed`) ⇒ a
        reply can only be recognized by its id.

        ⭐What this gate fills in is a missed detection: the stub used to be strictly one question, one answer,
          nothing extra ⇒ "recognize a reply by its id" and "treat the first event as the reply" looked identical
          across the whole test bench (measured: swap `_call`'s criterion for `lambda e: True`, and it was all
          green before the fix).
        ⭐First pin down that this run's stub really did insert that notification, otherwise this test cannot be
          told apart from "the stub did nothing at all"."""
        before = len(helpers.read_fake_log())
        d = scv.CodexDriver(CFG, Codex.MODEL, "low", "s", workdir("hs5"))
        try:
            self.assertEqual(d.tid, "t-fake")           # assert the correct value: what got picked up is the id from the thread/start reply
            r = d.turn("x", lambda s: None, 20, 10, None)
            self.assertEqual(r["text"], "echo[1]: x")
        finally:
            d.close()
        fresh = helpers.read_fake_log()[before:]
        # ⭐Both are needed, and at different positions: one sits between two replies, the other lands right after
        #   thread/start's reply (both were measured on a real CLI). Without this line, this test cannot be told
        #   apart from "the stub did nothing at all".
        self.assertEqual([x["stray"] for x in fresh if x.get("stray")],
                         ["remoteControl/status/changed", "thread/started"])

    def test_a_good_handshake_is_what_normally_happens(self):
        """Zero-input control: take away those two kinds of bad replies and walk the same path again — it must be
        able to start, obtain the threadId, and make it into the registry."""
        d = scv.CodexDriver(CFG, Codex.MODEL, "low", "s", workdir("hs4"))
        try:
            self.assertEqual(d.tid, "t-fake")
            self.assertEqual([c["family"] for c in scv.children() if c["pid"] == d.pid], ["codex"])
        finally:
            d.close()


class WhereTheTextComesFrom(_Staged):
    """⭐The two families' `text` come from different sources — this was read out of the two pieces of code
    themselves, never inferred from "they're both drivers":

    - codex (`scv.py:1188`): `text` is something we accumulate ourselves out of `item/completed` messages one at
      a time, while `on_delta` streams `item/agentMessage/delta` ⇒ "what streamed out = what got handed back" is
      an invariant we can guarantee (pinned by
      `Codex.test_two_agent_messages_in_one_turn_are_both_kept`, whose stub answer carries leading/trailing
      whitespace).
    - claude (`scv.py:1078`): `text` comes from the `result` field of the `result` event the CLI itself gives us,
      we accumulate not a single character of it ourselves; `on_delta` streams a different stream (`text_delta`).
      ⇒ The same invariant, for this family, is never our contract, it's the CLI's — we can only measure it, we
      cannot guarantee it.

    🔴So the codex bug (`.strip()`-ing each message one at a time, which eats the gap between messages) does not exist
      on the claude side: that side does a single outer trim on the one complete string a CLI produces, never
      strip-then-join piece by piece ⇒ never force this fix onto claude by analogy.
    ⚠️The only real difference left between the two families: claude's return value is trimmed, codex's is
      verbatim. That is a decision for the presentation layer (belongs to the wiring batch), never something this
      layer can rule on unilaterally.
    ⚠️And the stub naturally satisfies claude's external contract (`result` and the delta are both built from the
      same `ans`) ⇒ if anyone writes a `r["text"] == "".join(got)` on the claude side by analogy with codex, it
      would necessarily be green on the stub, yet it would be turning a CLI behavior no one has ever measured
      into our own assertion — yet another "contract the fixture happens to satisfy". The test case below exists
      to make that fact visible right away."""

    def test_claudes_text_is_the_result_field_not_the_deltas(self):
        """⚠️An adversarial mode (off by default): make the stub's `result` and delta stream deliberately
        different. Never a measured shape — whether a real CLI's `result` keeps up with its delta stream has
        never been measured (that belongs to a real-CLI run)."""
        mode("result_not_deltas")
        d = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("src1"))
        got = []
        try:
            r = d.turn("x", got.append, 20, 10, None)
        finally:
            d.close()
        self.assertEqual("".join(got), "echo[1]: x")            # what streamed out = the delta stream
        self.assertEqual(r["text"], "RESULT-echo[1]: x")        # what got handed back = the result field (assert the correct value)
        self.assertNotEqual(r["text"], "".join(got))            # the two come from different sources, and this line is the evidence itself


class RpcErrorText(unittest.TestCase):
    """`_rpc_error` is a pure function: the six cells below feed it directly, no need to start a stub.

    🔴Back when it had four behavioral branches, three of them had zero coverage: the review broke the bare
    string / no message / no code cells all at once, and not one of the full run's 329 tests went red. This round
    fills in those four cells and adds two more, `code=0` and an empty dict (each has its own red, see report
    P3/P2) ⇒ six cells in total. ⭐A pure function is the cheapest coverage there is, there's no reason to let it
    gamble on an end-to-end path instead."""

    CASES = (({"code": -32600, "message": "X"}, "X (code=-32600)"),   # has both: combine into one line
             ({"message": "X"}, "X"),                                 # no code: just give the original text
             ({"code": 0, "message": "X"}, "X (code=0)"),             # ⭐code=0 must never be treated as "there is none"
             ("X", "X"),                                              # a bare string: as-is
             ({"code": -1}, "{'code': -1}"),                          # not even a message: the whole object is kept (B21)
             ({}, "{}"))
    # ⚠️The empty-dict cell is this function's own contract (`not msg` ⇒ returns str(err)); the caller has a
    #   separate fallback for "err is empty" (`turn/completed` goes with "turn failed"), never mix up these two
    #   things.

    def test_the_six_grids(self):
        for err, want in self.CASES:
            with self.subTest(err=err):
                self.assertEqual(scv._rpc_error(err), want)


class TwoFamiliesOneShape(_Staged):
    """⭐`make_driver`'s consumer (the wiring batch) is built on the contract "the two families' method signatures
    match word for word", and it originally had zero gates for that: `Codex`'s 16 tests are a hand-copy of
    `Claude`'s eight, and adding a parameter to one family while forgetting the other would leave all three tables
    green while only breaking one family's path in production.

    ⚠️This gate's missed-detection surface is written down here explicitly (never read this as "the two families'
      consistency is already fully covered"): what it covers is the half that is "adding a parameter / renaming a
      parameter / changing a default value"; the half that is "changing a return field" is invisible to it —
      `turn()`'s returned dict missing a key would still leave this gate green. That half today relies on each
      family's own behavioral tests (the ones in `Claude`/`Codex` that read `text`/`usage`/`ttfc`); anyone
      changing the return shape has to go change those two families' tests, never this one."""

    SHARED = ("__init__", "turn", "alive", "close", "kill")

    def test_both_drivers_expose_the_same_call_shape(self):
        for name in self.SHARED:
            with self.subTest(method=name):
                self.assertEqual(inspect.signature(getattr(scv.ClaudeDriver, name)),
                                 inspect.signature(getattr(scv.CodexDriver, name)),
                                 name + " the two families' signatures no longer match ⇒ make_driver's caller would only break one family's path")

    def test_the_ruler_is_not_blind(self):
        """Positive control: feed it two synthetic samples that are deliberately different, and this ruler must be
        able to tell them apart. Never rely only on the real file happening to be consistent — "a comparison that
        is all green" and "a comparison performed against nothing" look identical."""
        class A:
            def turn(self, text, on_delta):
                pass

        class B:
            def turn(self, text, on_delta, extra=None):
                pass

        self.assertNotEqual(inspect.signature(A.turn), inspect.signature(B.turn))
        self.assertEqual(inspect.signature(A.turn), inspect.signature(A.turn))

    def test_both_drivers_carry_the_same_three_attributes(self):
        """The three cells `family`/`pid`/`stall`: a signature comparison cannot reach them, yet the consumer reads
        them directly."""
        c = scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("sig1"))
        x = scv.CodexDriver(CFG, Codex.MODEL, "low", "s", workdir("sig2"))
        try:
            for d in (c, x):
                with self.subTest(family=d.family):
                    self.assertEqual({a for a in ("family", "pid", "stall") if hasattr(d, a)},
                                     {"family", "pid", "stall"})
                    self.assertIsInstance(d.pid, int)
                    self.assertIsInstance(d.stall, (int, float))
            self.assertEqual((c.family, x.family), ("claude", "codex"))   # assert the correct value, never just "both have this attribute"
        finally:
            c.close()
            x.close()


class UnlistWriteFailure(_Staged):
    """🔴Un-registering itself can also blow up: the `_children_save` call inside `child_remove` swallows not a
    single exception (disk full / permissions / `SCV_HOME` deleted), and `_Pipe._unlist` used to call it bare.

    ⭐`run_cli` specifically guards against this in the same spot, and its comment spells out the reason word for
      word (never let it override a `TimeoutExpired` that's already in flight: the error class the caller sees
      would change completely); `_Pipe` had none of that ⇒ the same piece of reasoning was only half carried out
      (the third time this is the same mistake as I-1/`RegistryWriteFailure`).
    Each of the two paths gets its own cell, and the cost differs:
      - a failed turn: all three of `read_until`'s failure paths call `kill()` first, then `raise _fail(...)` ⇒
        that OSError would wipe out the CLI's original text and class entirely (B21/B26 both voided on the spot);
      - `close()`: the most natural way to write the wiring batch is `finally: d.close()` ⇒ the moment this
        throws, what it wipes out is the exception that's already in flight."""

    BOOM = "children.json 写不进去：磁盘满了"

    def test_a_bookkeeping_failure_does_not_replace_the_real_error(self):
        mode("crash")
        d = scv.CodexDriver(CFG, Codex.MODEL, "low", "s", workdir("b1"))
        box = {}

        def go():
            try:
                d.turn("x", lambda s: None, 20, 10, None)
            except scv.BridgeError as exc:
                box["e"] = exc

        with mock.patch.object(scv, "child_remove", side_effect=OSError(self.BOOM)):
            lines = log_lines_during(go)
        self.assertIn("e", box, "what got thrown was a bare OSError ⇒ the class and original text the caller got were both wiped out entirely")
        self.assertEqual((box["e"].klass, box["e"].family), ("crashed", "codex"))
        self.assertIn("boom: fake crash", box["e"].raw)                       # the CLI's original text is still there
        self.assertEqual(len(lines), 2, lines)                                # two things happened, two lines
        self.assertTrue([x for x in lines if self.BOOM in x], lines)          # the OS's own words, not a character changed
        self.assertTrue([x for x in lines if "boom: fake crash" in x], lines)

    def test_close_still_answers_when_the_table_cannot_be_written(self):
        d = scv.CodexDriver(CFG, Codex.MODEL, "low", "s", workdir("b2"))
        d.turn("x", lambda s: None, 20, 10, None)
        with mock.patch.object(scv, "child_remove", side_effect=OSError(self.BOOM)):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(d.close(), "exited")   # assert the correct value, never just "as long as it didn't throw"

    def test_the_bookkeeping_really_happens_otherwise(self):
        """Zero-input control: take away that OSError ⇒ the un-registering really happens (the row in the table is
        gone), which is what makes the two tests above meaningful instead of pointless."""
        d = scv.CodexDriver(CFG, Codex.MODEL, "low", "s", workdir("b3"))
        self.assertEqual([c["family"] for c in scv.children() if c["pid"] == d.pid], ["codex"])
        self.assertEqual(d.close(), "exited")
        self.assertEqual([c for c in scv.children() if c["pid"] == d.pid], [])


class StderrWatermark(_Staged):
    """carry-forward D17 (OUT-1, the fix has been ruled on = a watermark): every "clearly succeeded" clears the
    stderr tail to zero ⇒ the EOF path only classifies the segment newly added since the last clear success.
    ⭐Both families get the same fix, and both are exercised.
    🔴What it looks like unfixed: one line of stale noise from startup (here borrowing a sentence that looks like
      a login failure) would cause a later, unrelated crash to be judged `auth_required` ⇒ `fix_hint` sends the
      person to log in again — a lying error is more expensive than a silent one.
    ⭐This kind of noise has actually been measured on a real CLI (2026-09-23: after codex genuinely sent a turn,
      stderr had one ERROR line from `codex_core::tools::router`; the run with a bad model had
      `failed to connect to websocket … wss://…`; all three cells for a real claude were 0 bytes)."""

    def _second_turn_crashes(self, make, stale="1", crash_at="2"):
        """`stale`: when the noise is written (`1` = right at startup / `turn1` = during turn 1, before it
        answers); `crash_at`: which turn crashes.
        ⭐Before `mark_ok` clears it, wait first for that noise to really make it into the tail: the pump is
          asynchronous, and without waiting, "which watermark clear it" depends on the pump thread's timing ⇒ the
          test would randomly run empty (`mark_ok`'s own docstring writes down this bounded edge). Only wait,
          never change what it does."""
        mode("ok", FAKE_STALE_STDERR=stale, FAKE_CRASH_AT=crash_at)
        seen, real, real_ok = [], scv._Pipe._err_add, scv._Pipe.mark_ok
        noisy = lambda: any("401" in x for x in seen)
        armed = [stale == "1"]            # from which moment "the noise has been written out" holds: the `turn1` cell has to wait for turn 1 to begin

        def spy(pipe, text):
            seen.append(text)
            real(pipe, text)

        def ok_after_noise(pipe):
            end = time.time() + 5
            while armed[0] and time.time() < end and not noisy():
                time.sleep(0.02)
            real_ok(pipe)

        with mock.patch.object(scv._Pipe, "_err_add", spy), mock.patch.object(scv._Pipe, "mark_ok", ok_after_noise):
            d = make()
            try:
                if stale == "1":
                    end = time.time() + 5
                    while time.time() < end and not noisy():
                        time.sleep(0.05)
                    # precondition: that stale noise made it into the tail before the first turn (otherwise this
                    # test runs empty: it never even had a chance to be misread)
                    self.assertTrue(noisy(), seen)
                armed[0] = True
                if crash_at == "2":
                    d.turn("第一问", lambda s: None, 20, 10, None)
                with self.assertRaises(scv.BridgeError) as cm:
                    d.turn("第二问", lambda s: None, 20, 10, None)
            finally:
                d.kill()
        self.assertTrue(noisy(), seen)            # precondition: the noise really was written (the `turn1` cell is only written during turn 1)
        return cm.exception

    def test_claude(self):
        e = self._second_turn_crashes(lambda: scv.ClaudeDriver(CFG, "haiku", None, "s", workdir("wm1")))
        self.assertEqual(e.klass, "crashed")
        self.assertIn("Cannot find module", e.raw)                 # the crash's original text is still there (B21)
        self.assertNotIn("401", e.raw)

    def test_codex(self):
        e = self._second_turn_crashes(lambda: scv.CodexDriver(CFG, Codex.MODEL, "low", "s", workdir("wm2")))
        self.assertEqual(e.klass, "crashed")
        self.assertIn("Cannot find module", e.raw)
        self.assertNotIn("401", e.raw)

    # ⚠️codex has two watermarks (a successful handshake, a successful turn). The noise in the test above is
    #   written at startup ⇒ the handshake one clears it first ⇒ removing only the turn one would still leave it
    #   green; and removing only the handshake one, the turn one clears it instead ⇒ also still green (the two
    #   mask each other, Task 12 review M-1). ⇒ each one gets its own cell, with noise that only it can clear:
    def test_codex_turn_watermark(self):
        """The noise is only written during turn 1, before it answers (a real codex's noise really is written
        during a turn) ⇒ only the turn watermark can clear it."""
        e = self._second_turn_crashes(lambda: scv.CodexDriver(CFG, Codex.MODEL, "low", "s", workdir("wm3")),
                                      stale="turn1")
        self.assertEqual(e.klass, "crashed")
        self.assertNotIn("401", e.raw)

    def test_codex_handshake_watermark(self):
        """The noise is written right at startup, and it crashes on turn 1 itself ⇒ there is no successful turn in
        between, so only the handshake watermark can clear it."""
        e = self._second_turn_crashes(lambda: scv.CodexDriver(CFG, Codex.MODEL, "low", "s", workdir("wm4")),
                                      crash_at="1")
        self.assertEqual(e.klass, "crashed")
        self.assertNotIn("401", e.raw)


if __name__ == "__main__":
    unittest.main()
