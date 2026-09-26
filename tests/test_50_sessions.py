# -*- coding: utf-8 -*-
"""Session management. ⭐The check rides on the fake CLI's echo[n]: n = which turn number this process has answered
=> n keeps climbing = continued on the old process and only fed the increment; n back to 1 = it was rebuilt."""
import contextlib
import hashlib
import io
import os
import threading
import time
import unittest
from unittest import mock

from tests import helpers

import scv  # noqa: E402

HOME = ""
_REAL_HEAD = scv.cli_head


def setUpModule():
    global HOME
    HOME = helpers.fresh_home("sessions", unittest.addModuleCleanup)
    scv.cli_head = helpers.fake_head


def tearDownModule():
    scv.cli_head = _REAL_HEAD


CAT = ["claude/haiku", "codex/gpt-5.6-luna"]


def U(t):
    return {"role": "user", "content": t}


def A(t):
    return {"role": "assistant", "content": t}


def mgr(max_concurrent=4):
    return scv.SessionManager({"max_concurrent": max_concurrent}, lambda: CAT)


def call(m, sid, system, messages, **kw):
    started = []
    res = m.run(session_id=sid, model_id=kw.pop("model", "claude/haiku"), effort=None, system=system,
                messages=messages, on_started=started.append, on_delta=lambda s: None,
                cancel=kw.pop("cancel", None), **kw)
    res["_started"] = started
    return res


class Delta(unittest.TestCase):
    def setUp(self):
        os.environ["FAKE_MODE"] = "ok"
        self.m = mgr()

    def tearDown(self):
        self.m.close_all()

    def test_second_request_feeds_only_the_new_message(self):
        r1 = call(self.m, "s1", "SYS", [U("甲")])
        r2 = call(self.m, "s1", "SYS", [U("甲"), A(r1["text"]), U("乙")])
        self.assertEqual((r1["text"], r2["text"]), ("echo[1]: 甲", "echo[2]: 乙"))
        self.assertEqual((r1["rebuilt"], r2["rebuilt"]), (None, None))
        self.assertEqual(r1["_started"], [r1["queued_ms"]])

    def test_a_turn_that_dies_of_any_exception_kills_the_session(self):
        """🔴The root spot of review C-A: `on_delta` is the caller's own code, and it can throw any family of
        exception at all. It used to be that only `except BridgeError` triggered `_drop(kill=True)` => any other
        family leaves the rest of this question's answer sitting in the pipe, and the next question reads the
        previous one's answer (review measured this on the remote leg back to back: misalignment, `rebuilt=None`,
        not a single signal). ⭐The check has two conditions side by side: (1) that session got thrown away, (2) the
          next question's answer is the next question's own answer. The fixture is staged the way review reproduced
          it: answer one turn successfully first (to let `fp` settle), then have the second turn throw an unfamiliar
          exception on the first delta."""
        r1 = call(self.m, "s-boom", "SYS", [U("甲")])

        def boom(_s):
            raise KeyError("an unfamiliar exception from inside the caller's callback")

        with self.assertRaises(KeyError):
            self.m.run(session_id="s-boom", model_id="claude/haiku", effort=None, system="SYS",
                       messages=[U("甲"), A(r1["text"]), U("乙")], on_started=lambda ms: None,
                       on_delta=boom, cancel=None)
        self.assertNotIn("s-boom", self.m._sessions, "threw a non-BridgeError exception, yet the process is still hanging around")
        # ⚠️The third question's prefix deliberately matches the fingerprint saved from the first turn (the caller
        #   coming back with a different question is exactly this shape on the remote leg): swapping in a
        #   five-message sequence with a different first question would make the prefix not match, and it would
        #   rebuild whether or not it was fixed => that half of the check would run vacuously.
        r3 = call(self.m, "s-boom", "SYS", [U("甲"), A(r1["text"]), U("丙")])
        self.assertNotEqual(r3["text"], "echo[2]: 乙", "the next question read the previous one's answer left sitting in the pipe")
        self.assertIsNotNone(r3["rebuilt"], "the process got thrown away, so the next question must be a rebuild that can explain itself")
        self.assertIn("collected after an unfinished previous turn", r3["rebuilt"])     # 15b fix2 O2: the raw text used to be missing this one cause (this is exactly that spot)

    def test_caller_trimming_its_copy_of_the_reply_is_not_a_mismatch(self):
        r1 = call(self.m, "s2", "SYS", [U("甲")])
        r2 = call(self.m, "s2", "SYS", [U("甲"), A("（调用方自己修剪过的版本）"), U("乙")])
        self.assertEqual((r2["text"], r2["rebuilt"]), ("echo[2]: 乙", None))

    def test_prefix_mismatch_rebuilds_loudly_from_the_full_history(self):
        call(self.m, "s3", "SYS", [U("甲")])
        r2 = call(self.m, "s3", "SYS", [U("被改过的第一问"), A("x"), U("乙")])
        self.assertEqual(r2["rebuilt"], "the prefix does not match")
        self.assertTrue(r2["text"].startswith("echo[1]: [scv] Your previous session"), r2["text"])

    def test_changed_system_rebuilds_and_the_new_process_gets_the_new_system(self):
        mark = len(helpers.read_fake_log())
        r1 = call(self.m, "s4", "旧题面", [U("甲")])
        r2 = call(self.m, "s4", "新题面", [U("甲"), A(r1["text"]), U("乙")])
        self.assertEqual(r2["rebuilt"], "the prefix does not match")
        boots = [x for x in helpers.fake_log_since(mark) if x.get("family") == "claude"]
        self.assertEqual([b["system"] for b in boots], ["旧题面", "新题面"])

    def test_unknown_session_with_history_says_so(self):
        r = call(self.m, "never-seen", "SYS", [U("甲"), A("x"), U("乙")])
        self.assertIn("has no such session", r["rebuilt"])

    def test_dead_process_rebuilds(self):
        """⚠️This case's negative control is a notch weaker, and that is written down plainly here. Remove the
        `if not sess.driver.alive():` check, and the code crashes before it ever reaches a single assertion below
        (writing to the stdin of a process that is already dead => `BridgeError(crashed)` flies out) => that control
        only proves "removing it blows up", never "the reason it blew up is the very thing we mean to guard". It is
        at least loud (crashed, plus one line in bridge.log, plus the raw text naming that stdin is closed), never
        silent -- but whoever comes next needs to know this spot only carries this much weight."""
        r1 = call(self.m, "s5", "SYS", [U("甲")])
        self.m._sessions["s5"].driver.kill()
        r2 = call(self.m, "s5", "SYS", [U("甲"), A(r1["text"]), U("乙")])
        self.assertEqual(r2["rebuilt"], "the process is gone")
        self.assertTrue(r2["text"].startswith("echo[1]: "))

    def test_error_drops_the_session_and_leaves_no_child(self):
        """⚠️The line that goes red first is a negative-form assertion, but the reasoning still holds (review M-1):
        remove the `_drop` on the error path, and the first thing to go red is the `assertNotIn("s6", …)` below,
        while the very next `snapshot()["children"] == []` also goes red under the same mutation -- and that one is
        a correct-value assertion. Never read this as the "assert wrong-value not in" anti-pattern. (This is one of
        the 13 cases the brief handed down verbatim => the assertion order stays as-is, this is just one line
        pointing that out.)"""
        os.environ["FAKE_MODE"] = "auth"
        with self.assertRaises(scv.BridgeError) as cm:
            call(self.m, "s6", "SYS", [U("甲")])
        self.assertEqual(cm.exception.klass, "auth_required")
        self.assertNotIn("s6", self.m._sessions)
        self.assertEqual(self.m.snapshot()["children"], [])

    def test_a_read_failure_drops_the_session_too(self):
        """🔴"the same line of code" is never the same as "the same path got tested". The case above pins the auth
        path; this one pins the read-failure path -- both go through the same spot in `_run_session` ("if it did not
        finish answering, `_drop(kill=True)`", which was `except BridgeError` when this was written and is now
        `finally` plus `answered`), but they reach that spot in completely different ways (one is the CLI reporting
        its own error, the other is a read on our own pipe blowing up).

        ⭐It is also a measurement of a chain of reasoning: the reason `crashed` dares to give `retryable=True` rests
          entirely on this rebuild -- once a read fails, the pipe is permanently blind (the stderr pump throws and
          exits, its tail frozen, measured), and retrying on the same process is forever blind; only once "force a
          rebuild after this turn ends" holds does a retry land on a fresh pipe whose tail can be read, and only
          then does the real reason (say, `auth_required`) get a chance to be classified correctly next time.
          => The check is that the next call says "I have no such session" (= it really did switch processes),
          never "it threw crashed"."""
        r1 = call(self.m, "rf1", "SYS", [U("甲")])
        pipe = self.m._sessions["rf1"].driver.pipe
        # ⭐The injection point is the pump's own two lines (`_read_failed` plus `finally: _q.put(None)`), with the
        #   child process alive the whole time -- that is exactly the shape of the scene "our own read blew up while
        #   the CLI is perfectly fine".
        # ⚠️The other half, "`readline` really throws => it reaches these two lines", is pinned end to end by
        #   tests/test_30_drivers.py::ReadFailureIsNotAnEof (its injection point is right on `readline`) => only
        #   the two together make up the whole path, never look at just one.
        # never change this to a direct `popen.stdout.close()` here: that would deadlock (the pump is stuck inside
        #   `readline()` holding the buffer's lock, and `close()` would wait on it) -- which is exactly why
        #   `_close_pipes()` has to join first and only then close.
        pipe._read_failed("stdout", OSError(5, "Input/output error"))
        pipe._q.put(None)
        with self.assertRaises(scv.BridgeError) as cm:
            call(self.m, "rf1", "SYS", [U("甲"), A(r1["text"]), U("乙")])
        self.assertEqual(cm.exception.klass, "crashed")
        self.assertIn("blew up", cm.exception.raw)
        self.assertNotIn("rf1", self.m._sessions)          # a process that errored out must never be kept around to keep serving
        r3 = call(self.m, "rf1", "SYS", [U("甲"), A("x"), U("乙")])
        self.assertIn("has no such session", r3["rebuilt"])              # asserts the correct value: the retry lands on a new process
        self.assertTrue(r3["text"].startswith("echo[1]: "))

    def test_bad_model_is_refused_before_anything_spawns(self):
        before = len(helpers.read_fake_log())
        with self.assertRaises(scv.BridgeError) as cm:
            call(self.m, "s7", "SYS", [U("甲")], model="claude/opus & calc")
        self.assertEqual(cm.exception.klass, "bad_request")
        self.assertEqual(len(helpers.read_fake_log()), before)

    def test_last_message_must_be_user(self):
        with self.assertRaises(scv.BridgeError) as cm:
            call(self.m, "s8", "SYS", [U("甲"), A("x")])
        self.assertEqual(cm.exception.klass, "bad_request")

    def test_session_id_never_becomes_a_path(self):
        # ⭐Pick the process this case itself started using a window (re-review 2 M-3 audit): the check is "cwd has
        #   no evil in it", and any boot-log line left over from a previous case would satisfy it just as well =>
        #   `[-1]` reading someone else's line would be a false green.
        mark = len(helpers.read_fake_log())
        call(self.m, "../../evil", "SYS", [U("甲")])
        boots = [x for x in helpers.fake_log_since(mark) if x.get("family") == "claude"]
        self.assertEqual(len(boots), 1, boots)
        boot = boots[0]
        work = os.path.realpath(os.path.join(HOME, "work"))
        self.assertTrue(os.path.realpath(boot["cwd"]).startswith(work + os.sep), boot["cwd"])
        self.assertNotIn("evil", boot["cwd"])

    def test_no_session_means_one_shot(self):
        r = call(self.m, None, "SYS", [U("甲"), A("x"), U("乙")])
        self.assertTrue(r["text"].startswith("echo[1]: [scv] Your previous session"))
        self.assertEqual(self.m.snapshot()["sessions"], 0)
        self.assertEqual(self.m.snapshot()["children"], [])


