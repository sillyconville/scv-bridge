# -*- coding: utf-8 -*-
"""The rule "tests must never create a directory outside the temp directory" (the gate itself lives in
tests/helpers.py): a positive control plus a closing tally.
⭐This filename sorts last: `unittest discover` runs in filename order, and the tally has to wait for every suite
  before it to finish."""
import ast
import glob
import io
import json
import os
import secrets
import socket
import socketserver
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers


class NothingOutsideTheTempDir(unittest.TestCase):
    def test_a_run_made_nothing_outside_the_temp_dir(self):
        """The closing tally: the production code swallows that "refusal" (`except OSError`), so it never blows up
        on the spot — this is the only place it can be seen.
        ⚠️When not a single directory has been created yet (running just this one file, or `-k` picking only this
          one), an empty tally proves nothing ⇒ skip it, never report it green.
        (It is never skipped during a full run: on win32 a full run's skipped count is always 2, exactly the two
        POSIX-only cases in test_10_proc.)"""
        if not helpers.MKDIR_CALLS[0]:
            self.skipTest("no suite before this one has created a directory: the tally proves nothing")
        self.assertEqual(helpers.MADE_OUTSIDE, [], "(the tally covers %d directory creation(s))" % helpers.MKDIR_CALLS[0])

    def test_the_gate_bites(self):
        """positive control: three ways of creating a directory that land outside the temp directory ⇒ all
        refused, none of them actually created, and all of them recorded in the tally; creating one inside the
        temp directory still works as usual.
        (These entries are made by the control itself, and are struck off the tally once verified — they must
        never pollute the tally above.)"""
        self.assertIs(os.mkdir, helpers._guarded_mkdir)
        outside = os.path.join(os.path.dirname(helpers.ROOT), "scv-gate-probe-" + secrets.token_hex(4))
        self.addCleanup(lambda: os.path.isdir(outside) and os.rmdir(outside))
        mark = len(helpers.MADE_OUTSIDE)
        for make in (lambda: os.mkdir(outside), lambda: os.makedirs(outside, exist_ok=True),
                     lambda: Path(outside).mkdir(parents=True, exist_ok=True)):
            with self.assertRaises(helpers.OutsideTempDir):
                make()
            self.assertFalse(os.path.exists(outside))
        self.assertEqual(helpers.MADE_OUTSIDE[mark:], [os.path.abspath(outside)] * 3)
        del helpers.MADE_OUTSIDE[mark:]
        base = helpers.tempfile.mkdtemp(prefix=".scv-test-gate-")
        self.addCleanup(helpers.remove_tree, base)
        inside = os.path.join(base, "a", "b")
        os.makedirs(inside)
        self.assertTrue(os.path.isdir(inside))

    def test_mkdtemp_outside_is_refused_once_not_retried_forever(self):
        """Task 12 review M-5: the door used to raise `PermissionError` ⇒
        `tempfile.mkdtemp(dir=<a directory outside the temp directory that already exists>)` on Windows treats
        that as "the name is taken" and retries in place (5003 times in 2 seconds, never stopping without deleting
        the directory). ⇒ the door's refusal must never be a `PermissionError`: refusing once has to be enough to
        raise.
        ⚠️This cell must never hang the whole suite by itself: `TMP_MAX` is temporarily pushed down to 50 (if the
          door ever falls back to `PermissionError`, it will raise `FileExistsError` after 50 tries, and this test
          goes red, instead of spinning through 2.1 billion — 21 followed by eight zeros — loops)."""
        base = os.path.dirname(helpers.ROOT)              # outside the temp directory, already exists, writable
        prefix = "scv-gate-mkdtemp-" + secrets.token_hex(4) + "-"
        self.addCleanup(lambda: [os.rmdir(os.path.join(base, x)) for x in os.listdir(base) if x.startswith(prefix)])
        mark = len(helpers.MADE_OUTSIDE)
        self.addCleanup(helpers.MADE_OUTSIDE.__delitem__, slice(mark, None))   # the control's own entries must never pollute the tally
        got = None
        with mock.patch.object(tempfile, "TMP_MAX", 50):
            try:
                tempfile.mkdtemp(prefix=prefix, dir=base)
            except OSError as e:
                got = e
        # refusing once is enough to raise, and to stop (if the door fell back to PermissionError, what would be
        #   read here is ('FileExistsError', 50): treated as "the name is taken" and spun through 50 loops)
        self.assertEqual((type(got).__name__, len(helpers.MADE_OUTSIDE) - mark), ("OutsideTempDir", 1))
        self.assertEqual([x for x in os.listdir(base) if x.startswith(prefix)], [])


