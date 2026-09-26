# -*- coding: utf-8 -*-
"""tools/build.py: the committed scv.py is exactly the pieces under src/ joined in name order.

The pieces are the source; scv.py is what players download and pin by sha256, so it stays one file, and every other
test imports it (the tests run the file players get). `Build` holds the committed pair together; `BuildRefuses`
holds what the tool refuses, in a temporary tree (never the repository's src/ or scv.py)."""
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tests import helpers
import scv

BUILD_PY = os.path.join(helpers.ROOT, "tools", "build.py")


def load_build():
    spec = importlib.util.spec_from_file_location("scv_build", BUILD_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build = load_build()


class Build(unittest.TestCase):
    def test_scv_py_is_exactly_the_join_of_the_pieces(self):
        data, n = build.joined(helpers.ROOT)
        with open(scv.__file__, "rb") as f:
            have = f.read()
        self.assertGreater(n, 1)
        if have != data:
            self.fail("scv.py is not the join of src/ (they differ from line %d): edit the pieces under src/, then run "
                      "python tools/build.py" % build.first_difference(have, data))

    def test_pieces_are_checked_out_with_exactly_the_bytes_git_stores(self):
        """Same as test_95's `Bytes` for scv.py: an autocrlf=true checkout without the .gitattributes line gives CRLF
        pieces (build.py would then refuse them, loudly; this says why)."""
        if not shutil.which("git"):
            self.skipTest("no git")

        def git(*argv):
            p = subprocess.run(["git"] + list(argv), capture_output=True, timeout=60, cwd=helpers.ROOT,
                               **scv.new_session_kw())
            return p.returncode, p.stdout.decode("utf-8", "replace").strip()

        if git("rev-parse", "--is-inside-work-tree") != (0, "true"):
            self.skipTest("not a git checkout (run from an archive, say)")
        paths = [os.path.relpath(p, helpers.ROOT).replace(os.sep, "/") for p in build.pieces(helpers.ROOT)]
        for rel in paths:
            with self.subTest(piece=rel):
                self.assertEqual(git("check-attr", "eol", "--", rel), (0, "%s: eol: lf" % rel))
                self.assertEqual(git("hash-object", rel), git("hash-object", "--no-filters", rel))


class BuildRefuses(unittest.TestCase):
    GOOD = {"10_a.py": b"import os\n", "20_b.py": b"X = 1\n\n\ndef f():\n    return X\n"}
    OLD = b"old scv.py\n"

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix=".scv-test-build-")
        self.addCleanup(helpers.remove_tree, self.root)
        os.mkdir(os.path.join(self.root, "src"))
        for name, data in self.GOOD.items():
            self.put("src/" + name, data)
        self.put("scv.py", self.OLD)

    def put(self, rel, data):
        with open(os.path.join(self.root, rel), "wb") as f:
            f.write(data)

    def scv_py(self):
        with open(os.path.join(self.root, "scv.py"), "rb") as f:
            return f.read()

    def run_build(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = build.main(["--root", self.root] + list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_a_good_tree_is_joined_in_name_order_and_checked(self):
        self.put("src/05_first.py", b"# first\n")           # made last, sorts first
        self.assertEqual(self.run_build("--check")[0], 1)    # the old scv.py is not the join
        self.assertEqual(self.scv_py(), self.OLD)             # --check writes nothing
        rc, out, err = self.run_build()
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.scv_py(), b"# first\n" + self.GOOD["10_a.py"] + self.GOOD["20_b.py"])
        self.assertIn("3 pieces", out)
        self.assertEqual(self.run_build("--check")[0], 0)
        self.put("scv.py", self.scv_py().replace(b"X = 1", b"X = 2"))
        rc, out, err = self.run_build("--check")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("from line 3", err)                     # `X = 1` is line 3 of the join

    def test_the_order_is_by_name_not_by_what_the_directory_lists_first(self):
        """NTFS lists a directory in name order anyway, so the test above cannot tell a sort from none on Windows:
        hand build.py the listing reversed."""
        listdir = os.listdir
        with mock.patch.object(build.os, "listdir", lambda d: list(reversed(listdir(d)))):
            self.assertEqual(self.run_build()[0], 0)
        self.assertEqual(self.scv_py(), self.GOOD["10_a.py"] + self.GOOD["20_b.py"])

    def test_each_bad_tree_is_refused_and_scv_py_is_left_alone(self):
        cases = [  # (label, piece written over GOOD's, its bytes, words the refusal must say)
            ("CR", "20_b.py", b"X = 1\r\n", "src/20_b.py has a CR byte on line 1"),
            ("CR further down", "20_b.py", b"X = 1\n\n\r\n", "src/20_b.py has a CR byte on line 3"),
            ("no final newline", "20_b.py", b"X = 1", "src/20_b.py does not end with a newline"),
            ("empty", "20_b.py", b"", "src/20_b.py is empty"),
            ("BOM", "20_b.py", b"\xef\xbb\xbfX = 1\n", "src/20_b.py starts with a UTF-8 byte-order mark"),
            ("not UTF-8", "20_b.py", b"X = '\xff'\n", "src/20_b.py is not UTF-8"),
            ("cut before a body", "20_b.py", b"def f():\n", "src/20_b.py does not parse on its own"),
            ("cut inside a body", "20_b.py", b"    return X\n", "src/20_b.py does not parse on its own"),
            ("misnamed", "b.py", b"X = 1\n", "src/b.py is not named like a piece"),
            ("misnamed upper case", "30_B.py", b"X = 1\n", "src/30_B.py is not named like a piece"),
        ]
        for label, name, data, words in cases:
            with self.subTest(case=label):
                self.setUp()
                self.put("src/" + name, data)
                rc, out, err = self.run_build()
                self.assertEqual((rc, out), (1, ""), err)
                self.assertIn(words, err)
                self.assertEqual(self.scv_py(), self.OLD)
                self.assertEqual(sorted(os.listdir(self.root)), ["scv.py", "src"])
                self.assertEqual(self.run_build("--check")[0], 1)

    def test_no_src_or_no_pieces_is_refused(self):
        os.remove(os.path.join(self.root, "src", "10_a.py"))
        os.remove(os.path.join(self.root, "src", "20_b.py"))
        self.put("src/notes.txt", b"not a piece\n")          # other files are ignored, not joined
        rc, out, err = self.run_build()
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("src/ has no pieces", err)
        os.remove(os.path.join(self.root, "src", "notes.txt"))
        os.rmdir(os.path.join(self.root, "src"))
        rc, out, err = self.run_build()
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("there is no src/ directory", err)
        self.assertEqual(self.scv_py(), self.OLD)

    def test_a_failed_write_leaves_the_old_scv_py_whole_and_no_buffer(self):
        with mock.patch.object(build.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.run_build()
        self.assertEqual(self.scv_py(), self.OLD)
        self.assertEqual(sorted(os.listdir(self.root)), ["scv.py", "src"])


if __name__ == "__main__":
    unittest.main()
