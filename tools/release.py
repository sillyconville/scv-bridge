# -*- coding: utf-8 -*-
"""A small release tool (never ships to a player's machine: only the person doing the release runs it, in this
repository; like scv.py, standard library only).

Usage: python tools/release.py --commit <commit> [--root <repo root>]
Does three things, all from the bytes of the `scv.py` committed at that commit (`git cat-file blob <commit>:scv.py`,
the same bytes as `git show <commit>:scv.py`; using plumbing so no textconv ever touches it) — that is exactly the
copy the user downloads from raw.githubusercontent (NOTES.md::update-bytes, A4), never computed from the working
tree's file (an uncommitted change, or releasing an old commit, means the two are not the same bytes):
  ① compute the sha256;
  ② write the commit into setup.md's download-address commit (`<COMMIT>` or the hex stamped by the last release,
     exactly one per command block in the download step) and into `sha256 `…`` (one place);
  ③ print to stdout the `latest` section a dispatcher's hello reply should carry (JSON,
     `{"latest": {version, commit, sha256}}`).
⭐The criteria are never copied by hand: the shape of `--commit` and `latest`, the file-size ceiling, and the
  download-address prefix all come straight from `scv update`'s own copies in scv.py (`COMMIT_RE` / `SHA256_RE` /
  `SCV_PY_MAX_BYTES` / `UPDATE_BASE`) — `latest` is food for the `scv update` installed on a player's machine, so
  anything it would not accept is refused right here (the shape it does accept is exercised for real by
  tests/test_98_release.py, feeding `cmd_update`).
⭐The commit is always expanded to the full 40 hex digits before being written out: whether a short commit id is
  accepted on raw.githubusercontent ⏳ has not been measured, and as the repository grows a short one could become
  ambiguous.
⭐Every judgment that can fail happens before setup.md is written (refused = not a single byte of setup.md moved);
  writing goes through `scv._replace_file` (buffer → os.replace, with the failure path cleaning up its own buffer;
  a write-that-truncates-on-open failing halfway through would mean the original file is gone); every other byte
  of setup.md (including line endings) is left exactly as it was.
The release sequence (I-2: nothing wrote this down before, and "one fresh commit" cannot hold its own stamp —
  setup.md has to name a commit that already exists in the public repository): (1) publish commit A (code,
  setup.md still carrying the `<COMMIT>`/`<SHA256>` placeholders); (2) `python tools/release.py --commit A`;
  (3) commit the stamped setup.md as commit B (or serve it only from the site, never inside A); (4) give the
  dispatcher the printed `latest`. Never amend A after stamping — the stamped address would then point at a commit
  the public repository does not have (raw.githubusercontent → 404).
Exit code: 0 = stamped; 1 = refused (one reason on stderr); 2 = bad arguments (argparse)."""
import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import scv  # noqa: E402  ⭐borrows only `scv update`'s own checks and UPDATE_BASE (never keep a second copy here)

COMMIT_HOLE, SHA_HOLE = "<COMMIT>", "<SHA256>"      # the two placeholders in setup.md waiting to be filled in at release time


class Refused(Exception):
    """Refused: the one line on stderr is its exact words; setup.md was not touched."""


# ━━ Get that commit's scv.py
def git(root: str, *argv: str, why: str = "") -> bytes:
    """`why` = the human sentence said first when it fails (the cells where git itself says not a word —
    `rev-parse --quiet` — never leave just an empty colon, review M6); whatever git said is appended after it,
    never swallowed."""
    p = subprocess.run(["git", "-C", root] + list(argv), capture_output=True, timeout=60, **scv.new_session_kw())
    if p.returncode != 0:
        said = p.stderr.decode("utf-8", "replace").strip()[:400]
        head = why or "git %s did not succeed (rc=%d)" % (" ".join(argv), p.returncode)
        raise Refused(head + ("; git said: " + said if said else ""))
    return p.stdout


