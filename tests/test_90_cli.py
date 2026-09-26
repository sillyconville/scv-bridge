# -*- coding: utf-8 -*-
"""Subcommands (Task 12): run/start/stop/status/token/doctor.

⭐ doctor's own canary must first pass a positive control: against a fake CLI that really does leak, it must cry out.
⭐ this file never touches a single real CLI: the in-process cases swap `scv.cli_head` for the fake CLI; the cases
  that start a subprocess (the `scv start` ones) point config.json's `claude_bin`/`codex_bin` at a small script that
  wraps the fake CLI ⇒ the `detect()` inside the subprocess also only ever touches the stub (the brief's original
  version had the subprocess run the real local CLI's `--version`/`--help`: no cost, but the harness's results then
  followed whatever happened to be installed on this machine).
"""
import argparse
import ast
import contextlib
import functools
import io
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from tests import helpers
from tests.fake_dispatcher import Dispatcher
from tests.test_00_budget import call_sites
import scv

_REAL_HEAD = scv.cli_head
HOME = ""
NL = chr(10)
SCRIPT = os.path.join(helpers.ROOT, "scv.py")
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


def setUpModule():
    global HOME
    HOME = helpers.fresh_home("cli", unittest.addModuleCleanup)
    scv.cli_head = helpers.fake_head


def tearDownModule():
    scv.cli_head = _REAL_HEAD


def free_port():
    """A port that can be bound right now, and most likely can still be bound a few seconds later.
    🔴 never use `bind(0)`: that picks from the dynamic port range, and `Lifecycle` needs a few seconds (detect)
      before the subprocess actually binds it -- in those few seconds it could be taken by an outbound connection, or
      carved into an exclusion range by WinNAT/Hyper-V (the negative-control round actually hit `WinError 10013`;
      this dev machine's dynamic range is 13,977 ports starting at 1024, with a large chunk of it excluded) ⇒ pick
      from 20000-32000: Windows's default dynamic range starts at 49152 and Linux's at 32768, both outside it."""
    for _ in range(500):
        port = random.randrange(20000, 32000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError("could not find a bindable port in 20000-32000")


def sleeper():
    """A stand-in process (playing "a CLI left behind by the previous bridge"). ⭐ carries `new_session_kw()`: on
    POSIX, sweeping kills the whole group."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], **scv.new_session_kw())


GRANDCHILD = ("import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], "
              "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **%r); print(p.pid)")


def detached(test):
    """A stand-in process that is never our child (playing "the bridge"): the real bridge is what `scv start`
    detaches, never a child of `scv stop`.
    ⚠️ using a child as a stand-in would leave it a zombie on POSIX once it is SIGTERM'd (its birth id is still there)
      ⇒ `scv stop` would say "did not stop" -- that would be the harness's own fault, never the product's. Once the
      middle layer exits, the grandchild is reaped by init."""
    mid = subprocess.run([sys.executable, "-c", GRANDCHILD % (scv.new_session_kw(),)], capture_output=True, timeout=30)
    pid = int(mid.stdout.decode().strip())
    born = scv.proc_start_id(pid)
    test.assertTrue(helpers.born_alive(born))
    test.addCleanup(lambda: scv.kill_pid_tree(pid) if scv.proc_start_id(pid) == born else None)
    return pid, born


def gone(pid, born, within=15.0):
    end = time.time() + within
    while time.time() < end and scv.proc_start_id(pid) == born:
        time.sleep(0.2)
    return scv.proc_start_id(pid) == ""


def bury(proc):
    with contextlib.suppress(OSError):
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=15)


def died(proc, within=15.0):
    """⭐ judge with `wait`, never with the birth id: it is our own child, and on POSIX not waiting on it leaves a
    zombie (its birth id is still there)."""
    try:
        proc.wait(timeout=within)
        return True
    except subprocess.TimeoutExpired:
        return False


def pid_file():
    return scv.spath("bridge.pid")


def put_pid(rec):
    pid_file().write_text(json.dumps(rec), encoding="utf-8")


def log_lines_during(fn):
    """The new lines that really land on disk in bridge.log during this stretch (never a mock log's call count: what
    is measured is bytes) plus this stretch's stdout/stderr."""
    p = scv.spath("bridge.log")
    before = p.read_text(encoding="utf-8") if p.exists() else ""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn()
    after = p.read_text(encoding="utf-8") if p.exists() else ""
    return rc, after[len(before):].splitlines(), out.getvalue(), err.getvalue()


def fake_bins(home):
    """The `detect()` inside the subprocess also has to touch the fake CLI ⇒ wrap it in a small `cmd`/`sh` script,
    handed down through config.json's `*_bin` (`cli_head` takes `cmd /c` for a `.cmd`, the same path as a real claude
    installed by npm)."""
    out = {}
    for fam in ("claude", "codex"):
        if os.name == "nt":
            path = os.path.join(home, "fake-%s.cmd" % fam)
            text = '@echo off\r\n"%s" "%s" %s %%*\r\n' % (sys.executable, helpers.FAKE, fam)
        else:
            path = os.path.join(home, "fake-%s" % fam)
            text = '#!/bin/sh\nexec "%s" "%s" %s "$@"\n' % (sys.executable, helpers.FAKE, fam)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.chmod(path, 0o755)
        out[fam + "_bin"] = path
    return out


class _Staged(unittest.TestCase):
    """⭐ every case restores `FAKE_*` and the proxy variables on both entry and exit, back to `ok` by default. Never
    rely on `tearDownModule` to clean up all at once: the cases in between would silently run in the environment left
    by the previous one (the brief's original way of writing it had `HTTPS_PROXY` staying set from the second case
    on)."""

    def setUp(self):
        saved = {k: v for k, v in os.environ.items() if k.startswith("FAKE_") or k.upper() in PROXY_KEYS}
        self.addCleanup(self._restore, saved)
        os.environ["FAKE_MODE"] = "ok"

    @staticmethod
    def _restore(saved):
        for k in [k for k in os.environ if k.startswith("FAKE_") or k.upper() in PROXY_KEYS]:
            if k not in saved:
                del os.environ[k]
        os.environ.update(saved)


# ━━ doctor
class Doctor(_Staged):
    def test_mask_proxy(self):
        self.assertEqual(scv.mask_proxy("http://user:pw@10.0.0.1:7890"), "http://***@10.0.0.1:7890")
        self.assertEqual(scv.mask_proxy("http://10.0.0.1:7890"), "http://10.0.0.1:7890")
        # ⭐ many proxy tools also accept the schemeless form (`user:pw@host:port`) ⇒ the password must still be masked
        self.assertEqual(scv.mask_proxy("user:pw@10.0.0.1:7890"), "***@10.0.0.1:7890")
        self.assertEqual(scv.mask_proxy("localhost,127.0.0.1"), "localhost,127.0.0.1")

    def test_mask_proxy_when_the_password_has_an_at_or_a_slash(self):
        """Task 12 review I-2: the cells above only cover the simple form that "happens to satisfy the contract".
        A password with `@` in it: Node/Python/Rust all cut at the last `@` (a config like this really works), while
        it used to mask only up to the first `@` ⇒ the second half of the password leaked into doctor and bridge.pid.
        A password with `/` in it: it used to print the whole thing verbatim -- the config itself does not even work,
        and people only run doctor exactly when the proxy is not working. ⭐ pin down the correct value (never write
        `"ss" not in`: masking it into some other shape would also pass)."""
        self.assertEqual(scv.mask_proxy("http://u:p@ss@h:1"), "http://***@h:1")
        self.assertEqual(scv.mask_proxy("http://u:ab/cd+ef==@proxy:8080"), "http://***@proxy:8080")
        self.assertEqual(scv.mask_proxy("u:ab/cd@proxy:8080"), "***@proxy:8080")

    def test_the_bridges_proxies_are_masked_again_when_read_back(self):
        """The copy in bridge.pid is written the moment the bridge comes up: a bridge started before the I-2 fix
        wrote in one that was not fully masked ⇒ doctor reading it back and printing it verbatim would still leak.
        ⭐ mask it again on the way back in (one cell each for the two old broken shapes: half-masked, and left
        entirely verbatim)."""
        me = os.getpid()
        pid_file().write_text(json.dumps({"pid": me, "born": scv.proc_start_id(me), "port": 1, "ticket": "t", "proxy_env": {
            "HTTP_PROXY": "http://***@ss@h:1", "HTTPS_PROXY": "http://u:ab/cd+ef==@proxy:8080"}}), encoding="utf-8")
        self.addCleanup(lambda: pid_file().unlink() if pid_file().exists() else None)
        facts = scv.doctor_facts(scv.load_config(), live=False)
        self.assertEqual(facts["bridge"]["state"], "alive")                    # precondition: it really reached the "read it back" cell
        self.assertEqual(facts["bridge_proxy_env"], {"HTTP_PROXY": "http://***@h:1", "HTTPS_PROXY": "http://***@proxy:8080"})

    def test_facts_are_facts(self):
        os.environ["HTTPS_PROXY"] = "http://user:pw@10.0.0.1:7890"
        facts = scv.doctor_facts(scv.load_config(), live=False)
        self.assertEqual(facts["proxy_env"]["HTTPS_PROXY"], "http://***@10.0.0.1:7890")
        self.assertEqual(sorted(facts["families"]), ["claude", "codex"])
        # ⚠️ the brief describes three keys: since Task 4, `auth_status` has an always-present `probe_error` too
        #   ("could not tell" is kept separate from "not logged in")
        self.assertEqual(facts["families"]["claude"]["auth"],
                         {"logged_in": True, "method": "claude.ai", "plan": "max", "probe_error": ""})
        self.assertNotIn("someone@example.com", json.dumps(facts))
        self.assertNotIn("user:pw", json.dumps(facts))
        self.assertIn("not paired", facts["remote_url"])
        self.assertEqual(facts["config"], str(scv.spath("config.json")))
        # the stub has FAKE_FEATURES off ⇒ `features list` emits not a single line ⇒ "could not check", never "all 20
        #   are stale" (details in the next two cases)
        self.assertEqual(facts["families"]["codex"]["codex_off_unknown"], [])
        self.assertIn("could not check", facts["families"]["codex"]["codex_off_error"])

    def test_facts_count_the_children_and_their_memory(self):
        """§8: doctor reports "how many child processes it manages and how much memory". ⭐ what it reads is the
        registry (the whole SCV_HOME), and it counts as alive only if the birth id matches."""
        kid = sleeper()
        self.addCleanup(bury, kid)
        self.assertTrue(scv.child_add(kid.pid, "claude"))
        self.addCleanup(scv.child_remove, kid.pid)
        row = [c for c in scv.doctor_facts(scv.load_config(), live=False)["children"] if c["pid"] == kid.pid]
        self.assertEqual(len(row), 1, row)
        self.assertEqual((row[0]["family"], row[0]["alive"]), ("claude", True))
        self.assertGreater(row[0]["rss_kb"], 0)
        bury(kid)
        row = [c for c in scv.doctor_facts(scv.load_config(), live=False)["children"] if c["pid"] == kid.pid]
        self.assertEqual((row[0]["alive"], row[0]["rss_kb"]), (False, None))   # a dead one must never be reported with a memory number

    def test_live_check_passes_on_a_tight_cli(self):
        for fam, model in (("claude", "haiku"), ("codex", scv.CODEX_MODELS[0])):
            with self.subTest(family=fam):
                r = scv.live_check(scv.load_config(), fam, model)
                self.assertEqual((r["ok"], r["canary_leaked"]), (True, False), r)

    def test_live_check_catches_a_leaky_cli(self):
        """Positive control: the ruler must respond against a CLI that really reads the file (one call for each
        family)."""
        os.environ["FAKE_MODE"] = "leak"
        for fam, model in (("claude", "haiku"), ("codex", scv.CODEX_MODELS[0])):
            with self.subTest(family=fam):
                self.assertEqual(scv.live_check(scv.load_config(), fam, model)["canary_leaked"], True)

    def test_live_check_catches_a_reworded_leak(self):
        """Task 12 review M-3: the judgment used to only recognize the whole original string appearing verbatim ⇒ if
        it read it but pasted it back with the `CANARY-` prefix stripped and switched to uppercase, it was reported
        as "did not read it". ⭐ the judgment is loosened to "any 8 consecutive hex characters from the secret appear
        (case-insensitive)"; the false-alarm surface is in `scv._canary_seen`."""
        os.environ.update(FAKE_MODE="leak", FAKE_LEAK_SHAPE="reworded")
        for fam, model in (("claude", "haiku"), ("codex", scv.CODEX_MODELS[0])):
            with self.subTest(family=fam):
                r = scv.live_check(scv.load_config(), fam, model)
                self.assertIn("the code is ", r["answer"])                     # precondition: the stub really pasted back the rewritten copy
                self.assertNotIn("CANARY", r["answer"])
                self.assertEqual(r["canary_leaked"], True, r)

    def test_live_check_is_not_ok_when_the_cli_answers_something_else(self):
        """The negative control for the judgment "the channel is through, the model is listening": it answered, but
        not the addition problem we actually asked ⇒ must never count as passing.
        ⚠️ the brief's original judgment was "the echoed password must appear" -- measured against a real haiku, it
          refuses to echo (reading the whole sentence as a prompt-injection test), which would judge a healthy CLI as
          broken ⇒ switched to a random addition problem instead (both a real haiku and a real codex answered
          correctly once each, 📎 NOTES.md::live-check-prompt)."""
        os.environ["FAKE_NO_MATH"] = "1"
        r = scv.live_check(scv.load_config(), "claude", "haiku")
        self.assertEqual((r["ok"], r["canary_leaked"]), (False, False))
        self.assertTrue(r["answer"].startswith("echo[1]: "), r)    # the original words of the answer must be carried back: it is a human who judges "did not answer correctly"

    def test_a_right_answer_with_thousands_separators_is_right(self):
        """Measured against a real codex answering "4646 + 6290 = 10,936" -- it answered correctly, but it used to be
        judged "did not answer correctly" by `"10936" in` ⇒ a healthy bridge got reported as broken by
        `doctor --live` (the brief's echoed-password line is the same kind of broken). Run each family once for a
        few common ways of writing thousands separators."""
        for sep in (",", "，", ".", chr(160)):
            os.environ["FAKE_MATH_SEP"] = sep
            for fam, model in (("claude", "haiku"), ("codex", scv.CODEX_MODELS[0])):
                with self.subTest(sep=repr(sep), family=fam):
                    r = scv.live_check(scv.load_config(), fam, model)
                    self.assertIn(sep, r["answer"].rpartition("=")[2])     # precondition: the sum really carries that separator (otherwise this is vacuous)
                    self.assertEqual((r["ok"], r["canary_leaked"]), (True, False), r)

    def test_a_wrong_answer_that_contains_the_right_digits_is_wrong(self):
        """The negative control for the previous case (Task 12 review M-2): recognizing thousands separators must
        never accidentally judge a wrong answer as correct. The stub writes an extra digit before the sum:
        `110936` contains `10936` (the old bug of substring matching); with space-grouping it is `16 912`/`110 936`
        ("strip out anything that looks like a separator" would stitch it into a number that contains the sum)."""
        os.environ["FAKE_MATH_PREFIX"] = "1"
        for sep in ("", " "):
            os.environ["FAKE_MATH_SEP"] = sep
            for fam, model in (("claude", "haiku"), ("codex", scv.CODEX_MODELS[0])):
                with self.subTest(sep=repr(sep), family=fam):
                    r = scv.live_check(scv.load_config(), fam, model)
                    a, b = map(int, re.search("what is ([0-9]+) [+] ([0-9]+)", r["answer"]).groups())
                    # precondition: what it answered really is that wrong number, "an extra digit before the sum"
                    #   (otherwise this cell is vacuous)
                    self.assertEqual(r["answer"].rpartition("= ")[2].replace(" ", ""), "1" + str(a + b), r)
                    self.assertEqual((r["ok"], r["canary_leaked"]), (False, False), r)

    def test_the_right_answer_ruler_reads_numbers_not_substrings(self):
        """The ruler itself (`_says_number`): the 6 cells out of 15 that Task 12 review's `p_plain.py` judged wrong,
        plus the forms it should recognize."""
        cases = [(10936, "4646 + 6290 = 10,936.", True), (10936, "= 10 936.", True), (10936, "= 10.936", True),
                 (10936, "= 10" + chr(8239) + "936", True), (10936, "= 10_936", True), (10936, "= 10’936", True),
                 (10936, "**10936** and 5 more", True), (10936, "10936 5", True), (6912, "items 6 912 and more", True),
                 (10936, "= 10926", False), (10936, "= 110936", False), (10936, "= 109360", False),
                 (10936, "= 10 9 36", False), (6912, "1 234 + 5 678 = 6 913", False), (6912, "the sums of 6, 912", False),
                 (6912, "pids 16 912 and 40", False), (6912, "version 3.6.912 build", False), (6912, "at 1_6912", False),
                 (10936, "= 10,936.5", False), (10936, "= 1,0936", False), (10936, "= 10,93,6", False)]
        got = [(n, text) for n, text, want in cases if scv._says_number(text, n) != want]
        self.assertEqual(got, [])

    def test_live_check_reports_auth_failure_verbatim(self):
        """⚠️ codex's `fix_hint` was changed back and forth twice (Task 13 pointed it at scv's own login subcommand,
        Task 13c changed it back to `codex login`); why, is written in
        tests/test_40_errors.py::Classify::test_body_keeps_the_raw_text_verbatim."""
        os.environ["FAKE_MODE"] = "auth"
        want = {"claude": ("Not logged in · Please run /login", "claude auth login"),
                "codex": (None, "codex login")}
        for fam, model in (("claude", "haiku"), ("codex", scv.CODEX_MODELS[0])):
            with self.subTest(family=fam):
                r = scv.live_check(scv.load_config(), fam, model)
                self.assertEqual((r["ok"], r["error"]["type"], r["error"]["fix_hint"]),
                                 (False, "auth_required", want[fam][1]))
                if want[fam][0]:
                    self.assertEqual(r["error"]["message"], want[fam][0])     # ⭐ the original words unchanged to the letter (B21)
                else:
                    self.assertIn("401 Unauthorized", r["error"]["message"])

    def test_live_check_leaves_nothing_behind(self):
        """It builds a working directory and a canary under `work/`, and starts a CLI ⇒ all three must be cleaned up
        (both the passing and failing paths)."""
        for m in ("ok", "auth"):
            with self.subTest(mode=m):
                os.environ["FAKE_MODE"] = m
                before = len(scv.children())
                scv.live_check(scv.load_config(), "claude", "haiku")
                left = [p.name for p in scv.spath("work").iterdir()
                        if p.name.startswith(("doctor-", "canary-"))]
                self.assertEqual((left, len(scv.children())), ([], before))

    def test_old_claude_without_safe_mode_is_blocked_and_says_why(self):
        os.environ["FAKE_NO_SAFE_MODE"] = "1"
        found = scv.detect(scv.load_config())
        self.assertIn("--safe-mode", found["claude"]["blocked"])
        self.assertEqual([m for m in scv.catalog(scv.load_config(), found) if m.startswith("claude/")], [])

    def test_a_blocked_claude_is_not_asked_auth_status(self):
        """🔴 F-1: claude treats an argument it does not recognize as a real prompt and actually makes a call with it
        (measured at L198) ⇒ a claude old enough to not recognize `--safe-mode` is also quite likely to not recognize
        `auth status` ⇒ never ask it. The judgment is that the stub really was not asked, never what doctor said."""
        os.environ["FAKE_NO_SAFE_MODE"] = "1"
        mark = len(helpers.read_fake_log())
        facts = scv.doctor_facts(scv.load_config(), live=False)
        asked = [r for r in helpers.fake_log_since(mark) if r.get("family") == "claude" and r.get("quick")]
        self.assertEqual([r["quick"] for r in asked], [["--version"], ["--help"]])
        self.assertIn("not asked", facts["families"]["claude"]["auth"]["probe_error"])

    def test_zero_cost_probes_do_not_run_in_the_callers_directory(self):
        """Task 12 review M-7: doctor's zero-cost probes used to run in the caller's cwd (most likely the user's own
        repository). claude treats an argument it does not recognize as a real prompt and makes a call with it, and
        also reads the repo context of the current directory (measured at L198) ⇒ probes now always run inside the
        empty directory `work/probe`.
        The judgment is the cwd the stub itself saw, never what scv says it passed."""
        caller = tempfile.mkdtemp(prefix=".scv-test-caller-")
        self.addCleanup(helpers.remove_tree, caller)      # ⭐ register first: wind-down is last-in-first-out, chdir back before deleting (never delete the current directory)
        old = os.getcwd()
        os.chdir(caller)
        self.addCleanup(os.chdir, old)
        mark = len(helpers.read_fake_log())
        scv.doctor_facts(scv.load_config(), live=False)
        rows = [r for r in helpers.fake_log_since(mark) if "quick" in r]
        # precondition: all five probes really ran (otherwise "not a single one ran in the caller's directory" is
        #   vacuous)
        self.assertEqual(sorted({(r["family"], " ".join(r["quick"])) for r in rows}), [
            ("claude", "--help"), ("claude", "--version"), ("claude", "auth status --json"),
            ("codex", "--version"), ("codex", "features list"), ("codex", "login status")])
        norm = lambda p: os.path.normcase(os.path.realpath(p))
        self.assertEqual({norm(r["cwd"]) for r in rows}, {norm(str(scv.spath("work/probe")))}, rows)
        self.assertEqual(os.listdir(caller), [])

    def test_codex_feature_names_that_codex_no_longer_knows(self):
        os.environ["FAKE_FEATURES"] = "shell_tool,apps"
        unknown, err = scv.codex_features_unknown(helpers.fake_head("codex"))
        self.assertEqual(err, "")
        self.assertNotIn("shell_tool", unknown)
        self.assertIn("plugins", unknown)

    def test_a_codex_that_cannot_list_features_is_not_called_stale(self):
        """⭐ "could not check" and "checked, and all 20 names are stale" are two different things (the Nth time
        this file has hit this shape)."""
        for env in ({"FAKE_FEATURES_RC": "2"}, {"FAKE_FEATURES": ""}):
            with self.subTest(env=env):
                os.environ.update(env)
                unknown, err = scv.codex_features_unknown(helpers.fake_head("codex"))
                self.assertEqual(unknown, [])
                self.assertIn("could not check", err)
                os.environ.pop("FAKE_FEATURES_RC", None)
        os.environ["FAKE_FEATURES_RC"] = "2"
        self.assertIn("unrecognized subcommand", scv.codex_features_unknown(helpers.fake_head("codex"))[1])


class ClaudeAuthProbe(_Staged):
    """F-1/F-6/L299: `claude auth status --json`'s three ways of breaking each have their own destination, and none
    of them may ever be read as "not logged in"."""

    def probe(self):
        return scv.auth_status("claude", helpers.fake_head("claude"))

    def test_prose_instead_of_json_is_cannot_tell_not_logged_out(self):
        os.environ["FAKE_AUTH_STATUS"] = "prose"
        st, lines, _o, _e = log_lines_during(self.probe)
        self.assertEqual(st["logged_in"], False)
        self.assertIn("did not return", st["probe_error"])
        self.assertIn("I can help you", st["probe_error"])          # carries the original words (folded to one line, truncated)
        self.assertEqual(len([x for x in lines if "auth status" in x]), 1, lines)

    def test_json_without_loggedin_is_cannot_tell_and_does_not_copy_the_identity(self):
        """⚠️ this cell's original text is a JSON with identity information intact (email/orgId) ⇒ "could not tell"
        must be loud, but the original text must never be copied into probe_error (it goes onto doctor's screen --
        people paste that screen into an issue) or into bridge.log."""
        os.environ["FAKE_AUTH_STATUS"] = "nokey"
        st, lines, _o, _e = log_lines_during(self.probe)
        self.assertIn("loggedIn", st["probe_error"])
        self.assertIn("not quoting the original", st["probe_error"])
        self.assertNotIn("someone@example.com", st["probe_error"] + NL.join(lines))

    def test_noise_with_braces_before_the_json_still_parses(self):
        os.environ["FAKE_AUTH_STATUS"] = "noisy"
        self.assertEqual(self.probe(), {"logged_in": True, "method": "claude.ai", "plan": "max", "probe_error": ""})

    def test_method_is_folded_on_the_way_in(self):
        os.environ["FAKE_AUTH_STATUS"] = "twoline"
        self.assertEqual(self.probe()["method"], "claude.ai ⏎ (second line)")

    def test_codex_saying_nothing_reads_as_nothing_said(self):
        """L299: when `method` is empty, the line "codex said: ..." used to be read as "(codex said:)"."""
        os.environ["FAKE_STATUS_SILENT"] = "1"
        blocked = scv.detect(scv.load_config())["codex"]["blocked"]
        self.assertIn("codex said: (it said nothing at all, rc=1)", blocked)


class ChildProcessesNeverSeeTheSession(_Staged):
    """15c review I1: measured all the way at the consumer's end -- whether the environment the stub actually gets
    has session variables in it (`FAKE_ENV_NAMES=1`: the stub records the names in both families' namespaces, never
    the values).
    ⭐ covers every one of doctor's probes (`--version`/`--help`/`auth status`/`login status`/`features list`) plus
      starting each family's driver once; "what counts as a session variable" is judged solely by
      `scv.session_bound`. Negative control = review's mutation K1 (every call site routes around `child_env()`)."""

    FAKE = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_ENTRYPOINT", "AI_AGENT",
            "CODEX_THREAD_ID", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "TRACEPARENT", "TRACESTATE")
    WANT = [("claude", "--help"), ("claude", "--version"), ("claude", "-p --model"), ("claude", "auth status"),
            ("codex", "--version"), ("codex", "app-server --listen"), ("codex", "features list"), ("codex", "login status")]

    def test_every_probe_and_both_drivers_start_without_the_session(self):
        mark = len(helpers.read_fake_log())
        env = dict({n: "fake-" + n.lower() for n in self.FAKE}, FAKE_ENV_NAMES="1", CLAUDE_CODE_GIT_BASH_PATH="his-own")
        with mock.patch.dict(os.environ, env):
            scv.doctor_facts(scv.load_config(), live=False)
            for fam, model in (("claude", "haiku"), ("codex", "gpt-5.6-luna")):
                wd = scv.spath("work/no-session-" + fam)
                wd.mkdir(exist_ok=True)
                scv.make_driver(scv.load_config(), fam, model, None, "s", wd).close()
        rows = [r for r in helpers.fake_log_since(mark) if "env_names" in r]
        self.assertEqual(sorted({(r["family"], " ".join(r["argv"][:2])) for r in rows}), self.WANT)   # the ruler is not blind: every one of them really ran
        self.assertEqual([(r["family"], r["argv"][:2], n) for r in rows for n in r["env_names"] if scv.session_bound(n)], [])
        self.assertEqual([r["argv"][:2] for r in rows if "CLAUDE_CODE_GIT_BASH_PATH" not in r["env_names"]], [])   # his own still gets through


