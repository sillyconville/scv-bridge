# -*- coding: utf-8 -*-
"""Task 13: `setup` / `pair` / `update` (Task 13 also had a codex login subcommand; Task 13c dropped it, see
`NoLoginStepForScv`).

⭐This suite for `update` sends not a single byte out to the internet: `UPDATE_BASE` is swapped for a small server
  on loopback (`setUpModule`), and `target` is always a temporary file.
  🔴Never run an update against the default `target` that would succeed: the default is the scv.py in this
  repository (`tearDownModule` checks its sha256 has not changed).
⭐`pair` runs against the reference dispatcher (`tests/fake_dispatcher.py`); the plaintext cells' criterion is zero
  dials, never "it got a 1 back".
"""
import argparse
import ast
import base64
import hashlib
import http.client
import io
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from tests import helpers
from tests.fake_dispatcher import Dispatcher
from tests.test_00_budget import call_sites
from tests.test_90_cli import log_lines_during
import scv

NL = chr(10)
SCRIPT = os.path.join(helpers.ROOT, "scv.py")
_REAL_BASE = scv.UPDATE_BASE
_REAL_HEAD = scv.cli_head
HOME = ""
REPO_SHA = ""
GOOD = b"# -*- coding: utf-8 -*-\nVERSION = '9.9.9'\n"      # ⭐LF: writing it to disk in text mode on win32 would turn it into CRLF ⇒ the bytes would not match
OLD = b"VERSION = 'old'\n"
PREV = b"# the copy an earlier update left behind\n"
NOT_PYTHON = b"this is ( not python"
FILES = {}          # GET path → bytes (`scv update` fetches from here)
POSTS = {}          # POST path → (status code, reply bytes); status 0 = reply with one garbled status line (`http.client.BadStatusLine`)
REASONS = {}        # path (GET and POST both honor it) → (status code, reason phrase), empty body: the suite for the full failure message needs one that is long and carries control characters (Fix 4)
SERVED_TABLES = ("FILES", "POSTS", "REASONS")     # the module-level tables `_Raw` reads (the full set is checked by `TablesStartEachCaseFresh` against the AST)
AFTER_SETUP = {}    # table name → its shape right after setUpModule (`_Fresh.setUp` restores it to this)
SERVER = None
# Plaintext / loopback-lookalike remote addresses: the same set of cells as tests/test_80_remote.py::Unpaired (one ruler, checked at both call sites)
PLAIN = ("http://evil.example", "http://a-remote-host.example:9", "http://127.0.0.1.a-remote-host.example:9",
         "http://127.0.0.1@a-remote-host.example:9", "http://127.0.0.1:a-remote-host.example:443/x",
         "http://127.0.0.1:9" + NL + ".a-remote-host.example", "http://localhost:9")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def ns(**kw):
    return argparse.Namespace(**kw)