def resolve(root: str, commit: str) -> str:
    """`--commit` first passes `scv update`'s own shape gate (never hand git a string in the wrong shape: one
    starting with `-` would be taken as an option), then gets expanded to the full commit id."""
    if not scv.COMMIT_RE.fullmatch(commit):
        raise Refused("--commit must be 7-40 lower-case hex digits (`scv update` only accepts this shape, "
                      "scv.COMMIT_RE); got %r" % commit[:80])
    full = git(root, "rev-parse", "--verify", "--quiet", commit + "^{commit}",
               why="the repository (%s) has no commit %s: was the commit id copied wrong, or has that commit not "
                   "been fetched here yet?" % (root, commit))
    full = full.decode("ascii", "replace").strip()
    if not scv.COMMIT_RE.fullmatch(full):
        raise Refused("%s expanded to %r: `scv update` does not accept that shape (is this repository's object id "
                      "not a 40-digit SHA-1?)" % (commit, full[:80]))
    return full


def module_constant(tree: ast.Module, name: str):
    """The value of a module-level `NAME = <literal>`; missing / not a literal ⇒ refused (never guess one)."""
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == name and isinstance(node.value, ast.Constant)):
            return node.value.value
    raise Refused("that commit's scv.py has no top-level `%s = <literal>`" % name)


def released(root: str, full: str) -> tuple:
    """(bytes, version). ⭐The checks `scv update` runs before accepting a file are run here first: the size
    ceiling, UTF-8, valid Python."""
    blob = git(root, "cat-file", "blob", full + ":scv.py")
    if len(blob) > scv.SCV_PY_MAX_BYTES:
        raise Refused("that commit's scv.py is %d bytes, over the %d ceiling `scv update` will accept "
                      "(scv.SCV_PY_MAX_BYTES)" % (len(blob), scv.SCV_PY_MAX_BYTES))
    try:
        tree = ast.parse(blob.decode("utf-8"))
    except (SyntaxError, ValueError) as e:
        raise Refused("that commit's scv.py is not valid UTF-8 Python (`scv update` would refuse it): %s: %s"
                      % (type(e).__name__, e))
    base = module_constant(tree, "UPDATE_BASE")
    if base != scv.UPDATE_BASE:
        raise Refused("that commit's scv.py updates from %r, while the working tree's scv.py updates from %r: the "
                      "two are not the same public repository, align them first" % (base, scv.UPDATE_BASE))
    version = module_constant(tree, "VERSION")
    if not isinstance(version, str) or not version:
        raise Refused("that commit's scv.py has a VERSION that is not a non-empty string: %r" % (version,))
    return blob, version


# ━━ Stamp setup.md
def download_url_re() -> "re.Pattern":
    """The download step's address in setup.md: the same one `scv update` builds
    (`UPDATE_BASE/<commit>/scv.py`), with the commit either the placeholder or one already stamped."""
    return re.compile(re.escape(scv.UPDATE_BASE) + "/(?:" + re.escape(COMMIT_HOLE) + "|" + scv.COMMIT_RE.pattern + ")/scv[.]py")


def pinned_sha_re() -> "re.Pattern":
    return re.compile("sha256 `(?:" + re.escape(SHA_HOLE) + "|" + scv.SHA256_RE.pattern + ")`")


FENCE = re.compile(r"^```[a-z]*\n(.*?)^```$", re.M | re.S)     # the same pattern as tests/test_97_docs.py's FENCE (a tool never imports a test)


def download_blocks(text: str, urls) -> int:
    """How many command blocks the download step has (one per shell), with the download address in each block
    exactly once, and the whole file's download addresses exactly those places (review M1: it used to refuse
    only at 0 places ⇒ a block missing the address, or an extra look-alike address in the body, both got
    stamped anyway). Returns the block count.
    The download step = the `## ` section that carries the download address, never anything but exactly one of
    them; read after normalizing line endings (a CRLF checkout still finds the fences)."""
    body = text.replace("\r\n", "\n")
    steps = [s for s in re.split(r"(?m)^## ", body)[1:] if urls.search(s)]
    if len(steps) != 1:
        raise Refused("setup.md has a `%s/<commit>/scv.py` download address in %d section(s), want exactly 1 "
                      "(the download step)" % (scv.UPDATE_BASE, len(steps)))
    blocks = FENCE.findall(steps[0])
    per = [len(urls.findall(b)) for b in blocks]
    total = len(urls.findall(body))
    if not blocks or per != [1] * len(blocks) or total != len(blocks):
        raise Refused("the download step has %d command block(s), with %s download-address occurrence(s) per "
                      "block and %d in the whole file: want exactly 1 per block, and the whole file to have just "
                      "those" % (len(blocks), per, total))
    return len(blocks)


