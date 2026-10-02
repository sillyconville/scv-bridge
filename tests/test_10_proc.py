# -*- coding: utf-8 -*-
"""Tree-kill and orphan sweeping. ⭐Every OS is its own separate path: this file must run on CI on all three OSes
(Task 15)."""
import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import scv  # noqa: E402
from tests import helpers  # noqa: E402  ⭐only borrows the one judge `born_alive` (⛔ never calls fresh_home: this file manages its own SCV_HOME)


def setUpModule():
    os.environ["SCV_HOME"] = tempfile.mkdtemp(prefix=".scv-test-proc-")
    unittest.addModuleCleanup(helpers.remove_tree, os.environ["SCV_HOME"])    # D25: delete when done (test_99 reconciles it)


PARENT = '''
import subprocess, sys, time
p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
print(p.pid, flush=True)
time.sleep(120)
'''

CHATTY = '''
import sys, time
sys.stdout.write("half an answer")
sys.stdout.flush()
time.sleep(120)
'''

# ⭐Pin this exact sentence, ⛔ never judge it by a diff against the whole log increment for the word "family": any
# future log line that happens to mention it would make "it should not be loud" a false red.
_GATE_LINE = "was not given a family, yet is waiting"


class _FakePosixOs:
    """Just swaps a new skin onto scv's own `os.` lookups: patching `os.name` globally would break pathlib too
    (on Windows, `Path()` would go instantiate PosixPath ⇒ NotImplementedError). ⭐Fixed on the measurement side,
    ⛔ never by adding an IS_POSIX knob to production just to make it measurable."""

    name = "posix"

    def __init__(self, pgid="leader"):
        self._pgid = pgid
        # ⭐killpg/kill are made Mocks: real os on Windows has no killpg at all, and we absolutely never want to
        # really pull the trigger
        self.killpg = mock.Mock()
        self.kill = mock.Mock()

    def getpgid(self, pid):
        if self._pgid is None:
            raise ProcessLookupError("no such process")
        return pid if self._pgid == "leader" else self._pgid

    def __getattr__(self, key):      # everything else goes through the real os
        return getattr(os, key)


@contextlib.contextmanager
def _registerable():
    """For a fake pid: getting into the registry has to clear **two real gates** — the group-leader check (POSIX)
    and the birth-id check — a fake pid clears neither ⇒ without this wrapper, the few test cases below would
    **only be green on Windows**, and would all go red on Task 15's Linux/macOS legs.
    ⛔This is not about getting around the gates: the object under test in these cases is the registry itself,
    and each of those two gates has its own test file."""
    with mock.patch.object(scv, "os", _FakePosixOs()), \
            mock.patch.object(scv, "proc_start_id", return_value="fake-birth-cert"):
        yield


def _call_tail(node):
    """`scv.run_cli(...)` / `run_cli(...)` both return "run_cli"."""
    f = node.func
    return f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")


def _reap(proc, within=8.0):
    """⭐On POSIX, a child killed without a `wait()` becomes a zombie, and `ps -o lstart=` on a zombie **still
    returns 0 with a start time** ⇒ `proc_start_id()` comes back non-empty ⇒ `_gone()` would never turn True.
    `sweep_orphans` never waits (what it kills was never its own child to begin with), so whoever Popens a process
    in a test has to reap it themselves. ⛔Never loosen `_gone()` to work around this: that would be tuning the
    ruler until it goes blind."""
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=within)


def _gone(pid, within=8.0):
    end = time.time() + within
    while time.time() < end:
        if scv.proc_start_id(pid) == "":
            return True
        time.sleep(0.2)
    return False


class _CleanTable(unittest.TestCase):
    """Every case added here touches children.json ⇒ leave the table clean going in and coming out, ⛔ never let a
    leftover leak into the Brief cases (and never the other way around either — do not let the Brief cases decide
    this one's outcome)."""

    def setUp(self):
        self._reset()

    def tearDown(self):
        self._reset()

    def _reset(self):
        scv.spath("children.json").write_text("[]", encoding="utf-8")


class ChildrenFileSurvivesABadDeath(_CleanTable):
    """⭐The entire reason this file exists is to survive one abnormal death ⇒ being killed mid-write is exactly
    the scenario it has to handle."""

    def test_corrupt_table_shouts_instead_of_silently_forgetting(self):
        """A corrupted table = the last batch of orphans never gets reclaimed. ⛔This error must never be silent:
        all three tables staying green while the log says nothing is the worst outcome there is."""
        path = scv.spath("children.json")
        path.write_text('[{"pid": 1, "bo', encoding="utf-8")       # what a table looks like when it was killed mid-write
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            want = str(exc)                                        # the parser's own words, unchanged
        log_path = scv.spath("bridge.log")
        before = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        with contextlib.redirect_stderr(io.StringIO()):
            rows = scv.children()
        self.assertEqual(rows, [])
        # the log file not existing = it said nothing at all, the red is in the next line (⛔ never let it explode into a FileNotFoundError first)
        added = (log_path.read_text(encoding="utf-8") if log_path.exists() else "")[len(before):]
        self.assertIn("children.json", added)                      # say clearly which file was broken
        self.assertIn(want, added)                                 # assert the correct value: carry the original words

    def test_target_file_is_replaced_never_truncated_in_place(self):
        """⛔Never start a second process to race against it (there is no sync point ⇒ a false-negative gate):
        interrupt the "renaming" moment itself directly, and look at whichever copy is left on disk."""
        with _registerable():
            scv.child_add(4242, "claude")
        path = scv.spath("children.json")
        good = path.read_text(encoding="utf-8")
        with mock.patch("os.replace", side_effect=OSError("pretend the power went out at the moment of renaming")) as rep:
            with contextlib.suppress(OSError), _registerable():
                scv.child_add(4243, "codex")
        self.assertTrue(rep.called, "children.json was not landed through os.replace ⇒ it is truncated and rewritten "
                                    "in place, and being killed mid-write would lose it")
        self.assertEqual(os.fspath(rep.call_args[0][1]), os.fspath(path))   # what it was about to replace onto is the target file
        self.assertEqual(path.read_text(encoding="utf-8"), good)            # assert the correct value: the last full copy is still on disk