class _Counting(socketserver.BaseRequestHandler):
    """The suite's own counting service: records a hit for whoever connects, and replies with nothing (closes as
    soon as it is connected to)."""
    hits = None

    def handle(self):
        self.hits.append(self.client_address)


class NoTestDialsTheRealDefaultPort(unittest.TestCase):
    """The rule "tests must never dial this machine's real default port" (the gate itself lives in
    tests/helpers.py, 13b fix1): a closing tally plus a positive control."""

    def test_a_run_dialed_nothing_on_the_default_port(self):
        """The closing tally: the production code swallows that "refusal" (`local_get` catches `OSError` and
        returns None), so it never blows up on the spot — this is the only place it can be seen.
        ⚠️When only this one file is run, the tally is also empty (nobody dialed before it) — empty is the wanted
          result here, never skipped; the next positive control proves the gate has teeth."""
        self.assertEqual(helpers.DIALED_DEFAULT_PORT, [], chr(10).join(sorted(set(helpers.DIALED_DEFAULT_PORT))))

    def test_the_gate_bites(self):
        """positive control: dialing the default port in-process through scv's own door
        (`local_get` → urllib → `socket.create_connection`) ⇒ refused (`local_get` swallows it into None as
        always) and recorded in the tally; dialing some other port still goes through as usual (a zero-input
        control: taking a port the suite itself started and immediately closed gives "cannot connect", never "the
        gate refused it").
        🔴A13 (13b re-review N4): this test used to dial the real default port ⇒ the moment the gate lost its
          teeth, the positive control itself would go dial this machine's real 8765 (something else runs on it).
          ⇒ the gate's idea of "the default port" is `scv.DEFAULT_CONFIG["port"]` (`helpers.default_port()`): this
          test swaps that for the suite's own counting service's port — with the gate toothless, the dial lands
          on the counting service instead (this goes red, on the sentence "neither call was refused and the
          counting service was connected to twice"), and it never touches the real 8765."""
        import scv
        self.assertIs(socket.create_connection, helpers._guarded_create_connection)
        hits = []
        handler = type("H", (_Counting,), {"hits": hits})
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        own = srv.server_address[1]
        mark = len(helpers.DIALED_DEFAULT_PORT)
        self.addCleanup(helpers.DIALED_DEFAULT_PORT.__delitem__, slice(mark, None))   # the control's own entries must never pollute the tally
        with mock.patch.dict(scv.DEFAULT_CONFIG, {"port": own}):
            self.assertEqual(helpers.default_port(), own)                 # precondition: the gate's idea of "the default port" is now the counting service
            got = scv.local_get(own, "/healthz")
            try:
                socket.create_connection(("localhost", own), timeout=1).close()
                refused = None
            except helpers.DefaultPortDial:
                refused = "DefaultPortDial"
        end = time.time() + 2
        while refused is None and len(hits) < 2 and time.time() < end:  # if the gate has lost its teeth, wait for the counting service to record both calls
            time.sleep(0.05)
        # the gate really did block them: both calls refused, the counting service never connected to even once.
        #   With a toothless gate this would read (None, 2, None) — the two outgoing calls would have landed on
        #   the suite's own counting service (never the real 8765), and this is exactly the sentence that goes red
        self.assertEqual((refused, len(hits), got), ("DefaultPortDial", 0, None))
        self.assertEqual([x.split(" ← ")[0] for x in helpers.DIALED_DEFAULT_PORT[mark:]],
                         ["127.0.0.1:%d" % own, "localhost:%d" % own])
        self.assertIn("test_the_gate_bites", helpers.DIALED_DEFAULT_PORT[mark])                  # can point to which test it was
        # zero-input control: once the gate's idea of the default port is back to the real value, the same
        #   counting service can still be connected to as usual (never "the gate refuses whoever it sees", which
        #   would also make the test above all green)
        with socket.create_connection(("127.0.0.1", own), timeout=5):
            pass
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(OSError) as cm:
            socket.create_connection(("127.0.0.1", port), timeout=1)
        self.assertNotIsInstance(cm.exception, helpers.DefaultPortDial)
        self.assertEqual(len(helpers.DIALED_DEFAULT_PORT) - mark, 2)