class _Raw(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def said(self):
        """This path is in `REASONS` ⇒ reply with that status code plus that reason phrase, an empty body, and
        return True."""
        if self.path not in REASONS:
            return False
        self.send_response(*REASONS[self.path])
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def do_GET(self):
        if self.said():
            return
        data = FILES.get(self.path)
        self.send_response(200 if data is not None else 404)
        self.send_header("Content-Length", str(len(data or b"")))
        self.end_headers()
        self.wfile.write(data or b"")

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.said():
            return
        status, body = POSTS.get(self.path, (404, b"{}"))
        if status == 0:
            self.wfile.write(b"garbage" + b"\r\n\r\n")       # never real HTTP: `BadStatusLine` (an HTTPException, never an OSError)
            return
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def setUpModule():
    global HOME, SERVER, REPO_SHA
    with open(SCRIPT, "rb") as f:
        REPO_SHA = sha(f.read())
    HOME = helpers.fresh_home("t13", unittest.addModuleCleanup)
    scv.cli_head = helpers.fake_head
    SERVER = ThreadingHTTPServer(("127.0.0.1", 0), _Raw)
    SERVER.daemon_threads = True
    threading.Thread(target=SERVER.serve_forever, daemon=True).start()
    scv.UPDATE_BASE = "http://127.0.0.1:%d" % SERVER.server_address[1]
    FILES["/aaaaaaa/scv.py"] = GOOD
    FILES["/bbbbbbb/scv.py"] = NOT_PYTHON
    for name in SERVED_TABLES:                      # restored to this shape at the start of every case (`_Fresh.setUp`)
        AFTER_SETUP[name] = dict(globals()[name])


def tearDownModule():
    scv.UPDATE_BASE = _REAL_BASE
    scv.cli_head = _REAL_HEAD
    SERVER.shutdown()
    SERVER.server_close()
    with open(SCRIPT, "rb") as f:
        now = sha(f.read())
    if now != REPO_SHA:
        raise AssertionError("🔴the repository's scv.py was changed by this suite (%s → %s): some case ran update against the default target" % (REPO_SHA, now))


class _Fresh(unittest.TestCase):
    """A clean SCV_HOME for every case: `scv.prev.py` / `config.json` / `latest.json` would otherwise carry over
    between cases."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="case-", dir=HOME)
        old = os.environ["SCV_HOME"]
        os.environ["SCV_HOME"] = self.home
        self.addCleanup(os.environ.__setitem__, "SCV_HOME", old)
        helpers.pin_quiet_port()          # setup/doctor asks config for the port: never the real default port on this machine (13b fix1)
        # 🔴`REASONS` is module-level, and `_Raw` checks it first: without clearing it, a path an earlier case wrote
        #   in would shadow the next case's (re-review 4 I-c: the `/long` cell got shadowed by a 403, and the
        #   token-size ceiling of 512 lost its suite from then on) ⇒ every case starts from an empty table, so
        #   whatever it writes cannot carry over into another case
        # ⭐Cleaned up by shape (Task 14 re-review 5 out-of-scope ④): `FILES` / `POSTS` are also "written by cases,
        #   never cleared" — every table `_Raw` reads is restored to its shape right after setUpModule (`FILES`'s
        #   two shared fixtures are kept). Which tables those are is checked by `TablesStartEachCaseFresh` against
        #   `_Raw`'s AST.
        for name, snap in AFTER_SETUP.items():
            globals()[name].clear()
            globals()[name].update(snap)


class TablesStartEachCaseFresh(_Fresh):
    """Task 14 re-review 5 out-of-scope ④: the mutable module-level tables `_Raw` reads are, at the start of every
    case, in the shape they were right after setUpModule (the `REASONS` cell has the same shape as I-c; `FILES` /
    `POSTS` do not carry over today, but the shape is the same: writing `/ccccccc/` into `FILES` later, or two
    cases reusing the same POST path, would replay it)."""

    def test_every_table_the_server_reads_is_reset(self):
        """Which tables, taken from `_Raw`'s AST (the module-level names it reads whose value is a dict), never
        hand-copied — add a fourth table later and forget to register it here ⇒ this goes red."""
        with io.open(__file__, encoding="utf-8") as f:
            raw = next(n for n in ast.parse(f.read()).body if isinstance(n, ast.ClassDef) and n.name == "_Raw")
        read = {n.id for n in ast.walk(raw) if isinstance(n, ast.Name) and isinstance(globals().get(n.id), dict)}
        self.assertEqual(sorted(read), sorted(SERVED_TABLES))
        self.assertEqual(sorted(AFTER_SETUP), sorted(SERVED_TABLES))

    def test_what_one_case_writes_is_gone_when_the_next_case_starts(self):
        for name in SERVED_TABLES:
            globals()[name]["/written-by-an-earlier-case"] = (418, b"x")
        FILES["/aaaaaaa/scv.py"] = b"overwritten by an earlier case"
        self.setUp()                                                  # the start of the next case
        for name in SERVED_TABLES:
            with self.subTest(table=name):
                self.assertEqual(globals()[name], AFTER_SETUP[name])
        self.assertEqual(FILES["/aaaaaaa/scv.py"], GOOD)             # an overwritten shared fixture also goes back to its original value (never just dropping the newly added key)


def no_dial():
    """A dial probe: every one of `urllib`'s outgoing calls (`urlopen` and `build_opener().open` both go through
    it). Judging by "the count came back 1" is not enough: dialing out and then getting refused still returns 1.
    ⭐When it really is dialed, it raises an OSError, never returns a MagicMock: the latter would let the code
      under test blow up with a TypeError at some later, unrelated step, going red on a sentence that has nothing
      to do with the real issue (this actually happened during the negative-control pass); raising it instead
      sends the code down its normal failure path, going red on the `dial.call_args_list` assertion."""
    return mock.patch.object(urllib.request.OpenerDirector, "open", autospec=True,
                             side_effect=OSError("dial probe: this call must never have gone out"))


def tmp_left():
    """Write buffers left behind in the state directory's `tmp/` (if it was never buffered even once, this
    directory does not exist = nothing was left behind at all)."""
    d = scv.spath("tmp")
    return sorted(os.listdir(d)) if d.is_dir() else []


# Fix 4 (re-review 3, independent check 5, the maintainer chose (b)): a reason phrase longer than 128, carrying ESC/BEL/C1
LONG_REASON = "Nope " + chr(27) + "[31m" + "x" * 150 + chr(7) + chr(0x9b) + " TAIL-OF-THE-REASON"


def reason_in_full(tc, rc, lines, err, code):
    """`pair` / `update` are commands the user runs and reads on their own terminal ⇒ stderr carries the complete
    original words (control characters escaped the same way the `log()` door does it); the bridge.log line is
    still repr'd first and then cut to 128 as before (README's "at most 128" is about bridge.log). `str(e)` = the
    urllib phrasing, "HTTP Error <code>: <reason>"."""
    text = "HTTP Error %d: %s" % (code, LONG_REASON)
    tc.assertEqual(rc, 1)
    for c in (chr(27), chr(7), chr(0x9b)):
        tc.assertNotIn(c, err)
    tc.assertIn(scv._no_ctrl(text), err)                                          # the correct value: the full text (escaped)
    logged = [x for x in lines if "HTTPError: " in x]
    tc.assertEqual(len(logged), 1, lines)
    tc.assertTrue(logged[0].endswith("HTTPError: " + repr(text)[:128]), logged[0])   # the correct value: repr'd first, then cut to 128


# ━━ update (B29)
class Update(_Fresh):
    def setUp(self):
        super().setUp()
        self.target = Path(self.home) / "installed" / "scv.py"
        self.target.parent.mkdir()
        self.target.write_bytes(OLD)
        self.prev = scv.spath("scv.prev.py")
        self.prev.write_bytes(PREV)          # the copy left behind by the previous update: never allowed to be overwritten on a failure path

    def update(self, commit="", sha256=""):
        return log_lines_during(lambda: scv.cmd_update(ns(commit=commit, sha256=sha256), self.target))

    def untouched(self):
        """the criterion for "nothing was touched": the installed copy, the previous-version copy, no extra `.new`
        in the installed directory, and no write buffer left behind."""
        self.assertEqual(self.target.read_bytes(), OLD)
        self.assertEqual(self.prev.read_bytes(), PREV)
        self.assertEqual(sorted(os.listdir(self.target.parent)), ["scv.py"])
        self.assertEqual(tmp_left(), [])

    def test_matching_hash_replaces_and_keeps_the_previous_copy(self):
        rc, lines, out, err = self.update("aaaaaaa", sha(GOOD))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.target.read_bytes(), GOOD)            # ⭐by bytes: what is pinned is the sha256 of the bytes
        self.assertEqual(self.prev.read_bytes(), OLD)
        self.assertEqual(sorted(os.listdir(self.target.parent)), ["scv.py"])
        self.assertEqual(tmp_left(), [])
        self.assertIn("git diff --no-index", out)
        # "restart" must be given as two separate commands: `stop && start` is a parse error in Win11's default
        #   PowerShell 5.1 (not a single word of the line runs); as of 13b every one of them goes through the door
        self.assertEqual(helpers.door_lines_missing(out, "stop") + helpers.door_lines_missing(out, "start"), [], out)
        self.assertNotIn("&&", out)
        self.assertEqual(len([x for x in lines if "update: " in x and sha(GOOD) in x]), 1, lines)   # writes exactly one line to the log

    def test_an_uppercase_sha256_is_the_same_sha256(self):
        rc, _lines, out, err = self.update("aaaaaaa", sha(GOOD).upper())
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.target.read_bytes(), GOOD)

    def test_wrong_hash_touches_nothing_and_prints_both_hashes(self):
        """🔴"nothing was touched" has to be literally true: saving the previous version happens before the hash
        is found to be wrong ⇒ the previous-version copy has already been overwritten (carry-forward 5)."""
        rc, _lines, out, err = self.update("aaaaaaa", "0" * 64)
        self.assertEqual(rc, 1)
        self.untouched()
        self.assertIn("0" * 64, err)
        self.assertIn(sha(GOOD), err)
        self.assertIn("nothing was touched", err)

    def test_right_hash_but_not_python_is_refused(self):
        rc, _lines, out, err = self.update("bbbbbbb", sha(NOT_PYTHON))
        self.assertEqual(rc, 1)
        self.untouched()
        self.assertIn("nothing was touched", err)

    def test_a_commit_that_is_not_there_touches_nothing(self):
        rc, _lines, out, err = self.update("ccccccc", sha(GOOD))
        self.assertEqual(rc, 1)
        self.untouched()
        self.assertIn("404", err)
        self.assertIn("nothing was touched", err)

    def test_a_failed_download_shows_the_whole_reason(self):
        REASONS["/fffffff/scv.py"] = (404, LONG_REASON)          # never a path another case has already used (`/ddddddd/` belongs to the download-size-ceiling cell)
        rc, lines, _out, err = self.update("fffffff", sha(GOOD))
        self.untouched()
        reason_in_full(self, rc, lines, err, 404)

    def test_commit_must_look_like_a_commit_before_anything_is_dialled(self):
        """`commit` gets built into the download address (PROTOCOL.md: the shape check belongs to whoever builds
        the address).
        ⭐The cell with a trailing newline: `re.match`'s `$` lets a trailing newline through (the same cell
          `_model_ok` once tripped on) ⇒ `fullmatch` is required."""
        for bad in ("../../etc", "aaaaaaa" + NL, "AAAAAAA", "aaaaaa", "a" * 41, "", "aaaaaaa/../x"):
            with self.subTest(commit=bad), no_dial() as dial:
                rc, _lines, out, err = self.update(bad, sha(GOOD))
                self.assertEqual(dial.call_args_list, [])
                self.assertEqual(rc, 1)
                self.untouched()

    def test_sha256_must_be_64_hex_before_anything_is_dialled(self):
        """⭐The non-ASCII cell: `hmac.compare_digest` raises a TypeError on a str containing non-ASCII characters —
        if only the length were checked, this would be a bare traceback."""
        for bad in ("0" * 63, "g" * 64, "é" * 64, "0" * 64 + NL, " " + "0" * 63):
            with self.subTest(sha256=bad), no_dial() as dial:
                rc, _lines, out, err = self.update("aaaaaaa", bad)
                self.assertEqual(dial.call_args_list, [])
                self.assertEqual(rc, 1)
                self.untouched()

    def test_latest_json_supplies_the_pair_when_none_is_given(self):
        scv._atomic_write("latest.json", json.dumps({"version": "9.9.9", "commit": "aaaaaaa", "sha256": sha(GOOD)}))
        rc, _lines, out, err = self.update()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.target.read_bytes(), GOOD)
        self.assertIn("latest.json", out)                   # clearly says where this pair came from

    def test_half_a_pair_on_the_command_line_is_not_topped_up_from_latest_json(self):
        """half from the command line, half from latest.json = a combination neither of them ever actually gave ⇒
        refused, never pieced together."""
        scv._atomic_write("latest.json", json.dumps({"commit": "aaaaaaa", "sha256": sha(GOOD)}))
        for commit, digest in (("aaaaaaa", ""), ("", sha(GOOD))):
            with self.subTest(commit=commit, sha256=digest), no_dial() as dial:
                rc, _lines, out, err = self.update(commit, digest)
                self.assertEqual((rc, dial.call_args_list), (1, []))
                self.untouched()

    def test_already_that_version_does_not_download_or_touch_the_previous_copy(self):
        self.target.write_bytes(GOOD)
        with no_dial() as dial:
            rc, _lines, out, err = self.update("aaaaaaa", sha(GOOD))
        self.assertEqual((rc, dial.call_args_list), (0, []), out + err)
        self.assertEqual(self.prev.read_bytes(), PREV)       # never overwrite the previous version with "the same version"
        self.assertEqual(self.target.read_bytes(), GOOD)
        self.assertIn("already at this version", out)

    def test_when_the_previous_copy_cannot_be_kept_scv_py_is_not_swapped(self):
        """save the previous version first, and if it cannot be saved, never swap: swapping without keeping the
        previous version would leave the user with nothing to roll back to."""
        real = os.replace

        def swap(src, dst):
            if Path(dst) == self.prev:
                raise OSError(28, "No space left on device")
            return real(src, dst)

        with mock.patch.object(scv.os, "replace", side_effect=swap):
            rc, _lines, out, err = self.update("aaaaaaa", sha(GOOD))
        self.assertEqual(rc, 1)
        self.untouched()
        self.assertIn("nothing was touched", err)
        self.assertIn("No space left", err)

    def test_a_failed_swap_leaves_no_new_file_and_says_exactly_what_changed(self):
        """🔴`os.replace` has no except ⇒ a fixed-named `.new` scrap gets left behind in the user's installed
        directory (the same shape as Task 12 B9).
        ⭐The previous version has already been saved before this step ⇒ the words must never say "nothing was
          touched"; they have to say plainly what did get touched."""
        real = os.replace

        def swap(src, dst):
            if Path(dst) == self.target:
                raise PermissionError(13, "the file is held by another program")
            return real(src, dst)

        with mock.patch.object(scv.os, "replace", side_effect=swap):
            rc, _lines, out, err = self.update("aaaaaaa", sha(GOOD))
        self.assertEqual(rc, 1)
        self.assertEqual(self.target.read_bytes(), OLD)
        self.assertEqual(sorted(os.listdir(self.target.parent)), ["scv.py"])     # the `.new` scrap was cleaned up
        self.assertEqual(self.prev.read_bytes(), OLD)
        self.assertIn("held by another program", err)
        self.assertIn("did not swap", err)
        self.assertNotIn("nothing was touched", err)

    def test_a_download_bigger_than_any_scv_py_is_refused(self):
        """review M-7: the download's ceiling is "how big a scv.py can possibly be" (`SCV_PY_MAX_BYTES`), never the
        local API's request-body ceiling `MAX_BODY`."""
        big = b"#" * (scv.SCV_PY_MAX_BYTES + 1)            # all comments: the hash matches, it is valid Python — only the size ceiling can stop it
        FILES["/ddddddd/scv.py"] = big
        rc, _lines, out, err = self.update("ddddddd", sha(big))
        self.assertEqual(rc, 1, out + err)
        self.untouched()
        self.assertIn("nothing was touched", err)
        self.assertIn("exceeds %d bytes" % scv.SCV_PY_MAX_BYTES, err)

    def test_the_download_limit_does_not_follow_the_local_api_body_limit(self):
        """the other direction: someone tightening `MAX_BODY` for the local leg (it governs the local API's request
        body and the remote leg's incoming replies) must never make `scv update` start refusing too."""
        doc = b"# -*- coding: utf-8 -*-\n" + b"# padding\n" * 400 + b"VERSION = '9.9.9'\n"
        FILES["/eeeeeee/scv.py"] = doc
        with mock.patch.object(scv, "MAX_BODY", 1024):
            rc, _lines, out, err = self.update("eeeeeee", sha(doc))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.target.read_bytes(), doc)

    def test_a_half_written_new_file_is_taken_away(self):
        """the disk fills up halfway through writing: `.new` holds half a file ⇒ it must be cleaned up (never
        leaving a plausible-looking `scv.py.new` sitting in the installed directory)."""
        real = Path.write_bytes

        def half(p, data):
            if str(p).endswith(".new"):
                real(p, data[:5])
                raise OSError(28, "No space left on device")
            return real(p, data)

        with mock.patch.object(Path, "write_bytes", half):
            rc, _lines, out, err = self.update("aaaaaaa", sha(GOOD))
        self.assertEqual(rc, 1)
        self.assertEqual(self.target.read_bytes(), OLD)
        self.assertEqual(sorted(os.listdir(self.target.parent)), ["scv.py"])
        self.assertIn("did not swap", err)