class KillPidTreeGuardsTheGroup(_CleanTable):
    """⭐The guard rail has to live **right where the trigger is pulled**: on POSIX, doing `killpg(getpgid(pid))`
    on a pid that is not a group leader means taking out the bridge's own group along with it, and
    `suppress(OSError)` guarantees it happens without a sound.
    ⛔`child_add`'s group-leader gate does not block this path — `kill_pid_tree` takes a **bare pid**, and anyone
    can hand it one."""

    def test_refuses_to_killpg_a_pid_that_is_not_its_own_group_leader(self):
        fake = _FakePosixOs(999)                                   # getpgid(4242) -> 999 != 4242
        log_path = scv.spath("bridge.log")
        before = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        with contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(scv, "os", fake):
                scv.kill_pid_tree(4242)
        self.assertIs(fake.killpg.called, False, "killing the whole group of a pid that is not a leader would take out the bridge's own group")
        fake.kill.assert_called_once_with(4242, scv._SIGKILL)      # assert the correct value: only itself gets killed
        added = (log_path.read_text(encoding="utf-8") if log_path.exists() else "")[len(before):]
        self.assertIn("4242", added)                               # the fallback has to be loud: its descendants will leak as orphans

    def test_still_killpgs_a_proper_group_leader(self):
        """Zero-input control: if it never called killpg on anyone, the case above would still be green — killing
        the whole group would have silently disappeared."""
        fake = _FakePosixOs()                                      # getpgid(4242) -> 4242
        with contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(scv, "os", fake):
                scv.kill_pid_tree(4242)
        fake.killpg.assert_called_once_with(4242, scv._SIGKILL)


class ChildAddRefusesUngroupedPid(_CleanTable):
    """On POSIX, sweep kills by killpg-ing the **whole group** ⇒ accepting a pid that is not in its own group means
    the next sweep takes out the bridge's own group along with it, and `suppress(OSError)` would swallow it without
    a sound. This invariant used to only live in someone's memory; now it is a runtime gate."""

    def test_refuses_a_pid_that_is_not_its_own_group_leader(self):
        """⚠️This case is a **logic gate**: both os.name and os.getpgid are fake ⇒ what it proves is "the judgment
        and the refusal are correct", ⛔ it cannot prove anything about the real getpgid's behavior (that half is
        in the case below, which only runs on CI's Linux/macOS legs)."""
        log_path = scv.spath("bridge.log")
        before = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        with contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(scv, "os", _FakePosixOs(999)):
                got = scv.child_add(4242, "claude")                # 4242 != 999 ⇒ not a group leader
        self.assertIs(got, False, "refused it but returned True ⇒ the caller thinks the bridge took it over, and nobody would ever clean up that process")
        self.assertEqual(scv.children(), [], "accepting an ungrouped pid ⇒ the next sweep would killpg and take out the bridge's own group")
        added = (log_path.read_text(encoding="utf-8") if log_path.exists() else "")[len(before):]
        self.assertIn("4242", added)                               # a refusal has to be loud, and it has to name who

    def test_refuses_a_pid_whose_group_cannot_be_read(self):
        """The process was already gone before it could be registered ⇒ getpgid raises ⛔ never let the exception
        pass through to the caller unhandled, and ⛔ never quietly accept it either."""
        with contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(scv, "os", _FakePosixOs(None)):
                got = scv.child_add(4242, "claude")                # ⛔ must never raise
        self.assertIs(got, False)
        self.assertEqual(scv.children(), [])

    def test_refuses_a_pid_whose_birth_certificate_is_empty(self):
        """⭐An empty birth id ⇒ `sweep_orphans`'s judgment `if born and ...` can never be true ⇒ the process is
        alive, it is in the table, the bridge thinks it owns it, and yet **it can never actually be swept**, and
        the next time the table is written it gets silently dropped, with not one line of log."""
        log_path = scv.spath("bridge.log")
        before = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        with contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(scv, "os", _FakePosixOs()), \
                    mock.patch.object(scv, "proc_start_id", return_value=""):
                got = scv.child_add(4242, "claude")
        self.assertIs(got, False)
        self.assertEqual(scv.children(), [], "accepting a row whose birth id is empty ⇒ sweep can never kill it")
        added = (log_path.read_text(encoding="utf-8") if log_path.exists() else "")[len(before):]
        self.assertIn("4242", added)

    def test_still_accepts_a_proper_group_leader(self):
        """Zero-input control: if it refused everyone, the cases above would be green too — that would make it a
        gate that only ever says "no"."""
        with contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(scv, "os", _FakePosixOs(4242)), \
                    mock.patch.object(scv, "proc_start_id", return_value="fake-birth-cert"):
                got = scv.child_add(4242, "claude")
        self.assertIs(got, True, "accepted it but did not return True ⇒ the caller would go kill an already-registered process down the False path")
        self.assertEqual([(c["pid"], c["family"]) for c in scv.children()], [(4242, "claude")])