class DoctorListsTheEnvironment(_Staged):
    """15c ⑦2: doctor lists, one line each, the names of the variables in both families' namespaces that this
    process (the bridge is not running; when it is, see DoctorReportsTheRunningBridge) passes to the child CLI, and
    the session-variable names it stripped (never the values); for something the user set themselves that changes
    how the CLI answers (category B), it gets its own line spelling out exactly what it changes -- ⭐ that is the
    user's own setting ⇒ keep it, never strip it, just state it plainly (B14: only report the facts).
    ⑦6: the environment gets filtered and a variable that matters is missing (pinned down by a zero-cost read:
    on win32 only LOCALAPPDATA -- the codex bundled with the desktop build relies on it to find itself) ⇒ codex's
    line is ❌ plus ask the person to start the bridge from their own terminal (never say "not on PATH", never ask
    them to install something already installed); the cells for when the bridge is running are in
    DoctorReportsTheRunningBridge."""

    SESSION = {"CLAUDE_CODE_SESSION_ID": "sess-secret-1", "CODEX_THREAD_ID": "thread-secret-2", "AI_AGENT": "agent-secret-3"}
    USER = {"CLAUDE_CODE_GIT_BASH_PATH": "his-own-path-4", "CLAUDE_CODE_EFFORT_LEVEL": "effort-secret-5",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "model-secret-6", "OPENAI_BASE_URL": "https://secret-7.example"}

    def doctor(self):
        return log_lines_during(lambda: scv.main(["doctor"]))

    def test_names_of_both_kinds_are_listed_and_no_value_is(self):
        with mock.patch.dict(os.environ, dict(self.SESSION, **self.USER)):
            _rc, _lines, out, _err = self.doctor()
        got = out.splitlines()
        passed = [x for x in got if x.startswith(
            "· the two families of variables passed to the child CLI by this terminal (the bridge is not running; when the "
            "bridge starts, its own environment at that moment is what counts) (names only):")]
        stripped = [x for x in got if x.startswith(
            "· the session variables stripped by this terminal (the bridge is not running; when the bridge starts, its own "
            "environment at that moment is what counts)")]
        self.assertEqual((len(passed), len(stripped)), (1, 1), out)
        self.assertEqual(([k for k in self.USER if k not in passed[0]], [k for k in self.USER if k in stripped[0]]), ([], []))
        self.assertEqual(([k for k in self.SESSION if k not in stripped[0]], [k for k in self.SESSION if k in passed[0]]), ([], []))
        self.assertEqual([v for v in list(self.SESSION.values()) + list(self.USER.values()) if v in out], [])   # never a single value

    def test_a_setting_that_changes_how_the_cli_answers_says_what_it_changes(self):
        with mock.patch.dict(os.environ, dict(self.USER, MAX_THINKING_TOKENS="0", CLAUDE_CODE_MAX_OUTPUT_TOKENS="64")):
            _rc, _lines, out, _err = self.doctor()
        got = out.splitlines()
        for name, says in (("CLAUDE_CODE_EFFORT_LEVEL", "overrides the --effort"), ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "the name `haiku`"),
                           ("MAX_THINKING_TOKENS", "set to 0 to turn thinking off"), ("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "output cap")):
            with self.subTest(name=name):
                line = [x for x in got if x.startswith(
                    ("⚠️ %s is in the environment (this terminal (the bridge is not running; when the bridge starts, its own "
                     "environment at that moment is what counts); you set it yourself, passed to the child CLI unchanged): ") % name)]
                self.assertEqual(len(line), 1, out)
                self.assertIn(says, line[0])
        # zero-input control: a user-level setting (does not change how it answers) must never get a line of its own
        self.assertEqual([x for x in got if x.startswith("⚠️ CLAUDE_CODE_GIT_BASH_PATH") or x.startswith("⚠️ OPENAI_BASE_URL")], [])

    @unittest.skipUnless(os.name == "nt", "missing LOCALAPPDATA breaking the desktop build's codex is a win32 thing (⏳ not measured on POSIX; that table of vital variables is empty there)")
    def test_codex_missing_because_localappdata_was_filtered_out_says_so_and_where_to_start(self):
        with mock.patch.object(scv, "cli_head", lambda f, cfg=None: None if f == "codex" else helpers.fake_head(f)), \
                mock.patch.dict(os.environ):
            os.environ.pop("LOCALAPPDATA", None)
            rc, _lines, out, _err = self.doctor()
        got, want = out.splitlines(), ("❌ codex: not found — this terminal (the bridge is not running; when the bridge starts, "
                                       "its own environment at that moment is what counts)'s environment has no LOCALAPPDATA")
        at = [i for i, x in enumerate(got) if x.startswith(want)]
        self.assertEqual(len(at), 1, out)
        start = scv.self_cmd("start").splitlines()
        self.assertEqual(got[at[0] + 1: at[0] + 1 + len(start)], start)      # ⭐ the next step follows right below that line, and the whole line is pasteable (never rely only on the trailing "start the bridge")
        self.assertNotIn("· codex: not found on this machine", out)
        self.assertEqual(rc, 1, out)

    @unittest.skipUnless(os.name == "nt", "same as above")
    def test_an_empty_localappdata_counts_as_missing_too(self):
        """15c review M6: `cli_head` judges by whether it is truthy, while doctor used to judge by whether it exists
        at all ⇒ with `LOCALAPPDATA=""`, codex cannot be found, yet doctor still says "not on PATH". Now there is
        only one judgment left (`env_missing`)."""
        with mock.patch.object(scv, "cli_head", lambda f, cfg=None: None if f == "codex" else helpers.fake_head(f)), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": ""}):
            rc, _lines, out, _err = self.doctor()
        self.assertEqual(len([x for x in out.splitlines() if x.startswith(
            "❌ codex: not found — this terminal (the bridge is not running; when the bridge starts, its own environment at "
            "that moment is what counts)'s environment has no LOCALAPPDATA")]), 1, out)
        self.assertEqual(rc, 1, out)

    def test_codex_missing_with_localappdata_there_is_the_old_plain_line(self):
        """Zero-input control: LOCALAPPDATA is present in the environment (not filtered out) ⇒ the old plain line as
        usual, never ❌, never exit 1 over this."""
        with mock.patch.object(scv, "cli_head", lambda f, cfg=None: None if f == "codex" else helpers.fake_head(f)), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": HOME}):
            rc, _lines, out, _err = self.doctor()
        self.assertIn("· codex: not found on this machine (not on PATH, and config.json's codex_bin does not point to one either)", out.splitlines())
        self.assertEqual([x for x in out.splitlines() if x.startswith("❌ codex")], [])
        self.assertEqual(rc, 0, out)


class DoctorReportsTheRunningBridge(_Staged):
    """15c review I2: while the bridge is running, doctor's two name-listing lines report the copy recorded into
    bridge.pid the moment the bridge came up (never doctor's own), and state plainly whose it is -- this is exactly
    the cell 15c means to guard against: "an agent starts the bridge on someone's behalf (carrying session
    variables), and the person runs doctor from their own terminal". Never a value: bridge.pid also holds only
    names."""

    def put(self, env):
        me = os.getpid()
        pid_file().write_text(json.dumps({"pid": me, "born": scv.proc_start_id(me), "port": 1, "ticket": "t", "proxy_env": {},
                                          "env": env}), encoding="utf-8")
        self.addCleanup(lambda: pid_file().unlink() if pid_file().exists() else None)

    def lines(self):
        facts = scv.doctor_facts(scv.load_config(), live=False)
        self.assertEqual(facts["bridge"]["state"], "alive")                   # precondition: it really reached the "bridge is running" cell
        return facts, scv.doctor_verdict(facts)[0]

    def test_the_bridges_record_wins_over_a_clean_terminal(self):
        self.put({"passed": ["CODEX_HOME"], "stripped": ["AI_AGENT", "CLAUDE_CODE_SESSION_ID"],
                  "changes": {"CLAUDE_CODE_EFFORT_LEVEL": "it overrides the --effort the bridge passes"}, "missing": []})
        facts, lines = self.lines()
        who = "the bridge that is running (pid %d, from the moment it started)" % os.getpid()
        self.assertIn("· the session variables stripped by %s (the identity of the agent session that started it, never "
                      "passed to the child CLI): AI_AGENT, CLAUDE_CODE_SESSION_ID" % who, lines)
        self.assertIn("⚠️ CLAUDE_CODE_EFFORT_LEVEL is in the environment (%s; you set it yourself, passed to the child CLI "
                      "unchanged): it overrides the --effort the bridge passes" % who, lines)

    def test_a_terminal_full_of_session_variables_does_not_speak_for_a_clean_bridge(self):
        """The reverse direction: doctor's own side is carrying session variables, but the bridge was started from a
        clean terminal ⇒ report the bridge's "(none)", never report doctor's own."""
        self.put({"passed": [], "stripped": [], "changes": {}, "missing": []})
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "fake-sess", "CLAUDE_CODE_EFFORT_LEVEL": "fake-effort"}):
            facts, lines = self.lines()
        self.assertEqual(facts["env"]["stripped"], [])
        self.assertIn(("· the session variables stripped by the bridge that is running (pid %d, from the moment it started) "
                      "(the identity of the agent session that started it, never passed to the child CLI): (none)")
                      % os.getpid(), lines)
        self.assertEqual([x for x in lines if "CLAUDE_CODE_EFFORT_LEVEL" in x or "fake-" in x], [])

    def running_with(self, *fams):
        """Which families the running bridge reports in `/healthz` (never dial a real port: replace `our_health`
        directly)."""
        got = {f: {"version": "0.0.0", "blocked": False} for f in fams}
        return mock.patch.object(scv, "our_health", lambda port: {"protocol": scv.PROTOCOL, "version": scv.VERSION, "families": got})

    def test_a_bridge_started_without_localappdata_is_the_one_whose_codex_is_missing(self):
        """F1 addendum (maintainer's ruling fix1, ⑦3, the same shape as I2): the bridge was started from an
        environment missing LOCALAPPDATA (recorded in bridge.pid), and its `/healthz` has no codex, while doctor
        itself is in a complete environment (codex can be found) ⇒ judge by the bridge's own copy: codex is ❌, the
        line is marked as the bridge's, and the next step is to stop it and start it again from your own
        terminal."""
        self.put({"passed": [], "stripped": [], "changes": {}, "missing": ["LOCALAPPDATA"]})
        with self.running_with("claude"), mock.patch.dict(os.environ, {"LOCALAPPDATA": HOME}):
            facts, lines = self.lines()
            bad = scv.doctor_verdict(facts)[1]
        want = ("❌ codex: the bridge that is running (pid %d, from the moment it started) could not find it — the "
                "environment it started with has no LOCALAPPDATA") % os.getpid()
        at = [i for i, x in enumerate(lines) if x.startswith(want)]
        self.assertEqual(len(at), 1, lines)
        self.assertEqual(lines[at[0]].split(NL)[1:], (scv.self_cmd("stop") + NL + scv.self_cmd("start")).split(NL))   # the next step is a whole pasteable line
        self.assertIn("codex", bad)

    def test_a_bridge_started_with_everything_is_not_blamed_for_a_terminal_without_localappdata(self):
        """The other way around: the bridge came up in a complete environment (the recorded missing list is empty),
        and doctor's own terminal is missing LOCALAPPDATA ⇒ judge by the bridge, never report it as missing."""
        self.put({"passed": [], "stripped": [], "changes": {}, "missing": []})
        with self.running_with("claude", "codex"), mock.patch.dict(os.environ):
            os.environ.pop("LOCALAPPDATA", None)
            facts, lines = self.lines()
        self.assertEqual(facts["env"]["missing"], [])
        self.assertEqual([x for x in lines if x.startswith("❌") and "LOCALAPPDATA" in x], [])

    @unittest.skipUnless(os.name == "nt", "missing LOCALAPPDATA breaking the desktop build's codex is a win32 thing")
    def test_a_terminal_that_cannot_find_codex_says_it_is_only_this_terminal(self):
        """fix2 (review N-M3): the bridge is running and has codex; doctor's own terminal is missing LOCALAPPDATA,
        and its own probe cannot find codex ⇒ it used to fall back to the old plain line "not found on this machine
        (not on PATH ...)" -- which is the same as asking the person to install a codex that is already installed and
        already in use by the bridge, and the real reason is still lost. ⇒ a separate line states plainly: this is
        this terminal's own probe, what is missing, and that it has nothing to do with the running bridge; never ❌,
        never counted among the families with problems (the bridge is perfectly fine). facts keeps doctor's own
        missing list separately."""
        self.put({"passed": [], "stripped": [], "changes": {}, "missing": []})
        with self.running_with("claude", "codex"), mock.patch.dict(os.environ), \
                mock.patch.object(scv, "cli_head", lambda f, cfg=None: None if f == "codex" else helpers.fake_head(f)):
            os.environ.pop("LOCALAPPDATA", None)
            facts, lines = self.lines()
            bad = scv.doctor_verdict(facts)[1]
        self.assertEqual([x for x in lines if "not on PATH" in x], [])
        self.assertIn("· codex: this terminal's own probe did not find it — this terminal's environment has no LOCALAPPDATA "
                      "(the desktop build's own bundled codex is found through it); this says only about this terminal, and "
                      "has nothing to do with the bridge that is running", lines)
        self.assertEqual((bad, [x for x in lines if x.startswith("❌")]), ([], []))
        self.assertEqual(facts["env"]["own_missing"], ["LOCALAPPDATA"])

    def test_a_bridge_from_an_older_version_is_said_to_have_no_record(self):
        """A bridge started by an old version has no such field in bridge.pid ⇒ report doctor's own, and state
        plainly "that bridge did not record this" (never say "the bridge is not running")."""
        self.put(None)
        _facts, lines = self.lines()
        self.assertEqual(len([x for x in lines if x.startswith(
            "· the session variables stripped by this terminal (the running bridge did not record this: it was started by an older version)")]), 1, lines)

    def test_a_family_that_came_back_after_the_bridge_started_is_not_said_to_be_all_well(self):
        """I-4: the running bridge detects families once, at start (README says so plainly); after the player logs
        Codex in (or installs it) while the bridge keeps running, `status` sends him to `doctor` because `/healthz`
        still reports codex blocked, and doctor's own fresh detect finds it usable ⇒ without this fix, doctor would
        say all is well (its own probe is fresh) while the running bridge is still the one actually answering
        requests. Fixture: `our_health` says codex is blocked, doctor's own detect (fake CLI, FAKE_MODE=ok) finds
        it logged in and not blocked."""
        self.put({"passed": [], "stripped": [], "changes": {}, "missing": []})
        health = {"protocol": scv.PROTOCOL, "version": scv.VERSION,
                 "families": {"claude": {"version": "0.0.0", "blocked": False},
                              "codex": {"version": "0.0.0", "blocked": True}}}
        with mock.patch.object(scv, "our_health", lambda port: health):
            facts, lines = self.lines()
            bad = scv.doctor_verdict(facts)[1]
            with contextlib.redirect_stdout(io.StringIO()):
                rc = scv.cmd_doctor(argparse.Namespace(live=False))
        want = "⚠️ the running bridge still has codex off (it checks only when it starts) ⇒ stop, then start:"
        at = [i for i, x in enumerate(lines) if x.startswith(want)]
        self.assertEqual(len(at), 1, lines)
        self.assertEqual(lines[at[0]].split(NL)[1:], (scv.self_cmd("stop") + NL + scv.self_cmd("start")).split(NL))
        self.assertIn("codex", bad)
        self.assertEqual(rc, 1)
        # zero-input control: claude is not blocked on either side ⇒ never say this about a family that is fine
        self.assertEqual([x for x in lines if "claude" in x and x.startswith("⚠️ the running bridge still has")], [])