class DiffHint(_Fresh):
    """The maintainer's ruling: the user most likely has no clone of the repository, and does not know the previous
    version's commit id either (a file cannot carry its own hash) ⇒ compare the two local files instead.
    Criterion: every word is true, and every line given really runs in the shell it is labeled for. ⭐As of 13b
    that line is written by a single door, `paste_cmd` (the quoting/shell criteria live in exactly one place; the
    old `_paste_arg` had its own copy, and a path with a single quote in it simply got no command at all)."""

    def swap_in(self, where):
        target = where / "scv.py"
        target.parent.mkdir(parents=True)
        target.write_bytes(OLD)
        rc, _lines, out, err = log_lines_during(lambda: scv.cmd_update(ns(commit="aaaaaaa", sha256=sha(GOOD)), target))
        self.assertEqual(rc, 0, out + err)
        return target, out

    @staticmethod
    def diff_block(out):
        """the section under "if you have git installed" (exactly as the door hands it out: one line, or several
        "one label line, one command line" pairs)."""
        lines = out.splitlines()
        at = [i for i, x in enumerate(lines) if x.startswith("to see what changed this time")]
        end = [i for i, x in enumerate(lines) if x.startswith("⚠️ if the bridge is currently running")]
        return NL.join(lines[at[0] + 1:end[0]])

    def test_the_diff_line_splits_into_the_same_words_under_posix_rules(self):
        """bash's word-splitting rules (`shlex` is exactly that): every word must come through as itself; ⭐a
        directory with a space or Chinese characters in it must really be wrapped in quotes."""
        for where in (Path(self.home) / "plain", Path(self.home) / "with space" / "装在这里"):
            with self.subTest(where=where.name):
                target, out = self.swap_in(where)
                line = helpers.pasted_for(self.diff_block(out), "Git Bash")
                self.assertEqual(shlex.split(line), ["git", "diff", "--no-index",
                                                     scv.spath("scv.prev.py").as_posix(), target.as_posix()])

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell") and shutil.which("git"),
                         "needs PowerShell 5.1 and git on win32")
    def test_the_diff_line_really_runs_in_powershell(self):
        """⭐runs for real: PowerShell 5.1's quoting rules are not the same as bash's (that is exactly how the
        `&&` stumble happened), so never prove this with bash's rules alone.
        ⭐a directory with a straight single quote, or a curly one (PowerShell also treats ' ' ‚ ‛ as single
          quotes, review M-3②), used to get no command at all; as of 13b it gets one and that one really runs."""
        for where in (Path(self.home) / "plain", Path(self.home) / "with space" / "装在这里",
                      Path(self.home) / "it's here", Path(self.home) / ("curly" + chr(0x2019) + "s")):
            with self.subTest(where=where.name):
                _target, out = self.swap_in(where)
                line = helpers.pasted_for(self.diff_block(out), "PowerShell")
                p = helpers.run_in_shell("PowerShell", line, self.home)
                text = p.stdout.decode("utf-8", "replace")
                self.assertEqual(p.returncode, 1, line + NL + text + p.stderr.decode("utf-8", "replace"))   # a difference = 1
                self.assertIn("-VERSION = 'old'", text)
                self.assertIn("+VERSION = '9.9.9'", text)

    def test_a_relative_state_dir_still_gives_absolute_words(self):
        """review M-3①: `state_dir()` neither expands `~` nor converts to an absolute path — exactly what happens
        with `$env:SCV_HOME="~/.scv"` in PowerShell (it does not expand a `~` inside a string). If the path in the
        line were relative: bash expands a leading `~` into `$HOME`, while PowerShell hands it to a native program
        as a literal ⇒ the two shells would point at two different files, both following the cwd at paste time.
        ⇒ convert to an absolute path first (`cmd_update` does the converting; the door never guesses which word is
        a path); `~` is not among the "never quoted" characters."""
        cwd = tempfile.mkdtemp(prefix="cwd-", dir=self.home)
        old = os.getcwd()
        os.chdir(cwd)
        self.addCleanup(os.chdir, old)
        os.environ["SCV_HOME"] = "~/.scv"                 # `_Fresh`'s cleanup will restore it
        target, out = self.swap_in(Path("rel dir"))
        line = helpers.pasted_for(self.diff_block(out), "Git Bash")
        words = shlex.split(line)
        self.assertEqual(words, ["git", "diff", "--no-index", os.path.join(cwd, "~", ".scv", "scv.prev.py").replace(os.sep, "/"),
                                 os.path.abspath(target).replace(os.sep, "/")], line)
        self.assertNotIn(" ~", line)                        # not one leading `~` anywhere (they are all absolute paths)
        for w in words[3:]:
            self.assertTrue(os.path.isabs(w), w)

    def test_the_kept_copy_is_named_by_its_absolute_path(self):
        """re-review out-of-scope ④ (the same "every word is true" as M-3): when `SCV_HOME` is a relative path
        (exactly what `$env:SCV_HOME="~/.scv"` gives in PowerShell: it never expands a `~` inside a string), "the
        previous version is left as-is at: …" used to print `~/.scv/scv.prev.py` verbatim — read in bash that is
        `$HOME/.scv`, while the file actually sits under this call's own cwd. ⇒ print the absolute path instead
        (the failure sentences and the bridge.log line use the same variable)."""
        cwd = tempfile.mkdtemp(prefix="cwd-", dir=self.home)
        old = os.getcwd()
        os.chdir(cwd)
        self.addCleanup(os.chdir, old)
        os.environ["SCV_HOME"] = "~/.scv"                 # `_Fresh`'s cleanup will restore it
        target = Path(cwd) / "installed" / "scv.py"
        target.parent.mkdir()
        target.write_bytes(OLD)
        rc, lines, out, err = log_lines_during(lambda: scv.cmd_update(ns(commit="aaaaaaa", sha256=sha(GOOD)), target))
        self.assertEqual(rc, 0, out + err)
        want = os.path.join(cwd, "~", ".scv", "scv.prev.py")
        self.assertTrue(os.path.isfile(want))             # the ruler is not blind: the file really is there
        kept = [x for x in out.splitlines() if x.startswith("the previous version is left as-is at: ")]
        self.assertEqual(kept, ["the previous version is left as-is at: %s (to roll back, copy it back to %s)" % (want, target)], out)
        logged = [x for x in lines if "the previous version is at" in x]
        self.assertEqual(len(logged), 1, lines)
        self.assertTrue(logged[0].endswith("the previous version is at " + want), logged[0])

    def test_powershells_curly_single_quotes_count_as_quotes(self):
        """review M-3②: PowerShell also treats ' ' ‚ ‛ (U+2018-U+201B) as single quotes ⇒ single-quoting a path
        that carries one of them gets cut short early ⇒ handled the same way as `'`: written twice in the
        PowerShell line (that it really runs is pinned by the test above plus
        tests/test_90_cli.py::PasteInRealShells); inside cmd's or Git Bash's double quotes they are all ordinary
        characters."""
        for ch in (chr(0x2018), chr(0x2019), chr(0x201A), chr(0x201B), "'"):
            with self.subTest(ch=hex(ord(ch))), mock.patch.object(os, "name", "nt"):
                got = scv.paste_cmd(["git", "diff", "C:/a" + ch + "b/scv.py"])
                self.assertEqual(helpers.pasted_for(got, "PowerShell"), "git diff 'C:/a" + ch + ch + "b/scv.py'")
                self.assertEqual(helpers.pasted_for(got, "cmd"), 'git diff "C:/a' + ch + 'b/scv.py"')