class OneShot(unittest.TestCase):
    """🔴Re-review 2 M-1: neither end of the `finally` plus `answered` on the path with no session (`_run_once`) had
    a test at all (review mutation: never kill it even when it did not finish answering => 6 modules, 303 cases, all
    green; kill it even when it did finish answering => 121 cases, all green).
    ⭐One case per end, and the check is which one got called on the driver (a probe wrapping the real method, never
      a stub):
      - died midway (the caller's `on_delta` throws `KeyError`) => `kill`, and it comes back within a few seconds --
        going through a graceful `close()` instead would have the CLI keep dripping text out through the whole
        `CLOSE_GRACE_S` grace period (review measured 11.33 seconds, 0.32 seconds after the fix);
      - finished answering => `close` (a graceful close), never `kill`."""

    def setUp(self):
        self.m = mgr()
        self.calls = []
        for name in ("kill", "close"):
            real = getattr(scv.ClaudeDriver, name)

            def spy(d, _name=name, _real=real):
                self.calls.append(_name)
                return _real(d)

            patcher = mock.patch.object(scv.ClaudeDriver, name, autospec=True, side_effect=spy)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        os.environ["FAKE_MODE"] = "ok"
        self.m.close_all()

    def test_a_turn_that_dies_midway_is_killed_not_closed(self):
        os.environ["FAKE_MODE"] = "trickle"      # keeps dripping text forever, never wraps up: a graceful close would have to wait out the full grace period

        def boom(_s):
            raise KeyError("an unfamiliar exception from inside the caller's callback")

        t0 = time.time()
        with self.assertRaises(KeyError):
            self.m.run(session_id=None, model_id="claude/haiku", effort=None, system="SYS", messages=[U("甲")],
                       on_started=lambda ms: None, on_delta=boom, cancel=None, first_token=25, timeout=25)
        took = time.time() - t0
        self.assertEqual(self.calls, ["kill"], "did not finish answering, yet it went through a graceful close")
        self.assertLess(took, 5.0, "the turn that did not finish took %.2f seconds to come back (a graceful close would have to wait out the full %ss grace period)" % (took, scv.CLOSE_GRACE_S))

    def test_a_turn_that_finished_is_closed_not_killed(self):
        """The other end: one that finished answering must never be killed as if it had errored out (a graceful
        close means closing stdin and waiting for it to exit on its own).
        ⭐Without this case, the one above cannot be told apart from "kill it no matter whether it finished
          answering"."""
        r = call(self.m, None, "SYS", [U("甲")])
        self.assertEqual(r["text"], "echo[1]: 甲")
        self.assertEqual(self.calls, ["close"])