class DoctorCommand(_Staged):
    def doctor(self, *argv):
        return log_lines_during(lambda: scv.main(["doctor"] + list(argv)))

    def test_the_login_command_is_printed_on_real_lines(self):
        """L299: a pasteable command must be printed with real newlines -- the copy inside JSON is `\\n`-escaped, and
        selecting and pasting the whole line would be broken.
        ⭐ both families use the resolved executable plus the CLI's own login subcommand (13b review M4: claude's
          line used to be `fix_hint`'s bare `claude auth login` -- someone who configured `claude_bin` most likely
          did so exactly because it is not on PATH)."""
        os.environ["FAKE_MODE"] = "auth"
        rc, _lines, out, _err = self.doctor()
        got = out.splitlines()
        for fam in ("claude", "codex"):
            with self.subTest(family=fam):
                want = scv.login_cmd(fam, helpers.fake_head(fam)).splitlines()
                self.assertGreater(len(want), 0)
                self.assertEqual([w for w in want if w not in got], [], out)
                self.assertNotIn(scv.fix_hint("auth_required", fam), got)       # never let the bare line stand in for it
        self.assertEqual(rc, 1)

    def test_having_credentials_is_not_called_usable(self):
        """L192: `auth status` only tells you "local credentials exist", never "the credentials are still valid" ⇒
        the wording says exactly that, pointing the real judgment at `--live`."""
        rc, _lines, out, _err = self.doctor()
        line = [x for x in out.splitlines() if x.startswith("✅ claude")]
        self.assertEqual(len(line), 1, out)
        self.assertIn("local credentials found", line[0])
        self.assertIn("--live", line[0])
        # 13b: the whole-line command with `--live` is printed exactly once at the end (never a bare
        #   `scv doctor --live`); once `--live` has run, never ask them to run it again
        self.assertEqual(helpers.door_lines_missing(out, "doctor", "--live"), [], out)
        self.assertEqual(out.count(scv.self_cmd("doctor", "--live")), 1, out)
        _rc, _lines, live_out, _err = self.doctor("--live")
        self.assertNotIn(scv.self_cmd("doctor", "--live"), live_out)
        self.assertEqual(rc, 0)

    def test_stdout_starts_with_the_facts_as_json(self):
        _rc, _lines, out, _err = self.doctor()
        facts, _end = json.JSONDecoder().raw_decode(out)
        self.assertEqual(sorted(facts["families"]), ["claude", "codex"])

    def test_expired_credentials_point_at_a_login_that_really_runs(self):
        """The status command says "local credentials exist", but a real call gets 401 (the credentials expired or
        were revoked) -- `--live` exists exactly for this cell (L192).
        An error message must never lie, and this line's history: Task 12 gave a bare `codex login`, while scv back
          then always gave codex a dedicated CODEX_HOME ⇒ following it through still landed on 401 ⇒ Task 12/13
          switched to giving the login command for that specific home, split per shell. Since Task 13c, codex uses
          the player's own home ⇒ `codex login` logs into exactly that, but the executable is most likely not on PATH
          (the one bundled with the desktop build) ⇒ doctor gives the resolved path, never `fix_hint`'s bare line;
          printed with real newlines (the whole line is pasteable)."""
        os.environ.update(FAKE_MODE="auth", FAKE_STALE_LOGIN="1")
        rc, _lines, out, _err = self.doctor("--live")
        got = out[json.JSONDecoder().raw_decode(out)[1]:].splitlines()          # only look at the lines meant for a person, after the JSON
        # precondition: both families really reached the cell "has credentials, real call gets 401" (otherwise the
        #   lines below are vacuous: the blocked path already gave the right command to begin with)
        self.assertEqual([x.split(")")[0] for x in got if x.startswith("❌ ")],
                         ["❌ claude's real call failed (auth_required", "❌ codex's real call failed (auth_required"], out)
        for fam in ("claude", "codex"):                                         # 13b review M4: claude's line also goes through the door
            want = scv.login_cmd(fam, helpers.fake_head(fam)).splitlines()     # 13c review M4: computed by the door, never hand-assembled
            self.assertEqual([w for w in want if w not in got], [], out)
            self.assertNotIn(scv.fix_hint("auth_required", fam), got)          # the bare line (`fix_hint`) must never stand in for it
        # never ask them to log into some dedicated home anymore (since 15c, the environment line names CODEX_HOME --
        #   that is disclosure, never a request to log in there, except for this one)
        self.assertEqual([x for x in got if "CODEX_HOME" in x and "the two families of variables passed to the child CLI by" not in x], [], out)
        self.assertEqual(rc, 1)

    def test_a_clean_live_check_says_only_what_the_ruler_can_tell(self):
        """Task 12 review M-3: the judgment looks at "is a fragment of the secret in the answer", never "did it read
        it or not" (it cannot see one that was read but not said, or one that was paraphrased) ⇒ that line must never
        say "did not read it"."""
        rc, _lines, out, _err = self.doctor("--live")
        got = [x for x in out[json.JSONDecoder().raw_decode(out)[1]:].splitlines() if "real call went through" in x]
        self.assertEqual(len(got), 2, out)                                      # precondition: both families really reached "it went through"
        for line in got:
            self.assertIn("the canary outside the working directory never showed up in the answer", line)
            self.assertNotIn("never read it", line)
        self.assertEqual(rc, 0)

    def test_a_leaky_cli_fails_doctor_live(self):
        os.environ["FAKE_MODE"] = "leak"
        rc, _lines, out, _err = self.doctor("--live")
        self.assertEqual(rc, 1)
        self.assertEqual(len([x for x in out.splitlines() if "canary" in x and x.startswith("🔴")]), 2, out)