class Bytes(unittest.TestCase):
    """carry-forward 10 (M-8): what `scv update` pins is the file bytes' sha256 ⇒ three copies of bytes must be the
    same one: ① the one the release side hashes, ② the one the user downloads from raw.githubusercontent, ③ the
    one installed locally.
    ② = the committed blob (measured for real on 2026-09-24: for a file marked `*.bat text eol=crlf`, raw hands out
      LF, and the blob id matches the one in the tree; 📎 scv.py's NOTES.md::update-bytes); ③ is pinned by the
      `Update` tests' "equal byte for byte" checks.
    This test pins ①: hashing on this repository's checkout must produce the committed bytes — an `autocrlf=true`
    Windows checkout used to be CRLF instead."""

    def git(self, *argv):
        p = subprocess.run(["git"] + list(argv), capture_output=True, timeout=60, cwd=helpers.ROOT,
                           **scv.new_session_kw())
        return p.returncode, p.stdout.decode("utf-8", "replace").strip()

    def setUp(self):
        if not shutil.which("git"):
            self.skipTest("no git")
        if self.git("rev-parse", "--is-inside-work-tree") != (0, "true"):
            self.skipTest("not a git checkout (e.g. running from an archive)")

    def test_scv_py_is_checked_out_with_exactly_the_bytes_git_stores(self):
        rc, attrs = self.git("check-attr", "eol", "--", "scv.py")
        self.assertEqual((rc, attrs), (0, "scv.py: eol: lf"))           # LF on a checkout on any machine
        # the working tree's bytes = the bytes git would store (with the clean filter vs. without: any line-ending conversion at all would make them unequal)
        self.assertEqual(self.git("hash-object", "scv.py"), self.git("hash-object", "--no-filters", "scv.py"))


