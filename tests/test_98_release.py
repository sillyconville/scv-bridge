# -*- coding: utf-8 -*-
"""Task 15: the release script `tools/release.py` (B29: how setup.md's hash gets there), a few shape checks on the
CI workflow, and each .py file's grammar under the minimum Python.

⭐The criteria are exported from the code, never copied by hand: whether `latest` is usable is tested by feeding it
  through `scv update`'s own path in scv.py (`RemoteLeg._save_latest` writes it to disk → `cmd_update` reads it
  back through the shape gate → compares the hash by bytes); the download address's shape uses test_97's
  `download_url()` (`UPDATE_BASE` + `COMMIT_RE`); every cell of the bad-commit-id table first proves "`scv update`
  itself refuses it too".
⭐The hash is taken from the committed blob (A4): the suite commits two versions in a scratch repository and then
  changes the working tree's copy (an uncommitted line + CRLF) ⇒ hashing from the working tree, or from a
  different commit, both go red.
🔴This suite never touches the setup.md in this repository (Step 7's release gate: the real commit id is only
  stamped after the maintainer's ruling): it always stamps one in a scratch repository. The scratch repository
  never uses the git configuration of whoever is running the suite (autocrlf, signing, hooks all follow that
  machine): `GIT_CONFIG_NOSYSTEM` plus an empty `GIT_CONFIG_GLOBAL`.
"""
import argparse
import ast
import contextlib
import glob
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from tests import helpers
from tests.test_97_docs import download_url, step_with
import scv

NL = chr(10)
CR = chr(13)
RELEASE = os.path.join(helpers.ROOT, "tools", "release.py")
CI_YML = os.path.join(helpers.ROOT, ".github", "workflows", "ci.yml")