class DoctorSaysWhatCodexCarries(_Staged):
    """Task 13c: the price of codex using the player's own CODEX_HOME (no login needed) is that the instruction files
    there (and in any git ancestor directory) go into every single call in full, codex has no switch to turn this
    off, and this bridge cannot block it ⇒ doctor reports it honestly: path plus byte count; says nothing if there is
    none. Only ever stat, never read the content (B22).
    ⭐ 13c Fix 1b (maintainer's ruling): Fix 1 had plain doctor handshake all the way to thread/start to ask codex for
      its `instructionSources` -- but codex's thread/start does a startup prewarm: it connects to
      `wss://chatgpt.com/backend-api/codex/responses` and gets back a response id (measured against codex's own log
      library, 📎 NOTES.md::codex-user-home) ⇒ "plain doctor costs nothing" became a lie. ⇒ split into two layers:
      - plain doctor: never starts any session, only stats `$CODEX_HOME` (`~/.codex` if unset) -- an override of 0
        bytes is reported as AGENTS.md (review I2's (b)); the cell "the override is all whitespace" cannot be told
        apart by stat alone ⇒ say one honest extra sentence, never read the content just to tell them apart.
      - `doctor --live` (which already declares that it spends real cost): take the `instructionSources` from its own
        real call's own session reply (never start a separate session just for this), reporting whichever ones codex
        says (including ones in a git ancestor directory, M7); if codex does not say, report that it could not tell
        and fall back to plain doctor's lines.
    ⚠️ the stub's `instructionSources` rules are measured against a real codex 0.155 at zero cost
      (tests/test_15_fake_cli.py::FakeCodexPlayersOwnHome)."""

    SECRET = "SECRET-7d1e0c-do-not-print"
    LINE = "⚠️ codex will carry your %s (%s) into every call: codex has no switch to turn it off, and this bridge cannot block it"
    CAVEAT = "the override is there, but if it is all whitespace codex will switch to AGENTS.md"
    NO_SOURCES = "codex's thread/start reply has no instructionSources table (this version of codex does not say)"

    def home_with(self, **files):
        home = tempfile.mkdtemp(prefix="players-codex-")
        self.addCleanup(__import__("shutil").rmtree, home, True)
        for name, text in files.items():
            with io.open(os.path.join(home, name.replace("_", ".")), "w", encoding="utf-8", newline="") as f:
                f.write(text)
        return home

    def doctor(self, home, *argv, **env):
        """Really run `scv doctor`, while treating the very act of opening an AGENTS*.md as a violation (checking the
        output for whether the password is in it is never enough: reading it without printing it out is still
        reading it). `home=None` ⇒ never set CODEX_HOME for this run (the cell "unset means `~/.codex`")."""
        real_open, opened = io.open, []

        def guarded(file, *a, **k):
            if os.path.basename(str(file)).startswith("AGENTS"):
                opened.append(str(file))
                raise AssertionError("doctor opened the player's %s (only stat is allowed)" % file)
            return real_open(file, *a, **k)

        with mock.patch.dict(os.environ, dict(env, CODEX_HOME=home or "")), mock.patch("builtins.open", guarded), \
                mock.patch("io.open", guarded):
            if home is None:
                del os.environ["CODEX_HOME"]
            rc, lines, out, err = log_lines_during(lambda: scv.main(["doctor"] + list(argv)))
        # ⭐ pin down why first: if it was opened, it goes red on this line (otherwise the top-level handler would
        #   swallow that open into "did not go through", going red on the JSON-parsing line below instead)
        self.assertEqual(opened, [], "doctor opened the player's instruction file (only stat is allowed)")
        facts, end = json.JSONDecoder().raw_decode(out)
        return rc, facts["families"]["codex"], out[end:].splitlines(), out + err + NL.join(lines)

    @staticmethod
    def said(lines):
        return [x for x in lines if "into every call" in x]

    def caveats(self, lines):
        return [x for x in lines if self.CAVEAT in x]

    # ── plain doctor: zero cost, only stat
    def test_plain_doctor_starts_no_session_of_either_family(self):
        """🔴 Fix 1b's bug shape: plain doctor started a codex session (handshaking to thread/start ⇒ codex prewarms
        and connects to the inference endpoint). The judgment is built to the shape, never to the one concrete
        scene "codex's app-server": during plain doctor, both families' fake-CLI records must only ever be quick
        commands (`--version`/`auth status`/`login status`/`features list`, which the stub records as `quick`); not a
        single session record (which the stub records as `argv`: one call for claude, `app-server` for codex) is
        allowed.
        A positive control is welded into the same case: the same ruler can see both families starting a session
          under `--live` (otherwise a blind ruler would also run all green).
        🔴 the boundary is the same as
          tests/test_90_cli.py::Lifecycle::test_zero_quota_commands_start_no_session: it can only see processes
          started through `cli_head`."""
        def sessions(mark):
            return [r for r in helpers.fake_log_since(mark) if "argv" in r and "console" not in r]

        mark = len(helpers.read_fake_log())
        rc, _codex, _lines, _all = self.doctor(self.home_with(AGENTS_md="a" * 7))
        self.assertEqual(sessions(mark), [])
        self.assertEqual({r.get("family") for r in helpers.fake_log_since(mark) if "quick" in r}, {"claude", "codex"})
        self.assertEqual(rc, 0)
        mark = len(helpers.read_fake_log())
        self.doctor(self.home_with(AGENTS_md="a" * 7), "--live")
        self.assertEqual({r["family"] for r in sessions(mark)}, {"claude", "codex"})

    def test_the_file_is_reported_with_its_real_size_and_never_opened(self):
        text = "# my notes" + NL + "the key is " + self.SECRET + NL
        home = self.home_with(AGENTS_md=text)
        rc, codex, lines, everything = self.doctor(home)
        path, size = os.path.join(home, "AGENTS.md"), len(text.encode("utf-8"))
        self.assertEqual(codex["carried"], {"home": home, "files": [{"path": path, "bytes": size}]})
        self.assertEqual(self.said(lines), [self.LINE % (path, "%d bytes" % size)])
        self.assertNotIn(self.SECRET, everything)
        self.assertEqual(rc, 0)                                   # this is a fact, never a fault: it must never be judged "there is a problem" over this

    def test_nothing_there_means_not_a_word(self):
        """Zero-input control: not a single one exists ⇒ not a single word is said (otherwise the case above would
        also run all green on an implementation that "always says something").
        A 0-byte file is never carried by codex (measured at zero cost), and stat can tell it apart ⇒ likewise, not
        a single word is said."""
        for files in ({}, {"AGENTS_md": ""}, {"AGENTS_override_md": ""}, {"AGENTS_md": "", "AGENTS_override_md": ""}):
            with self.subTest(files=files):
                home = self.home_with(**files)
                _rc, codex, lines, _all = self.doctor(home)
                self.assertEqual(codex["carried"], {"home": home, "files": []})
                self.assertEqual(self.said(lines) + self.caveats(lines), [])

    def test_an_empty_override_means_agents_md_gets_loaded(self):
        """🔴 review I2's bug shape: an override of 0 bytes ⇒ codex falls back to AGENTS.md (measured against a real
        codex at zero cost); the version that guessed by whether the file existed reported "override, 0 bytes" --
        describing the file that is genuinely going into the context (possibly a few KB of private instructions) as
        practically nothing.
        0 bytes can be told apart by stat ⇒ report AGENTS.md directly, never add that vague sentence."""
        home = self.home_with(AGENTS_md="a" * 7, AGENTS_override_md="")
        _rc, codex, lines, _all = self.doctor(home)
        path = os.path.join(home, "AGENTS.md")
        self.assertEqual(codex["carried"], {"home": home, "files": [{"path": path, "bytes": 7}]})
        self.assertEqual(self.said(lines), [self.LINE % (path, "7 bytes")])
        self.assertEqual(self.caveats(lines), [])

    def test_an_override_with_bytes_is_reported_and_the_blank_case_is_said_out_loud(self):
        """An override with bytes in it ⇒ report it (when there is content, codex carries only it, never AGENTS.md:
        measured at zero cost). But when it is all whitespace, codex switches to using AGENTS.md instead, a cell stat
        alone cannot tell apart ⇒ say one honest sentence (the maintainer's ruling's exact words), never read the
        content just to tell them apart (opening the file inside `doctor()` is itself a violation).
        ⭐ the two cells (has content / all whitespace, same byte count) must produce identical output: plain doctor
          telling them apart would mean it read the content.
        Zero-input control: there is no AGENTS.md ⇒ there is nowhere to fall back to ⇒ that sentence is never
          said."""
        outs = []
        for over in ("b" * 11, " " * 9 + NL + chr(9)):
            with self.subTest(override=repr(over)):
                home = self.home_with(AGENTS_md="a" * 7, AGENTS_override_md=over)
                _rc, codex, lines, _all = self.doctor(home)
                o, a = os.path.join(home, "AGENTS.override.md"), os.path.join(home, "AGENTS.md")
                self.assertEqual(codex["carried"], {"home": home, "files": [{"path": o, "bytes": 11}],
                                                    "if_blank": {"path": a, "bytes": 7}})
                self.assertEqual(self.said(lines), [self.LINE % (o, "11 bytes")])
                self.assertEqual(self.caveats(lines), [("⚠️ %s (7 bytes): %s (a --live check asks codex itself which one it is using)")
                                                       % (self.CAVEAT, a)])
                outs.append([x.replace(home, "<home>") for x in self.said(lines) + self.caveats(lines)])
        self.assertEqual(outs[0], outs[1])
        home = self.home_with(AGENTS_override_md="b" * 11)
        _rc, codex, lines, _all = self.doctor(home)
        self.assertEqual(codex["carried"], {"home": home, "files": [{"path": os.path.join(home, "AGENTS.override.md"),
                                                                     "bytes": 11}]})
        self.assertEqual(self.caveats(lines), [])

    def test_a_file_we_cannot_stat_is_still_reported(self):
        """The plain-doctor version of review M9's cell: it exists, but its size cannot be determined ⇒ report it
        anyway, stating plainly why there is no number (never treat it as not existing, never make up a number)."""
        real_stat = Path.stat

        def stat(p, *a, **k):
            if p.name == "AGENTS.md":
                raise PermissionError(13, "access denied (made up by the harness)", str(p))
            return real_stat(p, *a, **k)

        home = self.home_with(AGENTS_md="a" * 7)
        with mock.patch.object(Path, "stat", stat):
            _rc, codex, lines, _all = self.doctor(home)
        (row,) = codex["carried"]["files"]
        self.assertEqual((row["path"], row["bytes"]), (os.path.join(home, "AGENTS.md"), None))
        self.assertIn("access denied (made up by the harness)", row["error"])
        self.assertEqual(self.said(lines), [self.LINE % (row["path"], "could not read its size: " + row["error"])])

    def test_a_directory_with_the_files_name_is_not_a_file(self):
        """13c review N4: `_stat_file` was missing an `is_file()` check ⇒ a directory that happens to be named
        `AGENTS.override.md`/`AGENTS.md` was reported as a file that "will be carried into every call", with the
        byte count being the directory's size. On win32 a directory's `st_size` is 0 (which the "0 bytes means don't
        report it" step conveniently filters out), and 4096 on POSIX ⇒ a false positive.
        ⭐ the judgment is pinned directly on `_stat_file` itself (this can go red on win32 too), plus one end-to-end
          cell: override is a directory, AGENTS.md is a file ⇒ only AGENTS.md is reported, and never say that "the
          override is there, but ..." sentence either."""
        home = self.home_with(AGENTS_md="a" * 7)
        os.mkdir(os.path.join(home, "AGENTS.override.md"))
        self.assertIsNone(scv._stat_file(Path(home) / "AGENTS.override.md"))
        self.assertEqual(scv._stat_file(Path(home) / "AGENTS.md"), {"path": os.path.join(home, "AGENTS.md"), "bytes": 7})
        _rc, codex, lines, _all = self.doctor(home)
        self.assertEqual(codex["carried"], {"home": home, "files": [{"path": os.path.join(home, "AGENTS.md"), "bytes": 7}]})
        self.assertEqual(self.caveats(lines), [])

    def test_no_codex_home_means_dot_codex_in_the_users_home(self):
        """CODEX_HOME unset ⇒ codex uses `~/.codex` (its own default) ⇒ doctor also stats there. The home directory
        is replaced with a temp directory (the harness must never touch the actual test runner's own ~/.codex)."""
        fake_user = self.home_with()
        os.mkdir(os.path.join(fake_user, ".codex"))
        with io.open(os.path.join(fake_user, ".codex", "AGENTS.md"), "w", encoding="utf-8", newline="") as f:
            f.write("c" * 5)
        with mock.patch.object(scv, "user_home", lambda: Path(fake_user)):
            _rc, codex, lines, _all = self.doctor(None)
        dot = os.path.join(fake_user, ".codex")
        self.assertEqual(codex["carried"], {"home": dot, "files": [{"path": os.path.join(dot, "AGENTS.md"), "bytes": 5}]})

    # ── doctor --live: ask codex itself (using that real call's own session)
    def test_live_reports_what_codex_itself_loads(self):
        """The `--live` real call's own session reply has `instructionSources` ⇒ report exactly whichever ones codex
        says, never guess with stat anymore: when the override is all whitespace, plain doctor can only speak
        vaguely, but `--live` must say it precisely (report AGENTS.md, never say that vague sentence, never report
        the override)."""
        home = self.home_with(AGENTS_md="a" * 7, AGENTS_override_md=" " * 9 + NL + chr(9))
        rc, codex, lines, _all = self.doctor(home, "--live")
        a = os.path.join(home, "AGENTS.md")
        self.assertEqual(codex["live"]["carried"], {"sources": [{"path": a, "bytes": 7}]})
        self.assertEqual(self.said(lines), [self.LINE % (a, "7 bytes")])
        self.assertEqual(self.caveats(lines), [])
        self.assertEqual(rc, 0)

    def test_live_sees_an_agents_md_in_a_git_ancestor(self):
        """Review M7: some ancestor directory of the working directory has a `.git` in it ⇒ codex carries every
        AGENTS.md along the path from that repo root down to the cwd (measured against a real codex at zero cost:
        carried only when `.git` is present). This is exactly the cell for a player whose home directory is a
        dotfiles repo, or whose SCV_HOME is set inside some repo -- stat cannot see it, `--live` can.
        ⭐ pins down "the session asked matches the shape of the real session": this copy is only visible when the cwd
          is under `SCV_HOME/work/` (asking from a different place would not get the files a real session would
          carry).
        ⭐ pins down "never start a separate session just to ask this": during `--live`, codex starts only one
          app-server -- starting one more would mean prewarming twice."""
        repo = self.home_with(AGENTS_md="p" * 5)
        os.mkdir(os.path.join(repo, ".git"))
        scv_home = os.path.join(repo, "scv-state")
        os.mkdir(scv_home)
        mark = len(helpers.read_fake_log())
        with mock.patch.dict(os.environ, {"SCV_HOME": scv_home}):
            helpers.pin_quiet_port()              # a separate SCV_HOME: doctor will ask config for the port (13b fix1 harness isolation)
            _rc, codex, lines, _all = self.doctor(self.home_with(), "--live")
            work = str(scv.spath("work"))
        self.assertEqual(codex["live"]["carried"], {"sources": [{"path": os.path.join(repo, "AGENTS.md"), "bytes": 5}]})
        self.assertEqual(len(self.said(lines)), 1, lines)
        boots = [r for r in helpers.fake_log_since(mark) if r.get("family") == "codex" and r.get("argv", [""])[0] == "app-server"]
        self.assertEqual(len(boots), 1, boots)
        cwd = os.path.normcase(boots[0]["cwd"])
        self.assertEqual(os.path.dirname(cwd), os.path.normcase(work))
        self.assertTrue(os.path.basename(cwd).startswith("doctor-"), cwd)
        self.assertFalse(os.path.exists(boots[0]["cwd"]))         # deleted once done asking

    def test_live_when_codex_does_not_say_it_says_so_and_keeps_the_stat_lines(self):
        """This version of codex's reply has no such field at all, or it is the wrong shape ⇒ say it could not tell;
        plain doctor's lines are still printed as usual (`--live` must never say less than plain doctor), and stat's
        estimate must never be passed off as what codex said (facts's `live.carried` holds only the reason it could
        not tell)."""
        for shape in ("missing", "bad"):
            with self.subTest(shape=shape):
                home = self.home_with(AGENTS_md="a" * 7)
                _rc, codex, lines, _all = self.doctor(home, "--live", FAKE_SOURCES_SHAPE=shape)
                self.assertEqual(codex["live"]["carried"], {"error": self.NO_SOURCES})
                self.assertEqual(self.said(lines), [self.LINE % (os.path.join(home, "AGENTS.md"), "7 bytes")])
                self.assertIn(("⚠️ could not tell which instruction files this session will carry: %s ⇒ the lines above "
                              "only estimate from files under CODEX_HOME, and cannot see one in a git ancestor directory")
                              % self.NO_SOURCES, lines)

    def test_live_a_session_that_will_not_start_is_a_problem(self):
        """Review M1⑤: the handshake step fails closed (the reply is not recognized) ⇒ the real call is bound to
        fail to come up too ⇒ the `--live` line carries the original words and judges this family as having a
        problem.
        ⚠️ since Fix 1b, plain doctor never handshakes (the zero-cost promise) ⇒ this cell is only visible under
          `--live`; the Fix 1 version of plain doctor could see it too, at the price of prewarming."""
        rc, codex, lines, _all = self.doctor(self.home_with(), "--live", FAKE_CONFIG_READ="nokey")
        self.assertNotIn("carried", codex["live"])
        bad = [i for i, x in enumerate(lines) if x.startswith("❌ codex's real call failed (unknown)")]
        self.assertEqual(len(bad), 1, lines)
        self.assertIn("has no mcp_servers table", lines[bad[0]])
        # 13c outside review scope: `unknown`'s `fix_hint` is empty ⇒ it used to end with "... paste this line to
        #   scv's maintainer =>" followed by a new line "see the original words above". The next step is already in
        #   the original words ⇒ so this line ends right there on the original words (never tack on an empty arrow,
        #   never point again to "see the original words above").
        self.assertTrue(lines[bad[0]].endswith("upgrade Codex CLI and try again first; if it still happens, paste this line to scv's maintainer"), lines[bad[0]])
        self.assertNotIn("see the original words above", NL.join(lines))
        self.assertIn("⇒ families with problems: codex", NL.join(lines))
        self.assertEqual(rc, 1)

    def test_live_a_source_codex_names_but_we_cannot_stat_says_so(self):
        """Review M9: codex says it is there, but we cannot stat it ⇒ report it anyway, stating plainly why there is
        no number (never treat it as not existing, never make up a number)."""
        gone = os.path.join(tempfile.gettempdir(), "scv-no-such-agents-7c1b", "AGENTS.md")
        _rc, codex, lines, _all = self.doctor(self.home_with(), "--live", FAKE_SOURCES_EXTRA=gone)
        (row,) = codex["live"]["carried"]["sources"]
        self.assertEqual((row["path"], row["bytes"]), (gone, None))
        self.assertTrue(row["error"])
        self.assertEqual(self.said(lines), [self.LINE % (gone, "could not read its size: " + row["error"])])

    def test_an_old_codex_home_key_gets_one_honest_line(self):
        """An old installation's config.json has `codex_home` written in it (our own default value from back then,
        never something they chose) ⇒ it is never used anymore, but if it is seen, say one honest sentence, tell them
        how to actually switch directories if they want to, and that this key can be deleted (maintainer's ruling;
        review M10). A zero-input control is welded into the same case: without this key, not a single word is
        said."""
        cfg = scv.load_config()
        scv.save_config(dict(cfg, codex_home="C:/old/install/codex-home"))
        self.addCleanup(scv.save_config, cfg)
        rc, _codex, lines, _all = self.doctor(self.home_with())
        self.assertIn("· config.json's codex_home (C:/old/install/codex-home) is no longer used: the codex this bridge starts uses "
                      "your own CODEX_HOME (~/.codex if unset); to have it use a different directory, set the CODEX_HOME "
                      "environment variable; this key can be deleted from config.json",
                      lines)
        self.assertEqual(rc, 0)
        scv.save_config(cfg)
        _rc, _codex, lines, _all = self.doctor(self.home_with())
        self.assertEqual([x for x in lines if "codex_home" in x], [])


class LocalCallsBypassProxy(_Staged):
    def test_healthz_works_with_a_dead_proxy_in_env(self):
        b, port, _tok = helpers.start_bridge()
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
        try:
            self.assertEqual(scv.local_get(port, "/healthz")["version"], scv.VERSION)
        finally:
            b.stop()

    def test_something_else_on_the_port_is_not_our_bridge(self):
        """Measured on this machine: port 8765 is already occupied by some other program (a `dashboard.py`) ⇒
        "something answers on the port" does not mean "the bridge is running".
        ⭐ whatever occupies the port has to genuinely answer 200 plus JSON: a socket that never answers would make
          `local_get` itself time out and return None, unable to tell apart "recognized as not the bridge" from
          "got no reply at all" (the first version was vacuous exactly this way)."""
        class Other(BaseHTTPRequestHandler):
            def do_GET(self):
                data = json.dumps({"status": "fine", "version": "9.9"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                return

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Other)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            port = srv.server_address[1]
            self.assertEqual(scv.local_get(port, "/healthz")["status"], "fine")     # the ruler is not blind: it really did answer
            self.assertIsNone(scv.our_health(port))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_a_redirect_off_the_loopback_is_not_followed(self):
        """13d, item 5 (Task 13, outside review scope, ②, which existed first): `local_get` should only ever dial the
        loopback, but an http->http 302 used to get followed to any host at all -- if whatever answers on the port is
        some other program (this machine's 8765 is exactly that), and it replies with a 302, `scv status`/`stop`/
        `doctor` would send a GET out onto the wider network.
        ⇒ the judgment lives inside the redirect door (`redirect_refused`, the same function as the rule for a
          request carrying a token): starting from the loopback, never follow to anywhere outside the loopback.
        ⭐ judges "not a single byte was dialed out": a dialing probe is pinned at the socket layer
          (`socket.create_connection`), recording who every single call connects to; connecting to anything outside
          the loopback raises on the spot ⇒ even when red, it must never actually reach out (the shape of red: an
          extra `('a-remote-host.example', 9)` shows up).
        A positive control is welded into the same case: loopback -> loopback's 302 is still followed (otherwise
          "not a single one followed" would also be green)."""
        class Other(BaseHTTPRequestHandler):
            def do_GET(self):
                where = {"/healthz": "http://a-remote-host.example:9/healthz",
                         "/hop": "http://127.0.0.1:%d/landed" % self.server.server_address[1]}.get(self.path)
                data = json.dumps({"status": "landed"}).encode("utf-8")
                self.send_response(302 if where else 200)
                if where:
                    self.send_header("Location", where)
                self.send_header("Content-Length", "0" if where else str(len(data)))
                self.end_headers()
                if not where:
                    self.wfile.write(data)

            def log_message(self, *a):
                return

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Other)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        port, real, dials = srv.server_address[1], socket.create_connection, []

        def probe(address, *a, **k):
            dials.append(tuple(address))
            if address[0] != "127.0.0.1":
                raise OSError("dialing probe: this call would connect to %r (outside the loopback), which is never allowed" % (address,))
            return real(address, *a, **k)

        with mock.patch("socket.create_connection", probe):
            self.assertIsNone(scv.local_get(port, "/healthz"))
            gone = list(dials)
            del dials[:]
            self.assertEqual(scv.local_get(port, "/hop"), {"status": "landed"})
        self.assertEqual(gone, [("127.0.0.1", port)])
        self.assertEqual(dials, [("127.0.0.1", port), ("127.0.0.1", port)])