# ━━ pair (§6 step 5: the one-time pairing code hand-sent during the seed period)
class Pair(_Fresh):
    def pair(self, url, code="GOOD"):
        return log_lines_during(lambda: scv.cmd_pair(ns(url=url, code=code)))

    def test_good_code_saves_url_and_token_and_a_bad_one_changes_nothing(self):
        d = Dispatcher()
        url = d.start()
        self.addCleanup(d.stop)
        rc, lines, out, err = self.pair(url + "/")
        cfg = scv.load_config()
        self.assertEqual((rc, cfg["remote_url"], cfg["remote_token"]), (0, url, d.token), out + err)
        self.assertIn(url, out)
        self.assertEqual(helpers.door_lines_missing(out, "stop") + helpers.door_lines_missing(out, "start"), [], out)   # only takes effect after a restart
        before = scv.spath("config.json").read_bytes()
        rc2, lines2, out2, err2 = self.pair(url, code="not-the-code")
        self.assertEqual(rc2, 1)
        self.assertEqual(scv.spath("config.json").read_bytes(), before)     # a failure never touches the existing config (byte for byte)
        self.assertIn("403", err2)
        # neither the code nor the token may ever land on disk or on screen (the code is GOOD, the token is
        #   tok-GOOD: one word covers both; review M-6 added the "never on screen" half)
        self.assertEqual([x for x in lines + lines2 if "GOOD" in x or "not-the-code" in x], [])
        for said in (out, err, out2, err2):
            self.assertNotIn("GOOD", said)
            self.assertNotIn("not-the-code", said)

    def test_plaintext_or_lookalike_urls_are_refused_before_a_single_dial(self):
        """the pairing call itself carries the pairing code ⇒ sending it in plaintext = the code has already
        leaked; blocking this only when `scv start` runs is already too late."""
        for url in PLAIN:
            with self.subTest(url=url), no_dial() as dial:
                rc, _lines, out, err = self.pair(url)
                self.assertEqual(dial.call_args_list, [])
                self.assertEqual(rc, 1)
                self.assertIn("only allowed on a loopback IP", err)
                self.assertEqual(scv.load_config()["remote_token"], "")

    def test_a_reply_without_a_usable_token_changes_nothing(self):
        """data from the other end of the network lands on the local disk and then gets stuffed into every call's
        `Authorization` header ⇒ its shape has to pass muster (one with a newline in it would make urllib blow up
        outright)."""
        cases = {"/empty": b'{"token": ""}', "/space": b'{"token": "a b"}',
                 "/newline": json.dumps({"token": "a" + NL + "b"}).encode(), "/list": b"[1, 2]",
                 "/notjson": b"<html>", "/long": json.dumps({"token": "x" * 513}).encode(),
                 "/nonascii": json.dumps({"token": "令牌"}).encode(), "/number": b'{"token": 7}'}
        for base, body in cases.items():
            POSTS[base + "/bridge/pair"] = (200, body)
            with self.subTest(reply=base):
                rc, _lines, out, err = self.pair(scv.UPDATE_BASE + base)
                self.assertEqual(rc, 1, out + err)
                self.assertEqual(scv.load_config()["remote_token"], "")

    def test_a_garbled_status_line_is_a_failure_in_words_not_a_traceback(self):
        """carry-forward 3 (Task 11 C-A): `http.client.HTTPException` is neither an OSError nor a ValueError."""
        POSTS["/garbled/bridge/pair"] = (0, b"")
        rc, _lines, out, err = self.pair(scv.UPDATE_BASE + "/garbled")
        self.assertEqual(rc, 1)
        self.assertIn("BadStatusLine", err)

    def test_a_failure_shows_the_whole_reason(self):
        REASONS["/reason/bridge/pair"] = (403, LONG_REASON)      # never a path another case has already used (`/long` belongs to the token-size-ceiling cell)
        rc, lines, _out, err = self.pair(scv.UPDATE_BASE + "/reason")
        self.assertEqual(scv.load_config()["remote_token"], "")
        reason_in_full(self, rc, lines, err, 403)