class Queue(unittest.TestCase):
    def tearDown(self):
        os.environ["FAKE_MODE"] = "ok"

    def test_queue_time_is_reported_separately(self):
        """The other half of B28: the time spent queued has to be reported separately, so the dispatcher can avoid
        counting it against the first-token gate."""
        os.environ["FAKE_MODE"] = "slow_first"
        os.environ["FAKE_DELAY"] = "1.5"
        m = mgr(max_concurrent=1)
        out = {}
        t = threading.Thread(target=lambda: out.update(a=call(m, "qa", "S", [U("甲")])))
        t.start()
        time.sleep(0.3)
        out["b"] = call(m, "qb", "S", [U("乙")])
        t.join()
        m.close_all()
        self.assertLess(out["a"]["queued_ms"], 300)
        self.assertGreaterEqual(out["b"]["queued_ms"], 1000)

    def test_cancel_while_queued_does_not_leak_the_slot(self):
        os.environ["FAKE_MODE"] = "slow_first"
        os.environ["FAKE_DELAY"] = "1.5"
        m = mgr(max_concurrent=1)
        t = threading.Thread(target=lambda: call(m, "ca", "S", [U("甲")]))
        t.start()
        time.sleep(0.3)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(scv.BridgeError) as cm:
            call(m, "cb", "S", [U("乙")], cancel=cancel)
        self.assertEqual(cm.exception.klass, "cancelled")
        t.join()
        os.environ["FAKE_MODE"] = "ok"
        self.assertEqual(call(m, "cc", "S", [U("丙")])["text"], "echo[1]: 丙")    # the slot is still there
        m.close_all()


# ━━━━━━━━━━━━ Everything below is outside the brief: the brief's 13 cases only ever go through claude, and only
# ever exercise "one smooth path" and the four kinds of rebuild ━━━━━━━━━━━━
# What gets added covers four gaps: (1) running each family through once (the whole codex family had zero coverage);
# (2) `rebuilt`'s fourth reason, "the model or effort changed", had zero coverage; (3) the three interfaces
# `close_session` / `gc_idle` / `snapshot` had zero coverage; (4) two timing contracts around concurrency.

CODEX = "codex/gpt-5.6-luna"
CAT2 = CAT + ["claude/sonnet"]
NL = chr(10)


def call_e(m, sid, system, messages, model="claude/haiku", effort=None, cancel=None, **kw):
    """The version with effort. ⚠️The `call` the brief gave above has effort pinned to None, and "changing the
    effort must trigger a rebuild" is exactly the case that needs testing => a separate one is started here, never
    modify that one."""
    started = []
    res = m.run(session_id=sid, model_id=model, effort=effort, system=system, messages=messages,
                on_started=started.append, on_delta=lambda s: None, cancel=cancel, **kw)
    res["_started"] = started
    return res


def log_lines_during(fn):
    """The new lines that really land on disk in bridge.log during this stretch. Never a count of calls to a mocked
    scv.log: what needs measuring is the bytes.
    (tests/test_30_drivers.py has one shaped the same way -- it is a measuring tool, never a check, so the two of
    them will not fight each other.)"""
    p = scv.spath("bridge.log")
    before = p.read_text(encoding="utf-8") if p.exists() else ""
    with contextlib.redirect_stderr(io.StringIO()):
        fn()
    after = p.read_text(encoding="utf-8") if p.exists() else ""
    return after[len(before):].splitlines()


def work_dirs():
    """Which directories currently sit under `work/`. ⭐Both C-1 and I-1's checks look at its delta -- returns the
    names, never a count, so when it goes red you can see at a glance which one leaked."""
    p = scv.spath("work")
    return sorted(x.name for x in p.iterdir()) if p.exists() else []


def boots(family):
    """The fake CLI's boot-time record. The claude family records `system` / `argv` / `cwd`; the codex family's
    prompt is recorded separately, on the thread/start entry."""
    return [x for x in helpers.read_fake_log() if x.get("family") == family]


class _Staged(unittest.TestCase):
    """⭐Every test case restores `FAKE_*` going in and coming out, and defaults back to `ok` (shape copied from
    tests/test_30_drivers.py).

    Never rely on the unwritten rule "every setUp remembers to reset FAKE_MODE": the next person adds a case that
    forgets to set the mode, and it silently runs in whatever mode the previous case left behind -- that kind of
    green means nothing, and nobody can tell.
    ⭐`self.mgr()` always hangs `addCleanup(close_all)` off it: leak one unclosed manager => its long-running CLI is
      still registered in children.json => `snapshot()["children"] == []` in some other class goes red for no
      apparent reason."""

    def setUp(self):
        saved = {k: v for k, v in os.environ.items() if k.startswith("FAKE_")}
        self.addCleanup(self._restore, saved)
        os.environ["FAKE_MODE"] = "ok"

    @staticmethod
    def _restore(saved):
        for k in [k for k in os.environ if k.startswith("FAKE_")]:
            if k not in saved:
                del os.environ[k]
        os.environ.update(saved)

    def mgr(self, max_concurrent=4, cat=None):
        m = scv.SessionManager({"max_concurrent": max_concurrent},
                               lambda: list(cat if cat is not None else CAT))
        self.addCleanup(m.close_all)
        return m


class CodexFamily(_Staged):
    """⭐The same rule, walked down each path once: the session layer is family-agnostic, but the two families'
    drivers are not -- codex's `__init__` has a whole extra handshake stretch, and what a rebuild's shot has to kill
    is different too. Not one of the brief's 13 cases ever exercised codex."""

    def test_codex_sessions_also_feed_only_the_new_message(self):
        m = self.mgr()
        r1 = call(m, "x1", "SYS", [U("甲")], model=CODEX)
        r2 = call(m, "x1", "SYS", [U("甲"), A(r1["text"]), U("乙")], model=CODEX)
        self.assertEqual((r1["text"], r2["text"]), ("echo[1]: 甲", "echo[2]: 乙"))
        self.assertEqual((r1["rebuilt"], r2["rebuilt"]), (None, None))
        self.assertEqual((r1["family"], r2["family"]), ("codex", "codex"))

    def test_codex_prefix_mismatch_rebuilds_from_the_full_history_too(self):
        """⭐This pins down even "the new process really did get the new prompt": codex's prompt travels through
        `thread/start`'s `baseInstructions`, which is not the same path at all as the claude family's
        `--system-prompt-file` => that family's green must never be treated as evidence for this one."""
        m = self.mgr()
        call(m, "x2", "旧题面", [U("甲")], model=CODEX)
        r2 = call(m, "x2", "新题面", [U("甲"), A("x"), U("乙")], model=CODEX)
        self.assertEqual(r2["rebuilt"], "the prefix does not match")
        self.assertTrue(r2["text"].startswith("echo[1]: [scv] Your previous session"), r2["text"])
        started = [x for x in helpers.read_fake_log() if "thread_params" in x]
        self.assertEqual(started[-1]["system"], "新题面")


