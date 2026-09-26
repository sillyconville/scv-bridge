# -*- coding: utf-8 -*-
"""Local job log plus local rate limiting (B30, section 9-(7)).

⭐This test file tests two things whose shape is completely different:
  (1) What gets recorded -- `write()`'s signature has no parameter at all that could carry a prompt or an answer.
      The check is an assertion on the signature itself, never "I checked that I did not pass one" (the latter does
      not protect the next call site).
  (2) How fast it grows -- `jobs.log` and `bridge.log` are the same family (append-only, driven by input from the
      network side), and the Task 8 audit filed the latter as B4. What is pinned here is "this family has only one
      place with the code" plus "the bytes it takes on disk have an upper bound".
"""
import ast
import contextlib
import inspect
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests import helpers
from tests.test_00_budget import call_sites   # ⭐borrow that one scanner, never copy a second one in here

import scv  # noqa: E402

NL = chr(10)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with io.open(os.path.join(ROOT, "scv.py"), encoding="utf-8") as _f:   # never a bare open: a leaked handle throws a ResourceWarning
    SRC = _f.read()
TREE = ast.parse(SRC)


def setUpModule():
    helpers.fresh_home("joblog", unittest.addModuleCleanup)


def append_openers(tree):
    """Places that `open()` a file in append mode (returns the enclosing function's name).

    🔴The gate and the positive control share this one function, never copy a second one into a test case: if the
      copy gets broken, the gate's own line stays green just the same (`test_00_budget.py`'s `_no_backslash` /
      `pointer_ok` in this same repository exist for exactly this reason).
    ⭐Recognize open by the last segment of its dotted full name => `open()` / `io.open()` / `p.open()` are all
      covered by one check, while `subprocess.Popen`'s last segment is `Popen`, so it is never caught by mistake.
    ⚠️mode's position is not fixed: in `open(p, "a")` it is the 2nd position, in `p.open("a")` it is the 1st
      position, and `io.open(p, "a")` looks exactly like the latter (both are an Attribute) => statically they
      cannot be told apart. So both positions are checked, and a file name is screened out by asking "is this
      literal a valid mode string" (`"x.log"` has a dot => it is not a mode). ⭐This tradeoff would rather over-flag:
      a gate that misses something is worse than no gate at all.
    🔴The first version only looked at the 2nd position => `p.open("a")` was missed entirely, and the positive
      control below is what caught it.

    🔴🔴The blind spot is non-empty, this gate being green never means the rule is fully upheld everywhere (review
      tried 10 different ways to write it, and 5 of them got away): (1) the mode sitting in a variable
      (`MODE = "a"; open(p, MODE)`), (2) a mode computed at runtime, (3) `os.open(p, os.O_APPEND)`,
      (4) `open(p, "r+")` plus `seek(0, 2)`, (5) an alias like `op = open`.
      What it catches is the most likely one -- copy a line like `open(spath("x.log"), "a", encoding="utf-8")"`
      right after `log()`. This sentence has to travel with the gate: the settled-idioms section item was retired
      precisely because this gate exists, and whoever reads the gate needs to know where its boundary is."""
    found = set()

    def dotted(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            head = dotted(node.value)
            return (head + "." + node.attr) if head else node.attr
        return ""

    def is_append_mode(node):
        v = node.value if isinstance(node, ast.Constant) else None
        return isinstance(v, str) and "a" in v and set(v) <= set("rwxabt+U")

    def visit(node, fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node.name
        if isinstance(node, ast.Call) and dotted(node.func).split(".")[-1] == "open":
            spots = list(node.args[:2]) + [kw.value for kw in node.keywords if kw.arg == "mode"]
            if any(is_append_mode(x) for x in spots):
                found.add(fn)
        for child in ast.iter_child_nodes(node):
            visit(child, fn)

    visit(tree, "<module>")
    return found


class Log(unittest.TestCase):
    def test_row_shape(self):
        j = scv.JobLog()
        j.write(leg="remote", model="claude/haiku", klass="ok", usage={"input_tokens": 3}, latency_s=1.2,
                ttfc=0.4, queued_ms=0, rebuilt=None, job_id="j1")
        row = j.tail(1)[0]
        self.assertEqual({k: row[k] for k in ("leg", "model", "klass", "job_id")},
                         {"leg": "remote", "model": "claude/haiku", "klass": "ok", "job_id": "j1"})
        self.assertIn("ts", row)

    def test_signature_cannot_carry_text(self):
        params = set(inspect.signature(scv.JobLog.write).parameters) - {"self"}
        self.assertEqual(params, {"leg", "model", "klass", "usage", "latency_s", "ttfc", "queued_ms", "rebuilt",
                                  "job_id", "cli_version"})
        # ⭐`cli_version` was added in Task 11: `model` records a name, never an identity (the same name can point
        #   to a different build at different times) => tallying BYOK usage off this log alone would silently
        #   undercount. Its value is confined by `_cli_version()` to a shape like `1.2.3` or an empty string =>
        #   it still cannot hold free text, this gate's nature has not been loosened.
        self.assertEqual(scv._cli_version("1.2.3 (Claude Code)"), "1.2.3")
        self.assertEqual(scv._cli_version("the version command never ran: … C:/Users/somebody/AppData/claude.cmd"), "")
        self.assertEqual(scv._cli_version("codex-cli 0.46.0"), "0.46.0")
        # a version-shaped directory in the quoted executable is never the CLI's version (the first CI run gave claude
        # the runner's Python version, 3.12.10, out of `…/hostedtoolcache/windows/Python/3.12.10/x64/python.exe`)
        sep = chr(92)
        timed_out = ("the version command never ran: Command '['C:%shostedtoolcache%swindows%sPython%s3.12.10%sx64%spython.exe',"
                     " '--version']' timed out after 10 seconds" % ((sep,) * 6))
        self.assertEqual(scv._cli_version(timed_out), "")
        self.assertEqual(scv._cli_version("[WinError 5] Access is denied: 'C:/Users/a/AppData/Roaming/nvm/v20.11.0/claude.cmd'"), "")

    def test_remote_rate_limit_and_expiry(self):
        j = scv.JobLog()
        self.assertEqual([j.allow_remote(2), j.allow_remote(2), j.allow_remote(2)], [True, True, False])
        j._remote.clear()
        j._remote.extend([time.time() - 3700, time.time() - 3650])       # more than an hour ago does not count
        self.assertTrue(j.allow_remote(2))

    def test_the_window_is_an_hour_not_a_minute(self):
        """The test above only proves that the old ones get dropped -- it cannot tell an hour apart from a minute: a
        60-second window would come out just as green under it (firing three in a row still gives T/T/F, and the two
        from 3650s ago still expire). The window's length has to be pinned from the other end: the ones still inside
        the window must still count. This is never a repeat, it is the other half of the same invariant."""
        j = scv.JobLog()
        j._remote.extend([time.time() - 3500, time.time() - 1800])       # 58 minutes ago, 30 minutes ago: both still inside the window
        self.assertFalse(j.allow_remote(2))

    def test_zero_per_hour_means_no_remote_at_all(self):
        """`remote_jobs_per_hour: 0` is how the user turns the remote leg off => it must truly let not a single one
        through."""
        self.assertFalse(scv.JobLog().allow_remote(0))


class _CappedHome(unittest.TestCase):
    """A clean SCV_HOME for every test case, plus a much smaller bound (never actually write 4 MiB)."""

    def setUp(self):
        self.base = os.environ["SCV_HOME"]
        self.home = tempfile.mkdtemp(prefix="case-", dir=self.base)
        os.environ["SCV_HOME"] = self.home
        self.cap, scv.LOG_CAP_BYTES = scv.LOG_CAP_BYTES, 4096
        self.warned, scv._log_warned = scv._log_warned, False
        self.rwarn, scv._rotate_warned = scv._rotate_warned, set()

    def tearDown(self):
        scv.LOG_CAP_BYTES = self.cap
        scv._log_warned = self.warned
        scv._rotate_warned = self.rwarn
        os.environ["SCV_HOME"] = self.base
        shutil.rmtree(self.home, ignore_errors=True)

    def _bytes(self, stem):
        """⚠️The ruler itself needs a lower bound (review I-D): when the glob used to go blind (the log moved / got
        renamed => not a single file matched) it returned 0, and all three places using it assert "<= the upper
        bound" => 0 is <= any upper bound, and `test_60_joblog`'s 32 test cases all came out green -- the same
        shape as I-1. => here it first pins "the file was really matched", and the caller separately pins "it
        actually reached the cap" (`_bounded`)."""
        hits = list(scv.state_dir().glob(stem + "*"))
        self.assertIn(stem, [p.name for p in hits], "the ruler did not measure %s: did it move or get renamed?" % stem)
        return sum(p.stat().st_size for p in hits)

    def _bounded(self, stem, upper):
        """Bytes on disk must land in [LOG_CAP_BYTES, upper]. ⭐The lower bound is never decoration: the fixture in
        all three places has been fed past the cap and rotated at least once (the `.1` file alone is roughly one
        cap's worth) => measuring less than the cap means the ruler did not measure anything."""
        got = self._bytes(stem)
        self.assertGreaterEqual(got, scv.LOG_CAP_BYTES, "%s only measured %d bytes: is the ruler blind, or did the fixture not feed it enough?" % (stem, got))
        self.assertLessEqual(got, upper)

    def _rows(self, name):
        p = scv.state_dir() / name
        return [x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []

    def _fill_to_cap(self, name):
        """Feed it one line at a time until "the next one should rotate", never hardcode "after N lines" -- line
        length varies. Returns how many lines it fed."""
        i = 0
        while True:
            p = scv.state_dir() / name
            if p.exists() and p.stat().st_size >= scv.LOG_CAP_BYTES:
                return i
            scv._append_capped(name, "r%03d " % i + "x" * 200)
            i += 1
            self.assertLess(i, 2000, "fed 2000 lines and still never hit the cap => this test never tested anything")


class Bounded(_CappedHome):
    """The B4 family: append-only, driven by input from the network side => bytes taken on disk must have an upper
    bound."""

    def _write_n(self, j, n, start=0):
        for i in range(start, start + n):
            j.write(leg="remote", model="claude/haiku", klass="ok", usage={"input_tokens": i}, latency_s=1.0,
                    ttfc=0.1, queued_ms=0, rebuilt=None, job_id="j%d" % i)

    def test_jobs_log_stays_bounded(self):
        j = scv.JobLog()
        self._write_n(j, 400)
        row_len = len(self._rows("jobs.log")[-1].encode("utf-8")) + 1
        self.assertGreater(row_len * 400, 3 * scv.LOG_CAP_BYTES,
                           "the fixture did not feed enough: 400 lines never went over the cap to begin with => the next line vacuously passed, it never really passed")
        self._bounded("jobs.log", 2 * scv.LOG_CAP_BYTES + row_len)
        self.assertEqual(json.loads(self._rows("jobs.log")[-1])["job_id"], "j399")   # the newest line must never be eaten by rotation

    def test_tail_spans_the_rotation(self):
        """The instant right after a rotation happens, `tail(20)` must never quietly give back fewer lines --
        "returned only 3 lines" and "there are only 3 lines total" look identical to whoever is reading the log, and
        the latter is false (the other 17 lines are sitting in the neighboring file).

        ⭐The scene is fed one line at a time, never computed: line length varies with `job_id`, and hardcoding "how
          many lines are left in the current one after 400 lines" would pin the conclusion on an incidental property
          of the fixture."""
        j, i = scv.JobLog(), 0
        while not (self._rows("jobs.log.1") and len(self._rows("jobs.log")) == 3):
            self._write_n(j, 1, start=i)
            i += 1
            self.assertLess(i, 2000, "fed 2000 lines and the scene of \"the neighbor has content, only 3 lines left in the current one\" never showed up")
        self.assertGreaterEqual(len(self._rows("jobs.log.1")), 17,
                                "the neighboring file has fewer than 17 lines => tail(20) could never be filled to begin with, this test tested nothing")
        self.assertEqual([r["job_id"] for r in j.tail(20)], ["j%d" % k for k in range(i - 20, i)])

    def test_rotation_name_is_checked_on_the_first_write(self):
        """If a rotation target (`<name>.1`) is forgotten from registration in `WRITES`, it must blow up on the very
        first line -- never blow up only on the day it finally hits the cap: by then no test is running, and the
        scene (`os.replace` throwing ValueError) is already far removed from the cause.
        ⭐This borrows `latest.json` as the sample: it is in `WRITES`, while `latest.json.1` is not."""
        with self.assertRaises(ValueError):
            scv._append_capped("latest.json", "x")
        self.assertFalse((scv.state_dir() / "latest.json").exists())   # never leave half a file behind

    def test_one_fat_line_cannot_blow_the_bound(self):
        """🔴C-1's gate: feeding fat lines and normal lines in alternation, disk usage must still stay within
        `2×(CAP + the per-line cap)`. ⭐This case does not repeat `test_jobs_log_stays_bounded`: that one feeds
        nothing but normal lines, and can never reach this hole at all."""
        for i in range(40):
            scv._append_capped("jobs.log", ("z" * (10 * scv.LOG_CAP_BYTES)) if i % 2 else "r%03d" % i)
        self._bounded("jobs.log", 2 * (scv.LOG_CAP_BYTES + scv.LINE_CAP_BYTES))
        fat = [x for x in self._rows("jobs.log") if x.startswith("z")]
        self.assertTrue(fat and all("truncated" in x for x in fat), "the fat line got cut but never said so => data was silently dropped")

    def test_a_fat_job_id_still_leaves_a_parseable_row(self):
        """I-1: `job_id` is the second free-text opening in the signature (review measured 41145 bytes landing on
        disk verbatim).

        ⭐Field-level truncation must never rely on `_append_capped`'s fallback: cutting the whole line would slice
          the JSON into half a line, and `tail()` hitting that half-line just throws => a single 41 KB job_id can
          kill the read path for the entire audit log."""
        j = scv.JobLog()
        j.write(leg="remote", model="claude/haiku", klass="ok", usage={"input_tokens": 1}, latency_s=1.0,
                ttfc=0.1, queued_ms=0, rebuilt=None, job_id="J" + "z" * 40960)
        row = j.tail(1)[0]                                    # reading it back = it was not sliced into half a line
        self.assertTrue(row["job_id"].startswith("Jzzz"))
        self.assertIn("truncated", row["job_id"])
        self.assertLess(len(row["job_id"].encode("utf-8")), scv.LINE_CAP_BYTES)

    def test_a_rotation_leaves_a_mark_at_the_top_of_the_new_generation(self):
        """I-3: a successful rotation is completely silent (review measured: 0 bytes on stderr, the new generation's
        first line is directly `r046`) => whoever reads the log gets a file that starts in the middle, and "this is
        all of it" looks identical to "the previous generation is right next to it"."""
        self._fill_to_cap("bridge.log")
        scv._append_capped("bridge.log", "轮转之后的第一行")
        first = self._rows("bridge.log")[0]
        self.assertIn("bridge.log.1", first)                  # says plainly where the previous generation went
        self.assertIn("previous generation", first)
        self.assertEqual(self._rows("bridge.log")[1], "轮转之后的第一行")   # the marker must never displace the real content

    def test_a_rotation_that_cannot_happen_shouts_once(self):
        """I-4: the "inferred" claim in the report got manufactured by review -- while another handle holds
        `jobs.log` down, disk usage grows to 18 times over, with zero noise the whole time. => "the cap might as
        well not exist when it keeps failing, and nobody knows" is not a risk hypothesis, it is a reproducible
        scene.

        Never route this shout through `log()`: `log()` itself holds `_log_lock` outside of `_append_capped` =>
        recursion plus deadlock."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch("os.replace", side_effect=OSError(13, "被按住了")):
            self._fill_to_cap("jobs.log")
            for i in range(20):
                scv._append_capped("jobs.log", "r%03d " % i + "x" * 200)
        shouts = [x for x in err.getvalue().splitlines() if "cannot be rotated" in x]
        self.assertEqual(len(shouts), 1, shouts)              # shouting once is enough, never spam the screen
        self.assertIn("被按住了", shouts[0])                   # the OS's own words, not one changed

    def test_each_name_shouts_its_own_rotation_failure(self):
        """🔴I-C: "shout once" used to mean once per whole process, never once per name => `bridge.log` fails first
        and uses up that one shout, and `jobs.log` failing right after it makes not a single sound (measured: disk
        usage kept growing by 4275 bytes with zero noise the whole time). ⭐The lock is already per-name, this flag
        just forgot to follow suit -- the same principle only got half implemented."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch("os.replace", side_effect=OSError(13, "被按住了")):
            for name in ("bridge.log", "jobs.log"):
                self._fill_to_cap(name)
                for i in range(5):
                    scv._append_capped(name, "r%03d " % i + "x" * 200)
        shouts = [x for x in err.getvalue().splitlines() if "cannot be rotated" in x]
        self.assertEqual(len(shouts), 2, shouts)                      # once per each of the two names
        self.assertEqual({x.split(" ")[0] for x in shouts}, {"bridge.log", "jobs.log"})

    def test_a_row_that_cannot_fit_falls_back_to_a_whole_skeleton(self):
        """🔴I-D(b): the field-level cut only handles `isinstance(v, str)`, while `usage` is a dict whose values are
        numbers => it is not constrained at all. Measured: `{"input_tokens": 10**4000}` => the whole line gets cut
        by `_append_capped` from the middle => `tail()` throws `JSONDecodeError`.

        ⭐The fix is to measure once after `json.dumps`, and fall back to a minimal but valid line if it is over =>
          never let `_append_capped` cut it from the middle: this way "every line of `jobs.log` is complete JSON"
          holds by construction, rather than resting on the unwritten assumption that "every other field is a closed
          set"."""
        j = scv.JobLog()
        j.write(leg="remote", model="claude/haiku", klass="ok", usage={"input_tokens": 10 ** 4000},
                latency_s=1.0, ttfc=0.1, queued_ms=0, rebuilt=None, job_id="j-fat")
        raw = self._rows("jobs.log")[-1]
        self.assertLessEqual(len(raw.encode("utf-8")), scv.LINE_CAP_BYTES)
        row = json.loads(raw)                                         # reading it back = complete JSON
        self.assertEqual(row["job_id"], "j-fat")                      # still recognizable as which call it was
        self.assertIn("only the skeleton was kept", row["scv_note"])

    def test_even_an_all_hostile_row_stays_one_parseable_line(self):
        """The fixture is used the other way around: every single opening is stuffed to the limit (a control
        character gets blown up by `json.dumps` into 6 characters), and the line must still be one line, complete
        JSON, within the cap. ⭐This one pins the "holds by construction" claim itself."""
        j = scv.JobLog()
        junk = (chr(1) + chr(2) + "题面") * 4000
        j.write(leg=junk, model=junk, klass=junk, usage={"input_tokens": 10 ** 4000, junk: junk},
                latency_s=1.0, ttfc=0.1, queued_ms=0, rebuilt=junk, job_id=junk)
        rows = self._rows("jobs.log")
        self.assertEqual(len(rows), 1)                                # one line, never sliced into several pieces
        self.assertLessEqual(len(rows[0].encode("utf-8")), scv.LINE_CAP_BYTES)
        json.loads(rows[0])

    def test_tail_skips_the_rotation_mark(self):
        """A rotation marker is metadata, never a job => it must never sneak into the pile of lines `tail()` returns
        (if it did, the consumer gets a dict with no `leg` / `model`, and that happens silently)."""
        j = scv.JobLog()
        self._write_n(j, 400)
        self.assertIn("previous generation", self._rows("jobs.log")[0], "fixture precondition: the current file really does start with a marker line")
        rows = j.tail(20)
        self.assertEqual(len(rows), 20)
        self.assertTrue(all("job_id" in r for r in rows), rows)

    def test_bridge_log_is_capped_too(self):
        """B4 itself. ⭐This family has only two members (counted: `config.json` / `children.json` are rewritten
        whole, `work/` / `tmp/` belong to the session family) => the fix is to make them share one place with the
        code, never "put a cap on `jobs.log` and leave `bridge.log` for a later round"."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for i in range(400):
                scv.log("x" * 200)
        self._bounded("bridge.log", 2 * scv.LOG_CAP_BYTES + 4096)
        self.assertIn("x" * 200, self._rows("bridge.log")[-1])


class OddCharsInJobId(_CappedHome):
    """Task 14 re-review 3, independent check 6: the remote `job_id` is only checked for "is a str, 1-128
    characters", the characters themselves are unrestricted. `json.dumps(ensure_ascii=False)` only escapes C0 => DEL
    / C1 / U+2028 / U+2029 land on disk verbatim (a terminal eats C1 when a human `type`s this file); and `_read`
    used to use `splitlines()`, where U+0085 / U+2028 would slice one line of JSON into two => `tail()` throws
    `JSONDecodeError` -- a single character kills the read path for the whole audit log.
    ⭐Two-part fix, three assertions, each pinning its own half: (1) the write side swaps them for `\\uXXXX` right
      after `json.dumps` (there are no raw bytes on disk); (2) `_read` only cuts on the newline character (it can
      still read back a line written before the upgrade -- those still carry the raw characters, sitting on disk
      until rotated away); (3) the round trip is equal (what gets swapped in has to be a legal JSON escape: swapping
      it for `?` instead would leave (1) and (2) both green, and only this one goes red)."""
    ODD = "JID<" + chr(0x9b) + chr(0x85) + chr(0x2028) + chr(0x2029) + chr(127) + chr(27) + ">END"

    def write(self, j):
        j.write(leg="remote", model="claude/haiku", klass="ok", usage={"input_tokens": 1}, latency_s=1.0,
                ttfc=0.1, queued_ms=0, rebuilt=None, job_id=self.ODD)

    def test_the_disk_holds_none_of_them_raw(self):
        self.write(scv.JobLog())
        raw = (scv.state_dir() / "jobs.log").read_bytes()
        self.assertIn(b'"job_id": "JID<', raw)                         # positive control: that line really did land on disk
        self.assertIn(b'>END"', raw)
        for c in self.ODD[4:-4]:
            with self.subTest(char=hex(ord(c))):
                self.assertNotIn(c.encode("utf-8"), raw)

    def test_tail_reads_back_a_line_an_older_version_wrote(self):
        """The pre-upgrade way of writing = the same write path (`_append_capped`), just without the escaping step
        after `json.dumps`."""
        scv._append_capped("jobs.log", json.dumps({"ts": "2026-09-24T00:00:00", "leg": "remote", "job_id": self.ODD},
                                                  ensure_ascii=False))
        self.assertIn(chr(0x2028).encode("utf-8"), (scv.state_dir() / "jobs.log").read_bytes())   # precondition: it really is raw bytes on disk
        self.assertEqual([r["job_id"] for r in scv.JobLog().tail(5)], [self.ODD])

    def test_the_job_id_round_trips(self):
        j = scv.JobLog()
        self.write(j)
        self.assertEqual([r["job_id"] for r in j.tail(5)], [self.ODD])

    def test_the_line_cap_is_measured_after_escaping(self):
        """Re-review 4 m-e: "escape first, then measure `over`" is load-bearing. A line that is within the cap
        unescaped but goes over once escaped must fall back to the skeleton; measuring it the other way around
        leaves it getting cut into half a line by `_append_capped`, `tail()` throws, and the read path for the whole
        log dies (re-review 4's cut R9: the probe measured three half-lines)."""
        jid, big = chr(0x9b) * 128, 10 ** 1499                  # job_id 256 raw bytes -> 768 bytes once escaped; usage a 1500-digit number
        row = {"ts": "2026-09-25T00:00:00", "leg": "remote", "job_id": jid, "model": "claude/haiku", "cli_version": "",
               "klass": "ok", "usage": {"input_tokens": big}, "latency_s": 1.0, "ttfc": 0.1, "queued_ms": 0, "rebuilt": None}
        cap = scv.LINE_CAP_BYTES - 2
        self.assertLessEqual(len(json.dumps(row, ensure_ascii=False).encode("utf-8")), cap)   # precondition: unescaped it is within the cap
        self.assertGreater(len(scv.JobLog._line(row).encode("utf-8")), cap)                     # precondition: escaped it goes over
        j = scv.JobLog()
        j.write(leg="remote", model="claude/haiku", klass="ok", usage={"input_tokens": big}, latency_s=1.0,
                ttfc=0.1, queued_ms=0, rebuilt=None, job_id=jid)
        got = j.tail(1)[0]                                       # reading it back = it was not sliced into half a line
        self.assertIn("only the skeleton was kept", got["scv_note"])
        self.assertEqual(got["job_id"], jid[:32])


class UsageIsNumbersOnly(unittest.TestCase):
    """The other half of I-1. ⭐The sanitizing point sits at the driver layer, never in `JobLog.write`: the same
    `usage` also flows into Task 10's response and Task 11's remote leg -- scrubbing it only at the point it lands on
    disk would still let both of those paths send anything the CLI stuffed in out onto the network verbatim.

    This test lives in this file because it guards the same guarantee ("never record the text") on the other half,
    the `usage` dict: the signature gate pins down the parameter names, it cannot pin down what the values can
    carry."""

    def setUp(self):
        scv._usage_warned.clear()

    def test_known_numbers_survive_both_families(self):
        self.assertEqual(scv.usage_numbers({"input_tokens": 3, "output_tokens": 0}, "claude"),
                         {"input_tokens": 3, "output_tokens": 0})
        self.assertEqual(scv.usage_numbers({"cached_input_tokens": 0, "reasoning_output_tokens": 7}, "codex"),
                         {"cached_input_tokens": 0, "reasoning_output_tokens": 7})

    def test_a_field_that_is_not_a_number_is_dropped_and_shouted(self):
        """🔴On the codex side, "constructed by name" only pins down the set of keys -- `last.get("inputTokens")`'s
        value still gets copied over verbatim => if the CLI puts a chunk of text into `inputTokens`, it goes into
        `jobs.log` verbatim too."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = scv.usage_numbers({"input_tokens": "整段题面在这里"}, "codex")
        self.assertEqual(out, {})
        self.assertIn("input_tokens", err.getvalue())
        self.assertNotIn("整段题面在这里", err.getvalue())   # never copy it back out again while shouting about it

    def test_an_unknown_field_is_dropped_and_shouted_not_silently_eaten(self):
        """Never drop it silently: the day the CLI adds a useful field, dropping it silently means nobody finds out
        (a silent mistake leaves all three tables green)."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = scv.usage_numbers({"input_tokens": 3, "service_tier": "standard"}, "claude")
        self.assertEqual(out, {"input_tokens": 3})
        self.assertIn("service_tier", err.getvalue())

    def test_it_shouts_once_per_field_not_once_per_turn(self):
        """If it were one line per turn, `bridge.log` would get flooded by a new field that is present on every
        single turn -- and we just put a cap on it."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for _ in range(5):
                scv.usage_numbers({"service_tier": "standard"}, "claude")
        self.assertEqual(len([x for x in err.getvalue().splitlines() if "service_tier" in x]), 1)

    def test_a_usage_that_is_not_a_dict_is_dropped_and_shouted(self):
        """🔴A regression newly introduced this round (I-B): `(raw or {})` only guards against falsy values => a
        truthy non-dict throws `AttributeError` outright. And this call site is `turn()`'s very last `return`
        line, outside of `_fail`, with no try around it at all => a turn that should have succeeded turns into an
        exception with no klass, never written to disk, never going through `_fail`. The line before this change
        was `ev.get("usage") or {}` -- which swallows any type at all.
        ⚠️The structural gate in this same file only scans for `raise`, and is inherently blind to an exception that
          never gets wrapped."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for bad in ("一段文本", ["列表"], 7):
                self.assertEqual(scv.usage_numbers(bad, "claude"), {})
        self.assertIn("str", err.getvalue())                  # says plainly what type it is
        self.assertNotIn("一段文本", err.getvalue())           # never copy it back out again

    def test_an_absurd_magnitude_is_dropped_and_shouted(self):
        """I-D(a): being a numeric type is never the same as being a value that can land on disk -- `10**4000` is an
        int, and after `json.dumps` it is 4001 characters, one field alone can push the whole line past the
        per-line cap (that is exactly the NC-11 failure mode coming back in through a different door)."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(scv.usage_numbers({"input_tokens": 10 ** 4000}, "claude"), {})
            self.assertEqual(scv.usage_numbers({"output_tokens": float("inf")}, "codex"), {})
        self.assertIn("input_tokens", err.getvalue())
        self.assertIn("output_tokens", err.getvalue())

    def test_a_bool_is_not_a_number(self):
        """`isinstance(True, int)` is `True` => without an explicit guard, `True` would get recorded as a token
        count."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(scv.usage_numbers({"input_tokens": True}, "claude"), {})

    def test_both_drivers_route_through_this_one_whitelist(self):
        """⭐There must only ever be one whitelist (the lead's ruling): writing it once per family means fixing one
        side and missing the other. The check is the AST, never "I checked that both places call it" -- `assertEqual`
        pins both ends (missing a call at either one goes red just the same)."""
        callers = call_sites(TREE, lambda n: n.split(".")[-1] == "usage_numbers")
        self.assertEqual(len(callers), 2, callers)                         # two places, never more, never fewer
        # ⭐Recognize it by the enclosing class, never by the innermost function name: the codex call site sits
        #   inside a `pred` closure inside `turn()` -- if the function name were the check, renaming the closure
        #   would make this gate report a difference nobody can make sense of.
        found = set()
        for cls in [n for n in ast.walk(TREE) if isinstance(n, ast.ClassDef)]:
            if any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "usage_numbers"
                   for c in ast.walk(cls)):
                found.add(cls.name)
        self.assertEqual(found, {"ClaudeDriver", "CodexDriver"})

    def test_the_codex_rename_table_lands_inside_the_whitelist(self):
        """The codex side renames camelCase before going through the whitelist => wherever the rename table sends
        things must be inside the whitelist, or else its own constructed usage would get dropped entirely by its own
        whitelist (and dropped loudly, which looks even worse)."""
        self.assertEqual(set(scv.CODEX_USAGE_KEYS.values()),
                         {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"})
        self.assertEqual(set(scv.CODEX_USAGE_KEYS.values()) - set(scv.USAGE_KEYS), set())


class Clip(unittest.TestCase):
    """C-1: without a cap on a single line, the claim "each name takes at most 2×LOG_CAP_BYTES on disk" is false --
    the cap is measured against the file that already exists, before the write, and puts zero constraint on this
    line itself (review measured a 5x blowout: a single line at 10× the cap => 40970 bytes on disk).

    ⚠️This is a separate matter from the settled idiom "fold, never truncate": that one governs the raw text at the
    entry point (fold it into one line, drop not a single character, hand the raw text to the caller exactly as
    given), this one governs writing it out to disk on the way out -- diagnostics need the beginning of the raw
    text, never the entire answer."""

    def test_short_text_is_returned_untouched(self):
        self.assertEqual(scv._clip("短的", 2048), "短的")

    def test_a_long_line_is_cut_and_says_how_much_it_lost(self):
        out = scv._clip("z" * 9000, 2048)
        self.assertLessEqual(len(out.encode("utf-8")), 2048)     # asserts the upper bound itself
        self.assertTrue(out.startswith("z" * 100))               # what is kept is the beginning (an error message is almost always up front)
        self.assertIn(str(9000 - (2048 - 64)), out)              # states the real number, never just a line saying "truncated"

    def test_it_never_cuts_a_character_in_half(self):
        """⭐Cut by byte count (the bound to pin is a byte count), but never cut a character in half -- one Chinese
        character is 3 bytes, and cutting one in half would turn this line into broken utf-8, while the `jobs.log`
        side needs `json.loads` to read it back."""
        out = scv._clip("中" * 4000, 2048)
        self.assertLessEqual(len(out.encode("utf-8")), 2048)
        self.assertEqual(out.encode("utf-8").decode("utf-8"), out)   # encodes back cleanly = no half character
        self.assertTrue(out.startswith("中中中"))


class OneLockPerName(_CappedHome):
    """C-2: what `_append_capped` is missing is "one lock per file name", never the "serialization is the caller's
    responsibility" the docstring used to say.

    `log()` uses one module-level `_log_lock`; `JobLog` uses `self._lock`, one per instance => two `JobLog()`
    instances are two separate locks hitting the same `jobs.log`, and each caller on its own does satisfy that
    sentence. Review measured a stale rotation once wiping out a whole generation: 22 lines / 4280 bytes gone,
    `tail(20)` returns 2 lines, with no error and no missing field."""

    def test_a_second_writer_waits_while_a_rotation_is_in_flight(self):
        inside, release, done, started = (threading.Event() for _ in range(4))
        real = os.replace

        def slow_replace(a, b):
            if not inside.is_set():      # 🔴Blocks only the first call: if it blocked both, the second writer would
                inside.set()             #   sit stuck inside `os.replace` -- that is the fixture blocking it in
                release.wait(5)          #   place of the implementation => removing the lock entirely would still
                                         #   come out green on this test (the first version was written exactly
                                         #   this way, and negative control NC-8 caught it on the spot).
            return real(a, b)

        self._fill_to_cap("jobs.log")
        with mock.patch("os.replace", slow_replace):
            t1 = threading.Thread(target=lambda: scv._append_capped("jobs.log", "触发轮转的那一发"))
            t1.start()
            self.assertTrue(inside.wait(5), "the first call never even reached rotation => this test tested nothing")

            def second():
                started.set()
                scv._append_capped("jobs.log", "第二个写手")
                done.set()

            t2 = threading.Thread(target=second)
            t2.start()
            # ⚠️Prove it is really running before judging that it is blocked, or a machine hiccup would make "never
            #   got scheduled" read as "hit the lock" (this seam is written by copying
            #   `test_30_drivers.py::test_both_doors_are_blocked_by_the_same_lock`).
            self.assertTrue(started.wait(5), "the second thread never even got scheduled")
            blocked = not done.wait(0.5)
            release.set()
            t1.join(5)
            t2.join(5)
        self.assertTrue(blocked, "the second writer was not blocked => there is no lock shared between rotation and appending (a stale rotation can vaporize a whole generation)")
        self.assertTrue(done.is_set(), "it still could not get through after the lock was released => deadlock")   # asserts the correct value, never just "being blocked is enough"
        # ⭐What really matters is the consequence: a stale rotation wipes out the steady-state generation entirely
        #   (the r000~ lines `_fill_to_cap` wrote).
        # 🔴The first version here asserted "are those two lines still there" -- which does not go red under its own
        #   negative control: the stale `os.replace` wipes out the steady-state generation, while those two lines
        #   each land in the two newly opened generations => searching across both generations was bound to find
        #   them. Measured: with the lock removed, the r000 generation goes from 20 lines to 0, and at that time not
        #   a single assertion was even looking at it.
        # ⭐Keep this as a rule: a newly added assertion also has to pass "would it go red if this were not fixed" --
        #   we always hold production code to this, and that one time it slipped through on the test side.
        rows = self._rows("jobs.log") + self._rows("jobs.log.1")
        self.assertTrue(any(x.startswith("r000 ") for x in rows), "the steady-state generation vanished entirely (that is exactly what the stale rotation wiped out)")
        self.assertTrue(any("触发轮转的那一发" in x for x in rows), "the first call got wiped out")
        self.assertTrue(any("第二个写手" in x for x in rows), "the second call got wiped out")


class OnePlace(unittest.TestCase):
    """⭐A real fix means making this family have only one place, never copying the cap into a second one.
    What this gate asks is "which test would go red if it were gone" -- and the answer is not a single one: if
    anyone in Task 10-13 opens another `open(spath('x.log'), 'a')`, the two upper-bound tests above stay green just
    the same (they only ever look at their own two files)."""

    def test_append_only_writes_go_through_one_place(self):
        """⭐`assertEqual`, never `assertLessEqual`: the empty set is a subset of any set => the moment the scanner
        goes blind, or `_append_capped` gets bypassed some day, `assertLessEqual` stays green just the same (that is
        exactly how two spath gates in this same repository stayed blind for an entire plan)."""
        self.assertEqual(append_openers(TREE), {"_append_capped"})

    def test_append_scanner_is_not_blind(self):
        """Positive control: all three ways of writing it -- a positional argument, `mode=`, `p.open()` -- must all
        be caught; read/write mode, a file name, `Popen` must never be flagged by mistake.

        🔴This case is not for show: it caught, on the spot, the first version's check missing `p.open('a')`
        entirely (mode sitting in the 1st position, not the 2nd)."""
        probe = ast.parse(NL.join((
            "def sneaky():", "    return open(spath('x.log'), 'a', encoding='utf-8')",
            "def kwmode(p):", "    return open(p, mode='a')",
            "def method(p):", "    return p.open('a')",
            "def reader(p):", "    return open(p, 'r')",
            "def writer(p):", "    return open(p, 'w')",
            "def namer():", "    return open('a.txt')",
            "def spawner(a):", "    return subprocess.Popen(a, 'a')")))
        self.assertEqual(append_openers(probe), {"sneaky", "kwmode", "method"})


if __name__ == "__main__":
    unittest.main()