def stamp(text: str, full: str, digest: str) -> tuple:
    """(the stamped text, download-address occurrence count). ⭐The ways this can refuse (all before anything is
    written): the download-address occurrences do not match the download step's command-block count
    (`download_blocks`); an address pointing at scv.py was not built from `UPDATE_BASE` (the public repository was
    renamed and setup.md never caught up — it would silently keep the old address); the pinned hash is not exactly
    one place; a placeholder is still left after stamping."""
    urls = download_url_re()
    n = download_blocks(text, urls)
    stray = [u for u in re.findall(r"https?://[^\s\"'`)]*?/scv[.]py", text) if not urls.fullmatch(u)]
    if stray:
        raise Refused("setup.md has an address pointing at scv.py that was not built from UPDATE_BASE (%s): %s"
                      % (scv.UPDATE_BASE, stray[:3]))
    shas = pinned_sha_re()
    if len(shas.findall(text)) != 1:
        raise Refused("setup.md has the sha256 pin (`sha256 `…``) in %d place(s), want exactly 1" % len(shas.findall(text)))
    out = urls.sub("%s/%s/scv.py" % (scv.UPDATE_BASE, full), text)
    out = shas.sub("sha256 `%s`" % digest, out)
    left = [h for h in (COMMIT_HOLE, SHA_HOLE) if h in out]
    if left:
        raise Refused("placeholder(s) %s are still left after stamping setup.md (written somewhere this script "
                      "cannot recognize)" % left)
    return out, n


def write_bytes_safely(path: str, data: bytes) -> None:
    """Goes through `scv._replace_file` (scv.py's one way to replace a whole file: write a buffer, then
    `os.replace`, with the failure path cleaning up its own buffer) — review M2: this used to write a second,
    equivalent copy of the same logic that, on failure, left `setup.md.release-tmp` behind in the repository root.
    The buffer sits next to setup.md (a cross-drive `os.replace` blows up).
    ⚠️The earlier "read the buffer back before swapping" step was removed: the buffer cannot be read back before
      the swap (it lives inside `_replace_file`), and reading it back after the swap and finding a mismatch would
      mean setup.md has already been touched — the "nothing was touched" sentence would then be a lie;
      `_replace_file` itself does not read it back either."""
    scv._replace_file(Path(path + ".release-tmp"), Path(path), data)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):      # when piped (a test harness, CI) write UTF-8, never the locale encoding (Chinese Windows is gbk)
        if not stream.isatty() and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    ap = argparse.ArgumentParser(prog="release.py", description="compute the sha256 of the committed scv.py, stamp setup.md, print latest")
    ap.add_argument("--commit", required=True, help="the commit to release (7-40 lower-case hex digits, gets expanded to the full one)")
    ap.add_argument("--root", default=ROOT, help="repository root (default: the repository this script lives in)")
    args = ap.parse_args(argv)
    setup_md = os.path.join(args.root, "setup.md")
    try:
        full = resolve(args.root, args.commit)
        blob, version = released(args.root, full)
        digest = hashlib.sha256(blob).hexdigest()
        with open(setup_md, "rb") as f:
            raw = f.read()
        text, n = stamp(raw.decode("utf-8"), full, digest)
        write_bytes_safely(setup_md, text.encode("utf-8"))
    except (Refused, OSError, UnicodeDecodeError, subprocess.SubprocessError) as e:
        print("❌ not released (setup.md untouched): %s" % e, file=sys.stderr)
        return 1
    print("setup.md: download address in %d place(s), sha256 in 1 place ⇒ %s (%s's scv.py, %d bytes)" % (n, setup_md, full, len(blob)), file=sys.stderr)
    print(json.dumps({"latest": {"version": version, "commit": full, "sha256": digest}}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