class TextPassthrough(_Staged):
    """⭐`text` passes through verbatim, this layer never trims it (this layer is transport, not presentation). The
    two families' trim state was already different to begin with, and the two cases below each pin one family --
    never write just the one.

    ⚠️The two cases do not carry the same weight, and that is written down plainly here:
      - The codex case is a real gate: its text is accumulated character by character by the driver, and the
        leading/trailing whitespace is still there => if anyone tacks a `.strip()` onto the session layer, it goes
        red on the spot (a negative control was run for this, see the report).
      - The claude case cannot prove "this layer does not trim": `ClaudeDriver` has already done one outer strip on
        `result`, so stripping it again is a no-op => it pins down a different thing entirely -- this family's
        whitespace is already gone before it ever reaches this layer. Never read this as "both families are
        covered", and never trim the codex family flat just because this one looks "clean"."""

    def test_codex_text_keeps_its_leading_and_trailing_whitespace(self):
        os.environ["FAKE_MODE"] = "empty_tail_message"
        r = call(self.mgr(), "w1", "S", [U("甲")], model=CODEX)
        self.assertEqual(r["text"], "  echo[1]: 甲" + NL)     # asserts the correct value: the whitespace on both ends is there, character for character

    def test_claude_text_arrives_already_trimmed_by_the_driver(self):
        """⚠️`FAKE_RESULT_PAD` is adversarial (off by default), never a measured shape -- nobody has measured whether
        real claude's `result` carries leading/trailing whitespace. ⭐But it cannot be skipped: without the stub
        supplying that whitespace on both ends, the line below comes out green just the same on an implementation
        where nobody trims anything at all (another fixture that happens to satisfy the contract)."""
        os.environ["FAKE_RESULT_PAD"] = "1"
        r = call(self.mgr(), "w2", "S", [U("甲")])
        self.assertEqual(r["text"], "echo[1]: 甲")
        self.assertNotIn(NL, r["text"])


class Sig(_Staged):
    """⭐`rebuilt`'s fourth reason. The fingerprint cannot recognize this spot: switch the model, and system and
    messages have not changed by a single character => the prefix still matches => without this `sig` check, the
    player picks sonnet and the one still answering is that same old haiku process, and completely silently at that
    (all three tables green, and nothing in the readings shows it either)."""

    def test_changing_the_model_rebuilds_and_the_new_process_really_gets_it(self):
        m = self.mgr(cat=CAT2)
        r1 = call(m, "g1", "SYS", [U("甲")])
        r2 = call(m, "g1", "SYS", [U("甲"), A(r1["text"]), U("乙")], model="claude/sonnet")
        self.assertEqual(r2["rebuilt"], "the model or effort changed")
        self.assertTrue(r2["text"].startswith("echo[1]: [scv] Your previous session"), r2["text"])
        # ⭐The check is a measured hit on the consumer side: the model on the new process's command line really did
        #   change, never just "we changed a field"
        argv = boots("claude")[-1]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")

    def test_changing_the_effort_rebuilds_too(self):
        m = self.mgr()
        r1 = call_e(m, "g2", "SYS", [U("甲")], effort="low")
        r2 = call_e(m, "g2", "SYS", [U("甲"), A(r1["text"]), U("乙")], effort="high")
        self.assertEqual(r2["rebuilt"], "the model or effort changed")
        argv = boots("claude")[-1]["argv"]
        self.assertEqual(argv[argv.index("--effort") + 1], "high")

    def test_the_same_model_and_effort_do_not_rebuild(self):
        """Zero-input control: remove the "it changed" condition and walk the same path again -- it must never
        rebuild no matter what it sees (otherwise the two cases above would both come out green under an
        implementation that always rebuilds)."""
        m = self.mgr()
        call_e(m, "g3", "SYS", [U("甲")], effort="low")
        r2 = call_e(m, "g3", "SYS", [U("甲"), A("x"), U("乙")], effort="low")
        self.assertEqual((r2["text"], r2["rebuilt"]), ("echo[2]: 乙", None))


