# -*- coding: utf-8 -*-
"""Join the pieces under src/ into scv.py (for whoever works on this repository; players never run it).

scv.py is what players download and pin by sha256 (setup.md, `scv update`), so it stays one file. In this
repository it is kept as pieces, src/NN_name.py, joined in name order, byte for byte, with nothing added
between them. Edit the pieces, run this, commit both. tests/test_01_build.py fails when the committed scv.py
is not exactly the join, so the tests always run the file players get.

A piece must be named NN_name.py (two digits, then lower-case letters, digits and underscores), be non-empty
UTF-8 without a byte-order mark, use LF line endings only, end with a newline, and parse as Python on its own
(so every cut falls between two top-level statements). Anything else is refused with one line on stderr and
scv.py is left as it was. Other files in src/ (not ending in .py) are ignored.

usage: python tools/build.py [--check] [--root DIR]
  (no flag)  write scv.py, through a buffer file and os.replace
  --check    write nothing; exit 1 if scv.py is not the join
exit codes: 0 ok; 1 refused, or --check found a difference; 2 bad arguments (argparse).
Standard library only, like scv.py."""
import argparse
import ast
import codecs
import hashlib
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = "scv.py"
PIECE_RE = re.compile("[0-9]{2}_[a-z0-9_]+[.]py")


class Refused(Exception):
    """One line naming the piece and what is wrong with it; nothing was written."""


def pieces(root):
    """The pieces' paths, in the order they are joined (sorted by name)."""
    src = os.path.join(root, "src")
    if not os.path.isdir(src):
        raise Refused("there is no src/ directory under %s" % root)
    names = sorted(n for n in os.listdir(src) if n.endswith(".py"))
    odd = [n for n in names if not PIECE_RE.fullmatch(n)]
    if odd:
        raise Refused("src/%s is not named like a piece (NN_name.py): rename it, or move it out of src/" % odd[0])
    if not names:
        raise Refused("src/ has no pieces")
    return [os.path.join(src, n) for n in names]


def check_piece(name, raw):
    """Refuse a piece that would not join cleanly."""
    if not raw:
        raise Refused("src/%s is empty" % name)
    if raw.startswith(codecs.BOM_UTF8):
        raise Refused("src/%s starts with a UTF-8 byte-order mark: save it without one" % name)
    if b"\r" in raw:
        raise Refused("src/%s has a CR byte on line %d: pieces use LF only (.gitattributes pins src/*.py to eol=lf; "
                      "a checkout made before that line existed keeps CRLF until it is checked out again)"
                      % (name, raw.count(b"\n", 0, raw.index(b"\r")) + 1))
    if not raw.endswith(b"\n"):
        raise Refused("src/%s does not end with a newline" % name)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise Refused("src/%s is not UTF-8 (byte %d)" % (name, e.start))
    try:
        ast.parse(text, name)
    except SyntaxError as e:
        raise Refused("src/%s does not parse on its own (line %s: %s): every cut must fall between two top-level "
                      "statements" % (name, e.lineno, e.msg))


def joined(root):
    """(bytes of the join, number of pieces)."""
    out = []
    for path in pieces(root):
        with open(path, "rb") as f:
            raw = f.read()
        check_piece(os.path.basename(path), raw)
        out.append(raw)
    return b"".join(out), len(out)


def first_difference(a, b):
    """1-based number of the first line where a and b differ (a line past the shorter one if one is a prefix)."""
    x, y = a.split(b"\n"), b.split(b"\n")
    return next((i + 1 for i, (p, q) in enumerate(zip(x, y)) if p != q), min(len(x), len(y)) + 1)


def write(path, data):
    """Buffer file next to the target, then os.replace: a write that fails half-way leaves the old file whole."""
    tmp = path + ".build-tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tools/build.py", description="Join src/*.py into scv.py.")
    ap.add_argument("--check", action="store_true", help="write nothing; exit 1 if scv.py is not the join")
    ap.add_argument("--root", default=ROOT, help="repository root (default: the one this file is in)")
    a = ap.parse_args(argv)
    try:
        data, n = joined(a.root)
    except Refused as e:
        print("build: refused: %s" % e, file=sys.stderr)
        return 1
    target = os.path.join(a.root, TARGET)
    try:
        with open(target, "rb") as f:
            have = f.read()
    except FileNotFoundError:
        have = None
    said = "%d pieces, %d lines, sha256 %s" % (n, data.count(b"\n"), hashlib.sha256(data).hexdigest())
    if have == data:
        print("scv.py is the join of src/ (%s)" % said)
        return 0
    if a.check:
        where = "there is no scv.py" if have is None else "they differ from line %d" % first_difference(have, data)
        print("build: scv.py is not the join of src/ (%s): edit the pieces under src/, then run python tools/build.py"
              % where, file=sys.stderr)
        return 1
    write(target, data)
    print("wrote scv.py (%s)" % said)
    return 0


if __name__ == "__main__":
    sys.exit(main())