# ━━ setup (B12/B13/B31)
class Setup(_Fresh):
    def test_setup_is_doctor_then_what_to_do_next(self):
        rc, _lines, out, err = log_lines_during(lambda: scv.cmd_setup(ns(live=False)))
        facts, end = json.JSONDecoder().raw_decode(out, out.index("{"))
        self.assertEqual(sorted(facts["families"]), ["claude", "codex"])
        tail = out[end:]
        for words in (("start",), ("token",), ("doctor", "--live"), ("pair",)):     # 13b: every one of these is the pasteable form (never a bare `scv …`)
            self.assertEqual(helpers.door_lines_missing(tail, *words), [], tail)
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(scv.load_config()["local_token"])     # mints the local token as a side effect

    def test_live_goes_through_to_doctor(self):
        """carry-forward 7: setup's `--live` is passed straight through to doctor."""
        rc, _lines, out, err = log_lines_during(lambda: scv.cmd_setup(ns(live=True)))
        self.assertEqual(len([x for x in out.splitlines() if "real call went through" in x]), 2, out + err)
        self.assertEqual(rc, 0)


# ━━ There is no "log in for scv, a second time" step (Task 13c, the maintainer's hard constraint)
class NoLoginStepForScv(_Fresh):
    """The maintainer's own words on 09-24: "the point is: no user login. Asking for a login raises suspicion" ⇒
    scv must never introduce a "log in for scv, a second time" step anywhere. Task 13's codex login subcommand
    (which ran `codex login` inside a CODEX_HOME dedicated to scv) was dropped along with it. Where the four
    properties its old suite pinned each ended up:
      · "the subprocess inherits this terminal (never with CREATE_NO_WINDOW)" — no longer wanted: there is no
        interactive subprocess left today; the exception for it in `NoConsoleWindows`'s BARE_OK was withdrawn too
        (pinned by count in
        tests/test_90_cli.py::NoConsoleWindows::test_every_spawn_site_goes_through_the_no_window_door);
      · "logs in inside a home dedicated to scv" — reversed: scv must never invent a home
        (tests/test_20_argv.py::NoDedicatedCodexHome);
      · "asks again in the same home after logging in", "must report a failed login" — no longer wanted: scv no
        longer starts a login at all. When there are no credentials, doctor hands the user a `codex login` command
        (with the resolved path) that they run themselves, logging into whatever home they normally use
        (tests/test_20_argv.py::CodexLoginGate)."""

    def test_there_is_no_login_subcommand(self):
        rc, _lines, out, err = log_lines_during(lambda: self._argparse_exit(["codex-login"]))
        self.assertEqual(rc, 2)
        self.assertIn("invalid choice", err)

    def test_setup_never_asks_for_a_login_for_scv(self):
        rc, _lines, out, err = log_lines_during(lambda: scv.cmd_setup(ns(live=False)))
        tail = out[json.JSONDecoder().raw_decode(out, out.index("{"))[1]:]
        self.assertIn(NL + "What's next (", tail)                     # the ruler is not blind: it really did read the "what's next" section
        # as of 15c, doctor has one line that lists names (the variables passed to the child CLI, with CODEX_HOME among them) — that is disclosure, never a request for the user to go log in somewhere; excluded here
        tail = NL.join(x for x in tail.splitlines() if "the two families of variables passed to the child CLI by" not in x)
        for word in ("codex-login", "CODEX_HOME", "login"):
            self.assertNotIn(word, tail)
        self.assertEqual(rc, 0, out + err)

    @staticmethod
    def _argparse_exit(argv):
        try:
            return scv.main(argv)
        except SystemExit as e:              # argparse refusing an unknown subcommand = SystemExit(2)
            return e.code


