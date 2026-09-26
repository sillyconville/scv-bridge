# -*- coding: utf-8 -*-
import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

NL = chr(10)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = os.path.join(ROOT, "tests", "fake_cli.py")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ━━ The gate "tests must never create a directory outside the temp dir" (a to-do hanging since Task 4,
#   carry-forward C14; closed out in Task 12)
# 🔴The shape of the bug: the harness handed production code a placeholder path (`"/x/y"`) ⇒ production code really
#   created it (`F:\x\y` sitting at the disk root — actually hit in Task 4).
# ⭐Built to match the shape: what it blocks is **the act of creating a directory**, ⛔ never one specific
#   placeholder ⇒ this test process's `os.mkdir` (`Path.mkdir`/`os.makedirs`/`tempfile.mkdtemp` all go through it)
#   is swapped for a door: land outside the temp dir ⇒ **refused on the spot** (⛔ never let it really get created)
#   + record it.
# ⚠️Production code may swallow the refusal (before Task 13c, `_ensure_codex_home` did `except OSError`) ⇒ relying
#   on "it blows up on the spot" alone would miss that; the record it leaves is reconciled by
#   tests/test_99_hygiene.py at the end of a full run.
# ⚠️Boundary (what it cannot see): ① it only covers **this process**: a child process the harness starts
#   (`python scv.py start`, `cmd /c mkdir`) is invisible to it; ② calling `nt.mkdir`/`posix.mkdir` directly (which
#   is what importlib does) is invisible to it; ③ anything that grabbed the original function **before the door was
#   installed** (`from os import mkdir` written in a module imported before helpers) is invisible to it — if
#   scv.py really did that, the import-set gate would go red first.
_REAL_MKDIR = os.mkdir
MADE_OUTSIDE = []
MKDIR_CALLS = [0]      # how many times it went through the door (allowed + refused): tells "something really ran" from "nothing did"
# ━━ The reconciliation ledger for "temp dirs the harness is done with must be deleted" (D25: `fresh_home` used to
#   never delete them — one round of fixes found 100+ `.scv-test-*` dirs left behind in %TEMP%)
# ⭐Built to match the shape: what it records is every `.scv-test-` prefixed directory created **directly** under
#   the system temp dir **by this process** (both `fresh_home`'s own and a harness's own
#   `mkdtemp(prefix=".scv-test-…")` count — ⛔ it does not record only the `fresh_home` case), and
#   tests/test_99_hygiene.py checks at the end of a full run that none of them are still there.
# ⚠️Boundary: it only recognizes the `.scv-test-` prefix (a temp dir with a different prefix is invisible to it);
#   it only recognizes this process (one a child process created is invisible to it).
SCV_TEST_PREFIX = ".scv-test-"
SCV_TEST_DIRS = []


class OutsideTempDir(OSError):
    """The door's refusal. 🔴⛔ Not `PermissionError`: on Windows `tempfile.mkdtemp` treats `PermissionError` as
    "this name is taken, try another one", and keeps retrying for as long as the target directory exists
    (`TMP_MAX` is 2147483647 on Windows) ⇒ a refusal that should have answered on the spot instead turned into
    **the whole test process hanging**, with the ledger growing without bound (Task 12 review M-5, measured: 5003
    times in 2 seconds). ⭐Constructing the subclass directly means errno never maps it to `PermissionError`; it is
    still an `OSError`, so the few spots in production code with `except OSError` catch it exactly as before
    (catching it still does not escape the reconciliation at the end)."""


def inside_temp(path):
    root = os.path.normcase(os.path.realpath(tempfile.gettempdir()))
    p = os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def _guarded_mkdir(path, *args, **kwargs):
    MKDIR_CALLS[0] += 1
    if not inside_temp(path):
        MADE_OUTSIDE.append(os.path.abspath(os.fspath(path)))
        raise OutsideTempDir(13, "tests must never create a directory outside the temp dir (the gate is in "
                                 "tests/helpers.py)", os.fspath(path))
    made = _REAL_MKDIR(path, *args, **kwargs)
    full = os.path.abspath(os.fspath(path))
    if (os.path.basename(full).startswith(SCV_TEST_PREFIX)
            and os.path.normcase(os.path.dirname(os.path.realpath(full))) == os.path.normcase(os.path.realpath(tempfile.gettempdir()))):
        SCV_TEST_DIRS.append(full)
    return made


def remove_tree(d):
    """Delete one harness directory. On Windows, a child process that just exited may still be holding a file
    inside for a moment ⇒ try a few times; if it still cannot be deleted, leave it — tests/test_99_hygiene.py's
    reconciliation names it (⛔ never swallow it here: that is most likely an unreaped process).
    ⭐Clear read-only first, then delete: git's object files are read-only, and `rmtree` cannot delete them on
    win32 (test_98's temp repo was named by the reconciliation the very first time)."""
    import stat
    import time

    def writable_then_retry(func, path, _exc):
        with contextlib.suppress(OSError):
            os.chmod(path, stat.S_IWRITE)
            func(path)

    kw = {"onexc": writable_then_retry} if sys.version_info >= (3, 12) else {"onerror": writable_then_retry}
    for _ in range(5):
        with contextlib.suppress(OSError):
            shutil.rmtree(d, **kw)
        if not os.path.exists(d):
            return
        time.sleep(0.2)


def scv_test_dirs_left():
    """The ones still on the ledger (should be empty at the end of a full run)."""
    return [d for d in SCV_TEST_DIRS if os.path.exists(d)]


os.mkdir = _guarded_mkdir

# ━━ The gate "tests must never dial this machine's real default port" (13b review's isolation "for the later
#   legs" to use; closed in fix1)
# 🔴The shape of the bug: a test case running `cmd_setup`/`cmd_doctor`/`cmd_status` used config.json's default port
#   (8765) ⇒ the in-process `our_health` really asked `127.0.0.1:8765/healthz` — on this dev machine 8765 belongs
#   to **another program** (a dashboard), so the test's conclusion followed whatever that program answered, and it
#   even got sent a request.
# ⭐Built to match the shape: what it blocks is **the act of dialing the default port on loopback**
#   (`socket.create_connection`, which urllib/http.client both go through) ⇒ refused on the spot (⛔ never really
#   dials out) + records it, and tests/test_99_hygiene.py reconciles it at the end of a full run; production
#   code's `except OSError` swallowing it does not escape the reconciliation either.
# ⚠️Boundary (what it cannot see): ① it only covers **this process**: a child process the harness starts
#   (`python scv.py status`) is invisible to it — those cases use `free_port()`/`quiet_port()` themselves, and go
#   through `refuse_default_port()` (A12, below) before starting the process (which start paths must go through it
#   is checked by AST in tests/test_99_hygiene.py); ② calling `socket.socket().connect(...)` directly is invisible
#   to it (scv.py has none today); ③ a "real port" other than the default port is invisible to it (it does not
#   know which ports belong to the harness itself).
_REAL_CREATE_CONNECTION = socket.create_connection
DIALED_DEFAULT_PORT = []
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1")


class DefaultPortDial(OSError):
    """The door's refusal (⛔ not `ConnectionRefusedError`: that cannot tell "the door refused it" apart from
    "nobody is really listening there"). Still an `OSError`: production code catches it exactly as before."""


def default_port():
    """scv's config's default port (⛔ never hard-code the number: if the gate's default changes, this follows)."""
    import scv
    return int(scv.DEFAULT_CONFIG["port"])


def _guarded_create_connection(address, *args, **kwargs):
    host, port = address[0], address[1]
    if str(host).lower() in LOOPBACK_NAMES and int(port) == default_port():
        f = sys._getframe(1)                       # record which test case dialed it (names it directly when the reconciliation goes red)
        while f is not None and not isinstance(f.f_locals.get("self"), unittest.TestCase):
            f = f.f_back
        DIALED_DEFAULT_PORT.append("%s:%s ← %s" % (host, port, f.f_locals["self"].id() if f is not None else "?"))
        raise DefaultPortDial(111, "tests must never dial this machine's real default port (the gate is in "
                                   "tests/helpers.py): %s:%s" % (host, port))
    return _REAL_CREATE_CONNECTION(address, *args, **kwargs)


socket.create_connection = _guarded_create_connection


FRESH_KEYS = ("SCV_HOME", "FAKE_LOG", "FAKE_MODE", "CODEX_HOME")


def fresh_home(tag, cleanup):
    """A separate SCV_HOME per test, so none of them cross-contaminate.
    ⭐Also hands out a **temporary** CODEX_HOME along with it (Task 13c): codex uses the player's own home ⇒ doctor
      `stat`s the AGENTS.md there (`codex_carried`). Without it, a test would be looking at
      **whoever is running the tests**' real `~/.codex` — the result would follow this machine, and that is a
      private directory of theirs. A case that wants "CODEX_HOME not set" pops it itself (that is how every
      zero-input control is written).
    ⭐`cleanup` = the caller's own cleanup registration: `setUpModule` passes `unittest.addModuleCleanup`,
      `setUpClass` passes `cls.addClassCleanup`, a test case passes `self.addCleanup` (D25: it used to never
      delete). ⛔ No default value: forgetting to pass it is a `TypeError` (loud), ⛔ never a silent "it just does
      not get deleted anymore". On cleanup, first restore these environment variables to what they were before
      this was created, then delete the directory — ⛔ never leave an SCV_HOME pointing at a deleted directory for
      a later test in the same module.
    🔴The ones that were **unset** before this was created must ⛔ never be popped: once SCV_HOME is unset, scv
      falls back to whoever is running the tests's **real** `~/.scv`; leaving it pointed at a deleted directory
      means whoever uses it next just recreates that directory, and test_99's reconciliation names it."""
    before = {k: os.environ.get(k) for k in FRESH_KEYS}
    d = tempfile.mkdtemp(prefix=SCV_TEST_PREFIX + tag + "-")
    cleanup(_done_with_home, d, before)
    os.environ["SCV_HOME"] = d
    os.environ["FAKE_LOG"] = os.path.join(d, "fake.jsonl")
    os.environ["FAKE_MODE"] = "ok"
    os.environ["CODEX_HOME"] = os.path.join(d, "players-codex-home")
    os.mkdir(os.environ["CODEX_HOME"])
    pin_quiet_port()
    return d


def _done_with_home(d, before):
    for k, v in before.items():
        if v is not None:
            os.environ[k] = v
    remove_tree(d)


def refuse_default_port(home=None):
    """A12 (13b re-review): call this **before** starting a `scv.py` child process that will read config — the
    in-process dialing gate (`_guarded_create_connection` above) cannot see a child process. Checks `home`'s
    (default = the current SCV_HOME) config.json: present, `port` is an int, ⛔ not the default port; if not ⇒
    `AssertionError` on the spot (⛔ never start the process). (If config.json is not there, the child process
    will mint one with the default ⇒ it will dial this machine's real default port.) On a CI machine the default
    port is usually free, so this hole is invisible there."""
    home = home or os.environ["SCV_HOME"]
    p = os.path.join(home, "config.json")
    port = None
    if os.path.exists(p):
        with io.open(p, encoding="utf-8") as f:
            port = json.load(f).get("port")
    if not isinstance(port, int) or port == default_port():
        raise AssertionError("before starting the scv.py child process: %s's port is %r (the default port is %d) "
                             "⇒ that child process would go dial this machine's real default port"
                             % (p, port, default_port()))


_QUIET = []


def quiet_port():
    """The port of a loopback HTTP service the harness starts for itself — "**not the bridge**" (it answers 404 to
    everything); only one is started per process, and it is shut down on exit.
    ⭐Point config's port at it ⇒ `our_health` ends up asking something the harness itself controls, ⛔ never this
      machine's real default port (13b fix1, harness isolation).
    ⚠️⛔ Never substitute "a closed port" for it: on win32, connecting to a closed loopback port takes about a
      second to time out (SYN retries), and once it is closed anyone at all could grab it."""
    if not _QUIET:
        import atexit
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class _NotABridge(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                return

        srv = ThreadingHTTPServer(("127.0.0.1", 0), _NotABridge)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        atexit.register(srv.server_close)          # atexit runs last-registered-first: shutdown before close
        atexit.register(srv.shutdown)
        _QUIET.append(srv)
    return _QUIET[0].server_address[1]


def pin_quiet_port():
    """Pin `port` to `quiet_port()` in the current SCV_HOME's config.json (every other key is left as-is; if the
    file is not there, only this one key is written, and the token is still minted by `load_config()` as usual).
    ⭐A test that opens its own SCV_HOME (test_95: one per case) calls it itself; forgetting to call it turns the
    `DIALED_DEFAULT_PORT` gate red."""
    p = os.path.join(os.environ["SCV_HOME"], "config.json")
    cfg = {}
    if os.path.exists(p):
        with io.open(p, encoding="utf-8") as f:
            cfg = json.load(f)
    cfg["port"] = quiet_port()
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f)


PASTE_SHELLS = ("PowerShell", "cmd", "Git Bash")


def pasted_for(text, shell):
    """`scv.paste_cmd`'s output → the one line `shell` should paste whole. No label (one line covers every shell)
    ⇒ it is that line itself; labeled, but this shell was not given one ⇒ `None`.
    ⚠️This only **reads** the format (a "run this in A/B:" line, the command on the next line), ⛔ it is not the
      judge of correctness: whether it can really be pasted is decided by a real shell (test_90
      `PasteInRealShells`)."""
    lines = text.splitlines()
    labels = [i for i, x in enumerate(lines) if x.startswith("run this in ") and x.endswith(":")]
    if not labels:
        assert len(lines) == 1, text
        return lines[0]
    assert labels == list(range(0, len(lines), 2)), text          # a label line, then a command line, ⛔ never anything else in between
    hits = [lines[i + 1] for i in labels if shell in lines[i][len("run this in "):-1].split("/")]
    assert len(hits) <= 1, text
    return hits[0] if hits else None


def door_lines_missing(text, *words):
    """Whether every line `scv.self_cmd(*words)` hands out appears in `text` **verbatim, on its own line** (the
    precondition for select-the-whole-line-and-paste); returns whichever lines are missing."""
    import scv
    have = text.splitlines()
    return [x for x in scv.self_cmd(*words).splitlines() if x not in have]


def git_bash():
    """Git Bash's `bash.exe` (found by walking up from where `git` sits: `…/Git/cmd/git.exe`,
    `…/Git/mingw64/bin/git.exe` → `…/Git/bin/bash.exe`); returns `None` if it cannot be found. 🔴⛔ Never
    `shutil.which("bash")`: on win32 it may find `System32\\bash.exe` first — that is **WSL**'s launcher."""
    git = shutil.which("git") if os.name == "nt" else None
    for up in (2, 3):
        p = os.path.join(*([os.path.dirname(git)] + [".."] * (up - 1) + ["bin", "bash.exe"])) if git else ""
        if p and os.path.isfile(p):
            return os.path.normpath(p)
    return None


_NO_GIT = object()


def shell_missing(shell, git=_NO_GIT):
    """`shell` (one of the three `run_in_shell` recognizes) cannot run on this machine ⇒ one sentence naming what
    is missing; it can run ⇒ `""`. The caller uses it to skip **just that one case** (13b review M8: ⛔ never hang
    a whole class on one shell). `git` is only for the harness's own control (pins down the exact wording used
    when "Git Bash could not be found")."""
    if os.name != "nt":
        return "these three shells are win32's (the POSIX leg has ⏳ no machine yet, Task 15 will start it separately)"
    if shell == "PowerShell":
        return "" if shutil.which("powershell") else "no PowerShell 5.1 (powershell is not on PATH)"
    if shell == "cmd":
        return "" if shutil.which("cmd") else "no cmd (cmd.exe is not on PATH)"
    return "" if (git_bash() if git is _NO_GIT else git) else "no Git Bash (walking up from git on PATH could not find bin/bash.exe)"


def run_in_shell(shell, line, cwd):
    """Run `line` as one line the user **pasted into** `shell`, returns a `CompletedProcess` (stdout/stderr are
    bytes). Ask `shell_missing` first.
    ⭐Each shell follows the parsing rules of "pasted in": PowerShell 5.1 goes through `-EncodedCommand` (raw
      UTF-16, ⛔ never a second layer of command-line quoting — `-Command <line>` would get rewritten once by
      Python's list2cmdline and once more by PowerShell's own re-parsing); cmd goes through `/d /s /c "<line>"`
      (`/s` only strips the one pair of quotes we added, the inside is left exactly as-is — the same as an
      interactive cmd); Git Bash goes through `-i`, reading one line from stdin (**interactive**: a `!` inside
      double quotes is still taken as history expansion, a non-interactive one cannot see this case — measured on
      this machine).
    ⚠️Only these three, and only on win32 (13b review M8: the bash/zsh branches that used to be left in were never
      reached by any test = dead code, deleted; the POSIX leg will be started separately in Task 15)."""
    import base64
    import scv
    kw = dict(capture_output=True, timeout=120, cwd=cwd, **scv.new_session_kw())
    if shell == "PowerShell":
        enc = base64.b64encode(line.encode("utf-16-le")).decode("ascii")
        return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc], **kw)
    if shell == "cmd":
        return subprocess.run('cmd.exe /d /s /c "' + line + '"', **kw)
    assert shell == "Git Bash", shell
    return subprocess.run([git_bash(), "--norc", "--noprofile", "-i"], input=(line + NL).encode("utf-8"), **kw)