@unittest.skipIf(os.name == "nt", "Windows has no os.getpgid, and the shape of killpg taking out the whole group does not exist there")
class ChildAddGroupGateOnRealPosix(_CleanTable):
    """⚠️⚠️This machine is Windows ⇒ this case **is skipped here**, and only has teeth on Task 15's Linux/macOS
    legs."""

    def test_real_ungrouped_process_is_refused(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])   # ⛔ deliberately without new_session_kw()
        try:
            self.assertIs(scv.child_add(proc.pid, "claude"), False)
            self.assertEqual(scv.children(), [])
        finally:
            # after I1, kill_pid_tree already blocks this on its own (won't killpg a non-leader) ⇒ proc.kill() is used here
            # so this case does not depend on that gate — the object under test is child_add's refusal, ⛔ it should not lean on another gate for cover
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                proc.wait(timeout=15)

    def test_real_grouped_process_is_accepted(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], **scv.new_session_kw())
        try:
            self.assertIs(scv.child_add(proc.pid, "claude"), True)
            self.assertEqual([c["pid"] for c in scv.children()], [proc.pid])
        finally:
            scv.kill_tree(proc)


class OrphanTrees(_CleanTable):
    def test_sweep_kills_the_orphans_grandchild_too(self):
        """⭐Built to match the shape of the bug: in the 78.6MB incident, the orphan itself was carrying another
        layer underneath it (a node launcher → a native binary). The two Brief `Orphans` cases start a plain
        single-layer sleep ⇒ they would stay green even with `/T` deleted — they are blind to this shape."""
        proc = subprocess.Popen([sys.executable, "-c", PARENT], stdout=subprocess.PIPE, **scv.new_session_kw())
        self.addCleanup(proc.stdout.close)
        grandchild = int(proc.stdout.readline().decode().strip())
        self.assertTrue(helpers.born_alive(scv.proc_start_id(grandchild)))     # positive control: it is alive before sweep
        scv.child_add(proc.pid, "claude")
        self.assertEqual(scv.sweep_orphans(), [proc.pid])
        _reap(proc)
        self.assertIsNotNone(proc.returncode, "nobody reaped it ⇒ on POSIX it becomes a zombie, and ps still returns a start time for a zombie ⇒ _gone() would never turn True")
        self.assertTrue(_gone(proc.pid))
        self.assertTrue(_gone(grandchild), "the orphan's grandchild is still alive = sweep only killed the one it had registered")


class Decode(unittest.TestCase):
    """⛔The Brief cases never touched the three producer-facing interfaces decode / proc_rss_kb / child_remove;
    these cases fill that in."""

    def test_falls_back_to_gbk_when_bytes_are_not_utf8(self):
        raw = "中".encode("gbk")
        with self.assertRaises(UnicodeDecodeError):      # positive control: this byte string really is not legal utf-8
            raw.decode("utf-8")
        self.assertEqual(scv.decode(raw), "中")           # assert the correct value, ⛔ not just "did not raise"
        self.assertEqual(scv.decode("中".encode("utf-8")), "中")