# ━━ Doors: there is only ever one criterion
class Doors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with io.open(SCRIPT, encoding="utf-8") as f:
            cls.tree = ast.parse(f.read())

    def callers(self, name):
        return {fn for fn, _ln, _n in call_sites(self.tree, lambda n: n.split(".")[-1] == name)}

    def test_the_plaintext_rule_is_written_once_and_used_at_both_doors(self):
        """carry-forward 1: a prefix-based check like `LOOPBACK_HTTP` lets `http://127.0.0.1.evil.example` through
        (Task 11 M-F) ⇒ the plaintext criterion may only ever exist in one place: the real dial site
        (`start_remote`, which also catches one hand-written into config.json) and `scv pair` use the exact same
        one.
        ⭐13d's rule 5: `redirect_refused` ("never follow from loopback to beyond loopback") also uses this
          exact criterion to judge "is this loopback" — one more caller is a deliberate, explicit decision, never a
          second hand-copied criterion (a hand copy is exactly the shape this gate exists to catch)."""
        self.assertEqual(self.callers("_loopback_http"), {"remote_url_refused", "redirect_refused"})
        self.assertEqual(self.callers("remote_url_refused"), {"start_remote", "cmd_pair"})

    def test_the_whole_file_replace_door_has_two_callers(self):
        """The door that does "a full replacement at any path the caller hands it" (`_replace_file`) has exactly
        two callers in scv.py: `_atomic_write` (whose target always goes through `spath()`) and `cmd_update` (whose
        target is the installed scv.py, the exception written at the top of § ②). The release tool in this
        repository, `tools/release.py`, calls it once more (Task 15, writing setup.md, never on the bridge's own
        run path), but this gate only scans scv.py, so it never sees it.
        ⚠️This checks only this one door (review M-2: the old docstring said "outside the state directory, this is
          the only write there is", and stuffing a line like `Path(__file__ + ".oops").write_bytes(b"")` into
          `cmd_setup` would still stay green). Direct calls to the disk-writing choke points are pinned by
          "who is writing" in
          tests/test_00_budget.py::Budget::test_disk_writes_only_in_named_functions, and together the two give the
          full picture of "who can write, through which door". Neither one covers where it writes to — whether the
          target path lies inside the state directory cannot be judged from the AST."""
        self.assertEqual(self.callers("_replace_file"), {"_atomic_write", "cmd_update"})

    def test_every_dial_goes_through_the_one_door_that_carries_the_redirect_rule(self):
        """review M-8: urllib's default redirect handler follows an https→http downgrade, and carries
        `Authorization` along verbatim (even across hosts) ⇒ there may only be one door for dialing out (`_open`),
        which fits every call with `_Redirects`, whose criterion lives in exactly one place (`redirect_refused`).
        ⭐What is pinned is the whole dialing graph: who builds the opener, who calls the door, and who calls the
          layer that reads the reply (`_fetch`) — otherwise a new caller that dials through the door would be
          invisible to the `NET_CALLERS` gate (which only recognizes functions that touch the networking module
          directly). Not one call to `urlopen` is allowed anywhere: it uses the default opener."""
        self.assertEqual(self.callers("urlopen"), set())
        self.assertEqual(self.callers("build_opener"), {"_open"})
        self.assertEqual(self.callers("_Redirects"), {"_open"})
        self.assertEqual(self.callers("redirect_refused"), {"redirect_request"})
        self.assertEqual(self.callers("_open"), {"_fetch", "_stream_once"})
        self.assertEqual(self.callers("_fetch"), {"_post", "local_get", "cmd_pair", "cmd_update"})


class Redirects(unittest.TestCase):
    """review M-8's criterion itself (`scv.redirect_refused`) and the handler that carries it. ⭐The "never follow
    when a token is carried" half is proven end-to-end against the real remote leg in
    tests/test_80_remote.py::Redirects (two loopback servers, and the plaintext side never receives a single
    call); the "without a token, only an https downgrade is blocked" half can only be proven here: setting up a
    real https dispatcher needs a certificate, and the standard library cannot manufacture one."""

    @staticmethod
    def req(url, token=""):
        r = urllib.request.Request(url)
        if token:
            r.add_header("Authorization", "Bearer " + token)
        return r

    def test_a_request_carrying_a_token_follows_no_redirect_at_all(self):
        """when urllib follows a redirect it carries `Authorization` along verbatim (this machine's 3.12 source:
        it only strips content-length/content-type), and does so even across hosts ⇒ blocking only the downgrade
        is not enough: https to a different https host would hand the token over just as readily."""
        for new in ("http://127.0.0.1:9/x", "https://elsewhere.example/x", "https://same.example/y"):
            with self.subTest(to=new):
                self.assertIn("never follow redirects", scv.redirect_refused(self.req("https://same.example/x", "tok"), new))

    def test_without_a_token_only_a_downgrade_is_refused(self):
        """the calls that carry no token are `scv update` (fetching a file from the public repository, whose
        content is backed by a sha256) and `scv pair` (the code sits in the request body, and urllib drops the
        body on 301/302/303 and never follows a 307/308 POST at all): a same-scheme redirect is still followed
        (raw redirects when a repository is renamed), but a downgrade never is."""
        self.assertIn("downgrade", scv.redirect_refused(self.req("https://raw.example/a"), "http://raw.example/a"))
        self.assertEqual(scv.redirect_refused(self.req("https://raw.example/a"), "https://mirror.example/a"), "")
        self.assertEqual(scv.redirect_refused(self.req("http://127.0.0.1:9/a"), "http://127.0.0.1:9/b"), "")

    def test_from_the_loopback_only_back_to_the_loopback(self):
        """13d's rule 5's criterion itself (end-to-end in
        tests/test_90_cli.py::LocalCallsBypassProxy::test_a_redirect_off_the_loopback_is_not_followed): starting
        from loopback ⇒ follow only a loopback IP (a hostname like `localhost` never counts — it can be rewritten
        by hosts/DNS, the same yardstick as the plaintext criterion); a call that did not start from loopback is
        never governed by this rule."""
        for new in ("http://a-remote-host.example:9/x", "http://localhost:9/x", "http://127.0.0.1.a-remote-host.example:9/x",
                    "https://raw.example/x"):
            with self.subTest(to=new):
                self.assertIn("beyond loopback", scv.redirect_refused(self.req("http://127.0.0.1:9/a"), new))
        self.assertEqual(scv.redirect_refused(self.req("http://127.0.0.1:9/a"), "http://127.0.0.2:8/b"), "")
        self.assertEqual(scv.redirect_refused(self.req("http://mirror.example/a"), "http://other.example/b"), "")

    def test_the_handler_refuses_and_closes_what_it_was_handed(self):
        """the handler layer: refusing means raising (the callers of `_fetch` / `_stream_once` treat it as a
        failure: retry / back off / hand a human sentence back), and it also closes the 3xx response it was
        holding (never leaving it open, which would be `ResourceWarning: unclosed`, the same as I-4)."""
        fp = io.BytesIO(b"")
        with self.assertRaises(ValueError) as cm:
            scv._Redirects().redirect_request(self.req("https://raw.example/a"), fp, 302, "Found", {},
                                              "http://raw.example/a")
        self.assertIn("downgrade", str(cm.exception))
        self.assertTrue(fp.closed)
        ok = scv._Redirects().redirect_request(self.req("https://raw.example/a"), io.BytesIO(b""), 302, "Found", {},
                                               "https://mirror.example/a")
        self.assertEqual(ok.full_url, "https://mirror.example/a")