# ━━ bridge.pid (A1-A4)
class PidFile(_Staged):
    def setUp(self):
        super().setUp()
        self.addCleanup(lambda: pid_file().unlink() if pid_file().exists() else None)

    def fake_bridge(self):
        pid, born = detached(self)
        put_pid({"pid": pid, "born": born, "port": 1, "ticket": "t"})
        return pid, born

    def test_stop_kills_the_bridge_it_recognises(self):
        pid, born = self.fake_bridge()
        old, scv.STOP_WAIT_S = scv.STOP_WAIT_S, 1.0     # the POSIX path sends SIGTERM first and then waits; the stand-in exits as soon as it receives it
        try:
            rc, lines, out, _err = log_lines_during(lambda: scv.cmd_stop(argparse.Namespace()))
        finally:
            scv.STOP_WAIT_S = old
        self.assertEqual(rc, 0)
        self.assertIn("stopped", out)
        self.assertTrue(gone(pid, born))
        self.assertFalse(pid_file().exists())

    def test_stop_does_not_claim_success_when_it_cannot_tell(self):
        """A1: of the birth id's three possible values, `None` means "could not tell". The brief's
        `if proc_start_id(pid):` read it as "already stopped" ⇒ it never killed anything yet printed "stopped", and
        even deleted the pid file."""
        pid, born = self.fake_bridge()
        with mock.patch.object(scv, "proc_start_id", return_value=None):
            rc, lines, out, err = log_lines_during(lambda: scv.cmd_stop(argparse.Namespace()))
        self.assertEqual(rc, 1)
        self.assertNotIn("stopped", out + err)
        self.assertIn("could not tell", err)
        self.assertTrue(pid_file().exists())
        self.assertEqual(scv.proc_start_id(pid), born)              # left it untouched
        self.assertTrue(any("could not tell" in x for x in lines), lines)  # landed on disk (C15)

    def test_stop_does_not_claim_success_when_the_kill_did_not_land(self):
        pid, born = self.fake_bridge()
        old = scv.STOP_WAIT_S, scv.STOPPED_WITHIN_S
        scv.STOP_WAIT_S, scv.STOPPED_WITHIN_S = 0.5, 0.5
        try:
            with mock.patch.object(scv, "kill_pid_tree"), mock.patch.object(scv.os, "kill"):
                rc, _lines, out, err = log_lines_during(lambda: scv.cmd_stop(argparse.Namespace()))
        finally:
            scv.STOP_WAIT_S, scv.STOPPED_WITHIN_S = old
        self.assertEqual(rc, 1)
        self.assertNotIn("stopped", out + err)
        self.assertIn("did not stop", err)
        self.assertTrue(pid_file().exists())
        self.assertEqual(scv.proc_start_id(pid), born)

    def test_stop_does_not_claim_success_when_it_cannot_ask_after_the_kill(self):
        """The other half of A1 (Task 12 review I-1): `bridge_owner` recognized the bridge and acted, but asking again
        right after killing it could not tell (None).
        ⚠️ the `…_when_it_cannot_tell` case makes `proc_start_id` always return None ⇒ that already judges unsure
          and returns early at the `bridge_owner` step, never even reaching the question asked after killing (the
          fixture happens to satisfy the contract). If this cell fell back to a plain truthy/falsy judgment: it would
          say "stopped", rc=0, delete the pid file, and even sweep once -- on POSIX, once `ps` times out after
          SIGTERM it lands right here, and the bridge might still be alive at that point, so sweeping would kill its
          CLIs along with it.
        ⭐ the script: could tell before acting (it is that bridge), could not tell after acting. Both the kill and
          the sweep are replaced with accounting stand-ins (the stand-in bridge is still a real, detached process: if
          the stand-in swap ever fails, only it takes the hit)."""
        pid, born = self.fake_bridge()
        killed = []
        with mock.patch.object(scv, "proc_start_id", side_effect=lambda p: None if killed else born), \
                mock.patch.object(scv, "kill_pid_tree", side_effect=killed.append), \
                mock.patch.object(scv.os, "kill", side_effect=lambda p, s: killed.append(p)), \
                mock.patch.object(scv, "sweep_orphans") as sweep, \
                mock.patch.object(scv, "STOP_WAIT_S", 0.3), mock.patch.object(scv, "STOPPED_WITHIN_S", 0.3):
            rc, lines, out, err = log_lines_during(lambda: scv.cmd_stop(argparse.Namespace()))
        self.assertEqual(killed[:1], [pid])            # precondition: it really reached the "acted, then asked again" cell
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn("stopped", out + err)
        self.assertIn("could not tell its birth id", err)         # states plainly "could not tell", never "still alive"
        self.assertTrue(pid_file().exists())
        self.assertEqual(sweep.call_count, 0)          # never sweeps (that bridge might still be alive, and its CLIs are all in the table)
        self.assertTrue(any("did not stop" in x for x in lines), lines)    # landed on disk (C15)

    def test_a_pid_file_from_another_version_is_left_alone(self):
        """A3: a birth-id format that is not recognized (an old version on win32 wrote .NET Ticks) ⇒ never judge it
        "not that bridge" and then delete the file."""
        pid, born = detached(self)
        other = "638629140000000000" if os.name == "nt" else ""
        self.assertFalse(scv.birth_known(other))
        put_pid({"pid": pid, "born": other, "port": 1})
        rc, _lines, out, err = log_lines_during(lambda: scv.cmd_stop(argparse.Namespace()))
        self.assertEqual(rc, 1)
        self.assertIn("not recognized", err)
        self.assertTrue(pid_file().exists())
        self.assertEqual(scv.proc_start_id(pid), born)

    def test_a_stale_pid_file_is_cleared_and_says_so(self):
        kid = sleeper()
        born = scv.proc_start_id(kid.pid)
        bury(kid)
        put_pid({"pid": kid.pid, "born": born, "port": 1})
        rc, _lines, out, _err = log_lines_during(lambda: scv.cmd_stop(argparse.Namespace()))
        self.assertEqual(rc, 0)
        self.assertIn("disappeared without winding down", out)
        self.assertFalse(pid_file().exists())

    def run_here(self, port=None):
        """Run `cmd_run` in-process. ⭐ the main loop is replaced with "record whatever bridge.pid holds at this
        moment and return", `signal.signal` is removed (never install a Ctrl+C handler inside the test process), and
        the port is switched to a free one -- when a gate is removed for a negative control, this really does start
        a bridge, which must never hang the whole harness.
        Returns (rc, the lines that landed on disk, stdout, stderr, whatever bridge.pid held the moment the main loop
        started)."""
        cfg = scv.load_config()
        old, served = cfg["port"], []
        cfg["port"] = port or free_port()
        scv.save_config(cfg)
        try:
            with mock.patch.object(scv, "serve_until", side_effect=lambda b, s: served.append(scv._read_pid_file())), \
                    mock.patch.object(scv.signal, "signal"):
                return log_lines_during(lambda: scv.cmd_run(argparse.Namespace(ticket=""))) + (served,)
        finally:
            cfg["port"] = old
            scv.save_config(cfg)

    def test_run_refuses_to_write_a_pid_file_it_cannot_vouch_for(self):
        """A2: if `born` is `None`, writing it in produces `null` ⇒ the next `scv stop` would say "no longer that
        bridge" and delete the file, while the bridge is still running."""
        real, me = scv.proc_start_id, os.getpid()
        with mock.patch.object(scv, "proc_start_id", side_effect=lambda pid: None if pid == me else real(pid)):
            rc, lines, _out, err, served = self.run_here()
        self.assertEqual((rc, served), (1, []))                   # never came up; if it had, served would hold that `born: null` copy
        self.assertFalse(pid_file().exists())
        self.assertIn("birth id", err)
        self.assertTrue(any("birth id" in x for x in lines), lines)

    def test_run_refuses_to_start_next_to_a_live_bridge_and_does_not_sweep_its_children(self):
        """🔴 while another bridge is still alive, sweeping is never allowed: all its CLI child processes are in the
        registry with matching birth ids ⇒ one sweep would kill them all."""
        self.fake_bridge()
        kid = sleeper()
        self.addCleanup(bury, kid)
        self.assertTrue(scv.child_add(kid.pid, "claude"))
        self.addCleanup(scv.child_remove, kid.pid)
        rc, _lines, _out, err, served = self.run_here()
        self.assertEqual((rc, served), (1, []))
        self.assertEqual(helpers.door_lines_missing(err, "stop"), [], err)   # 13b: the pasteable line (never a bare `scv stop`)
        self.assertFalse(died(kid, within=1.0))

    def test_run_says_once_that_an_old_codex_home_key_is_not_used(self):
        """13c review M10: saying it just once in doctor is not enough for an old installation's config.json holding
        `codex_home` -- someone who deliberately configured this isolation back then does not necessarily run doctor,
        and the isolation would quietly disappear ⇒ starting the bridge also drops a line into bridge.log (the same
        sentence as doctor's, `scv.unused_codex_home`). A zero-input control is welded into the same case."""
        scv.save_config(dict(scv.load_config(), codex_home="C:/old/install/codex-home"))
        self.addCleanup(lambda: scv.save_config({k: v for k, v in scv.load_config().items() if k != "codex_home"}))
        rc, lines, _out, _err, _served = self.run_here()
        self.assertEqual(rc, 0)
        self.assertEqual(len([x for x in lines if "codex_home (C:/old/install/codex-home) is no longer used" in x
                              and "can be deleted from config.json" in x]), 1, lines)
        scv.save_config({k: v for k, v in scv.load_config().items() if k != "codex_home"})
        rc, lines, _out, _err, _served = self.run_here()
        self.assertEqual((rc, [x for x in lines if "codex_home" in x]), (0, []))

    def test_run_writes_a_pid_file_it_can_vouch_for_and_takes_it_away(self):
        """The happy-path cell (the control for the two cases above): the moment the main loop starts, what is on
        disk is its own copy (birth id non-empty and matching), deleted once it winds down."""
        rc, _lines, _out, _err, served = self.run_here()
        self.assertEqual(rc, 0)
        self.assertEqual([(r["pid"], r["born"]) for r in served], [(os.getpid(), scv.proc_start_id(os.getpid()))])
        self.assertFalse(pid_file().exists())

    def test_run_says_a_busy_port_is_not_something_to_retry(self):
        """M-7 (Task 10 review): a port in use raises `crashed` (in RETRYABLE). Nothing on the bridge-starting path
        reads `retryable`, but the words still have to be right: if the previous bridge is not closed, retrying ten
        thousand times changes nothing."""
        b, port, _tok = helpers.start_bridge()
        self.addCleanup(b.stop)
        rc, lines, _out, err, served = self.run_here(port)
        self.assertEqual((rc, served), (1, []))
        self.assertIn(str(port), err)
        self.assertIn("config.json", err)
        self.assertEqual(helpers.door_lines_missing(err, "status") + helpers.door_lines_missing(err, "stop"), [], err)
        self.assertFalse(pid_file().exists())

    def test_start_waits_for_its_own_ticket_not_any_pid_file(self):
        """A4: the brief waits on `bridge.pid.exists()` -- exactly the f17c9e5 shape (waiting for a file to appear,
        and getting the leftover from last time).
        ⭐ the judgment is the one function `cmd_start` really uses, never a copy re-implemented in the harness."""
        b, port, _tok = helpers.start_bridge()
        self.addCleanup(b.stop)
        put_pid({"pid": os.getpid(), "born": scv.proc_start_id(os.getpid()), "port": port, "ticket": "old"})
        self.assertIsNone(scv.started(port, "mine"))                # leftover from last time: never counts
        put_pid({"pid": os.getpid(), "born": scv.proc_start_id(os.getpid()), "port": port, "ticket": "mine"})
        self.assertEqual(scv.started(port, "mine")["ticket"], "mine")
        self.assertIsNone(scv.started(free_port(), "mine"))        # right ticket, nothing answers on the port: not up yet

    def start_with(self, writes_own_file):
        """Run `cmd_start` in-process, with `spawn_detached` replaced by: genuinely starting a bridge on the
        configured port (the port answers), and only when `writes_own_file` is true does it write bridge.pid with the
        ticket issued for this run (when false, it stops right at the instant "the port is bound, but its own copy
        has not been written yet").
        A bridge.pid left behind by a previous, crashed bridge is placed on disk first (old ticket, pid no longer
        exists)."""
        port, cfg = free_port(), scv.load_config()
        old, cfg["port"] = cfg["port"], port
        scv.save_config(cfg)
        self.addCleanup(lambda: scv.save_config(dict(scv.load_config(), port=old)))
        dead = sleeper()
        dead_born = scv.proc_start_id(dead.pid)
        bury(dead)
        put_pid({"pid": dead.pid, "born": dead_born, "port": port, "ticket": "left-by-a-crash"})
        bridges = []

        def spawn(argv):
            b = scv.Bridge(scv.load_config())
            bridges.append(b)
            b.start_local(port)
            if writes_own_file:
                put_pid({"pid": os.getpid(), "born": scv.proc_start_id(os.getpid()), "port": port, "ticket": argv[-1]})
            return os.getpid(), ""

        try:
            with mock.patch.object(scv, "spawn_detached", side_effect=spawn), mock.patch.object(scv, "START_WAIT_S", 2.0):
                return log_lines_during(lambda: scv.cmd_start(argparse.Namespace()))
        finally:
            for b in bridges:
                b.stop()

    def test_start_does_not_take_a_crashed_bridges_pid_file_for_its_own(self):
        """A4's behavior cell: the previous case pinned down the one function `started()`; this case pins down that
        `cmd_start` really waits by ticket.
        The brief waits on "the port answers AND `bridge.pid.exists()`" ⇒ the moment the port answers, the old file
        left behind by the crashed bridge satisfies this on the spot ⇒ it reports "up", printing a dead man's pid,
        and the `scv stop` right after it stops based on that old file (the f17c9e5 shape)."""
        rc, _lines, out, err = self.start_with(writes_own_file=False)
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn("up:", out)
        self.assertIn("did not come up within", err)

    def test_start_takes_the_pid_file_written_with_its_own_ticket(self):
        """The happy-path control for the previous case (otherwise the previous case might just be red/green for
        some other reason): this bridge writes its own copy by ticket ⇒ it comes up, printing its own pid."""
        rc, _lines, out, err = self.start_with(writes_own_file=True)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("up: pid %d" % os.getpid(), out)


# ━━ Sweeping (A5/B9)
class Sweep(_Staged):
    def test_rows_that_cannot_be_asked_are_kept_named_and_then_let_go(self):
        """🧑‍⚖️ maintainer's ruling: a `None` row used to be silently forgotten (never killed, and no one remembers
        it once the table is cleared). Now: it stays in the table to be re-checked next time, is loud about it every
        time, and is only let go once it has failed to answer `SWEEP_STRIKES` times in a row. ⚠️ never be loud with
        "please end it by hand": on a non-admin machine, a `None` is most likely a pid the system has already
        recycled to a process we have no permission to inspect (a supplementary review general audit found all 143
        `None`s were access denied)."""
        kid = sleeper()
        self.addCleanup(bury, kid)
        self.assertTrue(scv.child_add(kid.pid, "claude"))
        self.addCleanup(scv._children_save, [])
        seen = []
        with mock.patch.object(scv, "proc_start_id", return_value=None):
            for _ in range(scv.SWEEP_STRIKES):
                _rc, lines, _o, _e = log_lines_during(scv.sweep_orphans)
                seen.append(([c.get("strikes") for c in scv.children() if c["pid"] == kid.pid], lines))
        self.assertEqual([s for s, _l in seen], [[k] for k in range(1, scv.SWEEP_STRIKES)] + [[]])
        for k, (_s, lines) in enumerate(seen, 1):
            said = [x for x in lines if "pid %d" % kid.pid in x]
            self.assertEqual(len(said), 1, lines)
            # ⭐ pins down that line's own cell (which attempt number, whether it was let go): that summary sentence
            #   always carries "no longer tracked" in it, so using it as the judgment would be vacuously true
            self.assertIn("pid %d (claude, attempt %d%s)" % (kid.pid, k, ", no longer tracked" if k == scv.SWEEP_STRIKES else ""),
                          said[0])
            self.assertNotIn("by hand", said[0])
            # ⭐ the unit being counted is a sweep, never a bridge start (review M-6): `scv stop` also sweeps once on
            #   its way down ⇒ the original words must state plainly which occasions come next
            self.assertIn("re-checked next sweep (starting or stopping the bridge)", said[0])
        self.assertFalse(died(kid, within=0.5))                    # never kill even one that could not be told

    def test_a_broken_table_is_kept_aside_before_the_next_write(self):
        """Ledger L110, M1: `children.json` failing to parse used to just be loud once, and `sweep_orphans` has two
        production call sites ⇒ the very next `scv start`/`scv stop` would overwrite the broken file, meaning the
        evidence was lost. ⇒ before writing the new table, first move the file as-is to `children.json.bad`, and be
        loud once, naming it. One cell each for three ways of breaking (unreadable / not a list / has a bad row), all
        going through the real production call site `sweep_orphans`."""
        table, bad = scv.spath("children.json"), scv.spath("children.json.bad")
        self.addCleanup(lambda: bad.unlink() if bad.exists() else None)
        self.addCleanup(scv._children_save, [])
        if bad.exists():            # never let one left behind by another case stand in as evidence for this one (waiting for a file to appear can mean getting last time's leftover)
            bad.unlink()
        for why, raw in (("read", b'[{"pid": 1, "born": "x", "family": "cla' + bytes([0xff])),
                         ("shape", b'{"pid": 1}'), ("rows", b'[{"pid": 1}]')):
            with self.subTest(why=why):
                table.write_bytes(raw)
                _rc, lines, _o, _e = log_lines_during(scv.sweep_orphans)
                self.assertEqual(bad.read_bytes() if bad.exists() else None, raw)     # the broken file is there as-is
                self.assertEqual(json.loads(table.read_text(encoding="utf-8")), [])  # the new table is written as usual (carries on as before)
                self.assertEqual(len([x for x in lines if str(bad) in x]), 1, lines)  # loud once, naming where the broken file is
                _rc, lines, _o, _e = log_lines_during(scv.sweep_orphans)             # the table is fine now ⇒ never move it again, never be loud again
                self.assertEqual((bad.read_bytes(), [x for x in lines if str(bad) in x]), (raw, []))

    def test_a_broken_table_that_cannot_be_moved_is_said_out_loud(self):
        """Cannot be moved (another process has the file locked) ⇒ write the new table anyway (never let a single
        broken file stop the whole registry), but be loud: state plainly "could not move it, the content is not
        preserved".
        ⚠️ an error message must never lie: this cell must never show "moved it"."""
        table, bad = scv.spath("children.json"), scv.spath("children.json.bad")
        self.addCleanup(lambda: bad.unlink() if bad.exists() else None)
        self.addCleanup(scv._children_save, [])
        if bad.exists():
            bad.unlink()
        table.write_bytes(b"not json")
        real = os.replace

        def replace(src, dst):
            if os.fspath(dst) == os.fspath(bad):
                raise PermissionError(32, "另一个程序正在使用此文件")
            return real(src, dst)

        with mock.patch.object(scv.os, "replace", side_effect=replace):
            _rc, lines, _o, _e = log_lines_during(lambda: scv._children_save([]))
        said = [x for x in lines if str(bad) in x]
        self.assertEqual(len(said), 1, lines)
        self.assertIn("could not move it", said[0])
        self.assertIn("另一个程序正在使用此文件", said[0])
        self.assertNotIn("moved the old copy", said[0])
        self.assertEqual((json.loads(table.read_text(encoding="utf-8")), bad.exists()), ([], False))

    def test_reading_a_broken_table_does_not_move_it(self):
        """The move happens on the write side: among the readers are `/healthz` (which needs no token) and doctor
        (which only reports facts) -- neither of them may ever touch the disk."""
        table, bad = scv.spath("children.json"), scv.spath("children.json.bad")
        self.addCleanup(lambda: bad.unlink() if bad.exists() else None)     # ⚠️ register first, run later: it must run after the table write below
        self.addCleanup(scv._children_save, [])
        if bad.exists():
            bad.unlink()
        table.write_bytes(b"not json")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(scv.children(), [])
        self.assertEqual((table.read_bytes() if table.exists() else None, bad.exists()), (b"not json", False))

    def test_a_failed_table_write_does_not_stop_the_sweep(self):
        """L447 ruling: that `_children_save` call is bookkeeping, never the action itself ⇒ if it cannot write,
        record one prominent log line and carry on, never blow up startup."""
        kid = sleeper()
        self.addCleanup(bury, kid)
        self.assertTrue(scv.child_add(kid.pid, "codex"))
        self.addCleanup(scv._children_save, [])
        with mock.patch.object(scv, "_children_save", side_effect=OSError("disk full")):
            _rc, lines, _o, _e = log_lines_during(scv.sweep_orphans)
        self.assertTrue(died(kid))                                 # the action still gets done
        self.assertEqual(len([x for x in lines if "disk full" in x]), 1, lines)


class TmpSweep(_Staged):
    def test_startup_clears_buffers_left_by_dead_processes(self):
        dead = sleeper()
        bury(dead)
        live = sleeper()
        self.addCleanup(bury, live)
        tmp = scv.state_dir() / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        names = {"config.json.%d" % dead.pid: False, "latest.json.%d" % live.pid: True,
                 "children.json.%d" % os.getpid(): True, "stray.txt": True}
        for n in names:
            (tmp / n).write_text("x", encoding="utf-8")
        try:
            scv.sweep_tmp()
            self.assertEqual({n: (tmp / n).exists() for n in names}, names)
        finally:
            for n in names:
                with contextlib.suppress(OSError):
                    (tmp / n).unlink()

    def test_a_failed_write_cleans_its_own_buffer(self):
        """B9: `save_config`/`_children_save`'s `os.replace` used to have not a single `except` around it ⇒ leaks
        `tmp/<name>.<pid>`."""
        cfg = scv.load_config()
        for name, write in (("config.json", lambda: scv.save_config(cfg)),
                            ("children.json", lambda: scv._children_save([]))):
            with self.subTest(name=name):
                with mock.patch.object(scv.os, "replace", side_effect=OSError("locked")):
                    with self.assertRaises(OSError):
                        write()
                self.assertFalse(scv._tmp_path(name).exists())

    def test_every_buffered_write_goes_through_one_place(self):
        """⭐ the real fix is making sure there is only one place (`_atomic_write`), never re-implementing the
        wind-down a third time somewhere else: `latest.json`'s write site has no behavioral test of its own, and it
        relies entirely on this one. The `os.replace` inside `_append_capped` is a rotation (moving the whole log
        file), never a write buffer. The one in `_children_keep_bad` moves a broken registry aside as-is to preserve
        evidence (ledger L110 M1, an explicitly written exception), and it is not a write buffer either.
        ⭐ Task 13: the "write to a buffer -> swap it in -> clean up after yourself on failure" piece was split out of
          `_atomic_write` into `_replace_file(tmp, dest, data)` -- `scv update` needs to swap the installed scv.py
          itself (which is not in the state directory, so its buffer can only sit right next to it) the same way, and
          must never re-implement this wind-down a third time somewhere else; files in the state directory still go
          through `_atomic_write` (which now only accounts for two paths). Who is allowed to call `_replace_file` is
          pinned by
          tests/test_95_setup_pair_update.py::Doors::test_the_whole_file_replace_door_has_two_callers."""
        with io.open(SCRIPT, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        where = set()
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef):
                for n in ast.walk(fn):
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "replace" \
                            and getattr(n.func.value, "id", "") == "os":
                        where.add(fn.name)
        self.assertEqual(where, {"_replace_file", "_append_capped", "_children_keep_bad"})


# ━━ Time-driven reaping (A6)
class GcByTheClock(_Staged):
    def test_idle_sessions_are_reaped_without_anyone_asking(self):
        """L723: `gc_idle()` used to only ever be called the moment a new session was built ⇒ with no one coming, it
        never shrinks at all."""
        b, _port, _tok = helpers.start_bridge()
        self.addCleanup(b.stop)
        b.sessions.run(session_id="idle-1", model_id="claude/haiku", effort=None, system="s",
                       messages=[{"role": "user", "content": "甲"}], on_started=lambda ms: None,
                       on_delta=lambda s: None, cancel=None)
        self.assertEqual(b.sessions.counts()["sessions"], 1)
        b.sessions._sessions["idle-1"].last_used -= scv.SESSION_IDLE_S + 1
        stop = threading.Event()
        old, scv.GC_EVERY_S = scv.GC_EVERY_S, 0.2
        t = threading.Thread(target=scv.serve_until, args=(b, stop), daemon=True)
        try:
            t.start()
            end = time.time() + 10
            while time.time() < end and b.sessions.counts()["sessions"]:
                time.sleep(0.1)
            self.assertEqual(b.sessions.counts()["sessions"], 0)
        finally:
            stop.set()
            t.join(10)
            scv.GC_EVERY_S = old

    def test_a_failed_reap_does_not_take_the_bridge_down(self):
        """It is background maintenance, never any particular request: if one reap blows up, the bridge must keep
        running, reap again on the next tick, and be loud about it."""
        sessions = mock.Mock()
        sessions.gc_idle.side_effect = [RuntimeError("the reap blew up")] + [0] * 1000
        stop = threading.Event()
        old, scv.GC_EVERY_S = scv.GC_EVERY_S, 0.1
        t = threading.Thread(target=scv.serve_until, args=(mock.Mock(sessions=sessions), stop), daemon=True)
        try:
            _rc, lines, _o, _e = log_lines_during(lambda: (t.start(), time.sleep(2.5)))
            self.assertTrue(t.is_alive())
            self.assertGreaterEqual(sessions.gc_idle.call_count, 2)
            self.assertEqual(len([x for x in lines if "the reap blew up" in x]), 1, lines)
        finally:
            stop.set()
            t.join(10)
            scv.GC_EVERY_S = old