# ━━ A12: check config's port before spawning an scv.py subprocess (the gate itself is `helpers.refuse_default_port`)
def _spawns_scv(call):
    """`subprocess.run` / `Popen(<argv>, …)` whose argv carries `SCRIPT` or `scv.__file__` (the two ways a suite
    spawns an scv.py subprocess)."""
    if not (isinstance(call, ast.Call) and call.args and isinstance(call.func, ast.Attribute)
            and call.func.attr in ("run", "Popen", "call", "check_call", "check_output")):
        return False
    for n in ast.walk(call.args[0]):
        if isinstance(n, ast.Name) and n.id == "SCRIPT":
            return True
        if isinstance(n, ast.Attribute) and n.attr == "__file__" and isinstance(n.value, ast.Name) and n.value.id == "scv":
            return True
    return False


def unguarded_scv_spawns(tree, where="?"):
    """The places that spawn an scv.py subprocess without calling `refuse_default_port` anywhere in that same
    function ⇒ ["file:function@line"]."""
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        guarded = any(isinstance(c.func, ast.Attribute) and c.func.attr == "refuse_default_port" for c in calls)
        out += ["%s:%s@%d" % (where, fn.name, c.lineno) for c in calls if _spawns_scv(c) and not guarded]
    return out


class NoScvChildDialsTheDefaultPort(unittest.TestCase):
    """A12 (13b re-review): the in-process dial gate cannot see a subprocess ⇒ before a suite spawns an `scv.py`
    subprocess, that same function must first go through `refuse_default_port`.
    ⚠️Boundary (Task 15 review M3 fed snippets into `unguarded_scv_spawns` and measured this for real; written
      down plainly, never over-engineered):
      the only shape it recognizes is `subprocess.<run|Popen|call|check_call|check_output>(<argv>, …)`, where the
      name `SCRIPT` or `scv.__file__` shows up directly inside that argv argument expression. What it cannot see
      (a false negative):
        · one spawned through a shell (`run_in_shell` pasting the one line `python … scv.py status`) — that call
          site (test_97's `SetupMdInRealShells.paste`) has its guard added by hand;
        · argv assembled into a variable first and then passed (`cmd = [sys.executable, SCRIPT]; subprocess.run(cmd)`);
        · a bare `run(…)` after `from subprocess import run` (only an attribute call like `x.run` is recognized);
        · a path that is named neither `SCRIPT` nor `scv.__file__` (`os.path.join(ROOT, "scv.py")`,
          `helpers.ROOT + "/scv.py"`);
        · `os.system` / `os.popen` / `os.spawn*`;
        · a subprocess spawned at module top level (outside any function).
      Counted as guarded when it is not (also a false negative): the guard counts as long as it appears anywhere in
        the same function — written after the spawn, or inside a branch that can never be reached
        (`if False: helpers.refuse_default_port(…)`), both still count as guarded.
      False positives (loud, harmless): an outer function is guarded and an inner nested function spawns the
        process ⇒ the inner one gets named.
      Today (Task 15) there are only three places in tests/ that spawn scv.py directly (test_90's `Lifecycle.scv`,
      the doctor-pipe one, and test_97's `PipedOutputIsUtf8.run_scv`), plus the one above that goes through a
      shell, and every one of them is guarded."""

    def test_every_direct_scv_spawn_in_tests_is_guarded(self):
        found, bad = 0, []
        for path in sorted(glob.glob(os.path.join(helpers.ROOT, "tests", "test_*.py"))):
            with io.open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            found += sum(1 for n in ast.walk(tree) if _spawns_scv(n))
            bad += unguarded_scv_spawns(tree, os.path.basename(path))
        self.assertGreaterEqual(found, 3)                  # the ruler is not blind: today there are at least three, Lifecycle.scv / the doctor-pipe one / PipedOutputIsUtf8
        self.assertEqual(bad, [])

    def test_the_scanner_is_not_blind(self):
        bad = ast.parse("def f():\n    subprocess.run([sys.executable, SCRIPT, 'status'])\n"
                        "def g():\n    subprocess.Popen([sys.executable, scv.__file__] + args)\n")
        self.assertEqual(unguarded_scv_spawns(bad), ["?:f@2", "?:g@4"])
        good = ast.parse("def f():\n    helpers.refuse_default_port(HOME)\n    subprocess.run([sys.executable, SCRIPT])\n")
        self.assertEqual(unguarded_scv_spawns(good), [])
        other = ast.parse("def f():\n    subprocess.run(['git', 'status'])\n")
        self.assertEqual(unguarded_scv_spawns(other), [])

    def test_the_guard_bites(self):
        """positive control: config's port is the default port, or there is no config.json, ⇒ AssertionError on
        the spot; the suite's own port ⇒ passes through."""
        d = tempfile.mkdtemp(prefix=".scv-test-portguard-")
        self.addCleanup(helpers.remove_tree, d)
        p = os.path.join(d, "config.json")
        with self.assertRaises(AssertionError):
            helpers.refuse_default_port(d)                       # no config.json: the subprocess would mint one with the default
        for port, ok in ((helpers.default_port(), False), (helpers.quiet_port(), True)):
            with io.open(p, "w", encoding="utf-8") as f:
                json.dump({"port": port}, f)
            with self.subTest(port=port):
                if ok:
                    helpers.refuse_default_port(d)
                else:
                    with self.assertRaises(AssertionError):
                        helpers.refuse_default_port(d)