class Rss(unittest.TestCase):
    def test_live_pid_reports_kb_dead_pid_reports_none(self):
        """⭐A dead process must be None, ⛔ never 0: 0 would be read downstream as "measured it, and it uses 0KB"."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], **scv.new_session_kw())
        try:
            kb = scv.proc_rss_kb(proc.pid)
            self.assertIsInstance(kb, int)
            self.assertGreater(kb, 0)
        finally:
            scv.kill_tree(proc)
        self.assertTrue(_gone(proc.pid))
        self.assertIsNone(scv.proc_rss_kb(proc.pid))


class ChildTable(_CleanTable):
    def test_remove_takes_only_the_named_row(self):
        """The registry is the sole basis for orphan sweeping: deleting the wrong row means either a miss (never
        reclaimed) or a wrongful kill.

        ⛔This uses fake pids — getting into the table needs _registerable() to fake out both the group-leader gate
        and the birth-id gate ⇒ it never touches a real process."""
        for row in scv.children():
            scv.child_remove(row["pid"])
        with _registerable():
            self.assertIs(scv.child_add(4242, "claude"), True)
            self.assertIs(scv.child_add(4243, "codex"), True)
        scv.child_remove(4242)
        self.assertEqual([(c["pid"], c["family"]) for c in scv.children()], [(4243, "codex")])
        scv.child_remove(4243)
        self.assertEqual(scv.children(), [])             # leave it clean, ⛔ never leak a fake pid into the Orphans cases after this

    def test_remove_shouts_when_there_was_nothing_to_remove(self):
        """Writing off an entry that was never in the table = either a double write-off, or a registration that
        was rejected and nobody noticed ⇒ ⛔ this must never be silent."""
        log_path = scv.spath("bridge.log")
        before = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        with contextlib.redirect_stderr(io.StringIO()):
            scv.child_remove(4242)
        added = (log_path.read_text(encoding="utf-8") if log_path.exists() else "")[len(before):]
        self.assertIn("4242", added)

    def test_rows_with_a_broken_shape_are_skipped_not_fatal(self):
        """⭐Legal JSON ⛔ is not the same thing as a correct shape: a hand-edited or old-version table would blow
        up **the whole startup**. A bad row should be skipped and complained about; a good row still gets used."""
        scv.spath("children.json").write_text(
            json.dumps([{"pid": 1, "born": "x", "family": "claude"},      # a good row
                        {"pid": "not a number", "born": "x", "family": "claude"},
                        {"born": "missing a pid", "family": "claude"},
                        "not a dict at all",
                        {"pid": True, "born": "x", "family": "claude"},   # ⭐isinstance(True, int) is True
                        {"pid": 2, "born": "x"}]),                        # missing family: a consumer would do c["family"]
            encoding="utf-8")
        log_path = scv.spath("bridge.log")
        before = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        with contextlib.redirect_stderr(io.StringIO()):
            rows = scv.children()
        self.assertEqual(rows, [{"pid": 1, "born": "x", "family": "claude"}])   # assert the correct value: the good row is left untouched
        added = (log_path.read_text(encoding="utf-8") if log_path.exists() else "")[len(before):]
        # ⛔Never just assert "the log increment is non-empty": any unrelated line would make an assertion like that green. Pin down exactly which line, and whether the count is right
        self.assertIn("5 row(s) shaped wrong", added)
        self.assertIn("the 1 good row(s) are still used", added)

    def test_a_table_that_is_not_a_list_is_refused(self):
        scv.spath("children.json").write_text(json.dumps({"pid": 1}), encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(scv.children(), [])


class KillTree(unittest.TestCase):
    def test_grandchild_dies_with_tree(self):
        proc = subprocess.Popen([sys.executable, "-c", PARENT], stdout=subprocess.PIPE, **scv.new_session_kw())
        self.addCleanup(proc.stdout.close)                          # ⛔ never leave an undeclared ResourceWarning behind as noise
        grandchild = int(proc.stdout.readline().decode().strip())
        self.assertTrue(helpers.born_alive(scv.proc_start_id(grandchild)))      # positive control: it is alive before the kill
        scv.kill_tree(proc)                                         # it already waited on it itself ⇒ no need to _reap again
        self.assertTrue(_gone(proc.pid))
        self.assertTrue(_gone(grandchild), "the grandchild is still alive = the tree-kill only killed the direct child")

    def test_run_cli_timeout_is_real(self):
        t0 = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            scv.run_cli([sys.executable, "-c", PARENT], timeout=2)
        self.assertLess(time.time() - t0, 12, "the timeout gate is fake: it waited for the grandchild to wake up on its own")


class RunCliRegistration(_CleanTable):
    """`run_cli` starts a whole tree ⇒ without registering it, if the bridge dies it becomes an untraceable orphan."""

    def test_registers_and_deregisters_the_tree_when_family_is_given(self):
        with mock.patch.object(scv, "child_add", return_value=True) as add, \
                mock.patch.object(scv, "child_remove") as rem:
            scv.run_cli([sys.executable, "-c", "pass"], family="claude")
        self.assertEqual(add.call_args[0][1], "claude")
        self.assertEqual(add.call_args[0][0], rem.call_args[0][0])   # the same pid goes into the table and comes back out

    def test_deregisters_even_when_it_times_out(self):
        """⭐Deregistering has to sit in a finally: the timeout path is exactly the one that needs it the most
        (not deregistering = a dead pid left behind in the table)."""
        with mock.patch.object(scv, "child_add", return_value=True), \
                mock.patch.object(scv, "child_remove") as rem:
            with self.assertRaises(subprocess.TimeoutExpired):
                scv.run_cli([sys.executable, "-c", PARENT], timeout=2, family="claude")
        self.assertTrue(rem.called, "no deregistering happened on the timeout path ⇒ a pid that is already dead is left behind in the registry")

    def test_a_refused_registration_is_shouted_and_not_deregistered(self):
        """⭐`run_cli` is `child_add`'s first caller ⇒ it ⛔ must never ignore that False (this is a contract it
        wrote itself). It chooses "loud but keep going" ⛔ never "kill it": cutting off a real live process over a
        bookkeeping failure is worse than letting it run unregistered.
        Deregistering has to be skipped along with it — otherwise `child_remove` would complain all over again about
        an account that never existed in the first place."""
        log_path = scv.spath("bridge.log")
        before = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        with contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(scv, "child_add", return_value=False), \
                    mock.patch.object(scv, "child_remove") as rem:
                scv.run_cli([sys.executable, "-c", "pass"], family="claude")
        self.assertIs(rem.called, False, "registration was refused, and it still went and deregistered ⇒ complaining all over again about an account that never existed")
        added = (log_path.read_text(encoding="utf-8") if log_path.exists() else "")[len(before):]
        self.assertIn("registry", added)

    def test_does_not_register_when_family_is_omitted(self):
        """Zero-input control: without a family, it must **really** not register — ⛔ never make it "register it either way"."""
        with mock.patch.object(scv, "child_add") as add:
            scv.run_cli([sys.executable, "-c", "pass"])
        self.assertIs(add.called, False)

    def test_shouts_when_an_unregistered_call_gets_a_long_timeout(self):
        """⭐"Only meant for a second-scale probe" used to only live in someone's memory ⇒ use **how long this path
        is allowed to run** as the judge to back it up instead."""
        log_path = scv.spath("bridge.log")
        before = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        with contextlib.redirect_stderr(io.StringIO()):
            scv.run_cli([sys.executable, "-c", "pass"], timeout=scv.PROBE_MAX_SECONDS + 1)
        added = (log_path.read_text(encoding="utf-8") if log_path.exists() else "")[len(before):]
        self.assertIn(_GATE_LINE, added)

    def test_quiet_for_a_real_probe_and_for_a_registered_long_job(self):
        """Zero-input control on both ends: a short probe stays quiet (or it would flood the screen for nobody to
        read), and a registered long job stays quiet too (that is not the error this gate is for)."""
        log_path = scv.spath("bridge.log")
        before = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        with contextlib.redirect_stderr(io.StringIO()):
            scv.run_cli([sys.executable, "-c", "pass"], timeout=2)
            with mock.patch.object(scv, "child_add", return_value=True), \
                    mock.patch.object(scv, "child_remove"):
                scv.run_cli([sys.executable, "-c", "pass"], timeout=600, family="claude")
        added = (log_path.read_text(encoding="utf-8") if log_path.exists() else "")[len(before):]
        # ⛔Never judge the whole increment for the word "family": any future log line that mentions it would make this case **falsely red**. Pin down that exact sentence instead
        self.assertNotIn(_GATE_LINE, added)

    def test_the_length_gate_sees_the_longest_path_of_all(self):
        """🔴`timeout=None` = no cap at all = under the judge of "how long this path is", that is the **longest**
        kind there is, and it happens to be `run_cli`'s own default value ⇒ the easiest misuse to write by
        accident (forgetting both family and a timeout) is exactly the one the old gate could not see at all, and
        also the worst one."""
        log_path = scv.spath("bridge.log")
        before = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        with contextlib.redirect_stderr(io.StringIO()):
            scv.run_cli([sys.executable, "-c", "pass"])            # default timeout=None
        added = (log_path.read_text(encoding="utf-8") if log_path.exists() else "")[len(before):]
        self.assertIn(_GATE_LINE, added)

    def test_a_write_failure_while_registering_does_not_orphan_the_tree(self):
        """🔴`child_add` sits outside the `try` ⇒ if `_children_save` raises OSError (disk full / permissions /
        SCV_HOME gone), it passes straight out of `run_cli`, and that is **after** `Popen`: this tree has nobody
        to communicate with it and nobody to kill_tree it, and the caller cannot even get the pid. ⭐This is
        exactly the shape Task 2 exists to handle."""
        log_path = scv.spath("bridge.log")
        before = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        with contextlib.redirect_stderr(io.StringIO()):
            # ⛔The birth id has to be faked first: a process that exits in an instant cannot have it read on
            # Windows, and child_add would return earlier than this ⇒ this case would not be red on the path we
            # actually want to test
            with mock.patch.object(scv, "proc_start_id", return_value="fake-birth-cert"), \
                    mock.patch.object(scv, "_children_save", side_effect=OSError("the disk is full")):
                done = scv.run_cli([sys.executable, "-c", "print(42)"], family="claude")
        self.assertEqual(done.stdout.strip(), b"42")               # assert the correct value: it still ran to completion, and its output still came back
        added = (log_path.read_text(encoding="utf-8") if log_path.exists() else "")[len(before):]
        self.assertIn("the disk is full", added)                   # the OS's own words, unchanged

    def test_a_write_failure_while_deregistering_does_not_hijack_the_timeout(self):
        """Deregistering raising OSError would **override the TimeoutExpired already in flight** ⇒ the error class
        the caller sees would change completely."""
        log_path = scv.spath("bridge.log")
        before = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        with contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(scv, "child_add", return_value=True), \
                    mock.patch.object(scv, "child_remove", side_effect=OSError("the disk is full again")):
                with self.assertRaises(subprocess.TimeoutExpired):   # ⛔ must never turn into an OSError
                    scv.run_cli([sys.executable, "-c", PARENT], timeout=2, family="claude")
        # ⭐Swallowing the exception is correct, **being completely silent about it** is not: without pinning this sentence, that except block could be replaced with a bare pass and stay green
        added = (log_path.read_text(encoding="utf-8") if log_path.exists() else "")[len(before):]
        self.assertIn("the disk is full again", added)              # the OS's own words, unchanged

    def test_timeout_keeps_what_the_cli_already_printed(self):
        """Error = the original text (B21): whatever the CLI had already printed ⛔ must never be thrown away on
        the timeout path."""
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            scv.run_cli([sys.executable, "-c", CHATTY], timeout=2)
        self.assertIn(b"half an answer", caught.exception.stdout or b"")


class Orphans(_CleanTable):
    def test_sweep_kills_recorded_child(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], **scv.new_session_kw())
        scv.child_add(proc.pid, "claude")
        self.assertEqual([c["pid"] for c in scv.children()], [proc.pid])
        self.assertEqual(scv.sweep_orphans(), [proc.pid])
        _reap(proc)
        self.assertIsNotNone(proc.returncode, "nobody reaped it ⇒ on POSIX it becomes a zombie, and ps still returns a start time for a zombie ⇒ _gone() would never turn True")
        self.assertTrue(_gone(proc.pid))
        self.assertEqual(scv.children(), [])

    def test_sweep_never_kills_a_recycled_pid(self):
        """The registry's birth id not matching = that PID already belongs to somebody else's process ⇒ ⛔ must
        never be killed."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], **scv.new_session_kw())
        try:
            scv.child_add(proc.pid, "claude")
            path = scv.spath("children.json")
            rows = json.loads(path.read_text(encoding="utf-8"))
            # ⚠️Has to be **a shape this version recognizes** (starting with `ft:` on win32): after the switch to
            # ctypes, writing plain "somebody-else" would go down the "old format, cannot recognize it" path
            # instead, and this test case would no longer go through the "does it match" comparison at all
            rows[0]["born"] = scv.BIRTH_WIN + "1" if os.name == "nt" else "somebody-else"
            path.write_text(json.dumps(rows), encoding="utf-8")
            self.assertEqual(scv.sweep_orphans(), [])
            self.assertTrue(helpers.born_alive(scv.proc_start_id(proc.pid)), "wrongfully killed a process whose birth id did not match")
        finally:
            scv.kill_tree(proc)