def setUpModule():
    helpers.fresh_home("release", unittest.addModuleCleanup)    # where `_save_latest` writes latest.json


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Release(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("git"):
            raise unittest.SkipTest("no git on PATH: the release script hashes the committed blob, and without git there is no blob")
        cls.tmp = tempfile.mkdtemp(prefix=".scv-test-release-")
        cls.addClassCleanup(helpers.remove_tree, cls.tmp)
        cls.repo = os.path.join(cls.tmp, "repo")
        os.mkdir(cls.repo)
        gitcfg = os.path.join(cls.tmp, "gitconfig")
        open(gitcfg, "wb").close()
        who = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        cls.env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=gitcfg, PYTHONIOENCODING="utf-8", **who)
        with open(os.path.join(helpers.ROOT, "setup.md"), "rb") as f:
            cls.setup_raw = f.read()
        with open(os.path.join(helpers.ROOT, ".gitattributes"), "rb") as f:
            attrs = f.read()
        with open(scv.__file__, "rb") as f:
            # ⭐I-2: never this repository's own committed bytes as-is — if setup.md has already been stamped for
            #   a real release (its sha256 pin already equals this exact blob's hash), the "download lines plus the
            #   sha256 line" test below would see the sha256 line as unchanged and go red (measured: "2 != 3", both
            #   subtests). The trailing line guarantees this scratch commit's hash can never coincide with a real
            #   release's, whichever setup.md this suite happens to run against.
            cls.first_bytes = f.read().replace((CR + NL).encode(), NL.encode()) + b"# the first commit\n"
        cls.second_bytes = cls.first_bytes + b"# the second commit\n"
        cls.put(".gitattributes", attrs)
        cls.put("setup.md", cls.setup_raw)
        cls.put("scv.py", cls.first_bytes)
        cls.git("init", "-q")
        cls.git("add", "--", ".gitattributes", "setup.md", "scv.py")
        cls.git("commit", "-q", "-m", "one")
        cls.first = cls.git("rev-parse", "HEAD")
        cls.put("scv.py", cls.second_bytes)
        cls.git("commit", "-q", "-am", "two")
        cls.second = cls.git("rev-parse", "HEAD")
        cls.put("scv.py", b"this is ( not python\n")
        cls.git("commit", "-q", "-am", "three")
        cls.broken = cls.git("rev-parse", "HEAD")
        # this working-tree copy differs from all three commits (an uncommitted line + CRLF): a hash computed
        # from the working tree matches none of them
        cls.worktree_bytes = cls.second_bytes.replace(NL.encode(), (CR + NL).encode()) + b"# only in the worktree\r\n"
        cls.put("scv.py", cls.worktree_bytes)

    @classmethod
    def put(cls, name, data):
        with open(os.path.join(cls.repo, name), "wb") as f:
            f.write(data)

    @classmethod
    def git(cls, *argv):
        p = subprocess.run(["git", "-C", cls.repo] + list(argv), capture_output=True, timeout=60, env=cls.env,
                           **scv.new_session_kw())
        assert p.returncode == 0, (argv, p.stderr.decode("utf-8", "replace"))
        return p.stdout.decode("ascii").strip()

    def setUp(self):
        self.put("setup.md", self.setup_raw)            # every case starts from this repository's own setup.md

    def release(self, *argv):
        p = subprocess.run([sys.executable, RELEASE, "--root", self.repo] + list(argv), capture_output=True, timeout=120,
                           env=self.env, **scv.new_session_kw())
        return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8", "replace")

    def setup_md(self):
        with open(os.path.join(self.repo, "setup.md"), "rb") as f:
            return f.read()

    def url(self, commit):
        return "%s/%s/scv.py" % (scv.UPDATE_BASE, commit)

    def test_it_hashes_the_committed_blob_and_stamps_setup_md(self):
        """A4: the hash = the bytes committed at that commit (never the working tree, never a different commit); a
        short commit id gets expanded to the full one; every block in setup.md's download step is swapped to this
        commit, the pinned hash is swapped to this value, and not one placeholder is left; every other line stays
        byte-for-byte unchanged (line endings too) — run once each for an LF checkout and a CRLF checkout."""
        want = sha(self.first_bytes)
        # ⚠️Both line-ending variants are built here on purpose: the repository's own copy is already CRLF on an
        #   autocrlf=true checkout (the "LF" cell used to just take it as-is, so on that kind of checkout both
        #   cells were CRLF — mutation R-crlf actually hit this: cut the "line endings unchanged" check and both
        #   cells went red together)
        lf = self.setup_raw.replace(b"\r\n", b"\n")
        for label, raw in (("LF", lf), ("CRLF", lf.replace(b"\n", b"\r\n"))):
            with self.subTest(setup_md=label):
                self.put("setup.md", raw)
                rc, out, err = self.release("--commit", self.first[:7])
                self.assertEqual(rc, 0, err)
                self.assertEqual(json.loads(out), {"latest": {"version": scv.VERSION, "commit": self.first, "sha256": want}})
                got = self.setup_md()
                text = got.decode("utf-8")
                for other in (self.worktree_bytes, self.second_bytes):          # zero-input control: none of the hashes computed from other bytes appear
                    self.assertNotIn(sha(other), text + out)
                dl = step_with("--create-dirs", text.replace(CR + NL, NL))
                self.assertEqual(sorted(dl), ["bash", "powershell"])
                for lang, body in dl.items():
                    self.assertIn(self.url(self.first), body, lang)
                self.assertEqual(text.count("sha256 `%s`" % want), 1)
                for hole in ("<COMMIT>", "<SHA256>"):
                    self.assertNotIn(hole, text)
                before, after = raw.split(b"\n"), got.split(b"\n")
                self.assertEqual(len(before), len(after))
                changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
                self.assertEqual(len(changed), len(dl) + 1, [after[i] for i in changed])   # the download lines plus the one pinned-hash line
                for i in changed:
                    line = after[i].decode("utf-8")
                    self.assertTrue(self.url(self.first) in line or ("sha256 `%s`" % want) in line, line)
                    self.assertEqual(before[i].endswith(b"\r"), after[i].endswith(b"\r"))

    def test_the_printed_latest_is_one_scv_update_takes(self):
        """A5: the printed `latest` is fed to `scv update`'s own path: `RemoteLeg._save_latest` (this is exactly
        how the bridge stores it when it gets a hello reply) → `cmd_update` with no arguments (reads latest.json)
        → the shape gate → compare by bytes. What is installed is exactly that commit's bytes ⇒ "already at this
        version", never a dial. Welded on: a positive control — the same path really does refuse a shape it does
        not accept (an upper-case commit id), so a blind ruler does not come back all green."""
        rc, out, err = self.release("--commit", self.first)
        self.assertEqual(rc, 0, err)
        latest = json.loads(out)["latest"]
        installed = os.path.join(os.environ["SCV_HOME"], "installed-scv.py")
        with open(installed, "wb") as f:
            f.write(self.first_bytes)
        leg = scv.RemoteLeg(types.SimpleNamespace(cfg={"remote_url": "https://example.invalid", "remote_token": "t",
                                                       "max_concurrent": 1}))
        for label, row, want_rc, want_text in (("what the release script stamped", latest, 0, "already at this version"),
                                               ("commit id upper-cased", dict(latest, commit=latest["commit"].upper()), 1, "not updated")):
            with self.subTest(latest=label):
                leg._save_latest(row)
                so, se = io.StringIO(), io.StringIO()
                with mock.patch.object(scv, "_fetch", side_effect=AssertionError("this cell must never fetch the file")), \
                        contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
                    rc = scv.cmd_update(argparse.Namespace(commit="", sha256=""), target=installed)
                self.assertEqual(rc, want_rc, so.getvalue() + se.getvalue())
                self.assertIn(want_text, so.getvalue() + se.getvalue())

    def test_a_commit_scv_update_would_refuse_is_refused_before_anything_is_touched(self):
        """`--commit` passes through `scv update`'s own shape gate: every cell first proves `scv.COMMIT_RE` itself
        refuses it too (never judge by a hand-copied table), then proves the release script refused it, printed
        nothing to stdout, and did not touch a single byte of setup.md. A commit id with a good shape that is not
        in the repository is refused the same way."""
        for bad in ("ABCDEF1", "abc123", "g" * 7, "-abcdef1", "a" * 41, "HEAD", self.first[:7] + NL, self.first.upper()):
            with self.subTest(commit=repr(bad)):
                self.assertIsNone(scv.COMMIT_RE.fullmatch(bad))
                rc, out, err = self.release("--commit=" + bad)
                self.assertEqual((rc, out), (1, ""), err)
                self.assertIn("not released (setup.md untouched)", err)
                self.assertEqual(self.setup_md(), self.setup_raw)
        self.assertIsNotNone(scv.COMMIT_RE.fullmatch("0000000"))
        rc, out, err = self.release("--commit", "0000000")
        self.assertEqual((rc, out, self.setup_md()), (1, "", self.setup_raw), err)
        # review M6: good shape, not in the repository — the most common slip at release time (a copy-pasted-wrong
        #   commit id). git says not a word under `--quiet`, so the old sentence ended on an empty colon ⇒ pin a
        #   human sentence that names that commit id (never settle for "the commit id showed up somewhere": the
        #   old sentence also just echoed argv verbatim)
        self.assertIn("the repository (%s) has no commit 0000000" % self.repo, err)

    def test_a_file_scv_update_would_refuse_is_not_released(self):
        """that commit's scv.py is not valid Python ⇒ `scv update` would refuse it on receipt ⇒ the release
        script refuses it first (never print a `latest` that cannot be installed)."""
        rc, out, err = self.release("--commit", self.broken)
        self.assertEqual((rc, out), (1, ""), err)
        self.assertIn("not valid UTF-8 Python", err)
        self.assertEqual(self.setup_md(), self.setup_raw)

    def test_a_failed_write_leaves_setup_md_and_no_buffer_behind(self):
        """review M2: a failed write ⇒ rc=1, not a single byte of setup.md touched, and not one new file in the
        repository root (it used to leave `setup.md.release-tmp` behind). How this is made to fail: on win32,
        setup.md is set read-only ⇒ the buffer can still be written, and `os.replace` is refused (WinError 5) —
        exactly the cell that used to leave a scrap behind; ⚠️on POSIX, rename does not look at the target file's
          own permission bits ⇒ the repository root is made unwritable instead (so the buffer cannot even be
          written: this arm cannot see "the buffer was written, then the swap failed", it only proves "it failed,
          left nothing, and did not touch setup.md"); root ignores directory permissions ⇒ skipped."""
        p = os.path.join(self.repo, "setup.md")
        if os.name == "nt":
            os.chmod(p, stat.S_IREAD)
            self.addCleanup(os.chmod, p, stat.S_IREAD | stat.S_IWRITE)
        else:
            if os.geteuid() == 0:
                self.skipTest("root ignores directory permissions: cannot make an unwritable repository root")
            os.chmod(self.repo, 0o555)
            self.addCleanup(os.chmod, self.repo, 0o755)
        before = sorted(os.listdir(self.repo))
        rc, out, err = self.release("--commit", self.first)
        self.assertEqual((rc, out), (1, ""), err)
        self.assertIn("not released (setup.md untouched)", err)
        self.assertEqual(self.setup_md(), self.setup_raw)
        self.assertNotIn("setup.md.release-tmp", os.listdir(self.repo))
        self.assertEqual(sorted(os.listdir(self.repo)), before)

    def test_releasing_again_restamps_the_last_release(self):
        """releasing a second time (setup.md already carries the hex stamped by the previous release) still
        stamps over it cleanly: not a trace of the previous commit or its hash is left."""
        self.assertEqual(self.release("--commit", self.first)[0], 0)
        rc, out, err = self.release("--commit", self.second)
        self.assertEqual(rc, 0, err)
        text = self.setup_md().decode("utf-8")
        self.assertEqual(json.loads(out)["latest"]["sha256"], sha(self.second_bytes))
        self.assertEqual(text.count(self.url(self.second)), len(step_with("--create-dirs", text)))
        self.assertIn("sha256 `%s`" % sha(self.second_bytes), text)
        self.assertNotIn(self.first, text)
        self.assertNotIn(sha(self.first_bytes), text)

    def test_a_setup_md_that_does_not_match_scv_update_is_refused_untouched(self):
        """a download address not built from `UPDATE_BASE` (the public repository was renamed and setup.md never
        caught up — it would silently keep the old address), the download-address occurrences not matching the
        download step's command-block count, two hashes pinned, or a placeholder left after stamping ⇒ refused,
        and setup.md is not touched by a single byte.
        ⭐Each refusal has one cell that only it catches (never a cell two checks both catch at once — that would
          not prove either one: mutation R-stray actually hit this — "one block's repository name was changed"
          used to use the copy that still had the placeholder, and with the "another repository's address" check
          cut out, the placeholder that was still left got caught by "a placeholder is left after stamping"
          anyway, and the cell stayed green):
          "one block lost its download address" and "one extra look-alike download address" are each caught only
          by the occurrence-count check (review M1: it used to refuse only at 0 places, so both of these cells got
          stamped anyway);
          "already stamped once, with another repository's address elsewhere in the body" is caught only by the
          "another repository's address" check (the occurrence count is still 2 = the block count, and there is
          no placeholder);
          "another placeholder elsewhere in the body" is caught only by the "placeholder left" check; "two hashes
          pinned" is caught only by its own check."""
        text = self.setup_raw.decode("utf-8")
        other = "https://raw.githubusercontent.com/someone-else/scv-bridge"
        stamped = text.replace("/<COMMIT>/", "/%s/" % self.second).replace("<SHA256>", sha(self.second_bytes))
        self.assertNotIn("<COMMIT>", stamped)
        moved = text.replace(scv.UPDATE_BASE, other)
        no_url = NL.join(x for x in text.split(NL) if not download_url().search(x))
        lines = text.split(NL)
        first_dl = next(i for i, x in enumerate(lines) if download_url().search(x))
        one_block_lost = NL.join(lines[:first_dl] + lines[first_dl + 1:])
        pin = re.search("sha256 `[^`]*`", text).group(0)
        two_pins = text.replace(pin, pin + " or " + pin, 1)
        for label, bad in (("the repository was renamed", moved),
                           ("already stamped once, only one block got the new repository name", stamped.replace(scv.UPDATE_BASE, other, 1)),
                           ("already stamped once, with another repository's address elsewhere in the body",
                            stamped + NL + "Mirror: %s/%s/scv.py" % (other, self.second) + NL),
                           ("still has the placeholder, only one block got the new repository name", text.replace(scv.UPDATE_BASE, other, 1)),
                           ("no download address", no_url), ("one block lost its download address", one_block_lost),
                           ("one extra look-alike download address", text + NL + "Mirror: %s/<COMMIT>/scv.py" % scv.UPDATE_BASE + NL),
                           ("two hashes pinned", two_pins),
                           ("another placeholder elsewhere in the body", text + NL + "Also see <SHA256>." + NL)):
            with self.subTest(setup_md=label):
                self.assertNotEqual(bad, text)                              # precondition: this cell really did change something
                self.put("setup.md", bad.encode("utf-8"))
                rc, out, err = self.release("--commit", self.first)
                self.assertEqual((rc, out), (1, ""), err)
                self.assertEqual(self.setup_md(), bad.encode("utf-8"))