class Refuse(_Staged):
    """The closed-set side. ⭐"Did it start a process or not" is the real check: asserting only that it threw
    bad_request would let an implementation that "starts the process first, then discovers the parameter is wrong"
    come out green just the same, and it pays the cost of one wasted startup every single time."""

    def test_unknown_effort_is_refused_before_anything_spawns(self):
        m = self.mgr()
        before = len(helpers.read_fake_log())
        with self.assertRaises(scv.BridgeError) as cm:
            call_e(m, "e1", "SYS", [U("甲")], effort="ultra")
        self.assertEqual(cm.exception.klass, "bad_request")
        self.assertEqual(len(helpers.read_fake_log()), before)

    def test_an_unknown_effort_is_refused_before_it_even_queues(self):
        """🔴The closed-set check on `effort` exists in two places (this layer, plus `claude_argv`), and that other
        one sits after the concurrency slot has already been taken => asserting only that it "threw bad_request"
        means removing this layer's gate turns not one single test red (Task 10's re-review audit measured: the
        full 499, all green) -- the two mechanisms cover for each other, and all three tables stay green.
        ⭐The check is swapped for "it got refused before it even got in line": when the slots are all taken, the two
          arms' responses do not differ by a single byte, what differs is 0.00s vs. 13.49s (re-review measured).
        ⚠️Use a thread plus `join()`, never call it directly: with that gate removed, this call would just queue
          forever, and calling it directly would hang the whole test suite (a hung suite and a red suite are never
          the same thing)."""
        m = self.mgr(max_concurrent=1)
        m._slots.acquire()          # occupies the one and only slot (never actually run a turn here: that would leave a process behind)
        box = {}

        def go():
            try:
                call_e(m, "eff-queue", "SYS", [U("甲")], effort="ultra")
            except scv.BridgeError as exc:
                box["e"] = exc

        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(3)
        try:
            self.assertIn("e", box, "it went and got in line => this layer's gate did not take effect (with the slots full, a bad request has to wait for a slot before it gets refused)")
            self.assertEqual(box["e"].klass, "bad_request")
        finally:
            m._slots.release()      # ⭐let it go: on the arm with the gate removed, that thread above is still waiting here

    def test_a_bad_message_anywhere_is_refused_before_anything_spawns(self):
        """🔴The brief's blind spot. ⭐Three axes, never one -- `run()` used to only check the last message's content,
        while `flatten()` touches every single one's role and content, and `fingerprint()` does the same. Measured
        on win32 on 2026-09-22, each of the three axes throws its own bare exception:
          - a content in the middle that is not a string => `TypeError: sequence item 3: expected str instance, NoneType found`
          - role missing entirely => `KeyError: 'role'`
          - a message that is not an object at all => `TypeError: string indices must be integers, not 'str'`
        None of the three is a BridgeError, not one line lands on disk, and all three blow up after `make_driver`
        => one bad request equals really starting one CLI process (measured: `children.json` gets +1 in each of the
        three cases), with the process left hanging around alive on that session.
        `messages` / `session_id` both come from the network => firing off a different id each time is enough to
        turn network input into a long-running process on this machine.

        ⭐What is truly load-bearing is the ordering "before a process ever starts", never the wording of that
          sentence => the check below is that `children.json` must not gain a single line, never "it threw
          bad_request" (asserting only the category would let an implementation that "starts the process first,
          then discovers the parameter is wrong" come out green just the same, and it leaves one CLI stranded for
          every bad request)."""
        m = self.mgr()
        axes = (("content is not a string", [U(None), A("x"), U("乙")]),
                ("role is missing entirely", [{"content": "甲"}, A("x"), U("乙")]),
                ("the message is not an object at all", ["甲", A("x"), U("乙")]))
        for tag, msgs in axes:
            with self.subTest(axis=tag):
                before = len(scv.children())
                with self.assertRaises(scv.BridgeError) as cm:
                    call(m, "bad-" + tag, "SYS", msgs)
                self.assertEqual(cm.exception.klass, "bad_request")
                self.assertEqual(len(scv.children()), before, "a CLI process was started before the parameter was found to be wrong")
                # never let the other two resources tallied by id from this same family (see the report's section
                # "resources tallied by session_id") be left behind either
                self.assertEqual((list(m._sessions), list(m._locks)), ([], []))

    def test_a_non_text_system_is_refused_before_anything_spawns(self):
        """🔴Same shape as the hole above, one parameter apart (review C-1). Measured on win32 on 2026-09-22, the
        same bad input costs the two families differently, and the family that fails is the silent one:
          - claude => `TypeError: data must be str, not NoneType` (blows up inside `sys_file.write_text`, whose
            except only catches OSError/ValueError), plus leaks a `work/` directory tallied by session_id;
          - codex => throws nothing at all, `baseInstructions: null` just goes out as-is, and it even finishes
            answering the turn normally.
        ⭐So both families have to be run through, never just claude: pinning only the family that blows up leaves
          the silent family green just the same. The check follows the neighboring case: `children.json` must not
          gain a single line, and `work/` must not gain a single directory."""
        m = self.mgr()
        for model, bad in (("claude/haiku", None), ("claude/haiku", 123), (CODEX, None), (CODEX, {"a": 1})):
            with self.subTest(model=model, system=type(bad).__name__):
                before = (len(scv.children()), work_dirs())
                with self.assertRaises(scv.BridgeError) as cm:
                    call(m, "sys-" + model, bad, [U("甲")], model=model)
                self.assertEqual(cm.exception.klass, "bad_request")
                self.assertEqual((len(scv.children()), work_dirs()), before)
                self.assertEqual((list(m._sessions), list(m._locks)), ([], []))

    def test_a_driver_that_fails_to_start_leaves_no_workdir_behind(self):
        """🔴When `make_driver` throws, `self._sessions[sid]` has not been assigned yet => the directory that was
        already created is then something none of `_drop` / `gc_idle` / `close_all` can find, and it just sits on
        disk (review I-1 measured this).

        ⭐The with-session case is worse than the one-shot case: the one-shot directory name is
          `sha256("once-"+uuid4)`, while the with-session one is `sha256(the network-supplied session_id)` => firing
          off different ids one after another piles up one directory per id.
        ⚠️Resending the same id cannot be measured this way (`mkdir(exist_ok=True)` reuses the same directory) => the
          two subTest cases below each use their own id. The check is the delta in `work/`, never "it threw
          crashed"."""
        os.environ["FAKE_MODE"] = "handshake_error"
        os.environ["FAKE_STUCK_S"] = "30"
        m = self.mgr()
        for tag, sid in (("with a session (goes through _run_session)", "starts-but-fails"), ("one-shot (goes through _run_once)", None)):
            with self.subTest(path=tag):
                before = work_dirs()
                with self.assertRaises(scv.BridgeError) as cm:
                    call(m, sid, "SYS", [U("甲")], model=CODEX)
                self.assertEqual(cm.exception.klass, "crashed")
                self.assertEqual(work_dirs(), before)
                self.assertEqual(list(m._sessions), [])

    def test_a_driver_that_blows_up_in_a_way_we_do_not_recognise_leaks_nothing_either(self):
        """🔴A gate has to be built to the shape of the bug: the shape here is "the directory leaks whenever
        `make_driver` throws any exception", never "leaks whenever it throws BridgeError". Last round I wrote it as
        `except BridgeError:` to match the scene at hand, and review's three probes each leaked one directory
        (`TypeError` / `KeyboardInterrupt` / `_run_once` plus `TypeError`).

        ⭐Less than 350 lines earlier in this very same file, `CodexDriver.__init__` already argued this exact point,
          word for word, for the same category of resource ("use `finally`, never `except BridgeError`: that only
          covers the one family of exceptions we ourselves recognize, and this stretch also has other people's
          (KeyboardInterrupt, json serialization)") -- I did not cite it, and I did not write down why it was okay to
          narrow it this time.
        ⚠️`KeyboardInterrupt` is deliberately run through this case too: it is not even an `Exception` => any fix
          written as `except Exception` would still leak in the face of it."""
        m = self.mgr()
        for exc in (TypeError("JSON serialization blew up"), KeyboardInterrupt()):
            for tag, sid in (("with a session", "boom-" + type(exc).__name__), ("one-shot", None)):
                with self.subTest(exc=type(exc).__name__, path=tag):
                    before = work_dirs()
                    with mock.patch.object(scv, "make_driver", side_effect=exc):
                        with self.assertRaises(type(exc)):
                            call(m, sid, "SYS", [U("甲")])
                    self.assertEqual(work_dirs(), before)
                    self.assertEqual(list(m._sessions), [])

    def test_a_good_effort_really_reaches_the_cli(self):
        """The other half of the negative control: the ones inside the closed set must never get blocked along with
        the rest (otherwise the case above would come out green just the same on an implementation that refuses
        every effort), and it has to actually reach the command line."""
        call_e(self.mgr(), "e2", "SYS", [U("甲")], effort="medium")
        argv = boots("claude")[-1]["argv"]
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")