class BirthCert(_CleanTable):
    """🔴Re-review item 9: the win32 birth id used to rely on starting a powershell each time, and serially it
    still came back rc=2 about 1% of the time — "could not tell" and "does not exist" collapsed into the same
    empty string ⇒ a perfectly fine turn got reported as crashed, and a full run turned into a lottery.
    📎 NOTES.md::birth-cert-ctypes"""

    def test_cannot_tell_is_retried_once_and_gone_is_not(self):
        """Pin down the retry policy with a stub (⛔ never rely on a real failure: the old powershell path was a 1%
        gamble, and this ctypes path can barely ever be made to fail at all)."""
        cases = (([None, "ft:1"], "ft:1", 2), (["", "ft:9"], "", 1), ([None, None], None, 2), (["ft:7", "ft:8"], "ft:7", 1))
        for seq, want, calls in cases:
            with self.subTest(seq=seq), mock.patch.object(scv, "_birth_once", side_effect=seq) as once:
                self.assertEqual(scv.proc_start_id(4242), want)
                self.assertEqual(once.call_count, calls)

    def test_child_add_says_which_kind_of_nothing_it_got(self):
        """The two kinds of "nothing" used to be the exact same sentence; ⭐the judge is that **the original words
        can tell them apart**."""
        log_path = scv.spath("bridge.log")
        for got, words in (("", "it is already gone"), (None, "could not tell its birth id")):
            with self.subTest(got=got):
                before = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
                with contextlib.redirect_stderr(io.StringIO()), _registerable(), \
                        mock.patch.object(scv, "proc_start_id", return_value=got):
                    self.assertFalse(scv.child_add(os.getpid(), "claude"))
                added = log_path.read_text(encoding="utf-8")[len(before):]
                self.assertIn(words, added)

    def test_a_real_pid_answers_every_time(self):
        """Stress test: ask a real, live pid 200 times in a row, ⛔ not one answer may be empty, ⛔ not one may be
        None, and every one must be the same.
        ⚠️The old powershell path failed serially on this machine about 1% of the time (review: 1 failure in 120
          runs) — 200 runs would expect about 2 failures, so this would have been red on the old implementation
          with roughly 87% probability (⏳ extrapolated from the 1% independent-failure estimate, was never run to
          exhaustion against the old implementation)."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **scv.new_session_kw())
        try:
            got = [scv.proc_start_id(proc.pid) for _ in range(200)]
        finally:
            scv.kill_tree(proc)
        bad = [g for g in got if not g]
        self.assertEqual(bad, [])
        self.assertEqual(len(set(got)), 1, "the same process answered with different birth ids")

    def test_a_process_that_exited_counts_as_gone(self):
        """The other half of the zero-input control: something that really has exited (even while we still hold a
        handle to it) must be "does not exist", ⛔ never "could not tell", and ⛔ it must never be handed a birth id
        either (that would make sweep think it is still alive)."""
        proc = subprocess.Popen([sys.executable, "-c", "pass"], **scv.new_session_kw())
        proc.wait(timeout=30)
        self.assertEqual(scv.proc_start_id(proc.pid), "")
        self.assertEqual(scv.proc_start_id(0), "")

    @unittest.skipUnless(os.name == "nt", "the old format only exists on win32 (POSIX's format never changed)")
    def test_an_old_format_row_is_not_swept_and_says_so(self):
        """🔴The format changed: an old version wrote .NET Ticks (plain digits) into the registry. ⭐The judge: ①
        ⛔ must never kill it (the process is still alive) ② must complain "old format" once ③ that row must be
        treated as "unrecognized", ⛔ never run through the matches/does-not-match comparison.
        ④ (re-review two, M-5) that sentence must **carry the pid and family**, and say clearly "already removed
          from the table, please end it by hand": the very end of sweep clears out the whole table, and this is the
          last time anyone will ever remember it — if the original words did not carry the pid, an entry that might
          really be an orphan left over from an old version would just be silently forgotten.
          ⭐Two rows, two families: with only one row, "reported only the first one" and "reported all of them"
          cannot be told apart."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **scv.new_session_kw())
        try:
            scv._children_save([{"pid": proc.pid, "born": "638943000000000000", "family": "claude"},
                                {"pid": 4242424, "born": "638943000000000001", "family": "codex"}])
            log_path = scv.spath("bridge.log")
            before = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
            with contextlib.redirect_stderr(io.StringIO()), mock.patch.object(scv, "_children_warned", set()), \
                    mock.patch.object(scv, "proc_start_id", side_effect=AssertionError("an old-format row must never be compared")):
                self.assertEqual(scv.sweep_orphans(), [])
            said = [x for x in log_path.read_text(encoding="utf-8")[len(before):].splitlines() if "old-format" in x]
            self.assertEqual(len(said), 1, said)
            for piece in ("pid %d (claude)" % proc.pid, "pid 4242424 (codex)", "already removed from the table", "by hand"):
                self.assertIn(piece, said[0])
            self.assertEqual(scv.children(), [], "fixture precondition: the table really is empty after sweeping (so this line is the last record of it)")
            self.assertTrue(helpers.born_alive(scv.proc_start_id(proc.pid)), "the old-format row killed a live process")
        finally:
            scv.kill_tree(proc)

    def test_a_wait_that_fails_means_cannot_tell_not_gone(self):
        """Re-review two, M-7: `WaitForSingleObject` returning `WAIT_FAILED` (0xFFFFFFFF) = **this question
        failed** ⇒ could not tell (`None`), ⛔ never "does not exist" (`""`) — the latter would make sweep think it
        is already gone, and make `child_add` say "it started and exited right away".
        ⚠️The stub only stands in for the five functions `_k32()` hands out (once it has the SYNCHRONIZE handle,
          the real API can almost never be made to land in this case); the handle still has to be closed.
          Zero-input control: the same stub with only the return value swapped to "still running" ⇒ falls through
          to `fn`."""
        closed = []
        for state, want in ((0xFFFFFFFF, None), (0x102, "got it")):
            with self.subTest(state=hex(state)):
                fake = scv._K32(OpenProcess=lambda *a: 77, GetProcessTimes=None,
                                WaitForSingleObject=lambda h, ms, _s=state: _s,
                                K32GetProcessMemoryInfo=None, CloseHandle=closed.append,
                                SetThreadExecutionState=None)
                with mock.patch.object(scv, "_k32", return_value=fake):
                    self.assertEqual(scv._win_ask(4242, scv._WIN_QUERY, lambda k, h: "got it"), want)
        self.assertEqual(closed, [77, 77], "the handle was not closed")

    def test_the_alive_ruler_is_not_blind(self):
        """Re-review two, M-8: `born_alive` is the judge shared by every "it is still alive" positive control ⇒
        each of the three values gets its own case here.
        The old code used to write `!= ""`, and after the three-value change `None` (could not tell) would also
        get treated as "alive"."""
        self.assertTrue(helpers.born_alive(scv.BIRTH_WIN + "1"))
        self.assertFalse(helpers.born_alive(""))
        self.assertFalse(helpers.born_alive(None))