class MinPyGrammar(unittest.TestCase):
    """scv.py declares it needs Python ≥ `MIN_PY` (setup.md's step 1 says the same); it is in the CI matrix
    (`CiWorkflow` pins that). This development machine has no 3.9 ⇒ every Python version first gets its syntax
    checked here: scv.py, tests/, and tools/ must all pass `ast.parse` under `MIN_PY`'s grammar.
    ⚠️`feature_version` is best-effort and only covers syntax (match / `except*` / type parameters ...): it does
      not cover the API (functions or keyword arguments that only exist from 3.10 on, such as `zip(strict=)`), it
      does not cover a form that is only evaluated at run time (the next test covers half of that), and it does
      not cover reusing the outer quote character inside an f-string, which is only allowed from 3.12 on
      (PEP 701). Those can only really be exercised by CI's own 3.9 cell (⏳ no remote to test on, unverified)."""

    FILES = [scv.__file__] + sorted(glob.glob(os.path.join(helpers.ROOT, "tests", "*.py"))) \
        + sorted(glob.glob(os.path.join(helpers.ROOT, "tools", "*.py")))

    def test_every_py_file_parses_with_the_min_python_grammar(self):
        self.assertGreater(len(self.FILES), 10)
        for path in self.FILES:
            with self.subTest(file=os.path.relpath(path, helpers.ROOT)):
                with io.open(path, encoding="utf-8") as f:
                    ast.parse(f.read(), path, feature_version=scv.MIN_PY)

    def test_no_annotation_evaluated_at_runtime_uses_a_union_bar(self):
        """In a file that has not turned on `from __future__ import annotations`, a function signature's
        annotations and a module's or class's top-level annotations are evaluated at definition time: `int | None`
        is a TypeError on 3.9 (it blows up on import). A file that has turned it on never evaluates its
        annotations, so it is never checked."""
        bad = []
        for path in self.FILES:
            with io.open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            if any(isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names)
                   for n in tree.body):
                continue
            bad += ["%s@%d" % (os.path.basename(path), n.lineno) for n in _evaluated_annotations(tree)
                    if any(isinstance(x, ast.BinOp) and isinstance(x.op, ast.BitOr) for x in ast.walk(n))]
        self.assertEqual(bad, [])

    def test_the_two_checks_are_not_blind(self):
        """positive control: `match` is refused under `MIN_PY`'s grammar; `int | None` in a signature without the
        future import is flagged, and one with it turned on is never flagged."""
        with self.assertRaises(SyntaxError):
            ast.parse("match x:\n    case 1:\n        pass\n", feature_version=scv.MIN_PY)
        sig = ast.parse("def f(x: int | None) -> str | None:\n    y: int | None = 1\n")
        self.assertEqual([n.lineno for n in _evaluated_annotations(sig)], [1, 1])      # a local variable's annotation is never evaluated ⇒ it is never counted