class FamilyCap(_Staged):
    """🔴That family of resources has only one upper bound (A1-A9, counted by Task 8): `session_id` is a
    network-supplied key with no bound of its own, and this machine hands out a process / thread / fd / lock /
    working directory / registry row against that same key. => What is checked is that "after adding one upper
    bound, every single one of these gets capped along with it", never "the `_sessions` dict does not exceed N" --
    pinning only the dict would let `_locks`, `children.json`, and `work/` each leak on their own and still come out
    green (which is exactly how they were leaking before this round).
    ⚠️The bound itself is set by fd count; the reasoning and the measurement live in the `scv.MAX_SESSIONS`
    section."""

    def caps(self, m):
        return (len(m._sessions), len(m._locks), len(m._lock_users), len(scv.children()), len(work_dirs()))

    def test_one_cap_holds_the_whole_family_down(self):
        m = self.mgr()
        m.max_sessions = 3
        for i in range(8):
            call(m, "cap-%d" % i, "S", [U("甲")])
            self.assertEqual(self.caps(m), (min(i + 1, 3),) * 5, "after the %dth one, something did not get capped along with the rest" % i)

    def test_without_the_cap_every_one_of_them_grows(self):
        """Zero-input control: remove the upper bound and walk the same path again -- every single one of these must
        really grow to 8. Without this line, the case above cannot be told apart from "this path could only ever
        open 3 to begin with"."""
        m = self.mgr()
        m.max_sessions = 999
        for i in range(8):
            call(m, "nocap-%d" % i, "S", [U("甲")])
        self.assertEqual(self.caps(m), (8, 8, 8, 8, 8))

    def test_the_one_that_got_evicted_comes_back_by_rebuilding(self):
        """Being evicted is never the same as being broken: the next time it comes, it rebuilds from the full
        history, and it can explain itself (`rebuilt`). ⭐This also pins down that the one evicted is the
        least-recently-used one: cap-0 gets evicted, cap-2 is still on its original process (echo[2])."""
        m = self.mgr()
        m.max_sessions = 2
        call(m, "lru-0", "S", [U("甲")])
        call(m, "lru-1", "S", [U("甲")])
        call(m, "lru-2", "S", [U("甲")])                      # squeezes out lru-0
        self.assertEqual(sorted(m._sessions), ["lru-1", "lru-2"])
        again = call(m, "lru-0", "S", [U("甲"), A("x"), U("乙")])
        self.assertIn("has no such session", again["rebuilt"])
        self.assertTrue(again["text"].startswith("echo[1]: [scv] Your previous session"), again["text"])

    def test_a_session_that_is_answering_is_never_the_one_evicted(self):
        """🔴Never, under any circumstance, evict one that is currently answering: `close()` would make that turn see
        a stdout EOF => it gets reported as `crashed`, meaning "the bridge crashed, retry it", which is exactly the
        lie `_closed_midflight()` exists to fix.
        ⭐The check is that turn's own outcome (it has to finish answering properly), never "is it still in the
        dict".
        ⚠️Both spots in the fixture are load-bearing, neither can be skipped:
          (1) the one in flight is deliberately the least-recently-used one (`last_used` frozen at the previous
            turn) -- staged any other way, LRU would dodge it on its own, and this gate would end up testing LRU's
            luck instead;
          (2) that turn is deliberately longer than `CLOSE_GRACE_S` (10s). ⭐This one was forced out by a negative
            control: it used to only make it 3 seconds slower, and deleting the lock check entirely still came out
            all green on this test -- `close()` is graceful (close stdin first, then wait out the grace period), and
            a short turn finishes answering on its own within the grace period => "evicted the one in flight" and
            "did not evict it" look identical. What is truly dangerous is the case where it cannot finish answering:
            once the grace period runs out it gets tree-killed, that turn sees an EOF => reported as `crashed`."""
        m = self.mgr(max_concurrent=4)
        m.max_sessions = 2
        # ⚠️`FAKE_MODE` / `FAKE_DELAY` are read in by the fake CLI at the moment it starts up => to make busy, this
        #   long-running process, answer slowly, they can only be set before it is built (changing the environment
        #   variable after it is already running does it no good at all -- that is exactly this case's first-version
        #   false green: that turn actually answered instantly, and the two arms looked identical).
        os.environ["FAKE_MODE"] = "slow_first"
        os.environ["FAKE_DELAY"] = "12"                       # > CLOSE_GRACE_S(10), see (2) above
        r1 = call(m, "busy", "S", [U("甲")])
        os.environ["FAKE_MODE"] = "ok"                        # everything built after this answers instantly
        os.environ.pop("FAKE_DELAY", None)
        call(m, "other", "S", [U("甲")])
        m._sessions["busy"].last_used = 0                     # in LRU terms it is the one most deserving of eviction
        box = {}

        def slow():
            try:
                box["r"] = call(m, "busy", "S", [U("甲"), A(r1["text"]), U("乙")])
            except Exception as exc:                          # need to be able to see which kind when this goes red
                box["e"] = exc

        t = threading.Thread(target=slow, daemon=True)
        t.start()
        time.sleep(1.0)                                       # it has already entered turn_lock, the CLI is thinking
        os.environ["FAKE_MODE"] = "ok"
        call(m, "newcomer", "S", [U("甲")])                    # this hits the upper bound: only other can be evicted, never busy
        t.join(60)
        self.assertNotIn("e", box, box.get("e"))
        self.assertEqual((box["r"]["text"], box["r"]["rebuilt"]), ("echo[2]: 乙", None))
        self.assertNotIn("other", m._sessions)

    def test_the_lock_table_is_reclaimed_with_the_session(self):
        """🔴A4: `_locks` used to be reclaimed by nothing at all (`_drop` / `gc_idle` / `close_all` all clean up
        `_sessions`, not this), and the key comes from the network => it only grows, never shrinks. ⭐Each of the
        three reclamation paths is walked once: a one-shot turn (which should never have kept an entry at all),
        closing it, and idle reclamation."""
        m = self.mgr()
        for i in range(5):
            m.run(session_id=None, model_id="claude/haiku", effort=None, system="S", messages=[U("甲")],
                  on_started=lambda ms: None, on_delta=lambda s: None, cancel=None)
        self.assertEqual((len(m._locks), len(m._lock_users)), (0, 0), "a one-shot turn should never have entered this table at all")
        call(m, "k1", "S", [U("甲")])
        call(m, "k2", "S", [U("甲")])
        self.assertEqual(len(m._locks), 2)
        m.close_session("k1")
        m._sessions["k2"].last_used = time.time() - 50
        m.gc_idle(max_idle=10)
        self.assertEqual((len(m._locks), len(m._lock_users), len(m._sessions)), (0, 0, 0))

    def test_the_value_of_the_cap_is_pinned_to_the_account_it_came_from(self):
        """🔴The mechanism is pinned down, the value is not pinned down at all: review changed `MAX_SESSIONS` from 24
        to 2400, and `test_50_sessions` plus `test_00_budget`'s 85 cases all came out green -- yet that fd
        arithmetic (`RLIMIT_NOFILE` soft limit 1024, 3 parent-side pipe fds per session, allowed to use only a
        fifth) is exactly what computes this value.
        ⭐So this assertion writes down the arithmetic itself, never `== 24`: whoever changes the value is bound to
        run into the arithmetic, and once the arithmetic changes they have to redo it."""
        self.assertLessEqual(scv.MAX_SESSIONS * 3, 1024 * 0.25,
                             "the session upper bound times 3 parent-side fds per session goes over a fifth of RLIMIT_NOFILE's soft limit")
        self.assertEqual(scv.MAX_SESSIONS, 24)

    def test_the_cap_is_always_above_the_concurrency_limit(self):
        """🔴I wrote that the `+1` in `max(MAX_SESSIONS, max_concurrent + 1)` was "never just padding out a number",
        and after review removed it, `test_50_sessions`'s 44 cases all came out green -- it only ever takes effect
        when `max_concurrent ≥ 24`, and the test harness has never been configured that way. => That "load-bearing"
        claim was, at the time, an unverified assertion written down in a comment.
        ⭐What it buys is one invariant: the session upper bound is greater than the concurrency slots => the number
          of turns in flight is always less than the upper bound => `_make_room` is always able to evict one (the
          fallback on that path, "every single one is currently answering, so build over the bound anyway", is the
          branch that truly can never be reached)."""
        for mc in (1, 4, scv.MAX_SESSIONS, scv.MAX_SESSIONS + 7):
            with self.subTest(max_concurrent=mc):
                self.assertGreater(mgr(max_concurrent=mc).max_sessions, mc)

    def test_a_live_session_keeps_its_lock(self):
        """Zero-input control: one that is still alive must never be swept away in passing -- sweeping it away means
        two threads each holding their own lock into the same session."""
        m = self.mgr()
        call(m, "keep", "S", [U("甲")])
        self.assertEqual(list(m._locks), ["keep"])
        r = call(m, "keep", "S", [U("甲"), A("x"), U("乙")])
        self.assertEqual((r["text"], r["rebuilt"]), ("echo[2]: 乙", None))
        self.assertEqual(list(m._locks), ["keep"])