# ━━ The ones that start a subprocess (really running `python scv.py start`)
class Lifecycle(_Staged):
    def setUp(self):
        super().setUp()
        self.port = free_port()
        cfg = scv.load_config()
        self.saved = dict(cfg)
        cfg.update(port=self.port, **fake_bins(HOME))
        scv.save_config(cfg)
        self.bridges = set()        # every bridge (pid, birth id) seen in this case: wind-down is checked against
                                     #   this, never against whatever bridge.pid holds at the moment of wind-down
        self.addCleanup(scv.save_config, self.saved)
        self.addCleanup(self.stop_for_sure)

    def stop_for_sure(self):
        """Wind-down must never fail silently: nobody used to check this `scv stop` call's return code ⇒ if it did
        not stop, the next case would run into this bridge and go red on some unrelated line ("bridge pid N is still
        alive, but port M does not answer" -- the negative-control round actually hit this once: on a busy machine,
        the wind-down's stop call, 40 seconds later, honestly said "did not stop"). ⇒ stop it again following the
        product's own suggested next step, and if that still fails, tree-kill it directly by pid (never drag the next
        case down with it), then fail this case: the failure must be recorded on the case where it actually happened.
        🔴 checking only rc is not enough (Task 12 review M-8): when `scv stop` lied (rc=0, said "stopped", but never
          actually killed it), it used to just return ⇒ that round leaked 6 background bridges on the dev machine.
          ⇒ after rc=0, check again: for every bridge seen in this case, the pid no longer matches that birth id.
          ⚠️ check against "the ones seen", never against whatever bridge.pid holds at the moment of wind-down: a
          lying stop would delete the pid file along with everything else."""
        rc, out, err = self.scv("stop")
        alive = sorted(b for b in self.bridges if scv.proc_start_id(b[0]) == b[1])
        if rc == 0 and not alive:
            return
        self.scv("stop")
        for pid, born in self.bridges:
            if scv.proc_start_id(pid) == born:
                scv.kill_pid_tree(pid)
        self.fail("wind-down's `scv stop` did not succeed (rc=%d, bridges still alive afterward %s): %s" % (rc, alive, (out + err)[-400:]))

    def scv(self, *argv, env=None, timeout=90):
        """⭐ the proxy variables are always stripped first, then set from `env`: if this machine (or CI) has a proxy
        set globally, "which process saw what" could no longer be told apart.
        ⭐ after every call, record whatever bridge is in the pid file on disk (`stop_for_sure` checks against
        this)."""
        base = {k: v for k, v in os.environ.items() if k.upper() not in PROXY_KEYS}
        full = dict(base, SCV_HOME=HOME, PYTHONIOENCODING="utf-8", **(env or {}))
        helpers.refuse_default_port(full["SCV_HOME"])       # A12: the subprocess cannot see the in-process dialing gate
        p = subprocess.run([sys.executable, SCRIPT] + list(argv), capture_output=True, timeout=timeout, env=full)
        rec = scv._read_pid_file() or {}
        if isinstance(rec.get("pid"), int) and helpers.born_alive(rec.get("born")):
            self.bridges.add((rec["pid"], rec["born"]))
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")

    def test_start_status_stop(self):
        rc, out, err = self.scv("start")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(scv.local_get(self.port, "/healthz")["version"], scv.VERSION)
        rc, out, _err = self.scv("start")
        self.assertIn("already running", out)
        rc, out, _err = self.scv("status")
        self.assertEqual((rc, json.loads(out)["version"]), (0, scv.VERSION))
        rc, out, err = self.scv("stop")
        self.assertEqual(rc, 0, out + err)
        end = time.time() + 10
        while time.time() < end and scv.local_get(self.port, "/healthz") is not None:
            time.sleep(0.3)
        self.assertIsNone(scv.local_get(self.port, "/healthz"))
        self.assertFalse(pid_file().exists())
        rc, out, _err = self.scv("status")
        self.assertEqual(rc, 1)
        self.assertIn("not running", out)

    def test_zero_quota_commands_start_no_session(self):
        """🔴 13c Fix 1b (a check the maintainer added): every session codex starts (thread/start) prewarms once and
        connects to the inference endpoint ⇒ not a single zero-cost command may ever start a session.
        This goes through the user's own entry point (really starting the subprocess `scv …`, with config.json's
        `claude_bin`/`codex_bin` pointing at the fake CLI): `start` (starts the bridge: detection, catalog) ->
        `status` -> `token` -> `doctor` -> `setup` -> `stop`. The judgment follows the shape of the bug, never the one
        concrete scene "codex's app-server": during this stretch, both families' fake-CLI records must only ever be
        quick commands (the stub records them as `quick`); not a single session record (the stub records these as
        `argv`: one call for claude, `app-server` for codex) is allowed.
        A positive control is welded into the same case: the same ruler can see both families starting a session
          under `scv doctor --live` (otherwise a blind ruler would also run all green).
        The full table of "who can reach all the way to starting a session" is pinned by
          tests/test_00_budget.py::Budget::test_only_real_requests_and_doctor_live_start_a_cli_session.
        🔴 the boundary (what the ruler cannot see): it can only see processes started through `cli_head(cfg)` -- the
          fake CLI is handed down through config.json's `claude_bin`/`codex_bin`; the day some path routes around
          `cli_head` (calling `shutil.which` directly, or a hardcoded path), what it would start is a real CLI, the
          stub would record nothing at all, and this case would still run green (outside 13c's review scope). Every
          path today goes through it (the line below, "both families' `quick` were both recorded", proves the
          subprocess really did use the stub); that AST gate is what governs "who may call the door"."""
        def sessions(mark):
            return [r for r in helpers.fake_log_since(mark) if "argv" in r and "console" not in r]

        mark = len(helpers.read_fake_log())
        for argv in (("start",), ("status",), ("token",), ("doctor",), ("setup",), ("stop",)):
            rc, out, err = self.scv(*argv)
            self.assertEqual(rc, 0, (argv, (out + err)[-600:]))
        self.assertEqual(sessions(mark), [])
        self.assertEqual({r.get("family") for r in helpers.fake_log_since(mark) if "quick" in r}, {"claude", "codex"})
        mark = len(helpers.read_fake_log())
        rc, out, err = self.scv("doctor", "--live")
        self.assertEqual({r["family"] for r in sessions(mark)}, {"claude", "codex"}, (out + err)[-600:])

    def test_start_sweeps_what_the_last_bridge_left_and_stop_sweeps_again(self):
        """A5: `sweep_orphans` used to have zero production call sites. One case pins down each of the two call
        sites: one on bridge startup, one on `scv stop`'s wind-down."""
        before = sleeper()
        self.addCleanup(bury, before)
        self.assertTrue(scv.child_add(before.pid, "claude"))
        rc, out, err = self.scv("start")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(died(before), "did not sweep last time's leftover when starting the bridge")
        after = sleeper()
        self.addCleanup(bury, after)
        self.assertTrue(scv.child_add(after.pid, "codex"))
        rc, out, err = self.scv("stop")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(died(after), "did not sweep during scv stop's wind-down")

    def test_stop_sweeps_the_work_directory_a_hard_kill_left_behind(self):
        """I-1: on Windows `stop` always hard-kills the bridge (there is no SIGTERM path) ⇒ `cmd_run`'s own
        `finally: bridge.stop()` never runs, so a still-alive session's `work/<hash>-<rand>` directory (system.txt
        and all) never gets removed on its own. Fixture: start, one call with a session id (keeps that CLI alive,
        so its directory is still there the moment `stop` hard-kills it), stop, start again ⇒ no session-shaped
        directory survives either sweep; a `work/probe`-named entry and a `doctor --live`-shaped `work/doctor-<hex>`
        entry (standing in for one running at the exact same moment) are both left untouched throughout."""
        rc, out, err = self.scv("start")
        self.assertEqual(rc, 0, out + err)
        status, _h, _body = helpers.http("POST", self.port, "/v1/chat/completions", token=scv.load_config()["local_token"],
                                         body={"model": "claude/haiku", "session": "keep-alive-1",
                                               "messages": [{"role": "user", "content": "甲"}]})
        self.assertEqual(status, 200)
        work = scv.spath("work")
        session_dirs = [p.name for p in work.iterdir() if p.is_dir() and scv.SESSION_WORKDIR_RE.fullmatch(p.name)]
        self.assertEqual(len(session_dirs), 1, list(work.iterdir()))   # fixture precondition: the call left one directory behind
        doctor_marker = work / "doctor-ab12cd34"
        doctor_marker.mkdir(exist_ok=True)
        (work / "probe").mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, doctor_marker, True)
        self.addCleanup(shutil.rmtree, work / "probe", True)
        rc, out, err = self.scv("stop")
        self.assertEqual(rc, 0, out + err)
        after_stop = {p.name for p in work.iterdir() if p.is_dir()}
        self.assertEqual([n for n in after_stop if scv.SESSION_WORKDIR_RE.fullmatch(n)], [], after_stop)
        self.assertEqual({"probe", "doctor-ab12cd34"} & after_stop, {"probe", "doctor-ab12cd34"}, after_stop)
        rc, out, err = self.scv("start")
        self.assertEqual(rc, 0, out + err)
        after_start = {p.name for p in work.iterdir() if p.is_dir()}
        self.assertEqual([n for n in after_start if scv.SESSION_WORKDIR_RE.fullmatch(n)], [], after_start)
        self.assertEqual({"probe", "doctor-ab12cd34"} & after_start, {"probe", "doctor-ab12cd34"}, after_start)

    def test_the_bridge_reports_the_proxies_it_sees_and_masks_them(self):
        """§8: doctor reports the proxy variables the bridge process itself actually sees -- never doctor's own
        process (the two can be started from two different terminals)."""
        rc, out, err = self.scv("start", env={"HTTPS_PROXY": "http://u:secretpw@10.9.9.9:1"})
        self.assertEqual(rc, 0, out + err)
        rc, out, _err = self.scv("doctor")
        facts, _end = json.JSONDecoder().raw_decode(out)
        self.assertEqual(facts["bridge_proxy_env"].get("HTTPS_PROXY"), "http://***@10.9.9.9:1")
        self.assertEqual(facts["proxy_env"].get("HTTPS_PROXY"), None)   # doctor's own process never had it set ⇒ the two copies can be told apart
        self.assertNotIn("secretpw", out + pid_file().read_text(encoding="utf-8"))

    def test_doctor_in_a_clean_terminal_reports_what_the_bridge_started_from_an_agent_session_strips(self):
        """15c review I2, end to end: the bridge is started from an environment carrying fake session variables
        (= an agent running `start` on someone's behalf), and doctor runs in this harness's own environment ⇒ what
        gets reported is the bridge's own copy.
        ⚠️ precondition: this harness process's own environment has none of these names (pinned down by the line
        below first), otherwise whose is whose cannot be told apart."""
        fake = {"CLAUDE_CODE_SESSION_ID": "fake-sess-id", "AI_AGENT": "fake-agent", "CLAUDE_CODE_EFFORT_LEVEL": "fake-effort"}
        self.assertEqual([k for k in fake if k in os.environ], [])
        rc, out, err = self.scv("start", env=fake)
        self.assertEqual(rc, 0, out + err)
        rc, out, _err = self.scv("doctor")
        facts, _end = json.JSONDecoder().raw_decode(out)
        self.assertTrue(facts["env"]["whose"].startswith("the bridge that is running (pid "), facts["env"])
        self.assertEqual([k for k in ("AI_AGENT", "CLAUDE_CODE_SESSION_ID") if k not in facts["env"]["stripped"]], [], facts["env"])
        self.assertIn("CLAUDE_CODE_EFFORT_LEVEL", facts["env"]["changes"])
        self.assertEqual([v for v in fake.values() if v in out + pid_file().read_text(encoding="utf-8")], [])   # never a value

    def test_the_remote_leg_comes_up_inside_scv_run(self):
        """A7: `start_remote()` used to have zero production callers, and Task 11's end-to-end cases all started the
        bridge in-process ⇒ this case proves it can really come up on the subcommand path."""
        d = Dispatcher()
        url = d.start()
        self.addCleanup(d.stop)
        cfg = scv.load_config()
        cfg.update(remote_url=url, remote_token=d.token)
        scv.save_config(cfg)
        rc, out, err = self.scv("start")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(d.wait(lambda: d.hellos and d.connects, 20), (d.hellos, d.connects))
        self.assertEqual(d.hellos[0]["bridge_version"], scv.VERSION)
        rc, out, _err = self.scv("status")
        self.assertEqual(json.loads(out)["remote"]["state"], "streaming")

    @unittest.skipUnless(os.name == "nt", "console windows are a win32 thing")
    def test_the_background_bridge_has_a_console_and_it_is_hidden(self):
        """Layer 1: the bridge has a console (never the DETACHED kind of "has none"), and that console's window is
        invisible.
        How it is judged: start a separate small process (also with no window) that calls
        `AttachConsole(the bridge's pid)` -- it cannot attach if the bridge has no console."""
        rc, out, err = self.scv("start")
        self.assertEqual(rc, 0, out + err)
        pid = json.loads(pid_file().read_text(encoding="utf-8"))["pid"]
        p = subprocess.run([sys.executable, "-c", CONSOLE_PROBE, str(pid)], capture_output=True, timeout=30,
                           creationflags=0x08000000)
        got = json.loads(p.stdout.decode("utf-8"))
        self.assertEqual((got["attached"], got["visible"]), (True, False), got)

    @unittest.skipUnless(os.name == "nt", "console windows are a win32 thing")
    def test_nothing_the_background_bridge_starts_opens_a_window(self):
        """End to end: every CLI subprocess the background bridge starts (here, the detect probe calls) sees its own
        console window as invisible.
        ⚠️ this case is a reading of the two layers combined: removing either layer alone still runs green (see the
        class description); it only proves that removing both together goes red."""
        mark = len(helpers.read_fake_log())
        rc, out, err = self.scv("start", env={"FAKE_CONSOLE": "1"})
        self.assertEqual(rc, 0, out + err)
        rows = [r for r in helpers.fake_log_since(mark) if "console" in r]
        self.assertGreaterEqual(len(rows), 3, rows)                 # the ruler is not blind: detect's calls really got recorded
        self.assertEqual([r for r in rows if r["console"]["visible"]], [])

    def test_doctor_survives_a_pipe_that_is_not_utf8(self):
        """🔴 under a pipe/redirect, stdout uses the locale encoding (gbk on Chinese Windows) in strict mode ⇒ a
        single ✅ is enough to raise `UnicodeEncodeError` -- and an agent reading doctor's output happens to always
        go through a pipe. ⚠️ on a machine whose locale encoding is already UTF-8 (CI), this case is vacuous; on this
        machine (cp936), removing that `reconfigure` call goes red."""
        env = {k: v for k, v in os.environ.items() if k.upper() not in ("PYTHONIOENCODING", "PYTHONUTF8")}
        helpers.refuse_default_port(HOME)                   # A12
        p = subprocess.run([sys.executable, SCRIPT, "doctor"], capture_output=True, timeout=90,
                           env=dict(env, SCV_HOME=HOME, PYTHONUTF8="0"))
        self.assertNotIn(b"UnicodeEncodeError", p.stderr)
        self.assertIn(p.returncode, (0, 1), p.stderr[-800:])
        self.assertTrue(p.stdout.startswith(b"{"), p.stdout[:200])

    def test_start_explains_when_the_bridge_dies_on_the_way_up(self):
        """The port is occupied by something that is not the bridge ⇒ the subprocess cannot bind it and quits right
        away ⇒ never sit idle for 60 seconds before saying "did not come up"."""
        with contextlib.redirect_stderr(io.StringIO()):
            for _ in range(5):                                    # pad in a few long lines first: the log's tail must exceed the single-line cap for "cut from the tail" to be observable
                scv.log("padding a long line: " + "x" * 1500)
        with socket.socket() as s:
            s.bind(("127.0.0.1", self.port))
            s.listen(1)
            t0 = time.time()
            rc, out, err = self.scv("start")
        self.assertEqual(rc, 1, out + err)
        self.assertLess(time.time() - t0, 45)
        self.assertIn("started and quit right away", err)
        # ⭐ the cause-of-death line (this run's own port) must reach the user's eyes as-is: it used to be stuffed
        #   into `log()`'s line, and the single-line cap cut it from the tail (the lines left by the previous case
        #   were themselves long, and this trailing sentence happened to be the one cut -- the negative-control round
        #   actually hit this)
        self.assertIn("the local API could not start (port %d)" % self.port, err)
        self.assertEqual(helpers.door_lines_missing(err, "run"), [], err)     # the next step (13b: the pasteable line)


# ━━ The background bridge must never make a black window flash on the user's desktop (maintainer's addendum:
#   the user saw it happen on their own screen)
# The names under `subprocess` that never start a process (constants, exceptions, result objects). ⭐ a closed
#   list: except for these, anything under `subprocess.` that gets called or taken counts as a place that starts a
#   subprocess -- `call`/`check_output`/`getoutput` open a window just like `Popen` does.
SUBPROCESS_INERT = {"PIPE", "DEVNULL", "STDOUT", "CompletedProcess", "SubprocessError", "TimeoutExpired",
                    "CalledProcessError"}

# Explicitly written exceptions: functions allowed to skip that door (bare), plus why. Every one of them must have a
#   reason; one more bare spot beyond these makes the structural gate go red: either route it through
#   `new_session_kw()`, or write here exactly why it is exempt.
# ⚠️ Task 13 once registered an exception here for "must inherit the user's terminal" (the interactive codex login
#   subcommand); Task 13c removed that subcommand (a hard constraint from the user: never log in again on scv's
#   behalf), and the exception was withdrawn along with it -- today, not a single place that starts a subprocess
#   inherits the terminal.
#   ⚠️ if this kind of exception is ever opened again in the future, the reason only ever holds for a foreground,
#   interactive path: nothing the background bridge starts may ever be registered here (that is exactly where the
#   flashing window comes from).
BARE_OK = {"_birth_once": "`ps -o lstart=`, runs only on POSIX: there is no such thing as a console window there",
           "proc_rss_kb": "`ps -o rss=`, same as above"}


def os_spawner(name):
    """The family under `os.` that starts a process: system/popen/startfile/spawn*/posix_spawn*/exec* (plus POSIX's
    fork/forkpty)."""
    return name in ("system", "popen", "startfile", "fork", "forkpty") or name.startswith(("spawn", "posix_spawn", "exec"))