class ModuleHygiene(unittest.TestCase):
    """⭐M6's structural gate. **A behavioral control cannot be built for this** — faking a class-name ordering to
    turn it red would only prove the fake harness itself is working.
    A structural assertion is deterministic: it directly asks, matching the shape of the bug, "who touched the
    registry, and did it inherit _CleanTable". It matches the exact shape of the bug: a new class name sorted ahead
    of it left a row behind, and the next test picked up the leftover."""

    TABLE_API = ("children", "child_add", "child_remove", "sweep_orphans")

    @staticmethod
    def _plain_constant_assign(stmt):
        """Only skips **a plain constant assignment** (such as this class's own TABLE_API tuple).

        ⭐The judge is aimed at the motive, ⛔ never at "class level is never looked at": a **real call** at class
        level (`rows = scv.children()`, storing `mock.patch.object(...)` as a class attribute) still has to be
        seen, or the exemption would be bigger than the hole it is meant to close."""
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)) or stmt.value is None:
            return False
        return not any(isinstance(n, (ast.Call, ast.Attribute, ast.Name)) for n in ast.walk(stmt.value))

    def _hits(self, node):
        scan = list(node.decorator_list)   # ⭐class-level decorators are in the scanning surface too (this file already uses class-level decorators)
        scan += [s for s in node.body if not self._plain_constant_assign(s)]
        found = []
        for root in scan:
            for n in ast.walk(root):
                if isinstance(n, ast.Attribute) and n.attr in self.TABLE_API:
                    found.append(n)
                elif isinstance(n, ast.Constant) and n.value in self.TABLE_API:
                    found.append(n)        # a string counts too: mock.patch.object(scv, "child_add") has it as a constant
                elif isinstance(n, ast.Call) and _call_tail(n) == "run_cli" \
                        and any(kw.arg == "family" for kw in n.keywords):
                    found.append(n)        # ⭐run_cli is itself a door into the table, but ⛔ only touches it when given a family
        return found

    def _classes_touching(self, tree):
        return [n.name for n in tree.body
                if isinstance(n, ast.ClassDef) and not n.name.startswith("_") and self._hits(n)]

    def _scan(self):
        with io.open(__file__, encoding="utf-8") as f:
            return self._classes_touching(ast.parse(f.read()))

    def test_scanner_sees_all_three_blind_spots(self):
        """Positive control: each of the three blind spots gets its own **synthetic sample**, ⛔ never rely only on
        a shape the real file happens to already have.
        The last class is the false-positive-side control: `run_cli` without a family never touches the table, ⛔
        it must never be named."""
        probe = ast.parse('''
@mock.patch.object(scv, "child_add")
class ViaClassDecorator(unittest.TestCase):
    pass

class ViaClassLevelCall(unittest.TestCase):
    TABLE_API = ("a plain constant assignment", "does not count as touching the table")
    patcher = mock.patch.object(scv, "child_remove")

class ViaRunCliWithFamily(unittest.TestCase):
    def go(self):
        scv.run_cli(["x"], family="claude")

class RunCliWithoutFamilyDoesNotTouchTheTable(unittest.TestCase):
    def go(self):
        scv.run_cli(["x"], timeout=2)
''')
        self.assertEqual(self._classes_touching(probe),
                         ["ViaClassDecorator", "ViaClassLevelCall", "ViaRunCliWithFamily"])

    def test_scanner_is_not_blind_on_the_real_file(self):
        """The other half of the positive control: it also has to recognize the real file, or the case below would
        just be an empty set inside an empty set — a vacuous green."""
        touching = self._scan()
        self.assertIn("ChildTable", touching)
        self.assertIn("RunCliRegistration", touching)
        self.assertNotIn("KillTree", touching)            # it calls run_cli but without a family ⇒ ⛔ must never be a false positive
        self.assertGreaterEqual(len(touching), 4)

    def test_every_class_touching_the_table_cleans_up_after_itself(self):
        offenders = [name for name in self._scan() if not issubclass(globals()[name], _CleanTable)]
        self.assertEqual(offenders, [], "these classes touch the registry but do not inherit _CleanTable ⇒ they only got a clean table by luck of alphabetical order")