def _evaluated_annotations(tree):
    """The annotation nodes that get evaluated at definition time: a function's parameter / return annotations,
    and a module's or class's top-level `x: T = …` (one inside a function body is never evaluated)."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            out += [x.annotation for x in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]
                    if x is not None and x.annotation is not None]
            if node.returns is not None:
                out.append(node.returns)
    for scope in [tree] + [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        out += [n.annotation for n in scope.body if isinstance(n, ast.AnnAssign)]
    return out


class CiWorkflow(unittest.TestCase):
    """A few shape checks on `.github/workflows/ci.yml` (Windows only until the POSIX pass, then one gate per OS; §8 "each OS is its
    own independent path").
    ⚠️The standard library has no YAML parser ⇒ this file, which we wrote ourselves with fixed indentation, is
      read line by line, and anything it fails to recognize goes red on the spot (never guessed at).
    ⏳Whether this workflow itself runs to green on GitHub: there is no remote, unverified."""

    @classmethod
    def setUpClass(cls):
        with io.open(CI_YML, encoding="utf-8") as f:
            cls.text = f.read()
        cls.lines = cls.text.splitlines()

    def flow_list(self, key):
        """The line `<key>: [a, "b", …]` ⇒ ["a", "b", …] (exactly one such line)."""
        hits = [x for x in self.lines if re.match(r"^\s+%s: \[.*\]\s*$" % re.escape(key), x)]
        self.assertEqual(len(hits), 1, key)
        return [v.strip().strip('"') for v in hits[0].split("[", 1)[1].rsplit("]", 1)[0].split(",")]

    def steps(self):
        """Every item under steps (from a `      - ` line to the next one) ⇒ [its body]."""
        start = self.lines.index("    steps:")
        out = []
        for x in self.lines[start + 1:]:
            if x.startswith("      - "):
                out.append([x])
            elif out and (x.startswith("        ") or not x.strip()):
                out[-1].append(x)
            else:
                break
        return [NL.join(s) for s in out]

    def test_windows_until_the_posix_pass_and_the_min_python(self):
        """Windows only (the maintainer's decision, 2026-09-26: Linux / macOS were never measured, README says so). The
        POSIX pass puts ubuntu-latest and macos-latest back, and this list with them."""
        self.assertEqual(sorted(self.flow_list("os")), ["windows-latest"])
        pys = self.flow_list("python")
        self.assertIn("%d.%d" % scv.MIN_PY, pys)
        self.assertGreater(len(pys), 1)
        self.assertIn("fail-fast: false", self.text)          # one OS going red must never cut off the others (B33: each is its own gate)

    def test_the_workflow_token_can_only_read(self):
        """review out-of-scope 5: a public repository's workflow only runs tests ⇒ the token the repository hands
        it must be read-only (never left to follow the repository's own settings). Pinned at the workflow's top
        level (never inside one job: a newly added job would slip past), and not one `write` / `write-all`
        anywhere in the whole file."""
        self.assertIn("permissions:", self.lines)                # this block exists at the top level (0-column indent) — so the next line does not blow up with a ValueError
        at = self.lines.index("permissions:")
        self.assertEqual(self.lines[at + 1:at + 2], ["  contents: read"])
        self.assertTrue(self.lines[at + 2].startswith(tuple("abcdefghijklmnopqrstuvwxyz")), self.lines[at + 2])  # the block has just this one line
        self.assertEqual([x for x in self.lines if re.search(r"\bwrite(-all)?\b", x)], [])

    def test_the_full_run_is_the_local_command(self):
        """the full-run step = the same one this repository has always been running (`-W default`: a
        ResourceWarning must stay visible)."""
        full = [s for s in self.steps() if "unittest discover" in s]
        self.assertEqual(len(full), 1)
        self.assertIn("python -W default -m unittest discover -s tests -t .", full[0])
        self.assertIn("PYTHONIOENCODING: utf-8", full[0])

    def test_the_pipe_step_runs_without_the_encoding_variables(self):
        """C21: the "piped status/doctor output is valid UTF-8" step must never carry `PYTHONIOENCODING` /
        `PYTHONUTF8` (the user's shell has neither) ⇒ neither variable may be written at the job/workflow level
        (that would leak into every step), and `PYTHONUTF8` must never appear anywhere at all (it even changes
        `open()`'s default encoding, and would turn green on CI a piece of code that only blows up on a
        GBK/cp1252 machine)."""
        steps = self.steps()
        pipe = [s for s in steps if "PipedOutputIsUtf8" in s]
        self.assertEqual(len(pipe), 1)
        self.assertNotIn("PYTHONIOENCODING", pipe[0])
        self.assertNotIn("PYTHONUTF8", self.text)
        outside = [x for x in self.lines if "PYTHONIOENCODING" in x and not x.startswith("          ")]
        self.assertEqual(outside, [])                          # only allowed inside one step's own env (10-column indent)


if __name__ == "__main__":
    unittest.main()