def fake_head(family, cfg=None):
    """Stands in for scv.cli_head: swaps the "official binary" for the fake CLI stub. Same signature as the real
    one."""
    return [sys.executable, FAKE, family]


def read_fake_log(path=None):
    path = path or os.environ["FAKE_LOG"]
    if not os.path.exists(path):
        return []
    with io.open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f.read().splitlines() if x.strip()]


def fake_log_since(mark):
    """The lines newly written to the fake CLI's log after line `mark` (take `len(read_fake_log())`). ⭐Taken as a
    **window**, ⛔ never as `[-1]`: this log is shared by the whole module, and a line from a previous case that
    arrives late would be mistaken by `[-1]` for this case's own (re-review two, M-3, a general sweep).
    When someone else's line gets mixed into the window, the caller's "exactly N of them" goes red — loudly, ⛔
    never a false green."""
    return read_fake_log()[mark:]


def born_alive(born):
    """The **one and only** judge of "this birth id says it is still alive": only a non-empty string counts. `""`
    = does not exist, `None` = could not tell, ⛔ neither one counts — after `proc_start_id` started returning
    three values, treating `!= ""` as "alive" would let `None` slip through too (re-review two, M-8)."""
    return isinstance(born, str) and born != ""


def start_bridge(**cfg_over):
    """Start a bridge in-process: a random port, the fake CLI. Before calling this, fresh_home() must already have
    run and scv.cli_head must already be swapped for fake_head."""
    import scv
    cfg = scv.load_config()
    cfg.update(cfg_over)
    b = scv.Bridge(cfg)
    return b, b.start_local(0), cfg["local_token"]


def http(method, port, path, body=None, token=None, headers=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def sse_data(body_bytes):
    return [ln[6:] for ln in body_bytes.decode("utf-8").splitlines() if ln.startswith("data: ")]