class TheRunLeftNoScvTestDirBehind(unittest.TestCase):
    """D25: `fresh_home` used to never delete anything (one round of fixes found over 100 `.scv-test-*` directories
    left behind in %TEMP%). The tally is kept by `helpers._guarded_mkdir`: every directory starting with
    `.scv-test-` that this process creates directly under the system temp directory (never limited to the one
    scene `fresh_home` covers).
    ⭐This class name sorts last in this file (`unittest` runs in name order): the directories earlier tests
      created themselves are on the tally too."""

    def test_every_scv_test_dir_made_in_this_run_is_gone(self):
        if not helpers.SCV_TEST_DIRS:
            self.skipTest("no suite before this one has created a .scv-test-* directory: the tally proves nothing")
        self.assertEqual(helpers.scv_test_dirs_left(), [], "(the tally covers %d directory/directories)" % len(helpers.SCV_TEST_DIRS))

    def test_the_ledger_is_not_blind(self):
        """positive control: `mkdtemp(prefix=".scv-test-…")` lands on the tally, `scv_test_dirs_left` can point to
        it while it still exists, and stops pointing to it once it is deleted; a different prefix, or one not at
        the temp directory's top level, must never land on the tally."""
        mark = len(helpers.SCV_TEST_DIRS)
        self.addCleanup(helpers.SCV_TEST_DIRS.__delitem__, slice(mark, None))
        d = tempfile.mkdtemp(prefix=".scv-test-ledger-")
        self.addCleanup(helpers.remove_tree, d)
        self.assertEqual(helpers.SCV_TEST_DIRS[mark:], [d])
        self.assertIn(d, helpers.scv_test_dirs_left())
        other = tempfile.mkdtemp(prefix="scv-ledger-other-")
        self.addCleanup(helpers.remove_tree, other)
        os.mkdir(os.path.join(d, ".scv-test-nested"))
        self.assertEqual(helpers.SCV_TEST_DIRS[mark:], [d])
        helpers.remove_tree(d)
        self.assertNotIn(d, helpers.scv_test_dirs_left())


if __name__ == "__main__":
    unittest.main()