class Lifecycle(_Staged):
    """The three interfaces `close_session` / `gc_idle` / `snapshot`. ⭐The check is always on the process side
    (whether it can still go on answering echo[2] / whether it is still in the registry), never "whether this key is
    still in the dict"."""

    def test_close_session_says_whether_it_knew_that_id(self):
        m = self.mgr()
        call(m, "c1", "S", [U("甲")])
        sess = m._sessions["c1"]
        self.assertTrue(m.close_session("c1"))
        self.assertFalse(m.close_session("c1"))          # second time: it no longer recognizes this one at all
        self.assertFalse(m.close_session("从来没有过"))
        self.assertFalse(sess.driver.alive())            # the process is really gone
        self.assertEqual(m.snapshot()["children"], [])   # cleaned out of the registry too
        self.assertFalse(sess.workdir.exists(), sess.workdir)
        self.assertIn("closed before", call(m, "c1", "S", [U("甲"), A("x"), U("乙")])["rebuilt"])

    def test_a_session_reopened_while_the_old_one_is_still_closing_keeps_its_files(self):
        """Outside review's original scope, ① (I2's root cause, present since BASE): the old and new instances
        of the same session id used to share one working directory, and the old one's teardown (`_drop`: `close()`
        first -- waiting at most CLOSE_GRACE_S -- then `rmtree`) deletes the `system.txt` / `isolation.json` the new
        one just wrote (review's probe ran into a retryable false `crashed` from exactly this).
        ⭐One instance, one directory (`_workdir` plus a random suffix). Fixture: the old CLI hangs around for 3
        seconds after stdin closes (`FAKE_EXIT_DELAY`), and while it is closing, the same id comes in again (with the
        full history) => the check is that the new instance answered as usual, its two files are still there after
        the old one finishes tearing down, and it can go on answering the next turn; the old directory is gone."""
        m = self.mgr()
        os.environ["FAKE_EXIT_DELAY"] = "3"
        self.addCleanup(os.environ.pop, "FAKE_EXIT_DELAY", None)   # if this threw partway through it would stay in the process => every fake claude afterward would hang around 3 seconds on close (fix2 M-c)
        call(m, "reopen", "S", [U("甲")])
        old = m._sessions["reopen"]
        os.environ.pop("FAKE_EXIT_DELAY")
        closing = threading.Thread(target=m.close_session, args=("reopen",), daemon=True)
        closing.start()
        time.sleep(0.5)                                  # the old one is hanging around (stdin closed, has not exited yet)
        self.assertTrue(closing.is_alive(), "fixture precondition: the old one is still tearing down when the new one arrives")
        r = call(m, "reopen", "S", [U("甲"), A("x"), U("乙")])
        new = m._sessions["reopen"]
        closing.join(20)
        self.assertFalse(closing.is_alive())
        self.assertEqual([(new.workdir / f).exists() for f in ("system.txt", "isolation.json")], [True, True],
                         "the old instance's teardown deleted the new instance's files")
        self.assertIn("closed before", r["rebuilt"])
        self.assertFalse(old.workdir.exists())
        r2 = call(m, "reopen", "S", [U("甲"), A("x"), U("乙"), A(r["text"]), U("丙")])
        self.assertEqual((r2["text"], r2["rebuilt"]), ("echo[2]: 丙", None))

    def test_gc_idle_only_takes_the_idle_ones(self):
        m = self.mgr()
        call(m, "i1", "S", [U("甲")])
        call(m, "i2", "S", [U("甲")])
        m._sessions["i1"].last_used = time.time() - 50
        self.assertEqual(m.gc_idle(max_idle=10), 1)
        self.assertEqual(sorted(m._sessions), ["i2"])
        # ⭐The one that survives is still its original process (echo[2] => it was not quietly rebuilt), never just
        #   "the key is still there"
        r = call(m, "i2", "S", [U("甲"), A("x"), U("乙")])
        self.assertEqual((r["text"], r["rebuilt"]), ("echo[2]: 乙", None))
        self.assertIn("idle too long", call(m, "i1", "S", [U("甲"), A("x"), U("乙")])["rebuilt"])

    def test_gc_idle_takes_nothing_when_nothing_is_idle(self):
        """Zero-input control: remove the "idle" condition and measure again -- it must never sweep up anything at
        all."""
        m = self.mgr()
        call(m, "i3", "S", [U("甲")])
        call(m, "i4", "S", [U("甲")])
        self.assertEqual(m.gc_idle(max_idle=3600), 0)
        self.assertEqual(sorted(m._sessions), ["i3", "i4"])
        r = call(m, "i3", "S", [U("甲"), A("x"), U("乙")])
        self.assertEqual((r["text"], r["rebuilt"]), ("echo[2]: 乙", None))

    def test_snapshot_reports_the_live_child(self):
        """⚠️`rss_kb` has only ever been measured on win32 (`proc_rss_kb`'s two implementations are two separate
        paths) => never read this test's green as "both OSes are correct"."""
        m = self.mgr()
        call(m, "n1", "S", [U("甲")])
        snap = m.snapshot()
        self.assertEqual((snap["sessions"], snap["queued"], snap["running"]), (1, 0, 0))
        self.assertEqual([(c["pid"], c["family"]) for c in snap["children"]],
                         [(m._sessions["n1"].driver.pid, "claude")])
        rss = snap["children"][0]["rss_kb"]
        self.assertIsInstance(rss, int)
        self.assertGreater(rss, 0, "a live process taking up 0KB => this ruler measured nothing")