def spawn_sites(tree):
    """Every place in scv.py that starts a subprocess ⇒ `Counter{(the function it's in, which path it takes): count}`:
    door = spread with `**new_session_kw()`; explicit = wrote its own `creationflags`/`start_new_session`; bare =
    neither; ref = never called on the spot, but taken instead (`functools.partial(subprocess.Popen, …)`,
    `getattr(subprocess, …)`, `sp = subprocess`) -- what flags it carries once taken cannot be seen from here.
    ⭐ the net (Task 12 review M-4): anything at all under `subprocess.` except `SUBPROCESS_INERT`, union the family
      that starts a process under `os.`; aliases are recognized together (`import subprocess as sp`, `sp = subprocess`,
      `from subprocess import Popen as P`, `P = subprocess.Popen`, chained ones too).
    ⭐ counted by number of spots (Counter), never by set: one more spot in the same function (say, adding another
      DETACHED call inside `spawn_detached`) must also go red.
    ⚠️ type annotations do not count (`proc: subprocess.Popen` is just a type; scv.py has
      `from __future__ import annotations` on, so annotations are never evaluated).
    ⚠️ boundary: no scoping (wherever a name is bound as an alias, the whole file counts it as an alias -- this can
      only over-report); reflection like `__import__`/`importlib`/`sys.modules` cannot be seen ("entering the door"
      is governed by `test_the_import_list_is_pinned`); ctypes's CreateProcess is governed by the native-code
      door."""
    sp, osn, direct = {"subprocess"}, {"os"}, set()
    typing = set()                      # every node inside an annotation (by id)
    for n in ast.walk(tree):
        notes = []
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            notes = [n.returns] + [x.annotation for x in ast.walk(n.args) if isinstance(x, ast.arg)]
        elif isinstance(n, ast.AnnAssign):
            notes = [n.annotation]
        for a in [a for a in notes if a is not None]:
            typing.update(id(x) for x in ast.walk(a))
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == "subprocess":
                    sp.add(a.asname or a.name)
                elif a.name == "os" or (a.name.startswith("os.") and not a.asname):
                    osn.add(a.asname or "os")
        elif isinstance(n, ast.ImportFrom) and n.module in ("subprocess", "os"):
            for a in n.names:
                live = (a.name not in SUBPROCESS_INERT) if n.module == "subprocess" else os_spawner(a.name)
                if live:
                    direct.add(a.asname or a.name)

    def spawner(e):
        if isinstance(e, ast.Name):
            return e.id in direct
        return isinstance(e, ast.Attribute) and isinstance(e.value, ast.Name) and (
            (e.value.id in sp and e.attr not in SUBPROCESS_INERT) or (e.value.id in osn and os_spawner(e.attr)))

    grew = True
    while grew:         # an alias of an alias: `a = subprocess` followed by `b = a`
        grew = False
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and n.value is not None:
                for t in [t for t in n.targets if isinstance(t, ast.Name)]:
                    v = n.value
                    for pool, hit in ((sp, isinstance(v, ast.Name) and v.id in sp),
                                      (osn, isinstance(v, ast.Name) and v.id in osn), (direct, spawner(v))):
                        if hit and t.id not in pool:
                            pool.add(t.id)
                            grew = True
    out = Counter()

    def kind(call):
        kws = call.keywords
        if any(k.arg is None and isinstance(k.value, ast.Call) and getattr(k.value.func, "id", "") == "new_session_kw"
               for k in kws):
            return "door"
        return "explicit" if any(k.arg in ("creationflags", "start_new_session") for k in kws) else "bare"

    def visit(node, fn):
        if id(node) in typing:
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node.name
        kids = list(ast.iter_child_nodes(node))
        if isinstance(node, ast.Call) and spawner(node.func):
            out[(fn, kind(node))] += 1
            kids.remove(node.func)
        elif spawner(node) and isinstance(node.ctx, ast.Load):
            out[(fn, "ref")] += 1
            kids = []
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            kids = []                          # `subprocess.PIPE`, `os.path`: the module name as an attribute's base, ordinary usage
        elif isinstance(node, ast.Name) and node.id in sp | osn and isinstance(node.ctx, ast.Load):
            out[(fn, "ref")] += 1              # the module itself was taken
        for c in kids:
            visit(c, fn)

    visit(tree, "<module>")
    return out


CONSOLE_PROBE = (
    "import ctypes, json, sys; k = ctypes.WinDLL('kernel32', use_last_error=True); u = ctypes.WinDLL('user32'); "
    "k.GetConsoleWindow.restype = ctypes.c_void_p; k.FreeConsole(); ok = k.AttachConsole(int(sys.argv[1])); "
    "err = ctypes.get_last_error(); h = k.GetConsoleWindow() if ok else None; "
    "print(json.dumps({'attached': bool(ok), 'err': err, 'hwnd': h or 0, "
    "'visible': bool(h) and bool(u.IsWindowVisible(ctypes.c_void_p(h)))}))")


class NoConsoleWindows(unittest.TestCase):
    """🔴 `DETACHED_PROCESS` leaves the background bridge with no console at all ⇒ every console-based child process
    it starts (cmd/node/codex/taskkill/python) gets a brand-new, visible window opened for it by the system: while
    this bridge runs, it keeps flashing on the user's desktop. Two layers to the fix (reasons in
    `scv.new_session_kw`): (1) the bridge itself gets a hidden console (`spawn_detached` uses `CREATE_NO_WINDOW`)
    (2) every subprocess carries `CREATE_NO_WINDOW`.
    ⚠️ the two layers mask each other: with only one layer left, the end-to-end case still runs green ⇒ each layer
      has its own test, and the end-to-end case only proves "the two together".
    ⚠️ boundary: whether a window really pops up is something no test can assert; what can be asserted is the flag
      bit (here), whether the bridge has a console and whether that console's window is visible, and whether the
      console window each subprocess itself sees is visible (the two cases in `Lifecycle`, which run only on
      win32)."""

    def test_every_spawn_site_goes_through_the_no_window_door(self):
        with io.open(SCRIPT, encoding="utf-8") as f:
            sites = spawn_sites(ast.parse(f.read()))
        # ⭐ the three spots in `spawn_detached` write their own flags (what it wants is "the bridge has a hidden
        #   console", reasons are with it); bare is only ever allowed for the ones listed in `BARE_OK`
        want = Counter({("run_cli", "door"): 1, ("__init__", "door"): 1, ("kill_pid_tree", "door"): 1,
                        ("spawn_detached", "explicit"): 3}) + Counter({(fn, "bare"): 1 for fn in BARE_OK})
        self.assertEqual(sites, want)
        self.assertEqual([fn for fn, why in BARE_OK.items() if not why], [])      # every exception carries its reason

    def test_the_door_scanner_is_not_blind(self):
        """Positive control: every way of writing a process start, every alias, must be counted; anything that never
        starts a process (constants, result objects, some other module's `.run`) must never be counted, not even
        once."""
        probe = ast.parse(NL.join((
            "import subprocess as sp", "import os as o", "from subprocess import check_output as co", "from os import system",
            "def f():", "    subprocess.Popen(['x'])",
            "def g():", "    subprocess.run(['x'], **new_session_kw())",
            "def h():", "    subprocess.Popen(['x'], creationflags=1)",
            "def i():", "    other.run(['x']); subprocess.CompletedProcess([], 0); x = subprocess.PIPE; os.path.join('a')",
            "def j():", "    subprocess.call(['x']); subprocess.check_output(['x']); subprocess.getoutput('x')",
            "def k():", "    os.system('x'); os.popen('x'); os.startfile('x'); os.spawnv(0, 'x', []); "
                        "os.posix_spawn('x', [], {}); os.execv('x', [])",
            "def l():", "    sp.Popen(['x']); o.system('x'); co(['x']); system('x')",
            "def m():", "    s2 = subprocess", "    s2.run(['x'])", "    P = subprocess.Popen", "    P(['x'], **new_session_kw())",
            "def n():", "    functools.partial(subprocess.Popen, ['x']); getattr(subprocess, 'Popen')",
            "def p():", "    subprocess.Popen(['x'], creationflags=1); subprocess.Popen(['y'], creationflags=8)",
            "def q(proc: subprocess.Popen, *more: subprocess.Popen) -> subprocess.Popen:", "    t: subprocess.Popen = None")))
        self.assertEqual(spawn_sites(probe), Counter({
            ("f", "bare"): 1, ("g", "door"): 1, ("h", "explicit"): 1, ("j", "bare"): 3, ("k", "bare"): 6,
            ("l", "bare"): 4, ("m", "ref"): 2, ("m", "bare"): 1, ("m", "door"): 1, ("n", "ref"): 2, ("p", "explicit"): 2}))

    @unittest.skipUnless(os.name == "nt", "console windows are a win32 thing")
    def test_the_door_carries_no_window_on_win32(self):
        self.assertEqual(scv.new_session_kw(), {"creationflags": 0x08000000})

    @unittest.skipUnless(os.name == "nt", "console windows are a win32 thing")
    def test_the_bridge_gets_a_hidden_console_not_none(self):
        """Both paths (breaking away from the Job succeeded / failed) must both be `CREATE_NO_WINDOW`, and neither
        may ever carry `DETACHED_PROCESS` (0x8)."""
        for fail_first in (False, True):
            with self.subTest(breakaway_refused=fail_first):
                seen = []

                def fake(argv, **kw):
                    seen.append(kw["creationflags"])
                    if fail_first and len(seen) == 1:
                        raise OSError(5, "access denied")
                    return mock.Mock(pid=1)

                with mock.patch.object(scv.subprocess, "Popen", side_effect=fake), \
                        contextlib.redirect_stderr(io.StringIO()):
                    scv.spawn_detached(["x"])
                last = seen[-1]
                self.assertEqual((last & 0x08000000, last & 0x8), (0x08000000, 0))
                self.assertEqual(bool(last & 0x01000000), not fail_first)


# ━━ Self-referring commands (13b): a string meant for a person to see must never contain a bare `scv <subcommand>`
#   again -- after installation, `scv` is not on PATH, so pasting it as-is is "not an internal or external command"
HOLE = chr(0)          # the stretch of a string build that could not be recognized (a variable, a call's return value...)
BARE_SELF = re.compile("(?<![A-Za-z0-9_.-])scv(?:[.]py)?['" + chr(34) + "]?[ ]+(?:[a-z][a-z0-9-]*|" + HOLE + ")")
PASTE_DOORS = ("paste_cmd", "self_cmd", "login_cmd")
# Who reads "what this installation actually looks like" (the interpreter, this file's path): only the door itself,
#   the one call that starts the background bridge, and the one spot where `update` swaps itself.
#   ⭐ pinned by number of spots (`Counter`): reading it one more time in the same function also goes red -- hand-
#   assembling `sys.executable + " " + __file__ + " stop"` to route around the door is exactly this shape.
INSTALL_READS = Counter({("self_cmd", "sys.executable"): 1, ("self_cmd", "__file__"): 1,
                         ("cmd_start", "sys.executable"): 1, ("cmd_start", "__file__"): 1, ("cmd_update", "__file__"): 1})


def _render(node, table):
    """The string this expression builds (the stretch that could not be recognized is replaced with `HOLE`); if it is
    not a string-building expression at all ⇒ `None`.
    Recognized: literals, string constants at the module's top level (`table`), `+`, `%` (positional and by name),
    f-strings, `.format` (positional, numbered, and keyword arguments are all substituted in),
    `sep.join(a literal list / a generator taken as-is)`, `print(multiple arguments, sep=…)` (joined by sep: it prints
    as one line), `chr(an integer)`."""
    r = functools.partial(_render, table=table)
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return table.get(node.id)
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else (r(v.value) or HOLE) for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = r(node.left), r(node.right)
        return None if a is None and b is None else (HOLE if a is None else a) + (HOLE if b is None else b)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and r(node.left) is not None:
        right = node.right
        named = {k.value: r(v) for k, v in zip(right.keys, right.values)
                 if isinstance(k, ast.Constant)} if isinstance(right, ast.Dict) else {}
        vals = iter([r(x) for x in (right.elts if isinstance(right, ast.Tuple) else [right])])

        def one(m):
            if m.group(0) == "%%":
                return "%"
            return (named.get(m.group(1)) if m.group(1) is not None else next(vals, None)) or HOLE
        return re.sub("%(?:[(]([^)]*)[)])?[-#0 +]*[0-9*]*(?:[.][0-9*]+)?[A-Za-z%]", one, r(node.left))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        head = r(node.func.value)
        if node.func.attr == "format" and head is not None:
            pos = [r(a) for a in node.args if not isinstance(a, ast.Starred)]
            kw = {k.arg: r(k.value) for k in node.keywords if k.arg}
            auto = iter(range(len(pos) + 1))

            def field(m):
                if m.group(0) in ("{{", "}}"):
                    return m.group(0)[0]
                name = m.group(1)
                if name == "":
                    name = str(next(auto))
                got = pos[int(name)] if name.isdigit() and int(name) < len(pos) else kw.get(name)
                return got or HOLE
            return re.sub("[{][{]|[}][}]|[{]([^{}!:]*)(?:![rsa])?(?::[^{}]*)?[}]", field, head)
        arg = node.args[0] if node.func.attr == "join" and node.args else None
        if isinstance(arg, (ast.GeneratorExp, ast.ListComp)) and len(arg.generators) == 1 and not arg.generators[0].ifs \
                and isinstance(arg.elt, ast.Name) and getattr(arg.generators[0].target, "id", None) == arg.elt.id:
            arg = arg.generators[0].iter                    # `w for w in (…)`: taken as-is, equal to the container itself
        if isinstance(arg, (ast.List, ast.Tuple)):
            parts = [r(x) for x in arg.elts]
            return (HOLE if head is None else head).join(HOLE if p is None else p for p in parts)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print" and node.args:
        sep = [k.value for k in node.keywords if k.arg == "sep"]
        sep = " " if not sep or (isinstance(sep[0], ast.Constant) and sep[0].value is None) else r(sep[0])
        parts = [r(a) for a in node.args]
        if all(p is None for p in parts):
            return None
        return (HOLE if sep is None else sep).join(HOLE if p is None else p for p in parts)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "chr" \
            and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, int):
        return chr(node.args[0].value)
    return None


def bare_self_commands(tree):
    """A bare self-referring command inside a string meant for a person to see ⇒ `[(line number, that phrase)]`.
    "Meant for a person to see" = every string built anywhere in the source, never a docstring (that is meant for
    someone reading the code, and a comment is not even in the AST to begin with). ⭐ built to the shape of the bug:
    look only after it has been built (`"scv " + "stop"`, `"scv %s" % sub`, `"%(p)s stop" % {…}`, an f-string,
    `.format` -- both the template and the arguments count (`"{} stop".format("scv")`, `"{p} stop".format(p="scv")`,
    13b review M1) -- `" ".join([...])`/`" ".join(w for w in (...))`, `print("…scv", "stop")` (multiple arguments
    joined by `sep` into one line, M1) are each their own way of missing it), and the stretch that could not be
    recognized (`HOLE`) sitting right after `scv ` also counts (never guess that it is not a subcommand); a
    hand-written one like `scv.py stop` counts too (the one the door computes only exists at runtime, and the source
    must never contain this shape).
    A phrase is recorded only on the smallest expression that built it (the line number is that line's own).
    Every way of writing it gets its own row in `test_the_bare_judge_sees_every_way_of_writing_it`.
    ⚠️ the false-alarm side (loud): something unrecognizable sitting right after `scv ` -- even if it actually is a
      version number (a string constant at the module's top level is recognized: the two `VERSION` spots do not go
      red); an English sentence "scv is ..." (the repo is all Chinese, 0 spots today).
    🔴 the boundary (cannot be seen, silent): built from a local variable inside a function (`m = "scv "; m + "stop"`),
      produced by something like `.replace`/`.upper()`, read in from a file or the network, expanded from
      `print(*a list)`/`.format(*a, **kw)`, `join` over a non-literal or a generator with a condition or
      transformation, written across several calls (`sys.stdout.write("scv ")` followed by writing `"stop"`)."""
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    table = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            v = _render(n.value, table)
            if v is not None and HOLE not in v:
                table[n.targets[0].id] = v
    said = {}
    for n in ast.walk(tree):
        text = None if id(n) in docs else _render(n, table)
        if text is not None:
            said[id(n)] = (n, {m.group(0) for m in BARE_SELF.finditer(text)})
    out = []
    for n, hits in said.values():
        inner = set().union(*[said[id(d)][1] for d in ast.walk(n) if d is not n and id(d) in said])
        out += [(n.lineno, h.replace(HOLE, "…")) for h in sorted(hits - inner)]
    return sorted(out)


def doors_in_templates(tree):
    """A door's output taken as a `%`/`.format` template ⇒ `[(line number, door)]`: a `%`/`{` inside a path would
    make formatting blow up on the spot, or quietly change the command."""
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mod):
            tmpl = n.left
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "format":
            tmpl = n.func.value
        else:
            continue
        out += [(c.lineno, c.func.id) for c in ast.walk(tmpl)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in PASTE_DOORS]
    return sorted(out)