class ProxyPasswordAcrossRedirects(unittest.TestCase):
    """13d's rule 6 (Task 13 re-review out-of-scope ③: this used to be inferred from the 3.12 source and was
    only fixed once 13d measured it for real): `ProxyHandler` adds the proxy password onto the request headers
    (`Proxy-authorization`), and urllib copies the request headers verbatim into the new request when it follows a
    redirect; if the new address is in `NO_PROXY`, it connects to the origin directly ⇒ the password ends up in
    the origin's hands.
    13d measured this for real (all on the local loopback: a proxy P that requires the password, an origin A that
    replies 302, an origin B that is in `NO_PROXY`; run once through `scv._fetch` and once end-to-end through
    `scv update`): B received `Proxy-Authorization: Basic <password>`; on the control arm (B is not in `NO_PROXY`),
    the hop that follows goes through the proxy and B never receives it.
    ⇒ the hop that is followed has it stripped (if the request really does go through a proxy again, `ProxyHandler`
    adds it back on its own). The two calls that carry no token, `scv update` and `scv pair`, both follow a
    same-scheme redirect.
    ⭐One test per arm (13d fix1, review M2): ① B is in `NO_PROXY` ⇒ the hop that follows never carries the
      password; ② B is not in `NO_PROXY` ⇒ the hop that follows still goes through the proxy and still carries the
      password, and still gets its answer back — if the stripping were moved to the wrong place (stripped after
      `ProxyHandler` adds the password back on), anyone using a password-protected proxy would get a 407 on every
      hop they follow, and only arm ①'s suite would stay all green.
    ⚠️All three servers sit on 127.0.0.1, told apart by port: `NO_PROXY="127.0.0.1:<B's port>"` (review M1: B used
      to bind 127.0.0.2, and `server_bind`'s `getfqdn` reverse lookup would sit idle for about 9.5 seconds on this
      machine, and macOS does not even have that address by default). 🔴A precondition: urllib compares every entry
      in `NO_PROXY` against both "host" and "host:port" (`proxy_bypass_environment`'s
      `hostonly == name or host == name`) — this machine's 3.12 source and another copy of the 3.11 standard
      library have the exact same function, letter for letter (checked in 13d fix1); ⏳3.9/3.10 unchecked. If it
      only compared the host, arm ①'s B would go through the proxy, and this would go red (loudly) on the sentence
      "the proxy received only one call". Only loopback is exercised: A replying 302 to B (both loopback IPs)
      satisfies rule 5 (never follow from loopback to beyond loopback)."""

    CREDS = "Basic " + base64.b64encode(b"px-user:px-SECRET-13d").decode()

    def setUp(self):
        seen = self.seen = {"P": [], "B": []}
        creds, servers = self.CREDS, []

        class B(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_GET(self):
                seen["B"].append(dict(self.headers.items()))
                self.send_response(200)
                self.send_header("Content-Length", "6")
                self.end_headers()
                self.wfile.write(b"landed")

        class A(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:%d/landed" % servers[0].server_address[1])
                self.send_header("Content-Length", "0")
                self.end_headers()

        class P(BaseHTTPRequestHandler):
            """a plaintext-http forwarding proxy: forwards only when the password is right (stripping the password
            before forwarding, exactly what a proxy is supposed to do); a wrong password gets a 407."""

            def log_message(self, *a):
                return

            def do_GET(self):
                seen["P"].append(dict(self.headers.items()))
                if self.headers.get("Proxy-Authorization") != creds:
                    self.send_response(407)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                u = urllib.parse.urlsplit(self.path)
                c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
                c.request("GET", u.path, headers={k: v for k, v in self.headers.items()
                                                  if k.lower() not in ("proxy-authorization", "proxy-connection")})
                r = c.getresponse()
                body = r.read()
                self.send_response(r.status)
                for k, v in r.getheaders():
                    if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                c.close()

        for h in (B, A, P):
            s = ThreadingHTTPServer(("127.0.0.1", 0), h)
            s.daemon_threads = True
            threading.Thread(target=s.serve_forever, args=(0.05,), daemon=True).start()   # shutdown() waits for at most one poll interval
            self.addCleanup(s.server_close)
            self.addCleanup(s.shutdown)
            servers.append(s)
        self.b_port, self.a_port, p_port = (s.server_address[1] for s in servers)
        self.proxy = "http://px-user:px-SECRET-13d@127.0.0.1:%d" % p_port

    def fetch(self, no_proxy):
        env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
        env.update(HTTP_PROXY=self.proxy, http_proxy=self.proxy)
        if no_proxy:
            env.update(NO_PROXY=no_proxy, no_proxy=no_proxy)
        with mock.patch.dict(os.environ, env, clear=True):
            return scv._fetch(urllib.request.Request("http://127.0.0.1:%d/x" % self.a_port), 10)

    def auths(self, who):
        return [h.get("Proxy-Authorization") for h in self.seen[who]]

    def test_a_followed_hop_leaves_the_proxy_password_behind(self):
        """arm ①: B is in NO_PROXY (by port) ⇒ the hop that follows connects to B directly, and never carries the
        proxy password."""
        self.assertEqual(self.fetch("127.0.0.1:%d" % self.b_port), b"landed")
        # the ruler is not blind: the first call really did carry the password through the proxy, and only that
        #   one call went through the proxy (otherwise "B never received it" could just mean neither call carried
        #   it, or B itself went through the proxy)
        self.assertEqual(self.auths("P"), [self.CREDS])
        self.assertEqual(self.auths("B"), [None])

    def test_a_followed_hop_through_the_proxy_still_carries_it(self):
        """arm ② (review M2): B is not in NO_PROXY ⇒ both calls go through the proxy, both calls carry the
        password, and `landed` comes back (what B receives is the call the proxy forwarded, with the proxy
        stripping the password before forwarding it)."""
        self.assertEqual(self.fetch(""), b"landed")
        self.assertEqual(self.auths("P"), [self.CREDS, self.CREDS])
        self.assertEqual(self.auths("B"), [None])


class Main(_Fresh):
    def test_the_commands_task_13_owns_are_real_now(self):
        """`tests/test_90_cli.py::Main` used to pin "not there yet" (a placeholder) for this ⇒ once it was
        replaced with the real thing, that sentence must never show up again.
        ⚠️Only feeds arguments that get refused before anything actually runs: the default target is the scv.py
          in this repository."""
        for argv, want in ((["update", "--commit", "../x", "--sha256", "0" * 64], "commit id"),
                           (["pair", "http://evil.example", "--code", "x"], "only allowed on a loopback IP")):
            with self.subTest(cmd=argv[0]), no_dial() as dial:
                rc, _lines, out, err = log_lines_during(lambda: scv.main(argv))
                self.assertEqual((rc, dial.call_args_list), (1, []))
                self.assertIn(want, err)
                self.assertNotIn("not there yet", out + err)


if __name__ == "__main__":
    unittest.main()