class KeepAwakeUnit(unittest.TestCase):
    """A job running, or the last one ended less than window_s ago => tick pokes (resets the system idle timer);
    otherwise it does not. window_s <= 0 => never pokes."""

    def _ka(self, window=600.0):
        now, pokes = [1000.0], []
        ka = scv.KeepAwake(window, poke=lambda: pokes.append(now[0]), clock=lambda: now[0])
        return ka, now, pokes

    def test_idle_bridge_never_pokes(self):
        ka, _now, pokes = self._ka()
        self.assertFalse(ka.tick())
        self.assertEqual(pokes, [])

    def test_a_running_job_pokes_and_the_tail_lasts_window_s(self):
        ka, now, pokes = self._ka(600.0)
        ka.begin()
        self.assertTrue(ka.tick())
        now[0] += 3600                     # one job ran for an hour: still running => still pokes
        self.assertTrue(ka.tick())
        ka.end()
        now[0] += 599
        self.assertTrue(ka.tick(), "599 s after the last job ended: still inside the tail")
        now[0] += 2
        self.assertFalse(ka.tick(), "past 600 s: no more pokes")
        self.assertEqual(len(pokes), 3)
        self.assertEqual(ka.pokes, 3)

    def test_two_jobs_overlap(self):
        ka, now, _p = self._ka(10.0)
        ka.begin(); ka.begin(); ka.end()
        now[0] += 100
        self.assertTrue(ka.wanted(), "one is still running")
        ka.end()
        now[0] += 11
        self.assertFalse(ka.wanted())

    def test_zero_window_is_off(self):
        ka, _now, pokes = self._ka(0)
        ka.begin()
        self.assertFalse(ka.tick())
        self.assertEqual(pokes, [])

    def test_a_poke_that_raises_is_logged_not_fatal(self):
        ka = scv.KeepAwake(600.0, poke=lambda: (_ for _ in ()).throw(OSError("denied")))
        ka.begin()
        with mock.patch.object(scv, "log") as lg:
            self.assertFalse(ka.tick())
        self.assertIn("stay awake", lg.call_args[0][0])

    @unittest.skipUnless(sys.platform == "win32", "win32 only")
    def test_the_real_poke_on_windows(self):
        """Positive control: really call SetThreadExecutionState once (without ES_CONTINUOUS: it only resets the
        timer once and leaves no state behind). A non-zero return means it worked."""
        self.assertNotEqual(scv._k32().SetThreadExecutionState(scv.ES_SYSTEM_REQUIRED), 0)

    def test_quiet_since_counts_from_the_later_of_waking_and_the_last_job(self):
        """0.3.0 (spec B38): the sleep clock starts at the later of "woke up" and "the last job ended"; `None` while a
        job runs. ⭐The same `_busy`/`_last` this class already keeps — never a second tally of jobs."""
        now = [100.0]
        ka = scv.KeepAwake(600.0, poke=lambda: None, clock=lambda: now[0])
        self.assertEqual(ka.quiet_since(50.0), 50.0, "no job yet: counts from waking")
        ka.begin()
        self.assertIsNone(ka.quiet_since(50.0), "a job is running")
        now[0] = 150.0
        ka.end()
        self.assertEqual(ka.quiet_since(50.0), 150.0, "the job ended after waking")
        self.assertEqual(ka.quiet_since(200.0), 200.0, "woke again after the job")

    def test_config_seconds(self):
        """`keep_awake_s` and 0.3.0's `idle_sleep_s` share one reader: missing ⇒ 600, a number ≥ 0 ⇒ that (0 is
        allowed: "off"), anything else ⇒ 600 and one line naming the key."""
        with mock.patch.object(scv, "log") as lg:
            self.assertEqual(scv._config_seconds({}, "idle_sleep_s"), 600.0)
            self.assertEqual(scv._config_seconds({"idle_sleep_s": 0}, "idle_sleep_s"), 0.0)
            self.assertEqual(scv._config_seconds({"idle_sleep_s": "10"}, "idle_sleep_s"), 600.0)
            self.assertEqual(scv._config_seconds({"idle_sleep_s": True}, "idle_sleep_s"), 600.0)
        self.assertEqual(lg.call_count, 2)
        self.assertIn("idle_sleep_s", lg.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