def install_reads(tree):
    """`Counter((the function it's in, "sys.executable"|"__file__"))`: who is reading what this installation
    actually looks like."""
    found = Counter()

    def visit(node, fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node.name
        if isinstance(node, ast.Name) and node.id == "__file__":
            found[(fn, "__file__")] += 1
        if isinstance(node, ast.Attribute) and node.attr == "executable" and getattr(node.value, "id", "") == "sys":
            found[(fn, "sys.executable")] += 1
        for child in ast.iter_child_nodes(node):
            visit(child, fn)

    visit(tree, "<module>")
    return found


class SelfCommands(unittest.TestCase):
    """13b judgment 4: a string meant for a person to see must never contain a bare `scv <subcommand>` again, and
    every one must go through the one and only door `scv.self_cmd` (⭐ what the door computes is `sys.executable` plus
    this file's absolute path, and pasting it really works: `PasteInRealShells`). Three judgments, each with its own
    positive control: (1) the bare form (`bare_self_commands`) (2) a door's output taken as a formatting template
    (`doors_in_templates`) (3) routing around the door and reading `sys.executable`/`__file__` directly.
    ⚠️ the forms it cannot see are written in each function's own docstring; the lines that leave over the network
      (`fix_hint`, the local API's refusal body, `/healthz`) must never go through the door -- pinned by
      tests/test_70_local_api.py::PromisedCommands and tests/test_80_remote.py::TooOld."""

    @classmethod
    def setUpClass(cls):
        with io.open(SCRIPT, encoding="utf-8") as f:
            cls.tree = ast.parse(f.read())

    def test_no_bare_self_command_in_anything_a_person_reads(self):
        self.assertEqual(["scv.py:%d：%s" % x for x in bare_self_commands(self.tree)], [])

    def test_the_bare_judge_sees_every_way_of_writing_it(self):
        """Positive control: one line per way of writing it (never put two on the same line: that would make it
        impossible to tell which one was recognized); the false-alarm-side lines must never go red."""
        probe = NL.join((
            'VERSION = "0.1.0"', 'NAME = "scv "',                                    # 1-2
            'def f(sub, x):',                                                          # 3
            '    """跑 `scv stop`：docstring 给读代码的人看，⛔算"""',                     # 4
            '    print("跑 `scv stop` 停桥")',                                            # 5 literal
            '    log("先跑 " + "scv " + "stop")',                                          # 6 concatenation
            '    a = "跑 scv %s" % "stop"',                                               # 7 % plus a literal
            '    b = "跑 scv %s" % sub',                                                  # 8 % plus something unrecognizable
            '    c = f"跑 scv {sub}"',                                                    # 9 f-string
            '    d = "跑 scv {}".format("stop")',                                         # 10 .format
            '    e = " ".join(["scv", "stop"])',                                          # 11 join
            '    g = "python scv.py stop"',                                               # 12 a hand-written scv.py
            '    h = NAME + "stop"',                                                      # 13 a module constant
            '    i = "scv codex-login"',                                                  # 14 a subcommand that no longer exists also counts
            '    # scv stop  注释不在 AST 里',                                             # 15
            '    j = "scv 停了（pid %d）" % 1',                                            # 16 false-alarm side: CJK
            '    k = "scv %s 起来了" % VERSION',                                           # 17 false-alarm side: recognized as a version number
            '    m = "用写它的那一版 scv 去停；myscv stop；scv-bridge x"',                  # 18 false-alarm side
            '    n = "跑：" + NL + self_cmd("stop")',                                    # 19 goes through the door
            # 13b review M1: `.format`'s arguments and `print`'s multiple arguments used to never get substituted in
            #   (silently green on the real file)
            '    o = "要起就跑 {} stop".format("scv")',                                  # 20 .format: scv is a positional argument
            '    p = "{p} stop".format(p="scv")',                                        # 21 .format: a keyword argument
            '    q = "{} {}".format("scv", "stop")',                                     # 22 .format: both are arguments
            '    u = "{0} {1}".format("scv", "stop")',                                   # 23 .format: numbered
            '    print("要起就跑 scv", "stop")',                                          # 24 print with multiple arguments (sep defaults to one space)
            '    s = " ".join(w for w in ("scv", "stop"))',                              # 25 join over a generator taken as-is
            '    t = "%(p)s stop" % {"p": "scv"}',                                       # 26 % by name
            '    print("scv", "stop", sep="")'))                                         # 27 false-alarm side: sep="" ⇒ scvstop
        got = bare_self_commands(ast.parse(probe))
        self.assertEqual(got, [(5, "scv stop"), (6, "scv stop"), (7, "scv stop"), (8, "scv …"), (9, "scv …"),
                               (10, "scv stop"), (11, "scv stop"), (12, "scv.py stop"), (13, "scv stop"), (14, "scv codex-login"),
                               (20, "scv stop"), (21, "scv stop"), (22, "scv stop"), (23, "scv stop"), (24, "scv stop"),
                               (25, "scv stop"), (26, "scv stop")])

    def test_door_output_is_never_a_format_template(self):
        self.assertEqual(doors_in_templates(self.tree), [])
        probe = NL.join(('a = ("跑：" + self_cmd("stop") + " %s") % y',          # 1 red
                         'b = (login_cmd("codex", h) + "{}").format(y)',             # 2 red
                         'c = "跑：%s" % paste_cmd(w)',                            # 3 the door's output as an argument: fine
                         'd = "跑 %s：" % y + NL + self_cmd("run")'))               # 4 concatenated after formatting is done: fine
        self.assertEqual(doors_in_templates(ast.parse(probe)), [(1, "self_cmd"), (2, "login_cmd")])

    def test_only_the_door_reads_where_this_install_lives(self):
        self.assertEqual(install_reads(self.tree), INSTALL_READS)
        probe = "def cmd_x():" + NL + "    print(sys.executable + ' ' + __file__ + ' stop')"
        self.assertEqual(install_reads(ast.parse(probe)), Counter({("cmd_x", "sys.executable"): 1, ("cmd_x", "__file__"): 1}))

    def test_nothing_that_leaves_the_machine_calls_the_door(self):
        """A door's output carries a local absolute path (with the username in it) ⇒ none of the doors that leave
        this machine may ever call it: `fix_hint`/`error_body`/`error_payload` (goes into an API error body, or a
        remote leg's delivery), `hello_payload` (reported upstream), the local API's refusal body (`_refuse`, which
        can be received even without a matching token), `/healthz` (`health`, and the remote leg's `describe`, both
        readable without a token). ⚠️ this is a named list (the miss side: remember to add a new door that leaves the
        machine here); and it only governs who calls the door -- it cannot see a door's output being passed along
        through the data flow (codex's `blocked` carries `login_cmd`, stored into `found` via `detect`): the
        `/healthz` cell is pinned by tests/test_70_local_api.py::HealthzHasNoLocalPaths against the response body
        itself (13b review M3)."""
        egress = {"fix_hint", "error_body", "error_payload", "hello_payload", "_refuse", "health", "describe"}
        callers = {fn for fn, _ln, _n in call_sites(self.tree, lambda n: n in PASTE_DOORS)}
        self.assertTrue(callers)                                            # the ruler is not blind
        self.assertEqual(sorted(callers & egress), [])
        probe = "def fix_hint(k, f):" + NL + "    return self_cmd('doctor')"
        self.assertEqual({fn for fn, _ln, _n in call_sites(ast.parse(probe), lambda n: n in PASTE_DOORS)} & egress, {"fix_hint"})

    def test_the_door_is_this_install(self):
        """What the door computes is what this installation actually looks like: interpreter = `sys.executable`,
        script = this file's absolute path (never `python3`/`python`/`~`), with the subcommand appended as-is; change
        the interpreter or where it is installed, and it changes along with it."""
        for exe, where in ((sys.executable, SCRIPT), ("C:/Py 3/python.exe", "D:/装在 这里/scv.py")):
            with self.subTest(exe=exe), mock.patch.object(sys, "executable", exe), mock.patch.object(scv, "__file__", where):
                self.assertEqual(scv.self_cmd("doctor", "--live"), scv.paste_cmd([exe, os.path.abspath(where), "doctor", "--live"]))


class StatusSaysTooOld(_Staged):
    """The remote leg refused this version (`refused`): that line's original words go into /healthz, and must never
    carry a local path ⇒ `status` appends a pasteable `update` line after the JSON."""

    def test_a_refused_bridge_gets_a_pastable_update_line(self):
        """The judgment is the `too_old` state (set in the same place as that original-words line, `RemoteLeg.run`);
        zero-input control: no other state may ever print it.
        ⭐ that line is printed on stderr (the same convention as `_cmd_failed`'s next step): stdout is the whole JSON
          meant for a machine to read, and `too_old` is exactly when an agent most needs to read `remote.refused`
          (13b review M2: it used to print on stdout, making `json.loads` raise `Extra data` on the spot)."""
        for state, want in (("too_old", True), ("streaming", False), ("", False)):
            health = {"protocol": scv.PROTOCOL, "version": scv.VERSION, "remote": {"state": state}}
            with self.subTest(state=state), mock.patch.object(scv, "our_health", return_value=health):
                rc, _lines, out, err = log_lines_during(lambda: scv.main(["status"]))
            self.assertEqual(json.loads(out), health)                                # stdout is still that whole JSON
            self.assertEqual((rc, helpers.door_lines_missing(err, "update") == []), (0, want), err)

    def test_a_blocked_family_points_at_doctor(self):
        """After 13b review M3, /healthz only says whether it is blocked, never why ⇒ `status` points to a pasteable
        doctor line on stderr; zero-input control: with nothing blocked, it must never print it."""
        for fams, want in (({"claude": {"version": "1.0.0", "blocked": False}, "codex": {"version": "", "blocked": True}}, True),
                           ({"claude": {"version": "1.0.0", "blocked": False}}, False)):
            health = {"protocol": scv.PROTOCOL, "version": scv.VERSION, "families": fams, "remote": {"state": "off"}}
            with self.subTest(want=want), mock.patch.object(scv, "our_health", return_value=health):
                rc, _lines, out, err = log_lines_during(lambda: scv.main(["status"]))
            self.assertEqual(json.loads(out), health)
            self.assertEqual((rc, helpers.door_lines_missing(err, "doctor") == [], "codex" in err), (0, want, want), err)


# ━━ Commands meant for a person to paste (13b): every line the one and only door `paste_cmd` hands out must really
#   work when pasted into the shell it is labeled for
@unittest.skipUnless(os.name == "nt", "PowerShell 5.1/cmd/Git Bash are the three win32 shells (⏳ no machine for the POSIX branch, Task 15 will take it up separately)")
class PasteInRealShells(unittest.TestCase):
    """13b judgment 2: pasteable means it really works in a shell the user actually uses -- Win11’s default
    PowerShell 5.1, Claude Code’s Bash tool (Git Bash), and cmd. ⭐ only ever run something with zero side effects:
    `scv.py version`, and a `.cmd` that only echoes back its arguments (the codex npm installs is exactly a `.cmd`).
    ⭐ directories are picked to the shape of the bug, one directory per pitfall (never put two pitfalls in one cell:
      with `$` and `!` both present, removing the door’s rule for `!` would still run all green, covered for by `$`’s
      single quotes -- masking each other): space plus CJK, single quote plus a curly quote (PowerShell also treats
      ‘’’ as a single quote), `$`, a backtick (expands inside bash double quotes), `!` (interactive bash’s history
      expansion) plus `&`/parentheses (the `.cmd` line’s Git Bash pitfall), `%PATH%` (still expands inside cmd’s
      double quotes ⇒ the door must never give this to cmd), and CJK alone (one line covers it all).
    ⭐ the interpreter uses a venv’s launcher (`python.exe` plus `pyvenv.cfg` copied into that directory is enough to
      run), never touching the real installation directory.
    ⭐ whichever shell is missing, only that shell’s cell is skipped, naming it in the reason (13b review M8: the
      whole class used to hang entirely on Git Bash, and without it neither PowerShell nor cmd ran at all)."""

    DIRS = ("中文", "with space 中文", "it's a" + chr(0x2019) + "b", "dollar$x", "tick`x", "bang!x&(1)", "pct%PATH%z")
    SHELLS = ("PowerShell", "cmd", "Git Bash")

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="paste-")
        cls.addClassCleanup(shutil.rmtree, cls.root, True)     # ⭐ cleaned up even if setUpClass blows up partway (tearDownClass would not be called at that point)
        venv = os.path.join(cls.root, "venv")
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", venv], check=True, capture_output=True, timeout=300,
                       **scv.new_session_kw())
        for name in cls.DIRS + ("plain",):
            d = os.path.join(cls.root, name)
            os.mkdir(d)
            for src in (os.path.join(venv, "Scripts", "python.exe"), os.path.join(venv, "pyvenv.cfg"), SCRIPT):
                shutil.copy(src, d)
            with open(os.path.join(d, "codex.cmd"), "wb") as f:
                f.write(b"@echo codex-got %*" + chr(13).encode() + NL.encode())

    def runs_everywhere(self, text, want):
        """Whichever shell `text` gives a line for, run it in that shell for real, and stdout must have `want` in it;
        returns whichever shells were not given one."""
        missing = []
        for shell in self.SHELLS:
            line = helpers.pasted_for(text, shell)
            if line is None:
                missing.append(shell)
                continue
            why = helpers.shell_missing(shell)
            if why:
                with self.subTest(shell=shell):
                    self.skipTest(why)
                continue
            p = helpers.run_in_shell(shell, line, self.root)
            out = p.stdout.decode("utf-8", "replace") + p.stdout.decode("gbk", "replace")
            self.assertIn(want, out, "pasting this line in %s did not run through: %s%s%s" % (shell, line, NL, p.stderr.decode("utf-8", "replace")[-600:]))
        return missing

    def test_every_self_command_line_runs_in_its_shell(self):
        """End to end for the self-referring-command door (`self_cmd`): pin `sys.executable`/`scv.__file__` to the
        interpreter and scv.py inside that directory, and every line the door computes really prints the version
        number in the shell it is labeled for."""
        for name in self.DIRS:
            d = os.path.join(self.root, name)
            with self.subTest(dir=name):
                with mock.patch.object(sys, "executable", os.path.join(d, "python.exe")):
                    with mock.patch.object(scv, "__file__", os.path.join(d, "scv.py")):
                        text = scv.self_cmd("version")
                # only the cell with `%` in it is never given to cmd (positive control: every other cell is given
                #   to cmd too, and really runs through)
                self.assertEqual(self.runs_everywhere(text, "0.1.0"), ["cmd"] if "%" in name else [], text)

    def test_a_plain_program_with_a_quoted_argument(self):
        """The leading word carries no quotes, but the ones after it do (the `git diff` case, `python.exe` in a plain
        directory while scv.py is in one with a space in it): PowerShell's line must never add `& `, and stays in
        command mode as-is. ⚠️ the leading word uses the interpreter the harness itself copied into a plain-named
        directory, never this machine's own `sys.executable` (13b review M5: on a machine where Python is installed
        under `C:/Program Files/…`, it would need quoting, and this case would go falsely red -- an accidental
        property of the fixture doing the judgment's job for it)."""
        exe = os.path.join(self.root, "plain", "python.exe")
        if not scv.PASTE_PLAIN.fullmatch(exe.replace(os.sep, "/")):
            self.skipTest("the temp directory itself carries a character that would need quoting, so a plain-named interpreter path could not be assembled: " + exe)
        d = os.path.join(self.root, "with space 中文")
        text = scv.paste_cmd([exe, os.path.join(d, "scv.py"), "version"])
        self.assertFalse(helpers.pasted_for(text, "PowerShell").startswith("&"), text)
        self.assertEqual(self.runs_everywhere(text, "0.1.0"), [])

    def test_the_codex_login_line_runs_in_every_shell(self):
        """`login_cmd` goes through the same door (13c review M3: Git Bash used to be unable to paste it); once the
        `cmd /c` shell around an npm-installed `.cmd` is stripped away, all three shells can start it (Git Bash
        rewrites `/c` into `C:/`). ⚠️ a `.cmd` in every shell gets handed to cmd to be parsed all over again (measured
        at 13b): for the `&`/parentheses cell, Git Bash does not add quotes ⇒ cmd breaks it apart ⇒ the door must
        never give this to Git Bash; for the `%PATH%` cell, all three shells expand it ⇒ not a single line is ever
        given, and it is stated honestly."""
        want = {"bang!x&(1)": ["Git Bash"]}
        for name in self.DIRS:
            d = os.path.join(self.root, name)
            with self.subTest(dir=name):
                text = scv.login_cmd("codex", ["cmd", "/c", os.path.join(d, "codex.cmd")])
                if "%" in name:
                    self.assertTrue(text.startswith("(cannot be written as one pasteable line") and NL not in text, text)
                    continue
                self.assertEqual(self.runs_everywhere(text, "codex-got login"), want.get(name, []), text)

    def test_a_missing_shell_skips_only_itself_and_says_which(self):
        """13b review M8: on a Windows without Git Bash (no git on PATH) ⇒ only Git Bash's cell is skipped, naming it
        in the reason; PowerShell/cmd run as usual.
        ⭐ take a real test case from the same class, run it once under "Git Bash cannot be found", and look at its
          result (never just read `shell_missing`'s return value: what needs proving is that the test itself really
          takes this path)."""
        result, ran, real = unittest.TestResult(), [], helpers.run_in_shell
        with mock.patch.object(helpers, "git_bash", return_value=None):
            with mock.patch.object(helpers, "run_in_shell", side_effect=lambda sh, *a: ran.append(sh) or real(sh, *a)):
                type(self)("test_a_plain_program_with_a_quoted_argument").run(result)
        self.assertEqual((result.testsRun, result.errors, result.failures), (1, [], []))
        self.assertEqual(ran, ["PowerShell", "cmd"])                                       # the other two shells run as usual
        self.assertEqual([why for _t, why in result.skipped], [helpers.shell_missing("Git Bash", git=None)])
        self.assertIn("Git Bash", result.skipped[0][1])
        self.assertEqual(type(result.skipped[0][0]).__name__, "_SubTest")                  # what got skipped is that cell, never the whole test case

    def test_what_the_door_withholds_really_would_not_run(self):
        """Negative control (whether the lines the door withholds really would not run -- otherwise "withholding"
        might just be the door being timid, leaving the user one line short): hand-write the withheld line with the
        same set of quotes, run it for real in that shell, and it must fail to run."""
        d = os.path.join(self.root, "bang!x&(1)")
        bash = "'" + os.path.join(d, "codex.cmd").replace(os.sep, "/") + "' login"         # the form the door would normally give to Git Bash
        with self.subTest(shell="Git Bash"):
            if helpers.shell_missing("Git Bash"):
                self.skipTest(helpers.shell_missing("Git Bash"))
            self.assertNotIn("codex-got login", helpers.run_in_shell("Git Bash", bash, self.root).stdout.decode("utf-8", "replace"))
        py, script, stub = (os.path.join(self.root, "pct%PATH%z", n).replace(os.sep, "/") for n in ("python.exe", "scv.py", "codex.cmd"))
        # the first two lines are a pair: same directory, PowerShell runs through (zero-input control: the directory
        #   itself is not broken), cmd does not run through (`%PATH%` gets expanded)
        for shell, line, runs in (("PowerShell", "& '%s' '%s' version" % (py, script), True),
                                  ("cmd", '"%s" "%s" version' % (py, script), False),
                                  ("PowerShell", "& '%s' login" % stub, False)):
            with self.subTest(shell=shell, line=line[-30:]):
                if helpers.shell_missing(shell):
                    self.skipTest(helpers.shell_missing(shell))
                out = helpers.run_in_shell(shell, line, self.root).stdout.decode("utf-8", "replace")
                self.assertEqual("0.1.0" in out or "codex-got" in out, runs, line + NL + out)


# ━━ main (C15/B10/E19)
class Main(_Staged):
    def test_an_unexpected_failure_is_logged_and_told_in_words(self):
        with mock.patch.object(scv, "cmd_status", side_effect=RuntimeError("the bridge's own bug")):
            rc, lines, out, err = log_lines_during(lambda: scv.main(["status"]))
        self.assertEqual(rc, 1)
        self.assertEqual(len([x for x in lines if "the bridge's own bug" in x]), 1, lines)
        self.assertIn("did not succeed", err)
        self.assertIn("bridge.log", err)

    def test_a_filesystem_without_hardlinks_gets_told_what_to_do(self):
        """L77: `os.link` raises on the spot on exFAT/FAT32 (an error must be loud, but usefully loud) ⇒ the
        subcommand layer gives one human sentence."""
        home = os.path.join(HOME, "no-hardlinks")
        old = os.environ["SCV_HOME"]
        os.environ["SCV_HOME"] = home
        try:
            with mock.patch.object(scv.os, "link", side_effect=OSError(1, "Incorrect function")):
                rc, _lines, out, err = log_lines_during(lambda: scv.main(["token"]))
        finally:
            os.environ["SCV_HOME"] = old
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")                                   # stdout only ever gives the token itself
        self.assertIn("hard link", err)
        self.assertIn("NTFS", err)                                  # ⭐ next step: which filesystem to switch to (the old line only said "could not create it")
        self.assertIn("SCV_HOME", err)

    def test_token_prints_only_the_token(self):
        rc, _lines, out, err = log_lines_during(lambda: scv.main(["token"]))
        self.assertEqual((rc, out, err), (0, scv.load_config()["local_token"] + NL, ""))

    # ⚠️ there used to be a case here, "Task 13's two commands honestly say 'not in this version yet'" (pinning down
    #   the placeholder): Task 13 replaced the placeholder with the real thing, and that case moved to
    #   tests/test_95_setup_pair_update.py::Main::test_the_commands_task_13_owns_are_real_now (never feed
    #   `https://example.invalid` here anymore: the real `scv pair` would dial it).


if __name__ == "__main__":
    unittest.main()