class Concurrency(_Staged):
    """Two timing contracts. Neither can be judged by "did it finish running" -- what has to be measured is exactly
    which moment it happens."""

    def test_a_second_turn_on_the_same_session_waits_instead_of_stomping_on_the_first(self):
        """The same session answers only one turn at a time. ⭐Without that `turn_lock`, the second turn would read
        `fp` as still an empty string while the first turn has not finished answering => it gets judged as "the
        prefix does not match" => the process the first turn is currently using gets killed on the spot (the first
        turn ends up with crashed). This is never a "just a bit slower" kind of loss."""
        os.environ["FAKE_MODE"] = "slow_first"
        os.environ["FAKE_DELAY"] = "1.5"
        m = self.mgr()
        box = {}

        def first():
            try:
                box["r1"] = call(m, "p1", "S", [U("甲")])
            except scv.BridgeError as exc:
                box["e1"] = exc

        t = threading.Thread(target=first)
        t.start()
        time.sleep(0.8)                      # let the first turn register the session first, and still be stuck inside the CLI
        r2 = call(m, "p1", "S", [U("甲"), A("x"), U("乙")])
        t.join()
        self.assertNotIn("e1", box, "the first turn got trampled to death by the second one: %s" % box.get("e1"))
        self.assertEqual(box["r1"]["text"], "echo[1]: 甲")
        self.assertEqual((r2["text"], r2["rebuilt"]), ("echo[2]: 乙", None))

    def test_snapshot_counts_the_turn_in_flight_and_the_one_waiting_behind_it(self):
        """Positive control: these two counters, `running` / `queued`, used to only be watched by the lines saying
        "should go back to 0 once idle" -- and an implementation that never increments them at all still comes out
        green under those same lines (a negative control was run for this: removing each of the two increments in
        turn turns running and queued red respectively). This case measures the moment they are not 0."""
        os.environ["FAKE_MODE"] = "slow_first"
        os.environ["FAKE_DELAY"] = "2.5"
        m = self.mgr(max_concurrent=1)
        box = {}

        def go(sid, msg):
            try:
                call(m, sid, "S", [U(msg)])
            except scv.BridgeError as exc:
                box[sid] = exc

        t1 = threading.Thread(target=go, args=("q1", "甲"))
        t2 = threading.Thread(target=go, args=("q2", "乙"))
        t1.start()
        time.sleep(0.5)
        t2.start()                            # there is only one slot => this one can only queue
        time.sleep(0.4)
        snap = m.snapshot()
        t1.join()
        t2.join()
        self.assertEqual(box, {})
        self.assertEqual((snap["running"], snap["queued"]), (1, 1))

    def test_a_throw_right_after_taking_the_slot_does_not_leak_it(self):
        """🔴Once a concurrency slot is taken, it absolutely has to be given back, no matter which family of
        exception flies through in between.

        `with self._lock: self.running += 1` used to sit outside the `try` => the moment it throws (`KeyboardInterrupt`
        / `MemoryError` can both reach it), that `finally` never runs at all => the slot never comes back.
        ⭐It breaks silently: `BoundedSemaphore` does not heal itself, and once enough leaks add up to
          `max_concurrent`, this bridge can never take on work again, with every new request simply queuing
          forever -- not a single error message anywhere.
        ⚠️The check uses `acquire(timeout=1)`, never sending another request: if the slot really did leak, sending a
          request would just hang (the test turns into a hang, never a red), and a hung test is far harder to debug
          than a red one."""
        m = self.mgr(max_concurrent=1)

        class BoomOnNth:
            """Throws on the nth time it enters the lock. ⭐Never mock `_acquire`: that way the slot never actually
            gets taken, and `release()` would throw `Semaphore released too many times` -- which tests a completely
            different thing (my first version got exactly this wrong). `_acquire` itself enters the lock twice
            (queued +-1), and the call inside `run` is the 3rd time."""

            def __init__(self, real, n):
                self.real, self.n, self.i = real, n, 0

            def __enter__(self):
                self.i += 1
                if self.i == self.n:
                    raise KeyboardInterrupt("after the slot was taken, before entering try")
                return self.real.__enter__()

            def __exit__(self, *a):
                return self.real.__exit__(*a)

        m._lock = BoomOnNth(m._lock, 3)
        with self.assertRaises(KeyboardInterrupt):
            call(m, "slot1", "SYS", [U("甲")])
        got = m._slots.acquire(timeout=1)
        self.assertTrue(got, "a concurrency slot leaked: an exception was thrown after taking it, and it never came back")
        m._slots.release()

    def test_on_started_fires_when_the_slot_is_granted_not_when_the_call_arrives(self):
        """`on_started(queued_ms)`'s contract is "the moment the concurrency slot is granted" => the check can only be
        a moment in time: asserting only "it was called once" would let an implementation that calls it on the
        function's very first line come out green just the same, and the dispatcher's first-token gate starts
        counting from that moment -- however long it waited in the queue is exactly how unfairly it gets penalized."""
        os.environ["FAKE_MODE"] = "slow_first"
        os.environ["FAKE_DELAY"] = "1.5"
        m = self.mgr(max_concurrent=1)
        t = threading.Thread(target=lambda: call(m, "p2", "S", [U("甲")]))
        t.start()
        time.sleep(0.3)
        t0, stamps = time.time(), []
        res = m.run(session_id="p3", model_id="claude/haiku", effort=None, system="S", messages=[U("乙")],
                    on_started=lambda q: stamps.append(time.time() - t0), on_delta=lambda s: None, cancel=None)
        t.join()
        self.assertEqual(len(stamps), 1)
        self.assertGreater(stamps[0], 1.0, "fired at %.3fs = it called on_started before it even had the slot" % stamps[0])
        # upper bound: the queue duration it reports has to match the moment it really got to => never let the two sides compute it separately
        self.assertLess(stamps[0], res["queued_ms"] / 1000.0 + 0.5)


class Log(_Staged):
    """The log line for a rebuild. ⭐It is the one and only place ops can see "the player's session got rebuilt" =>
    it has to be loud, and it must never write the raw session id into it (that string comes from outside, and would
    ride all the way into the log on disk)."""

    RAW = "secret-session-id-42"

    def test_a_rebuild_shouts_exactly_one_line_and_hashes_the_session_id(self):
        m = self.mgr()
        call(m, self.RAW, "SYS", [U("甲")])
        lines = log_lines_during(lambda: call(m, self.RAW, "SYS", [U("改过的"), A("x"), U("乙")]))
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("the prefix does not match", lines[0])
        # ⭐The correct-value assertion is placed ahead of the negative-form one (review M-1): both lines go red
        #   under the same mutation, but if the one that goes red first were `assertNotIn`, a reader would mistake
        #   this for the "assert wrong-value not in" anti-pattern. Computing that hash independently => asserts what
        #   it should look like, never just "the raw text is not in there".
        self.assertIn(hashlib.sha256(self.RAW.encode("utf-8")).hexdigest()[:8], lines[0])
        self.assertNotIn(self.RAW, lines[0])

    def test_a_turn_that_does_not_rebuild_writes_nothing(self):
        """Zero-input control: remove the "rebuild" and measure the same path again -- the one line above has to come
        from a rebuild, never from "a line gets written every single turn" (a log like that would be turned into
        background noise by its own team within a day)."""
        m = self.mgr()
        self.assertEqual(log_lines_during(lambda: call(m, "lg2", "SYS", [U("甲")])), [])


if __name__ == "__main__":
    unittest.main()
