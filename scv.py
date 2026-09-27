#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scv — a local agent bridge: borrows the agent CLI (Claude Code / Codex) already logged in on this machine.
One file for what players get, joined from the pieces under src/ by tools/build.py; only the standard library,
Python >= 3.9. The three things a reader of this file most wants to check are collected at the top:
  ① what it connects to   ② what paths it writes   ③ what command lines it assembles
It never logs in on its own, never reads any credential file, never modifies the CLI, never sends a request in the
CLI's place — it only starts the official binary, feeds the prompt in over stdin, and hands the answer back."""
from __future__ import annotations

import argparse
import ast
import collections
import contextlib
import ctypes
import functools
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import queue
import re
import secrets
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.2.1"
PROTOCOL = 1
MIN_PY = (3, 9)
LINE_BUDGET = 5550      # ⭐the line count is only a proxy metric: auditability is guaranteed by those AST gates,
#                         never by this number. Why 5550, and why the old 2000/3000/3800/4300/4400/4500/4600/5600/5400/5450/5500 no
#                         longer hold ⇒ tests/test_00_budget.py::Budget::test_line_budget's docstring;
#                         📎 NOTES.md::line-budget-3000
NL = chr(10)

# "Settled idioms" = decisions this file has already argued through (not new rules being introduced here), so
#   whoever writes the N+1th place can see what the 1st place already argued.
# 🔴Every line in the block must carry a pointer, not even an indented continuation line is exempt (the gate scans
#   by whether this line has a pointer); if there is more to say than fits, say it above this block. ⭐The slots
#   here are for what only human memory can hold: once a gate mechanically guarantees something, it should be
#   retired and the why moved into that gate's docstring; ⭐but retired never means gone — every one leaves a line
#   below saying which gate owns it now.
# 📎 NOTES.md::idiom-block
# ━━ Settled idioms
# ⭐Clean up resources with `finally` plus a flag, never `except <a family we recognize>` (scv.py:1992 CodexDriver.__init__)
# ⭐"Did we do this ourselves" uses an explicit flag, never guessed from the exception type (scv.py:1636 _Pipe._read_failed)
# ⭐The parent side has exactly one release point for stdout/stderr; the stdin one is the polite close signal (scv.py:1800 _Pipe._close_pipes)
# ⭐An id from outside never goes into a path, only its hash does (scv.py:2257 SessionManager._workdir)
# ⭐Text from outside is folded to one line at the border where it comes in, never truncated at the border where it goes out (scv.py:307 _one_line)
# ━━ Retired (pointers spell out the fully qualified name, never a line number: nothing in tests/ watches over line numbers, and line numbers going stale is exactly the lesson from the block above)
# ⭐The driver layer's failure paths all go through `_fail()` → tests/test_30_drivers.py::NoSilentFailurePath::test_every_raise_in_the_drivers_goes_through_the_logging_door
# ⭐Disk paths only through `spath()` → tests/test_00_budget.py::Budget::test_state_dir_only_reached_through_spath
# ⭐The append-only log has exactly one place that writes to disk → tests/test_60_joblog.py::OnePlace::test_append_only_writes_go_through_one_place

# ━━ ① What it connects to (network addresses appear only in this one section in the whole file; tests/test_00_budget.py pins it)
LOCAL_HOST = "127.0.0.1"                      # the local API only listens on loopback
LOCAL_URL = "http://127.0.0.1:%d"             # used by a subcommand checking its own /healthz
LOOPBACK_HOSTS = (LOCAL_HOST, "localhost", "::1")   # the Host header accepts only these three (B23's first line, blocks DNS rebinding)
HTTPS = "https://"                            # a remote address must start with this (loopback is the exception, for tests)
REMOTE_PATHS = {"pair": "/bridge/pair", "hello": "/bridge/hello", "stream": "/bridge/stream", "result": "/bridge/result"}
#   the remote host is never hardcoded: it comes only from `scv pair <url>`, stored in config.json's remote_url.
#   Not paired = not one byte goes out to the internet.
UPDATE_BASE = "https://raw.githubusercontent.com/sillyconville/scv-bridge"   # `scv update` fetches {UPDATE_BASE}/{commit}/scv.py
# ━━ ② What paths it writes (all under state_dir(); the one exception: `scv update` replacing itself + the `<it>.new` beside it)
#   ⚠️the codex it starts will write its own things (log store, cache) into the player's own CODEX_HOME — codex
#   writes that, not this file.
SCV_PREV = "scv" + ".prev.py"   # ⚠️split apart on purpose, not decoration: the whole string would trip the hostname check in section ①'s address gate (`scv.prev` looks like a domain)
WRITES = ("config.json", "bridge.pid", "children.json", "children.json.bad", "jobs.log", "jobs.log.1", "bridge.log",
          "bridge.log.1", "latest.json", "work", "tmp", SCV_PREV)
#   The two with `.1` are the previous generation produced by rotation, see `_append_capped()`: as long as rotation
#   succeeds, every name on disk stays < 2×(LOG_CAP_BYTES + LINE_CAP_BYTES). ⭐That second term is not padding: the
#   rotation gate measures the file that already exists, and with no per-line cap a single line could punch
#   straight through it. ⚠️When it cannot be moved (another process has it open) it will keep growing — that gets
#   one loud complaint.
#   `children.json.bad` = the registry file kept as-is (only the most recent one) when the registry is corrupt,
#   moved aside before the new table is written, see `_children_save()`.
#   `scv.prev.py` = the previous version of scv.py kept as-is (only the most recent one) before `scv update`
#   replaces it — copy it back to roll back, see `cmd_update()`.


def user_home() -> Path:
    """The one way this reads the user's home directory (tests/test_00_budget.py::Budget::test_home_dir_touched_once
    pins there being only one place): the state directory's default and codex's default home directory (what an
    ordinary doctor stat "brings along") both come from here."""
    return Path.home()


def state_dir() -> Path:
    return Path(os.environ.get("SCV_HOME") or (user_home() / ".scv"))


def spath(name: str) -> Path:
    """The one exit for a path that touches disk: a name not in WRITES blows up on the spot.
    ⭐Checking only the first segment is not enough: `work/../../oops.txt`'s first segment is also work, yet it
    lands outside state_dir(), and the mkdir(parents=True) below would happily build the outside directory for it too."""
    parts = name.replace(chr(92), "/").split("/")
    if parts[0] not in WRITES or ".." in parts or "" in parts[1:]:
        raise ValueError("refusing to write this path: " + name)
    p = state_dir() / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ━━ ③ What command lines it assembles — see claude_argv() / codex_argv() below, the only two places in the whole
#   file that assemble CLI arguments


class BridgeError(Exception):
    """`klass` = the category (auth_required / quota / timeout / crashed / cancelled / bad_request /
    local_rate_limit / unknown), `raw` = the CLI's original words, not a character changed (B21).
    `logged` = has this one already logged a line to `bridge.log` (`_fail()` sets it). ⭐It is an explicit flag,
    never guess from `klass` who already wrote it to disk: the same `bad_request` can come from `_fail()` (already
    logged) or from `SessionManager` / the HTTP layer's argument validation (not logged) — and it is the latter
    that the layer that finally catches it needs to make up for.
    `on_disk` = the words used for the line that fills in the logging (defaults to `raw`): the spot in the original
    words that quotes the other side's free text is swapped for a sentence that does not quote the original
    (`resolve_model`)."""

    def __init__(self, klass: str, raw: str, family: str = ""):
        super().__init__(klass + ": " + raw)
        self.klass, self.raw, self.family, self.on_disk = klass, raw, family, raw
        self.logged = False


DEFAULT_CONFIG = {"port": 8765, "max_concurrent": 4, "remote_url": "", "remote_token": "",
                  "remote_jobs_per_hour": 600, "allowed_origins": [], "claude_bin": "", "codex_bin": "",
                  "keep_awake_s": 600, "extra_models": {"claude": [], "codex": []}}

_cfg_lock = threading.RLock()   # reentrant: the path that mints a token re-enters save_config() once more


def _tmp_path(name: str) -> Path:
    """The one constructor for a disk write buffer: the name carries a pid so it never steps on the one another
    process is currently writing (writing the same target file concurrently is a straight WinError 32 on Windows)."""
    return spath("tmp/" + name + "." + str(os.getpid()))


def _cfg_tmp() -> Path:
    """The write buffer for config.json."""
    return _tmp_path("config.json")


def _replace_file(tmp: Path, dest: Path, data) -> None:
    """The one way to replace a whole file: write the buffer first, then `os.replace` (`write_text` truncates on
    open, so dying halfway through means the previous copy is gone too).
    🔴The failure path cleans up its own buffer: the old code wrote this three times over with not one `except`
      between them ⇒ every failure left a copy behind (even a half-written one). ⭐`finally` plus a flag (the
      first item in the settled-idioms block). `bytes` is written as bytes; `str` is written as utf-8 text
      (newlines become CRLF on win32).
    ⚠️This file has only two callers: `_atomic_write` (files in the state directory) and `cmd_update` (the
      installed scv.py — its buffer has to sit right beside it: `os.replace` across drives blows up); the release
      tool in the repository, `tools/release.py`, calls it once more (to write setup.md, not on the bridge's own
      run path). Gate: tests/test_90_cli.py::TmpSweep::test_every_buffered_write_goes_through_one_place"""
    done = False
    try:
        if isinstance(data, bytes):
            tmp.write_bytes(data)
        else:
            tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, dest)
        done = True
    finally:
        if not done:
            with contextlib.suppress(OSError):
                tmp.unlink()


def _atomic_write(name: str, data) -> None:
    """Whole-file replace for a file in the state directory: the buffer sits at `tmp/<name>.<pid>` (only the kind
    of leftover that piles up across restarts is swept up by `sweep_tmp()` when the bridge starts)."""
    _replace_file(_tmp_path(name), spath(name), data)


def sweep_tmp() -> list:
    """When the bridge starts, clean out the write buffers in `tmp/` whose process is no longer there (the name's
    tail is the pid that wrote it, see `_tmp_path`).
    Never touches a live one (this process itself is live too): that could be the very copy another `scv` process
    is about to `os.replace`, and deleting it blows up on the spot.
    ⚠️The one whose pid the system recycled to a different live process stays until that process dies (bounded,
    never guessed)."""
    d, gone = spath("tmp"), []
    for p in (d.iterdir() if d.is_dir() else ()):
        pid = p.name.rpartition(".")[2]
        if pid.isdigit() and proc_start_id(int(pid)) == "":
            with contextlib.suppress(OSError):
                p.unlink()
                gone.append(p.name)
    if gone:
        log("cleaned up %d write buffer(s) left behind by dead processes in tmp/: %s" % (len(gone), ", ".join(gone[:8])))
    return gone


def save_config(cfg: dict) -> None:
    p = spath("config.json")
    with _cfg_lock:
        _atomic_write("config.json", json.dumps(cfg, ensure_ascii=False, indent=2))
        with contextlib.suppress(OSError):
            os.chmod(p, 0o600)


def _mint_token(cfg: dict, p: Path) -> dict:
    """Mints local_token. ⭐Only one winner is allowed across processes: the content is written to tmp first, then
    a hard link races to grab config.json; whoever loses just uses the winner's copy (overwriting someone else's
    token is a silent kind of wrong: the other side gets a 401, one restart fixes it, and nobody ever knows it
    happened).
    Never fall back to `open(p, "x")`: that creates config.json at 0 bytes before the content is written, and in
    that instant another process's `exists()` reads true while what it reads back is an empty string. A hard link
    makes "exclusive creation" and "complete content" hold at the same time — the moment p shows up, it is already
    the finished copy."""
    cfg["local_token"] = "scv-" + secrets.token_urlsafe(24)
    tmp = _cfg_tmp()
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.link(tmp, p)
        with contextlib.suppress(OSError):
            os.chmod(p, 0o600)
    except FileExistsError:
        disk = json.loads(p.read_text(encoding="utf-8"))
        if disk.get("local_token"):
            cfg.update(disk)
        else:
            save_config(cfg)   # the copy on disk has no token (hand-edited / an old version) ⇒ fill one in
    except OSError as exc:     # this filesystem cannot make hard links ⇒ complain loudly, then re-raise it, never fall back silently
        # ⭐make the complaint useful (an earlier measurement): exFAT/FAT32-style USB drive formats are the most common case ⇒ say so right away
        log("minting local_token failed, this filesystem cannot make hard links (exFAT/FAT32 are the common case) "
            "⇒ point SCV_HOME at a directory on NTFS/ext4/APFS and run again: " + str(exc))
        raise
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    return cfg


_extra_warned: set = set()


def _extra_once(why: str, msg: str) -> None:
    """A complaint about `extra_models` is logged once per category, never once per occurrence.
    🔴The old claim that "`load_config()` runs once per process" was false: measured, calling it three times in
      the same process complains three times, and Task 12 has every subcommand call `load_config()` once ⇒ one
      bad config still gets rinsed into background noise — exactly the thing this call site exists to avoid.
    ⭐`why` is the category (the shape + which family), never the whole sentence — the whole sentence carries the
      specific name, and swapping in a different bad name makes it look like a "new" message all over again
      (measured: changing the config 40 times = 40 lines). This file's other two siblings (`_rotate_warned` by
      name, `_refuse_warned` by code) are both closed sets — never invent a third kind here."""
    if why not in _extra_warned:
        _extra_warned.add(why)
        log(msg)


def _check_extra_models(cfg: dict) -> None:
    """A name in `extra_models` that never makes it into the catalog complains once at the border where it comes
    in (`catalog()` silently filters those out).
    ⭐The call site is here (`load_config()`), never `catalog()`: that side may be called once per request.
      ⚠️But "only once here" is not free — it is earned by `_extra_once` (see it).
    ⚠️Both sides must use the same one ruler (`MODEL_RE.fullmatch`, see `_model_ok`): if this side says a name is
      fine and that side filters it out, the user ends up holding a log line that says "no problem" while hunting
      for a model that is not in `/v1/models` at all."""
    extra = cfg.get("extra_models")
    if not isinstance(extra, dict):
        if extra:
            _extra_once("kind", "config.json's extra_models is not an object (it is %s) ⇒ the whole thing had no effect"
                        % type(extra).__name__)
        return
    for family, names in extra.items():
        fam = str(family)[:32]
        if family not in ("claude", "codex"):
            _extra_once("family:" + fam, "config.json's extra_models has a family this bridge does not recognize: %s"
                                         " ⇒ that whole family had no effect" % fam)
        elif not isinstance(names, list):
            _extra_once("list:" + fam, "config.json's extra_models[%s] is not a list (it is %s) ⇒ that whole family had no effect"
                        % (fam, type(names).__name__))
        else:
            bad = [str(m)[:32] for m in names if not (isinstance(m, str) and MODEL_RE.fullmatch(m))]
            if bad:
                _extra_once("names:" + fam,
                            "config.json's extra_models[%s] has some names shaped wrong for a model, they did not make it into /v1/models: %s"
                            % (fam, ", ".join(bad)))


def load_config() -> dict:
    p = spath("config.json")
    with _cfg_lock:   # the whole read-modify-write goes inside the lock: otherwise several threads in the same process would each mint their own
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        if p.exists():
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
        if not cfg.get("local_token"):
            cfg = _mint_token(cfg, p)
    _check_extra_models(cfg)   # ⭐outside the lock: it writes to disk (`log`), and `_cfg_lock` should only guard the read-modify-write part
    return cfg

def _one_line(text) -> str:
    """Text from outside (the CLI's stdout/stderr, an exception's `str(e)`) gets folded to one line before it
    crosses into our side.
    ⭐Folding is never truncation: `bridge.log` wants "one line, one entry", and B21 wants "the original words,
      not a character changed" — `splitlines()[0]` trades the second for the first, and the half that gets traded
      away is exactly the reason: node's crash puts the top of the stack in its first two lines, and the line that
      actually tells you how to fix it (`Error: Cannot find module …`) is the third one. Folding to one line lets
      both constraints hold at once.
    ⭐Fold at the border where it comes in: fixing it at the border where it goes out just leaves the next call
      site short again.
    ⚠️Never touch text that was already one line — otherwise "folding" and "mangling the original words" stop
      being distinguishable."""
    s = str(text)
    if NL not in s and chr(13) not in s:
        return s
    return " ⏎ ".join(x.rstrip() for x in s.splitlines() if x.strip())


# C0 / DEL / C1 + U+2028 / U+2029 (line/paragraph separators: Cc class, `str.splitlines()` still breaks a line at
# them ⇒ `_log_tail()` splits one line into two, D24)
CTRL_CHARS = re.compile("[" + chr(0) + "-" + chr(31) + chr(127) + "-" + chr(159) + chr(0x2028) + chr(0x2029) + "]")


def _no_ctrl(text: str) -> str:
    """Control characters become "backslash x two hex digits" (U+2028/U+2029 get "backslash u four hex digits",
    the same notation `JobLog._line` uses). ⭐`log()` — this one door — calls it: the other side can stuff ESC/BEL
    into an HTTP reason phrase, and writing that to disk as-is would let it manipulate the front-end terminal and
    `_log_tail()`'s echo (measured, Task 14 re-review 2). Newlines are already folded away by `_one_line` first
    (that part is never touched). ⭐The set = every line break `splitlines()` recognizes (other than the two
    `_one_line` already folds away): gate
    tests/test_97_docs.py::ControlCharsInBridgeLog::test_every_line_break_splitlines_knows_is_escaped"""
    return CTRL_CHARS.sub(lambda m: chr(92) + ("x%02x" if ord(m.group(0)) < 256 else "u%04x") % ord(m.group(0)), text)


LOG_CAP_BYTES = 4 * 1024 * 1024
LINE_CAP_BYTES = 2048


def _clip(text: str, limit: int = LINE_CAP_BYTES) -> str:
    """The truncation used at the border where things go to disk: over the limit, keep the first `limit` bytes and
    say plainly how much got cut.
    ⚠️This is not the same thing as `_one_line()`'s "folding is never truncation", and the two do not conflict
      either: that one covers the original words at the border where they come in (not a byte dropped, `raw` still
      goes back to the caller unchanged, B21); this one covers the copy that lands on disk — diagnosis wants the
      start of the original words (the error message is almost always at the front), not the model's entire reply.
    ⭐It is the precondition for the claim "every name on disk has an upper bound": the rotation gate measures the
      file that already exists, and puts zero constraint on the line currently being written ⇒ without a per-line
      cap, one line alone could punch straight through it.
    ⭐Cuts by bytes, but never cuts a character in half: it leaves 64 bytes for the marker, and
      `decode(..., "ignore")` drops the half-character left dangling at the tail ⇒ what goes out is always legal
      utf-8, and always <= limit. 📎 NOTES.md::clip-not-fold"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    head = raw[:limit - 64].decode("utf-8", "ignore")
    return head + "…(truncated %d bytes)" % (len(raw) - len(head.encode("utf-8")))


_append_locks_guard = threading.Lock()
_append_locks: dict = {}
_rotate_warned: set = set()   # 🔴keeps this by name, never a single global bool: otherwise once `bridge.log`
                              #   complains that one time, `jobs.log` gets zero complaints of its own (the lock
                              #   was already per-name, the flag just never caught up)


def _append_lock(name: str) -> threading.Lock:
    """One lock per file name — that is the invariant `_append_capped()` actually needs.
    🔴Never "serializing is on the caller": `log()` uses a module-level lock, `JobLog` uses one lock per instance
      ⇒ two `JobLog()`s are two locks hitting the same `jobs.log`, and each caller satisfies that sentence on its
      own. The result is one stale rotation silently overwriting a whole generation — no error, no missing field.
    ⭐Fixed order outer to inner (the caller's lock, then this one), the inner never reaches back for the outer ⇒
      no deadlock.
    ⭐This table has at most `len(WRITES)` rows ⇒ it never grows. 📎 NOTES.md::one-lock-per-name"""
    with _append_locks_guard:
        lock = _append_locks.get(name)
        if lock is None:
            lock = _append_locks[name] = threading.Lock()
        return lock


def _append_capped(name: str, line: str) -> None:
    """The one write door for an append-only log: past the cap, move the whole thing to `<name>.1`, then append
    this line.
    ⭐The cap is written in exactly one place, never copied again in a second spot (`tests/test_60_joblog.py::OnePlace`
      pins there being only this one function that opens in append mode). This family has only two members,
      `bridge.log` / `jobs.log`, and both are driven by network-side input — the rate the other side sends at is
      never ours to control.
    ⭐As long as rotation succeeds, every name on disk stays < 2×(LOG_CAP_BYTES + LINE_CAP_BYTES), through rotation
      (here) plus the per-line cap (`_clip`) holding at the same time. ⚠️"As long as rotation succeeds" is not a
      throwaway phrase: while another process has this file open it will keep growing, and that gets one loud
      complaint.
      2 bytes are left per line for the newline (`open(..., "a")` turns into CRLF on win32) ⇒ this cuts at
      `LINE_CAP_BYTES - 2`.
    🔴This cap protects the byte count, never the information: rotation guarantees disk stays bounded, but a
      hot loop of our own making (say, the remote leg redialing at 45 Hz with zero progress) can squeeze every
      other line out of rotation within seconds ⇒ "disk filled up" gets blocked, "the log got washed out by our
      own noise" does not. ⇒ a caller that will write to disk at high frequency has to throttle or dedupe itself,
      never expect this to do it.
    ⭐Whether rotation went fine or not has to be visible either way: success leaves one line at the very start of
      the new generation (never through `log()` — it is already holding `_log_lock` out there, which would mean
      recursion plus deadlock), failure gets one loud complaint. ⇒ this mechanism is never allowed to be mute.
      📎 NOTES.md::b4-rotation"""
    p, prev = spath(name), spath(name + ".1")   # ⭐the two names clear the gate together: forgetting to register
    with _append_lock(name):                    #   `.1` in WRITES blows up on the very first line, not on the day it hits 4 MiB (by then the scene is far from the cause)
        mark = ""
        try:
            if p.stat().st_size >= LOG_CAP_BYTES:
                os.replace(p, prev)
                mark = "⤶ the previous generation filled up (%d bytes), moved to %s.1, this one starts here" % (LOG_CAP_BYTES, name)
        except FileNotFoundError:
            pass                                # no such file yet ⇒ nothing to rotate, and that is not an error
        except OSError as exc:                  # could not move it: complain once per name, never flood the screen and never complain only about the first one
            if name not in _rotate_warned:
                _rotate_warned.add(name)
                print(name + " cannot be rotated, it will keep growing: " + str(exc), file=sys.stderr, flush=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write((_clip(mark, LINE_CAP_BYTES - 2) + NL if mark else "") + _clip(line, LINE_CAP_BYTES - 2) + NL)


_log_lock = threading.Lock()
_log_warned = False


def log(msg: str) -> None:
    global _log_warned
    # ⭐the fallback gate is built on the one exit: this is the only place in the whole file that writes to
    #   bridge.log ⇒ folding it here means whoever appends another multi-line one later cannot escape it (never
    #   count on every call site remembering to fold — the next call site is the next slot).
    # 🔴one cap has to cover every exit for this line: `_append_capped` only protects the copy on disk, and the
    #   `print(..., file=sys.stderr)` below takes the copy that never passed through `_clip` ⇒ the same cap was
    #   only half enforced (measured: one request came back with 200,000 characters of stderr).
    #   ⇒ cut once, right here, so both exits get the same line.
    #   ⚠️the cut inside `_append_capped` stays as it is: it is that file's one write door, and the cap must never
    #   be left to the caller's memory.
    line = _clip(time.strftime("%Y-%m-%d %H:%M:%S ") + _no_ctrl(_one_line(msg)), LINE_CAP_BYTES - 2)
    with _log_lock:
        try:
            _append_capped("bridge.log", line)
        except OSError as exc:   # wrong needs to be loud: a log silently vanishing is three tables all green. one complaint is enough, never flood the screen
            if not _log_warned:
                _log_warned = True
                print("could not write to bridge.log: " + str(exc), file=sys.stderr, flush=True)
    print(line, file=sys.stderr, flush=True)


def decode(raw: bytes) -> str:
    """On Windows, going through a cmd pipe occasionally spits out bytes in the console code page: try strict
    utf-8 first, fall back to gbk on failure."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", "replace")

_SIGKILL = getattr(signal, "SIGKILL", 9)


CREATE_NO_WINDOW = 0x08000000


def new_session_kw() -> dict:
    """The one keyword door for starting a child process. POSIX: the child gets a group of its own, so killing the tree
    has a group to kill whole.
    win32: no group (there taskkill /T finds the tree by parent and child), but always `CREATE_NO_WINDOW` — 🔴when the
      parent has no visible console (the background bridge that `scv start` starts, a future autostart / pythonw),
      Windows opens a new, visible black window for every console child (cmd, node, codex, taskkill, python): measured
      on a user's desktop, flashing non-stop. ⭐The background bridge itself also gets a hidden console
      (`spawn_detached`); each layer has its own test — with one layer gone the other stays green, so never take
      "nothing flashed end to end" as proof of either layer.
    Gate: tests/test_90_cli.py::NoConsoleWindows::test_every_spawn_site_goes_through_the_no_window_door"""
    return {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}


def kill_pid_tree(pid: int) -> None:
    """Killing only the direct child is not enough: on Windows a CLI often starts through cmd /c; on Linux codex is two
    layers, a node launcher → the native binary.
    🔴On POSIX, first ask "is it the leader of its own group?": if not, `os.getpgid(pid)` returns the bridge's own
      group, one killpg takes the lot down, and the `suppress(OSError)` below makes sure it happens without a sound.
    ⭐The guard has to sit where the shot is fired: `kill_pid_tree` takes a bare pid that anyone can hand it, so the
      registration gate in `child_add` does not cover this path. Relying on callers to remember "never call it with
      an ungrouped pid" is relying on people remembering, which is exactly what this guard replaces."""
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15,
                           **new_session_kw())
        return
    try:
        leader = os.getpgid(pid) == pid
    except OSError:
        leader = False   # can't tell ⇒ treat it as not a leader: better to miss a few descendants than kill our own group
    if leader:
        with contextlib.suppress(OSError):
            os.killpg(pid, _SIGKILL)
    else:
        log("⚠️ pid %d is not the leader of its own group (it was started without new_session_kw()) ⇒ killing only "
            "it, its descendants may be left as orphans; not killing the group, that would take the bridge's own "
            "group down with it" % pid)
    with contextlib.suppress(OSError):
        os.kill(pid, _SIGKILL)


def kill_tree(proc: subprocess.Popen) -> None:
    kill_pid_tree(proc.pid)
    with contextlib.suppress(OSError):
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=15)


# ━━ A process's birth id / memory (win32 goes through ctypes, POSIX goes through ps). ⭐One block: only called by
#   the registry / sweep / snapshot, never reads any other global
# 🔴win32 used to start one powershell per question: even serialized, that came back rc=2 (empty stderr) about
#   1% of the time, and under concurrency there were also .NET access conflicts; "could not tell" and "does not
#   exist" got merged into the same empty string ⇒ roughly 1 in every 100 process starts had a perfectly healthy
#   turn reported as crashed (re-review addendum, item 9, measured). ⇒ switched to `OpenProcess` + `GetProcessTimes`:
#   stdlib, microsecond-scale, starts no child process, and can tell "pid does not exist" (ERROR_INVALID_PARAMETER)
#   apart from "could not tell" (access denied etc). 📎 NOTES.md::birth-cert-ctypes
BIRTH_WIN = "ft:"          # win32 birth id prefix: creation FILETIME. ⚠️old versions wrote .NET Ticks (no prefix) ⇒ see `birth_known`
_WIN_ERROR_INVALID_PARAMETER, _WIN_EXITED, _WIN_STILL_RUNNING = 87, 0, 0x102
_WIN_QUERY, _WIN_SYNC, _WIN_VM_READ = 0x1000, 0x00100000, 0x0010
# ⭐`_k32()` hands out only these six functions, never the whole of kernel32: kernel32 itself also has
#   CreateProcessW / CreateFileW / LoadLibraryW / GetProcAddress (start a process, write to disk, load another
#   DLL) — handing out the whole object would open a door right next to the child-process gate and the disk gate.
#   🔴But this is a door against slipping, never against a deliberate bypass: every ctypes function object holds
#   its DLL in a private `_objects` dict (`_k32().OpenProcess._objects["0"]` is the whole of kernel32, the
#   re-review addendum's third pass measured this working). Adding one more function ⇒ add one field here,
#   tests/test_00_budget.py::Budget::test_native_code_has_one_door goes red; reaching for a private attribute like
#   `_objects` ⇒ tests/test_00_budget.py::Budget::test_no_private_attribute_is_reached_off_self goes red; a
#   string-built reflection has no gate at all.
#   SetThreadExecutionState (0.2.0, KeepAwake): only ever called with ES_SYSTEM_REQUIRED alone — resets the idle
#   timer once, leaves no state behind.
_K32 = collections.namedtuple("_K32", "OpenProcess GetProcessTimes WaitForSingleObject K32GetProcessMemoryInfo "
                                      "CloseHandle SetThreadExecutionState")


class _WinMem(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint32), ("faults", ctypes.c_uint32), ("peak_ws", ctypes.c_size_t),
                ("ws", ctypes.c_size_t), ("a", ctypes.c_size_t), ("b", ctypes.c_size_t), ("c", ctypes.c_size_t),
                ("d", ctypes.c_size_t), ("pagefile", ctypes.c_size_t), ("peak_pagefile", ctypes.c_size_t)]


@functools.lru_cache(maxsize=None)
def _k32():
    """⚠️Every function needs its `argtypes`/`restype` written out: the default `c_int` truncates a 64-bit handle
    (Task 11's resource survey hit `handles: -1` once because of this). Load our own `WinDLL`, never the shared
    `ctypes.windll`: changing a signature on that one changes it for everyone else too.
    🔴The one and only place in the whole file that loads native code, and it returns a `_K32` (six functions),
    never the DLL object itself — the reason is in the `_K32` line above."""
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.OpenProcess.argtypes, dll.OpenProcess.restype = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32], ctypes.c_void_p
    dll.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_uint64)] * 4
    dll.GetProcessTimes.restype = ctypes.c_int
    dll.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    dll.WaitForSingleObject.restype = ctypes.c_uint32
    dll.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WinMem), ctypes.c_uint32]
    dll.K32GetProcessMemoryInfo.restype = ctypes.c_int
    dll.CloseHandle.argtypes, dll.CloseHandle.restype = [ctypes.c_void_p], ctypes.c_int
    dll.SetThreadExecutionState.argtypes, dll.SetThreadExecutionState.restype = [ctypes.c_uint32], ctypes.c_uint32
    return _K32(dll.OpenProcess, dll.GetProcessTimes, dll.WaitForSingleObject, dll.K32GetProcessMemoryInfo,
                dll.CloseHandle, dll.SetThreadExecutionState)


def _win_ask(pid: int, access: int, fn):
    """Open a handle to ask one thing. Returns `fn(k, h)`'s result; `""` = does not exist (including "already
    exited, just someone is still holding the handle"); `None` = could not tell."""
    if not 0 < pid < 2 ** 32:
        return ""
    k = _k32()
    h = k.OpenProcess(access | _WIN_SYNC, 0, pid)
    if not h:
        return "" if ctypes.get_last_error() == _WIN_ERROR_INVALID_PARAMETER else None
    try:
        state = k.WaitForSingleObject(h, 0)
        # ⭐exited but someone is still holding the handle (say, our own Popen we have not waited on yet) ⇒ counts
        #   as "does not exist", same as the old Get-Process behavior
        if state == _WIN_EXITED:
            return ""
        # ⭐any other return value (`WAIT_FAILED` = 0xFFFFFFFF) = this particular question failed ⇒ could not tell,
        #   never does not exist (re-review addendum two, M-7)
        if state != _WIN_STILL_RUNNING:
            return None
        return fn(k, h)
    finally:
        k.CloseHandle(h)


def _win_birth(k, h):
    t = [ctypes.c_uint64() for _ in range(4)]
    if not k.GetProcessTimes(h, *(ctypes.byref(x) for x in t)):
        return None
    return BIRTH_WIN + str(t[0].value)


def _win_rss(k, h):
    m = _WinMem()
    m.cb = ctypes.sizeof(_WinMem)
    return int(m.ws // 1024) if k.K32GetProcessMemoryInfo(h, ctypes.byref(m), m.cb) else None


def _birth_once(pid: int):
    """One attempt. Returns the birth id / `""` (does not exist) / `None` (could not tell)."""
    if os.name == "nt":
        return _win_ask(pid, _WIN_QUERY, _win_birth)
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return decode(out.stdout).strip() if out.returncode == 0 else ""


def proc_start_id(pid: int):
    """A process's "birth id". The system recycles PIDs, and an orphan sweep that only trusts the PID would kill
    someone else's process.
    ⭐Three values: a birth id (non-empty string) / `""` = this pid does not exist / `None` = could not tell
    (retried once, still no).
    🔴The old code had the latter two share one empty string (the docstring itself said "the empty string is
      ambiguous"), leaving callers no choice but to treat it all as "uncertain" ⇒ one stray could-not-tell would
      kill a perfectly healthy, freshly started process and report it as crashed.
    ⭐"Could not tell" gets retried once, "does not exist" never does (that is a settled answer — retrying would
      only waste time).
    ⚠️The POSIX side (`ps -o lstart=`) has not changed a character; only OSError/timeout now read as "could not
      tell" instead of "does not exist"."""
    got = _birth_once(pid)
    if got is None:
        time.sleep(0.05)
        got = _birth_once(pid)
    return got


def birth_known(born: str) -> bool:
    """Does this version recognize the format of the birth id sitting in this registry row. 🔴On win32, old
    versions wrote powershell's .NET Ticks (plain digits, local time); the new one is `ft:<FILETIME>`: the two are
    never comparable ⇒ an old row is treated as "unrecognized" — never killed, and never treated as "matches"
    either (the matching logic here happens not to skip killing it either way, but that is a coincidence, never
    rely on it). The POSIX format has not changed."""
    return bool(born) and (os.name != "nt" or born.startswith(BIRTH_WIN))


def proc_rss_kb(pid: int) -> int | None:
    """⚠️On POSIX, an un-reaped dead child (a zombie) gets 0 back, not None: `ps -o rss=` prints 0 for a zombie and
    still returns 0 as its exit code. This is exactly the "0 gets read downstream as measured, using 0KB" that the
    Rss test itself warns about ⇒ whoever Popens it has to reap it.
    ⭐The same cut on win32 goes through `K32GetProcessMemoryInfo` instead (used to start one powershell per pid,
    ≈0.7s each)."""
    if os.name == "nt":
        got = _win_ask(pid, _WIN_QUERY | _WIN_VM_READ, _win_rss)
        return got if isinstance(got, int) else None
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, timeout=20)
        return int(decode(out.stdout).strip()) if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None

_children_lock = threading.Lock()
PROBE_MAX_SECONDS = 10   # a run_cli that has not registered past this long is not a probe, it is a real live process ⇒ complain


def _row_ok(c) -> bool:
    """The shape of one row in the registry. ⚠️Two traps: `isinstance(True, int)` is true ⇒ `pid: true` would slip
    through; `family` is required too — a consumer reaches straight for `c["family"]`, and leaving it out just
    pushes a KeyError downstream."""
    return (isinstance(c, dict) and isinstance(c.get("pid"), int) and not isinstance(c.get("pid"), bool)
            and isinstance(c.get("born"), str) and isinstance(c.get("family"), str))


_children_warned: set = set()


def children() -> list:
    """⭐Valid JSON is not the same as the right shape: a hand-edited table, or one an old version wrote, can blow
    up the whole startup over one row missing a key.
    Bad rows are skipped and complained about, good rows still get used — on the sweep path, managing fewer is
    better than managing none.
    🔴The same cause complains only once: this function gets called once per `/healthz` hit, and that is the one
      endpoint that needs no token ⇒ once the table is broken, one unauthenticated poll can rotate real
      diagnostics out at the 4 MiB pace (measured: 5 polls = 5 lines). This is the whole bridge's one and only
      path where unauthenticated input drives a write to local disk. Never turn this into a cache: that would let
      the reading go stale."""
    good, why, msg = _children_parse()
    if why:     # wrong needs to be loud: a broken table means the last batch of orphans is never reclaimed, never swallow it silently
        _children_once(why, msg)
    return good


def _children_parse() -> tuple:
    """Registry → `(good rows, how it broke, the original words)`, how-it-broke ∈ ""/read/shape/rows. ⭐`children()`
    and the check before writing the table share this one ruler."""
    p = spath("children.json")
    try:
        rows = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except (OSError, ValueError) as exc:
        return [], "read", "children.json could not be read, the last batch of orphans can only be let go: " + str(exc)
    if not isinstance(rows, list):
        return [], "shape", "children.json is not a list (got %s) ⇒ discarding the whole thing" % type(rows).__name__
    good = [c for c in rows if _row_ok(c)]
    if len(good) != len(rows):
        return good, "rows", "children.json has %d row(s) shaped wrong, skipped (the %d good row(s) are still used)" % (len(rows) - len(good), len(good))
    return good, "", ""


def _children_once(why: str, msg: str) -> None:
    """The same kind of breakage complains only once (`why` is the category, never the whole sentence — the whole
    sentence's length would change with the row count, which would make deduping pointless)."""
    if why not in _children_warned:
        _children_warned.add(why)
        log(msg)


def _children_save(rows: list) -> None:
    """⭐Atomic write: the entire reason this file exists is to survive an abnormal death ⇒ getting killed mid-write
    is exactly the scenario it has to handle.
    A bare write_text truncates the previous copy first; getting killed in that instant means the last batch of
    orphans is never reclaimed again, and not a sound is made about it.
    🔴When the copy on disk is not a fully-good table (unreadable / not a list / has bad rows) ⇒ move it aside to
      `children.json.bad` as-is before writing: the old code only complained once when it could not be read, and
      the next time the table got written (`sweep_orphans` has two production call sites: the next `scv start` /
      `scv stop`) the bad copy got overwritten and the evidence was gone (an earlier review, M1).
      ⭐The move happens on the write side, never the read side: whoever writes already holds `_children_lock`,
      while the readers include the token-free `/healthz` and the facts-only doctor — neither should touch disk,
      and neither should race the writer to rename the same file."""
    _, why, msg = _children_parse()
    if why:
        _children_keep_bad(msg)
    _atomic_write("children.json", json.dumps(rows))


def _children_keep_bad(msg: str) -> None:
    """⭐Keeps only the most recent copy (overwrites the previous .bad): bounded on disk; every move logs one line
    to bridge.log naming the path, so the history of past moves lives there.
    ⚠️When it cannot be moved (another process has it open) ⇒ complain, and write anyway: stopping the registry
    altogether over one bad copy (every CLI started after that can no longer register) would be worse."""
    bad = spath("children.json.bad")     # ⭐the path is written at the front of the message: `log()` truncates from the tail when it is too long
    try:
        os.replace(spath("children.json"), bad)
    except OSError as exc:
        log("⚠️ the registry is broken, tried to move it to %s first to keep as evidence, could not move it (%s) ⇒ it will be overwritten by the new table any moment, its content will not survive. How it broke: %s" % (bad, exc, msg))
        return
    log("⚠️ the registry is broken ⇒ moved the old copy to %s as-is before writing the new table (kept as evidence; breaking again will replace this one). How it broke: %s" % (bad, msg))


def child_add(pid: int, family: str) -> bool:
    """⭐Returns False = this process never made it into the registry, the bridge cannot manage it ⇒ the caller must
    clean up after it itself (kill it and complain), never treat it as nothing happened.
    On POSIX this only accepts a pid that is "the leader of its own process group": sweep kills the whole group
    with killpg, and taking in an ungrouped pid means the next sweep takes the bridge's own group down with it too
    — and the suppress(OSError) inside kill_pid_tree would swallow that without a sound.
    Windows does not check this: taskkill /T finds the tree by parent-child relationship there, this shape does
    not exist on that side.
    Never turn this into raising an exception: the call site sits right after a successful spawn, and raising
    would drop the caller into a state of "the process is up but never registered".
    ⚠️But that is not a promise that it never raises: the `_children_save` call at the very end does not swallow a
      single exception ⇒ callers have to handle both shapes (returning `False` and raising `OSError` both mean "did
      not register"). Never try/except it away here: swallowing that original message would leave the caller
      holding nothing but an unexplained `False`. 📎 NOTES.md::child-add-contract"""
    if os.name != "nt":
        try:
            leader = os.getpgid(pid) == pid
        except OSError as exc:
            log("refusing to register pid %d (%s): could not find its process group: %s" % (pid, family, exc))
            return False
        if not leader:
            log("refusing to register pid %d (%s): it is not the leader of its own group ⇒ new_session_kw() was skipped when it was started. "
                "Registering it would let the next sweep killpg the bridge's own group down with it" % (pid, family))
            return False
    born = proc_start_id(pid)   # ⚠️computed outside the lock (on POSIX it starts a `ps`)
    if not born:
        # ⭐the two kinds of "nothing" are said apart: it is already gone (died right after starting) / could not
        #   tell (retried once). Used to be the same sentence.
        log("refusing to register pid %d (%s): %s ⇒ registering it would never be swept anyway (sweep's test is whether the birth id still matches)"
            % (pid, family, "it is already gone (exited right after starting)" if born == "" else "could not tell its birth id (retried once)"))
        return False
    with _children_lock:
        _children_save(children() + [{"pid": pid, "born": born, "family": family}])
    return True


def child_remove(pid: int) -> None:
    with _children_lock:
        rows = children()
        kept = [c for c in rows if c.get("pid") != pid]
        if len(kept) == len(rows):   # settling an account that was never in the table ⇒ either a double removal, or the registration was refused and nobody noticed
            log("⚠️ child_remove did not find pid %d: it was not in the registry to begin with" % pid)
        _children_save(kept)


SWEEP_STRIKES = 3   # how many sweeps a row whose birth id could not be told gets to stick around (see `sweep_orphans`)


def sweep_orphans() -> list:
    """Sweeps last run's leftovers at startup. Only kills when the birth id still matches. Returns the pids
    actually killed.
    ⚠️Only call this after confirming no other bridge is using this SCV_HOME (`cmd_run` / `cmd_stop` both go
      through `bridge_owner()` first): a live bridge's own CLIs are all in this same table too, birth ids and all
      ⇒ one sweep would kill every one of them.
    🧑‍⚖️A row that could not be told (`None`) is never killed and never forgotten: it used to be silently dropped
      when the table was cleaned. Now it stays in the table to be re-checked next time, complaining once each
      time, and only lets go after `SWEEP_STRIKES` consecutive sweeps (starting the bridge and `scv stop` each
      count as one; the count lives in that row's `strikes`) still could not tell.
      ⚠️The complaint never says "please end it by hand": on a non-admin machine, `None` is mostly the pid having
      been recycled by the system to a process we have no permission to inspect (the re-review addendum's survey:
      all 143 `None`s were access denied) — going after that pid would kill the wrong process.
    ⭐The `_children_save` at the end is bookkeeping, never the action itself (whatever needed killing is already
      killed above) ⇒ if it cannot be written, complain and move on, never blow up the startup (the truly fatal
      "state directory is not writable" gets hit by `load_config` on that same startup path anyway — never let
      bookkeeping beat it to the punch).
    ⚠️Inside the lock is N × (a birth id, plus maybe one `kill_pid_tree`): the former is microsecond-scale on
      win32, the latter is up to 15s worst case ⇒ only call this right before starting the bridge / right after
      stopping it."""
    killed, old, unsure, keep = [], [], [], []
    with _children_lock:
        for c in children():
            born = c.get("born") or ""
            if born and not birth_known(born):
                old.append(c)            # a format an old version wrote (win32's .NET Ticks): never killed, never compared
                continue
            now = proc_start_id(int(c["pid"])) if born else ""
            if born and now == born:
                kill_pid_tree(int(c["pid"]))
                killed.append(int(c["pid"]))
            elif now is None:
                n = c.get("strikes")
                c = dict(c, strikes=(n if isinstance(n, int) and not isinstance(n, bool) else 0) + 1)
                unsure.append(c)
                if c["strikes"] < SWEEP_STRIKES:
                    keep.append(c)
        try:
            _children_save(keep)
        except OSError as exc:
            log("⚠️ swept, but could not write the registry back (%s) ⇒ the next sweep (starting or stopping the bridge) will scan the same batch again (a birth id mismatch is never a false kill)"
                % exc)
    if unsure:
        log("⚠️ %d row(s) in the registry could not have their birth id told (retried) ⇒ not killed: %s. The CLIs the bridge starts run as the same user as the bridge, so normally this can always be told "
            "⇒ most likely this pid has been recycled by the system to a process we have no permission to inspect (never end it by pid); "
            "it stays in the table to be re-checked next sweep (starting or stopping the bridge), and stops being tracked after %d misses in a row" % (len(unsure), "; ".join(
                "pid %s (%s, attempt %d%s)" % (c["pid"], c["family"], c["strikes"],
                                            ", no longer tracked" if c["strikes"] >= SWEEP_STRIKES else "")
                for c in unsure), SWEEP_STRIKES))
    if old:
        # 🔴the table written back above does not have them any more ⇒ this is the last time anyone will remember
        #   them. If the message did not carry the pid, an orphan that might genuinely be left over from the
        #   previous version would be silently forgotten, with nobody left able to go clean it up (re-review
        #   addendum two, M-5).
        # ⚠️"check the process name first" is not a formality: the system recycles pids to other processes,
        #   killing by pid alone can kill the wrong one.
        log("⚠️ children.json has %d row(s) with an old-format birth id (written by the previous version, this one does not recognize it) ⇒ not swept, "
            "already removed from the table: %s. If those pids are still running that same CLI (check the process name first: pids get recycled by the system to other processes), "
            "please end them by hand" % (len(old), "; ".join("pid %s (%s)" % (c["pid"], c["family"]) for c in old)))
    if killed:
        log("⚠️ cleaned up CLI child processes left over from last time: %s" % killed)
    return killed


def run_cli(argv: list, input: bytes | None = None, cwd=None, timeout=None, env=None,
            family: str | None = None) -> subprocess.CompletedProcess:
    """One single-shot child process, with a timeout that actually takes effect: kill the whole tree first, then
    clean up, and the cleanup itself is time-limited (a process outside the tree that still holds the pipe must
    never be allowed to hang us too).
    ⭐Only goes into the registry when `family` is given (paired with the settling of accounts in `finally`).
      Leaving out family = this tree never enters the registry, and if the bridge dies it becomes an orphan nobody
      can find ⇒ only ever use this for a second-scale probe (the `--version` kind).
    🔴"Only for probes" relies on people remembering ⇒ the runtime gate below backs it up: the test is how long
      this path can run — a long timeout is a real live process, and a real live process must be registered.
      📎 NOTES.md::run-cli-probe-gate
    ⭐`env` defaults to `child_env()` (15c review I1: it used to default to `None` = inherit as-is, and leaving out
      `env=` silently went back to before session variables were stripped)."""
    env = child_env() if env is None else env
    if family is None and (timeout is None or timeout > PROBE_MAX_SECONDS):
        # 🔴`timeout is None` means no upper bound at all, and that happens to be this function's own default value
        #   ⇒ the easiest misuse to write is exactly the worst kind.
        # ⚠️the wording has to match what caused it: for `timeout=None`, the usual real fix is "give it a
        #   second-scale timeout" ⇒ saying only "please pass family" would shove a real probe into the registry and
        #   pay for a birth id for nothing (microsecond-scale on win32 now, one `ps` on POSIX)
        log("⚠️ run_cli was not given a family, yet is waiting %s: this tree is not in the registry, and if the bridge dies it becomes an orphan nobody can find. "
            "Anything over %s second(s) is not a probe ⇒ please pass family; if it really is a probe, give it a second-scale timeout instead"
            % ("an unbounded time" if timeout is None else "%s second(s)" % timeout, PROBE_MAX_SECONDS))
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=cwd, env=env, **new_session_kw())
    registered = False
    if family is not None:
        try:
            registered = child_add(proc.pid, family)   # it is necessarily the group leader: new_session_kw() is already applied above
        except OSError as exc:
            # 🔴this line sits after Popen and before try: `_children_save` does not swallow a single exception
            #   (disk full / permissions / SCV_HOME deleted), and letting it pass through means this tree has no
            #   one left to communicate with it or kill_tree it, and the caller cannot even get its pid ⇒ exactly
            #   the shape this file exists to prevent.
            log("⚠️ writing the table failed while registering pid %d (%s): %s" % (proc.pid, family, exc))
        if not registered:
            # never ignore that False (that is child_add's own contract). Chosen here: complain but keep running,
            # never kill it — killing a real live process over one bookkeeping failure is worse than letting it
            # run unregistered.
            log("⚠️ the tree run_cli started never made it into the registry (pid %d, %s): it keeps running, but the bridge cannot find it if it dies" % (proc.pid, family))
    try:
        try:
            out, err = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            kill_tree(proc)
            with contextlib.suppress(subprocess.TimeoutExpired):
                # error = the original text (B21): whatever the CLI had already printed must never be thrown away on the timeout path
                exc.stdout, exc.stderr = proc.communicate(timeout=15)
            raise exc
    finally:
        if registered:   # ⭐settling the account happens in finally: the timeout path is exactly where it is needed most. Only settle the one that actually got registered
            try:
                child_remove(proc.pid)
            except OSError as exc:
                # never let this override a TimeoutExpired already in flight: the error category the caller sees would change completely (B21/B26)
                log("⚠️ failed to settle the account for pid %d, a dead pid will be left in the registry: %s" % (proc.pid, exc))
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)

CLAUDE_MODELS = ("haiku", "sonnet", "opus")
CODEX_MODELS = ("gpt-6-luna", "gpt-5.6-terra", "gpt-6-sol")    # 0.2.0: the service's own subscription seats (maintainer, 2026-09-27); next: find local models by themselves
EFFORTS = ("low", "medium", "high")
MODEL_RE = re.compile("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CODEX_GLOB = "OpenAI/Codex/bin/*/codex.exe"
# The two sentences codex's `login status` is recognized by. ⭐rc != 0 is both how it says "not logged in" and how
# it looks when it is simply broken ⇒ rc alone cannot tell the two apart, so this also checks whether it actually
# said one of these two sentences (`_codex_auth_from_text` and the admission gate share this one ruler).
CODEX_STATUS_MARKS = ("Logged in", "Not logged in")
# Every codex built-in feature unrelated to "answer a piece of text" gets turned off (the list comes from an
# earlier measurement of config.json's codex_disabled_features, plus shell_tool). ⚠️codex says nothing when it
# does not recognize a `-c` ⇒ a name going stale fails silently; `scv doctor` checks this list against `codex
# features list`.
# ⭐The last two are off by default already (Task 13c added them): the player's own config.toml can turn them back
#   on — `memories` injects his memory summary into every session, `multi_agent_v2` adds a whole family of tools
#   for spawning sub-agents; `-c` sits on top of his own layer ⇒ turned off here once more. Each one is
#   load-bearing on its own (measured at zero budget: dropping either one alone puts that same thing straight back
#   into the request sent to the model).
CODEX_OFF = ("shell_tool", "apps", "browser_use", "browser_use_external", "computer_use", "code_mode_host",
             "image_generation", "multi_agent", "plugins", "skill_search", "sleep_tool", "tool_suggest",
             "unified_exec", "view_image", "goals", "hooks", "workspace_dependencies", "in_app_browser",
             "skill_mcp_dependency_install", "tool_call_mcp_elicitation", "memories", "multi_agent_v2")


def _closed(value, allowed, what: str) -> str:
    """The gate for a closed set. ⚠️Truncate the echoed-back original value: some of these `value`s come from the
    network (`effort` is one), and this sentence goes straight into both the response body and stderr — neither of
    which has a cap like `_clip`'s ⇒ without truncating, the other side stuffs in 200,000 characters and gets
    200,000 characters echoed back (measured). ⭐Truncate to just enough for diagnosis: the reader wants to know
    what shape the value they sent had."""
    if value not in allowed:
        raise BridgeError("bad_request", "%s is not in the set this bridge recognizes: %s" % (what, repr(value)[:32]))
    return value


def _model_ok(model) -> str:
    """The closed-set gate for a model name. ⭐What B27 has to pin down is "the remote side cannot slip a single
    character into the command line" ⇒ this guarantee has to live at the one and only assembly point: today the
    only entry is `resolve_model` (which only lets through names `catalog` has already reported), but the moment
    Task 5 opens one more path — a retry, a default-value fallback, doctor self-check — that builds argv directly,
    the closure is gone, and no test at all would go red.
    ⚠️The test is `MODEL_RE`, never `CLAUDE_MODELS`: `extra_models` is a legitimate source.
    ⚠️`fullmatch`, never `match`: `match`'s `$` lets a trailing newline through (`"haiku" + NL` would still pass)."""
    if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
        raise BridgeError("bad_request", "model name is not a shape this bridge recognizes: %r" % (model,))
    return model


def env_missing(name: str) -> bool:
    """The one test for "this environment variable is missing" (shared by `cli_head` looking for the desktop
    codex, and doctor's `CRITICAL_ENV`; an empty string counts as missing, 15c review M6)."""
    return not os.environ.get(name)


def cli_head(family: str, cfg: dict) -> list | None:
    """The executable part of a CLI. Returns None if not found. A configured value is checked for existing before
    use (codex's self-update swaps out its hashed directory)."""
    conf = cfg.get(family + "_bin") or ""
    exe = conf if conf and Path(conf).exists() else shutil.which(family)
    if not exe and family == "codex" and not env_missing("LOCALAPPDATA"):
        hits = sorted(Path(os.environ["LOCALAPPDATA"]).glob(CODEX_GLOB), key=lambda p: p.stat().st_mtime)
        exe = str(hits[-1]) if hits else None
    if not exe:
        return None
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe]            # npm installs these as .cmd, only startable through cmd
    return [exe]


def claude_argv(head: list, model: str, sys_file, settings_file, effort: str | None = None) -> list:
    """The one and only place in the whole file that assembles claude's arguments. The only inputs from outside
    are model/effort, and both come from closed sets (B27).
    ⚠️When started through cmd /c on Windows, the arguments get parsed a second time by cmd ⇒ what protects this is
    the closed set, never escaping."""
    argv = list(head) + ["-p", "--model", _model_ok(model), "--safe-mode",
                         "--system-prompt-file", str(sys_file), "--exclude-dynamic-system-prompt-sections",
                         "--disallowedTools", "*", "--strict-mcp-config", "--disable-slash-commands",
                         "--settings", str(settings_file),
                         "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                         "--include-partial-messages"]
    if effort:
        argv += ["--effort", _closed(effort, EFFORTS, "effort")]
    return argv


def codex_argv(head: list, model: str, effort: str | None) -> list:
    """The one and only place in the whole file that assembles codex's arguments. The model goes through -c
    (app-server does not take --model).
    ⭐codex runs in the player's own CODEX_HOME (no login needed, Task 13c) ⇒ whatever is in his config.toml comes
      along by default. `-c` is the layer sitting on top of that: it overrides scalars entirely — the two lines
      below are exactly how that blocks his standing instructions and the external program he runs after every
      turn, each load-bearing on its own; tables cannot be overridden that way ⇒ the MCP servers he configured, the
      skills he installed (down to the skill list itself) get turned off one by one in thread/start
      (`_codex_user_off`).
    ⚠️app-server does not recognize `--ignore-user-config` / `--ignore-rules` (clap says `unexpected argument` on
      the spot, only `codex exec` has them).
    What this blocks, and what it does not, is read off from behavior case by case: 📎 NOTES.md::codex-user-home"""
    q = chr(34)
    argv = list(head) + ["app-server", "--listen", "stdio://",
                         "-c", "model=" + q + _model_ok(model) + q,
                         "-c", "model_reasoning_effort=" + q + _closed(effort or "low", EFFORTS, "effort") + q,
                         "-c", "web_search=" + q + "disabled" + q,
                         "-c", "developer_instructions=" + q + q,
                         "-c", "notify=[]"]
    for name in CODEX_OFF:
        argv += ["-c", "features." + name + "=false"]
    return argv


# What the agent session that starts the bridge sets for its own child processes to say "which session am I in"
#   (15c, lead item 7-1): exact names = written up in both families' packages / measured what they actually set for
#   a child process; prefix families copy Claude Code's own binding scheme for its eval sandbox. Never widen the
#   prefix to `CLAUDE_CODE_*`: the user's login method and Git Bash path also live under it (B22).
#   `TRACESTATE` and `TRACEPARENT` are the same W3C pair (fix1 item 12).
SESSION_VARS = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENT", "TRACEPARENT", "TRACESTATE", "CLAUDE_CODE_ENTRYPOINT",
                "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_INVOKED_SKILLS", "CODEX_CI", "CODEX_VERSION",
                "CODEX_INTERNAL_ORIGINATOR_OVERRIDE")
SESSION_PREFIXES = ("CLAUDE_CODE_SESSION_", "CLAUDE_CODE_MESSAGING_", "CLAUDE_CODE_HOST_", "CLAUDE_CODE_REMOTE", "CLAUDE_CODE_SDK_",
                    "CLAUDE_CODE_RELAUNCH_", "CLAUDE_CODE_BRIDGE_", "CLAUDE_BG_", "CODEX_THREAD_", "CODEX_SESSION_",
                    "CODEX_SANDBOX", "CODEX_NETWORK_PROXY_")


def session_bound(name: str) -> bool:
    """Does this variable describe "the agent session that started me" (strip it) or the user's own setting (keep
    it). win32 names are case-insensitive ⇒ compare uppercased."""
    return name.upper() in SESSION_VARS or name.upper().startswith(SESSION_PREFIXES)


def child_env() -> dict:
    """The child process's environment = our own environment with the session-bound families (`session_bound`)
    stripped out, everything else as-is (proxy variables, his own CODEX_HOME, his own login method, settings that
    change behavior — doctor lists the latter), never adding a single one. 📎 NOTES.md::child-env-session-vars
    ⭐codex uses the player's own CODEX_HOME (no login needed, Task 13c): anyone who wants scv's codex to use a
      different directory sets this environment variable themselves — that is the escape hatch, and it needs not
      one line of code. ⚠️Returns a copy: changing it must never change this process's own environment.
    ⚠️What this cannot cover: a new exact name that does not look session-related (the `CLAUDE_EFFORT` kind) needs
      a human to add it to the list (doctor lists the names actually passed down = a new one gets seen); when
      codex's network proxy is on, the session-bound part is the value of `HTTP(S)_PROXY`, and the name is generic
      — stripping by name cannot catch that."""
    return {k: v for k, v in os.environ.items() if not session_bound(k)}

PASTE_PLAIN = re.compile("[A-Za-z0-9._/:+" + chr(0x4E00) + "-" + chr(0x9FFF) + "-]+")   # what all three shells treat as a plain character (measured)
SHELL_QUOTES = "'" + chr(0x2018) + chr(0x2019) + chr(0x201A) + chr(0x201B)   # PowerShell treats the last four as single quotes too


def paste_cmd(argv: list) -> str:
    """A list of arguments → a piece of text that can be pasted and just run (handing someone a command that will
    not run is the same as lying). ⭐The one and only ruler in the whole file for quoting/shell rules: the
    self-reference command (`self_cmd`), the CLI's own login command (`login_cmd`), and the `git diff` line printed
    after `scv update` all go through it.
    ⭐One line if every shell agrees; otherwise one line per shell, label on one line and the command on the next
    (they select and paste the whole line), and lines that happen to match get grouped under one label.
    On win32 the three shells are worked out separately (every branch here has been run in that machine's real
    shell, 📎 NOTES.md::paste-cmd):
      - backslashes always become `/`: Git Bash swallows an unquoted backslash (13c review M3), and all three
        shells' own programs accept `/`;
      - entirely made of `PASTE_PLAIN` characters ⇒ no quoting; otherwise PowerShell uses single quotes (a
        quote-like character gets doubled), and prepends `& ` when the leading word is quoted (in 5.1 a quoted
        leading word is just a string, not a command); cmd uses double quotes, and never gets a `%` (cmd expands
        `%X%` even inside double quotes);
      - Git Bash uses double quotes, switching to single quotes for `$` / backtick / `!` / `"` (an interactive
        bash's double quotes still expand `!` as history);
      - starting with `.cmd` / `.bat` (an npm-installed CLI): all three shells hand it to cmd to parse a second
        time ⇒ never give it a `%` for any shell (`%PATH%` would get expanded), and never give Git Bash `& ( ) ^ , ; =`
        (it does not quote those, and cmd breaks the line right there).
    On POSIX, bash/zsh share the same single-quoting rule ⇒ always one line (⏳ written to the rule, never run on
    POSIX)."""
    ws = [str(w) for w in argv]
    if os.name != "nt":
        return " ".join(w if PASTE_PLAIN.fullmatch(w) else shlex.quote(w) for w in ws)
    ws, q = [w.replace(chr(92), "/") for w in ws], chr(34)
    bat, pct = ws[0].lower().endswith((".cmd", ".bat")), any("%" in w for w in ws)
    if bat and pct:
        return "(cannot be written as one pasteable line: %s is a .cmd, cmd would expand the %% in the path a second time)" % ws[0]

    def each(quote):
        return " ".join(w if PASTE_PLAIN.fullmatch(w) else quote(w) for w in ws)

    ps = each(lambda w: "'" + "".join(c * 2 if c in SHELL_QUOTES else c for c in w) + "'")
    lines = {"PowerShell": ps if PASTE_PLAIN.fullmatch(ws[0]) else "& " + ps,
             "cmd": None if pct else each(lambda w: q + w + q),
             "Git Bash": None if bat and set("&()^,;=") & set("".join(ws)) else
             each(lambda w: shlex.quote(w) if set("$`!" + q) & set(w) else q + w + q)}
    by = {}
    for shell, line in lines.items():
        if line is not None:
            by.setdefault(line, []).append(shell)
    if len(by) == 1:
        return next(iter(by))
    return NL.join("run this in " + "/".join(shells) + ":" + NL + line for line, shells in by.items())


LOGIN_ARGS = {"claude": ["auth", "login"], "codex": ["login"]}     # the CLI's own login subcommand (`fix_hint`'s two lines are also built from this)


def login_cmd(family: str, head: list | None) -> str:
    """Gives the user a login command he can paste and just run — the CLI's own login command (what codex logs
    into is the same home the bridge uses: his own). Never "logging in again for scv's sake": it is the same login
    he would normally do to use this CLI day to day. Turning it into something pasteable is `paste_cmd`'s job.
    ⚠️Uses the resolved executable, never the bare name: codex is mostly not on PATH (the desktop build's copy,
      found by globbing); claude having `claude_bin` configured is also usually exactly because it is not on PATH
      (13b review M4).
    ⚠️npm installs it as `.cmd`, and `cli_head` hands back `cmd /c <it>` ⇒ the pasted line drops that shell: all
      three shells can start a `.cmd` directly, and Git Bash would rewrite `/c` as a path, `C:/` (both measured on
      this machine)."""
    parts = list(head or [family])
    return paste_cmd((parts[2:] if parts[:2] == ["cmd", "/c"] else parts) + LOGIN_ARGS[family])


def _claude_auth_from_json(raw: str) -> dict | None:
    """`claude auth status --json` → three fields; returns `None` = there is no JSON object with a `loggedIn` field
    anywhere in this output.
    🔴F-1: never read "could not read it" as "not logged in": claude has no such thing as "an unrecognized
      subcommand" — a version that does not recognize `auth status` treats it as a real prompt and replies with
      something the model said (an earlier measurement) ⇒ that is "could not tell", and it is `auth_status`'s job
      to complain about it.
    ⭐F-6: try parsing from every line-start `{`, never `raw.index("{")` — a stray brace earlier in some noise line
      would otherwise make the rest of the parse fail.
    ⭐Values are folded to one line at the border where they come in (an earlier measurement): `method` gets
      printed in doctor's own line, and also goes into bridge.log."""
    dec = json.JSONDecoder()
    for m in re.finditer("(?m)^[ ]*[{]", raw):
        try:
            d = dec.raw_decode(raw, m.end() - 1)[0]
        except ValueError:
            continue
        if isinstance(d, dict) and "loggedIn" in d:
            return {"logged_in": bool(d.get("loggedIn")), "method": _one_line(str(d.get("authMethod") or "")),
                    "plan": _one_line(str(d.get("subscriptionType") or ""))}
    return None


def _codex_auth_from_text(raw: str, rc: int) -> dict:
    """codex `login status`'s output → three fields. ⭐The status line is picked by content, never by position: it
    prints noise before and after, `lines[-1]` gets whichever line luck hands it ⇒ `method` can quietly turn into
    an unrelated sentence (and it is meant to be printed for the user to read).
    ⚠️When not one line is recognizable, the whole original text is kept (B21) — never throw the truth away just to
    "pick a line"."""
    lines = [x.strip() for x in raw.splitlines() if x.strip()]
    hit = [x for x in lines if any(m in x for m in CODEX_STATUS_MARKS)]
    # ⚠️when nothing is recognizable, the whole original text is kept (B21) — but folded to one line first before
    #   handing it out: `method` gets printed into blocked for the user to read, and also goes into bridge.log,
    #   and a multi-line value is broken at both ends. Folding is never truncation — not a word gets dropped.
    # ⭐it has to say something even when it said nothing at all (an earlier measurement): never let the caller
    #   downstream read this as "(codex said:)" with nothing after it
    said = hit[0] if hit else (_one_line(NL.join(lines)) or "(it said nothing at all, rc=%d)" % rc)
    return {"logged_in": rc == 0, "method": said, "plan": ""}


def _probe_dir() -> Path:
    """The cwd for a zero-budget probe (`--version` / `--help` / login status / feature list): `work/probe`, an
    empty directory.
    🔴Never run this in the caller's own cwd (usually the user's own repository): claude treats an argument it does
    not recognize as a real prompt and actually calls out with it, and also reads the current directory's
    repository context (an earlier measurement). Every call today uses arguments it recognizes ⇒ this is defense
    in depth (Task 12 review M-7)."""
    p = spath("work/probe")
    p.mkdir(exist_ok=True)
    return p


def auth_status(family: str, head: list) -> dict:
    """Zero-budget login status. ⚠️Only says "there are local credentials", never "the credentials still work" —
    the real test is doctor --live's actual call.
    ⭐`probe_error` is always present: `logged_in=False` has two different origins — "we asked, and he is not
      logged in" and could not even ask at all. Reading the latter as the former sends the user off to run a login
      that will not fix anything. Never tell the two apart by "is this key present": an absent key and "asked, no
      error" look identical."""
    try:
        if family == "claude":
            p = run_cli(list(head) + ["auth", "status", "--json"], timeout=PROBE_MAX_SECONDS,
                        env=child_env(), cwd=_probe_dir())
            st = _claude_auth_from_json(decode(p.stdout))
            if st is None:
                said = decode(p.stdout + p.stderr).strip()
                if "{" in said:     # ⚠️this could be a full set of identity info (email/orgId) ⇒ never quote the original, only say the shape (the same idea as B24's whitelist)
                    said = "(there is a brace in the output, but no JSON with a loggedIn field could be parsed out of it; not quoting the original: it might carry an email)"
                said = _clip(_one_line(said) or "(it said nothing at all)", 300)
                why = "claude auth status --json did not return a JSON object with loggedIn (rc=%d): %s" % (p.returncode, said)
                log(why + " ⇒ treated as could-not-tell; ⚠️if this version does not recognize auth status, it may have just sent this as a real prompt")
                return {"logged_in": False, "method": "", "plan": "", "probe_error": why}
        else:
            p = run_cli(list(head) + ["login", "status"], timeout=PROBE_MAX_SECONDS, env=child_env(),
                        cwd=_probe_dir())
            st = _codex_auth_from_text(decode(p.stdout + p.stderr), p.returncode)
        return dict(st, probe_error="")
    except (OSError, subprocess.SubprocessError) as e:
        log("%s's login-status probe never ran (%s): %s" % (family, " ".join(head), _one_line(e)))
        return {"logged_in": False, "method": "the status command never ran: %s" % _one_line(e), "plan": "",
                "probe_error": _one_line(e)}


def _claude_safe_mode_gate(head: list) -> str:
    """The claude family's admission check: does this version of the CLI recognize `--safe-mode`.
    ⭐"Could not tell" and "asked, and the answer is no" are two different things, never merged into one message:
      the probe fails and `"--safe-mode" not in ""` comes out true anyway ⇒ the user gets sent to upgrade a CLI
      that was never broken, upgrades it, and nothing changes.
    ⭐`--help` is read from both channels: whether the CLI puts it on stdout or stderr is its own business ⇒
      reading only one channel means some version that switches pipes triggers the false refusal above.
      📎 NOTES.md::cannot-tell-vs-answered-no"""
    try:
        p = run_cli(list(head) + ["--help"], timeout=PROBE_MAX_SECONDS, cwd=_probe_dir())
    except (OSError, subprocess.SubprocessError) as e:
        log("claude's --help probe never ran (%s): %s" % (" ".join(head), _one_line(e)))
        return "could not tell whether this version of Claude Code recognizes --safe-mode (%s); never report this family until there is an answer" % _one_line(e)
    if p.returncode != 0:
        # 🔴the rc axis: it starts fine but rc != 0 never raises an exception ⇒ this never reaches the except above,
        #   and `--safe-mode` is not in the output either ⇒ it falls through to the same "please upgrade" line below.
        #   "raised an exception" and "rc != 0" both have to count as "could not tell".
        why = _one_line(decode(p.stdout + p.stderr).strip()) or "(it said nothing at all)"
        log("claude's --help probe returned rc=%d (%s): %s" % (p.returncode, " ".join(head), why))
        return ("could not tell whether this version of Claude Code recognizes --safe-mode (--help exited rc=%d, it said: %s); "
                "never report this family until there is an answer" % (p.returncode, why))
    if "--safe-mode" not in decode(p.stdout + p.stderr):
        return ("this version of Claude Code does not recognize --safe-mode (without it, your ~/.claude/CLAUDE.md would go into every single call); "
                "please upgrade Claude Code")
    return ""


def _codex_gate(head: list) -> str:
    """The codex family's admission check: are there local credentials. "Could not tell" and "asked, and not
    logged in" each get their own wording, never impersonating each other.
    ⭐Asks the player's own CODEX_HOME (the same one the real work uses: both sides use `child_env()`) ⇒ not logged
      in just means asking him to log in there once — the same login he would do to use codex day to day, never
      "logging in again for scv's sake" (a hard constraint the maintainer ruled on in Task 13c)."""
    st = auth_status("codex", head)
    if st["probe_error"]:
        return "could not tell whether codex has local credentials (%s); never report this family until there is an answer" % st["probe_error"]
    if not st["logged_in"] and not any(m in st["method"] for m in CODEX_STATUS_MARKS):
        # 🔴the rc axis, codex's case: rc != 0 but it never said one word about login status (a broken config.toml,
        # a broken install...) ⇒ we genuinely do not know whether this is about credentials at all. Never call a
        # crash "you are not logged in" and send him off to run a login that will not fix anything.
        return "could not tell whether codex has local credentials (codex said: %s); never report this family until there is an answer" % st["method"]
    if not st["logged_in"]:
        # ⚠️wording only claims "whether there are credentials": `login status` never says whether they still work (necessary, not sufficient).
        # ⚠️the command goes on its own line: when the path has a space it becomes several per-shell lines, and tacking it onto "please run:" would read as "please run: run this in PowerShell: …"
        return "codex has no local credentials (codex said: %s); please run:%s%s" % (st["method"], NL, login_cmd("codex", head))
    return ""


def detect(cfg: dict) -> dict:
    """⭐Every probe always uses `timeout=PROBE_MAX_SECONDS` (a real CLI measures 0.05~0.43s, 20x headroom): a
    probe over budget would trip the run_cli gate every single time ⇒ a gate our own noise turns into wallpaper is
    worse than no gate.
    ⭐A probe's env = the real one used for actual work (both are `child_env()`): never probe one CODEX_HOME and
      actually run another, or the probe's conclusion is not about the same thing.
    ⭐Every failure path logs one line to bridge.log and returns to the caller: could-not-tell says "could not
      tell" plus the OS's original words, never explain away a probe that never ran at all as "your CLI is too old"
      or "you are not logged in"."""
    found = {}
    for family in ("claude", "codex"):
        head = cli_head(family, cfg)
        if not head:
            # 🔴the third shape of a family vanishing entirely: it `continue`s further up than this ⇒ the "log one
            #   line if blocked is non-empty" line below never sees it, while the user sees the exact same thing
            #   (this family is missing from /v1/models). Never let it walk away silently.
            log("refusing to report the %s family: this machine has no executable for it "
                "(not on PATH, and config.json's %s_bin does not point to one either)" % (family, family))
            continue
        try:
            # ⚠️this truncation is deliberate, never a missed `_one_line`: that rule covers the original error text,
            #   this one wants exactly one field (a version number should only ever be one line). ⇒ the test is
            #   "do I want the original words, or one field".
            ver = decode(run_cli(list(head) + ["--version"], timeout=PROBE_MAX_SECONDS,
                                 cwd=_probe_dir()).stdout).strip().splitlines()[0]
        except (OSError, IndexError, subprocess.SubprocessError) as e:
            ver = "the version command never ran: %s" % _one_line(e)
            log("%s's --version probe never ran (%s): %s" % (family, " ".join(head), _one_line(e)))
        blocked = _claude_safe_mode_gate(head) if family == "claude" else _codex_gate(head)
        if blocked:
            # ⭐the decision path needs to be loud too: this family disappears from /v1/models entirely, and all
            #   the user will ever see is "my claude is gone". The log lines above cover "the probe failed", this
            #   one covers "and so this family is not reported" — two different things, and the second one is what
            #   he actually needs to look up.
            #   ⭐built at the `detect` layer, never on those individual `return`s: one more refusal reason later
            #   gets logged automatically too.
            #   ⚠️fold, never truncate: `blocked` can be multiple lines (codex's case carries two pasteable command
            #     lines, a crashed CLI's original text can run several lines), pouring it in as-is would wreck "one
            #     line, one entry", and `splitlines()[0]` would throw away the reason itself (node's crash puts the
            #     stack head in the first two lines, the actually useful sentence comes after). Both constraints
            #     have to hold at once.
            log("refusing to report the %s family: %s" % (family, _one_line(blocked)))
        found[family] = {"head": head, "version": ver, "blocked": blocked}
    return found


CLI_VER_RE = re.compile("[0-9]+(?:[.][0-9]+)+")


def _cli_version(raw) -> str:
    """A CLI's self-reported line → just the part shaped like a version (`1.2.3`), empty if none is recognizable.
    🔴B24's whitelist covers key names, never values: the failure path above stored the OS's original words in this
      same `version` field, and `subprocess.TimeoutExpired`'s original text carries the full path to the
      executable — that is, the username, the directory layout — and this field gets sent to the network as-is by
      `hello_payload()`; `--version` timing out while `--help` does not is already enough to trigger it.
    ⭐The test is a shape whitelist, never a blacklist that strips out paths: the next new way to leak will not
      send a memo first.
    ⚠️At least two segments (`1.2`): with one segment, any run of digits sitting in a path (`C:/Users/alice/…`)
      would get reported as a version.
    ⚠️Never from inside a path: a directory can carry a version (nvm's `…/v20.11.0/claude.cmd`, a hosted Python's
      `…/Python/3.12.10/x64/python.exe`), and the failure text quotes the executable — the first CI run reported
      the runner's Python version as claude's. So a word (split at whitespace, quotes, brackets, commas) holding
      `/` or a backslash is skipped whole."""
    text = str(raw or "")
    for ch in "'" + '"' + "[](),;":
        text = text.replace(ch, " ")
    for word in text.split():
        m = None if ("/" in word or chr(92) in word) else CLI_VER_RE.search(word)
        if m:
            return m.group(0)
    return ""


def catalog(cfg: dict, found: dict) -> list:
    table = {"claude": CLAUDE_MODELS, "codex": CODEX_MODELS}
    out = []
    for family, info in found.items():
        if info.get("blocked"):
            continue
        # ⭐the catalog side and the assembly side must use the same one ruler (`fullmatch`, see `_model_ok`):
        #   letting it through here and rejecting it there means the user sees a model in /v1/models that
        #   bad_requests the moment it is picked.
        extra = [m for m in ((cfg.get("extra_models") or {}).get(family) or []) if isinstance(m, str) and MODEL_RE.fullmatch(m)]
        for m in list(table[family]) + extra:
            if family + "/" + m not in out:
                out.append(family + "/" + m)
    return out


def shown_model(model_id, cat: list) -> str:
    """The model name used when writing to disk (`jobs.log`'s `model`, `bridge.log`): only ever one this bridge has
    actually reported in /v1/models (a closed set); anything else gets written as a sentence that never quotes the
    original. A model name comes from the network: the one that did not clear the gate can be any text the other
    side chose to stuff in (Task 14 review I1, measured: a 500-character model name left 221 bytes of it sitting in
    jobs.log). ⚠️The error body handed back to the caller still carries the original name — that one goes back to
    the person who sent it."""
    if isinstance(model_id, str) and model_id in cat:
        return model_id
    return "(an unreported model name, %d bytes)" % len(str(model_id).encode("utf-8", "replace"))


def resolve_model(model_id, cat: list) -> tuple:
    if not isinstance(model_id, str) or model_id not in cat:
        e = BridgeError("bad_request", "this bridge has never reported this model: %r (see /v1/models)" % (model_id,))
        e.on_disk = "this bridge has never reported this model: %s (see /v1/models)" % shown_model(model_id, cat)
        raise e
    family, _, model = model_id.partition("/")
    return family, model

# 🔴The three single words are matched by "no letter glued on either side", never a bare substring and never
#   regex's own word boundary — both extremes have already bitten once: a bare substring judged `presets`/
#   `quotation` as quota (429 plus fix_hint lying); the `chr(92)+"b"` kind of word boundary counts `_` and CJK as
#   part of the word too ⇒ a real quota code like `insufficient_quota` falls through to unknown instead.
#   ⭐Both sides block `[A-Za-z]`, never `[a-z]`: the latter's correctness rests entirely on the `.lower()` line
#     below — move that lower away and `overloadedFn`'s capital `F` stops being blocked, the false positive comes
#     right back, and every test is still green at that moment.
#   ⚠️Still cannot fix the `(reading 'resets')` case (text alone cannot tell the two apart) ⇒
#     tests/test_40_errors.py keeps one expectedFailure pinned on it. 📎 NOTES.md::quota-word-boundary
QUOTA_PAT = ("usage limit", "session limit", "hit your", "rate limit", "exceeded your")
QUOTA_WORD = re.compile("(?<![A-Za-z])(?:overloaded|resets|quota)(?![A-Za-z])")
AUTH_PAT = ("not logged in", "please run /login", "401 unauthorized", "missing bearer", "login required",
            "token expired", "token has expired", "invalid api key", "authentication_error", "codex login")
HTTP_STATUS = {"auth_required": 401, "quota": 429, "local_rate_limit": 429, "bad_request": 400, "cancelled": 499,
               "timeout": 504, "crashed": 502, "unknown": 502}
# 🔴`local_rate_limit` = refused by the bridge's own rate limit, and its window is by the hour ⇒ giving only
#   "retryable" and never "how long to wait" means the client just retries immediately, gets 429 again, and keeps
#   retrying until it has burned a whole hour ⇒ this one class must have a fix_hint. 📎 NOTES.md::local-rate-limit
RETRYABLE = ("timeout", "crashed", "quota", "local_rate_limit")


def classify(raw: str, default: str = "unknown") -> str:
    """The class is an added field; the original text is never touched. Quota is checked before login: quota
    wording sometimes carries the word "login" in it, never the other way round.
    ⚠️`default` comes from the caller and is returned as-is ⇒ the caller is responsible for keeping it inside
      `HTTP_STATUS` (otherwise the downstream `HTTP_STATUS[klass]` is a purely self-inflicted KeyError). The wiring
      layer either blocks it at the door (fail loud) or goes uniformly through `.get(klass, 502)`.
    ⚠️Returning `unknown` means "we could not read what this CLI said", and it is the only signal that the pattern
      list has gone stale ⇒ the log line has to carry both `klass` and the original words, or it can go stale
      forever with nobody ever finding out."""
    low = (raw or "").lower()
    if any(p in low for p in QUOTA_PAT) or QUOTA_WORD.search(low):
        return "quota"
    if any(p in low for p in AUTH_PAT):
        return "auth_required"
    return default


def fix_hint(klass: str, family: str) -> str:
    if klass == "auth_required":
        # ⭐codex uses the player's own CODEX_HOME (Task 13c) ⇒ a bare `codex login` logs into exactly the home the
        #   bridge uses, so this sentence is true. (At Task 13 the home was scv's own; this line used to point at
        #   scv's own codex-login subcommand, which went away with 13c.)
        #   ⚠️It does not know where the executable is (mostly not on PATH): only doctor can work that one out, the
        #   one with a path (`login_cmd`).
        return " ".join([family] + LOGIN_ARGS[family]) if family in LOGIN_ARGS else ""
    if klass == "quota":
        return "wait for the quota to reset (see the original words for how long), or switch to a model from the other family on this bridge"
    if klass == "local_rate_limit":
        # ⭐This sentence is the companion to `retryable=True`, never decoration: without spelling out whose rate
        #   limit it is and how long the window is, the client will just retry immediately and hit it again. Never
        #   copy the number (`remote_jobs_per_hour`'s default) into this sentence — copy it and it starts lying the
        #   moment the user changes the config, and nobody can tell that it is lying.
        # ⚠️There are two different sources, and how long to wait differs between them ⇒ this sentence must never
        #   name only one (it used to mention only the hourly one, while "in-flight is at the cap" is something to
        #   wait a few seconds for). ⭐Whoever reads this is the dispatcher's implementer or operator (production
        #   hits of this class on the local leg are 0) ⇒ give the knob's name directly, and say that retrying needs
        #   a new id (the same id only ever gets one sequence starting from 0).
        return ("this is the bridge's own rate limit, not an upstream quota; which one you hit is in the original "
                "words — the hourly job count is config.json's remote_jobs_per_hour (window by the hour), the "
                "in-flight count is max_concurrent (wait for a few to finish) ⇒ do not retry immediately, retry "
                "with a new job_id")
    return ""


def error_body(e: BridgeError) -> dict:
    return {"error": {"message": e.raw, "type": e.klass, "code": e.klass, "retryable": e.klass in RETRYABLE,
                      "fix_hint": fix_hint(e.klass, e.family), "family": e.family}}


def error_payload(e: BridgeError, leg: str) -> dict:
    """The one place that computes the error response body, and along the way fills in "the copy nobody logged
    yet" — ⭐both legs share this one function.
    🔴Why the accounting sits at this layer: the `bad_request`/`cancelled` that `SessionManager` raises is normal
      control flow, and it deliberately does not go through `_fail()` (writing a line for every bad request that
      comes in would turn bridge.log into background noise) ⇒ that layer and this one each assume the other is
      logging it — the textbook way to build a silent failure. This line is that missing entry.
    ⭐The test is the explicit flag `e.logged`, never guessing from `klass` who already logged it.
    ⚠️🔴Never let either leg call `error_body()` on its own: on the leg that skips this, every `bad_request` the
      other side gets is zero lines on disk — while all three tables are green. Gate:
      tests/test_70_local_api.py::OneDoor::test_error_body_is_computed_in_exactly_one_place
    📎 NOTES.md::api-is-the-last-catcher"""
    if not e.logged:
        e.logged = True
        log("%s: this request failed (%s): %s" % (leg, e.klass, e.on_disk))
    return error_body(e)

CLAUDE_STALL_S = 30     # claude has a thinking_delta heartbeat at most every ≤2s while it thinks ⇒ 30s of nothing means it is truly stuck
CODEX_STALL_S = 90      # codex has zero events while it thinks; too tight a leash misjudges a normal slow answer as stuck
FIRST_TOKEN_S = 45      # the longest wait for "there is a heartbeat but no content yet"; the dispatcher can override it via opts.first_token_timeout
CLOSE_GRACE_S = 10
# The handshake (initialize/thread/start) codex does on its own has its own cap. ⚠️This number is not one of the
# plan's four windows: it is a pure fallback Task 7 set on its own — on a real CLI those two calls are
# millisecond-scale, and 60s is only there so a stuck one has some ceiling, never a threshold that was measured.
CODEX_HANDSHAKE_S = 60


def _rpc_error(err) -> str:
    """JSON-RPC's `error` → the one sentence of original words handed to a human. There is only one implementation
    allowed anywhere both families share this same reasoning.
    ⚠️`err` is not guaranteed to be an object: `.get` called directly on a bare string raises an `AttributeError`,
      which is not a `BridgeError`, logs no line at all, and picks exactly the "something has already gone wrong"
      path to blow up in — wiping out the CLI's original words entirely.
    ⭐Takes `message`, never `str(the whole object)`: the latter shows the user a Python dict's repr
      (`{'code': -32600, 'message': '…'}`), which shortchanges B21's "error = the original text" right there.
      ⚠️It does not affect classification (`classify` matches by substring, and the original words are still
      inside the repr) ⇒ all three tables stay green, and only a human reading it would notice.
    ⭐`code` must never get dropped: it is the machine-readable half of this error, folded into the same line.
    ⚠️When even `message` is missing, the whole object is kept as-is (B21) — never make up a sentence to fill it
      in."""
    if not isinstance(err, dict):
        return str(err)
    msg = err.get("message")
    if not msg:
        return str(err)
    code = err.get("code")
    return "%s (code=%s)" % (msg, code) if code is not None else str(msg)


USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
              "cached_input_tokens", "reasoning_output_tokens")
CODEX_USAGE_KEYS = {"inputTokens": "input_tokens", "cachedInputTokens": "cached_input_tokens",
                    "outputTokens": "output_tokens", "reasoningOutputTokens": "reasoning_output_tokens"}
USAGE_MAX = 10 ** 12          # one trillion tokens: if this ever shows up it is the CLI or our own arithmetic gone wrong, never a real conversation turn
_usage_warned: set = set()


def _usage_once(family: str, key: str) -> bool:
    """Only complain once per family, per field. ⚠️Complaining one line per turn would let a new field that is
    "present on every turn" flood `bridge.log`, which we just capped — the very thing that cap exists to prevent."""
    if (family, key) in _usage_warned:
        return False
    _usage_warned.add((family, key))
    return True


def usage_numbers(raw, family: str) -> dict:
    """The usage a CLI gives us ⇒ keep only the numeric values on the whitelist. ⭐The cleaning point sits at the
    driver layer, never at the point that writes to disk.
    ⭐Why: the same `usage` also goes into the wiring layer's response, and into the remote leg ⇒ cleaning it only
      at the point that writes to disk still leaves those two paths sending whatever the CLI stuffed in straight
      out onto the network, as-is. Clean untrusted data at the layer where it comes in the door, and every
      downstream consumer benefits.
    ⭐The whitelist is allowed to exist in exactly one place: both families share this one function, never write it
      twice, once per family — fixing one copy would leave the other one missed.
    ⭐What gets pinned down is the value, not just the key: even on the codex side, where we build the key
      ourselves by name, the value is still copied over as-is.
    ⚠️A dropped field must be loud, never silent (the day the CLI adds a useful field, dropping it silently means
      nobody ever finds out), but only complain once per field — complaining one line per turn would let a new
      field that is "present on every turn" flood `bridge.log`, which we just capped.
    ⚠️When complaining, report only the field name, never the value, and truncate the name too: a hostile call can
      turn the key name itself into a prompt.
    🔴Has to swallow any type at all: `(raw or {})` only blocks falsy values ⇒ a truthy non-dict raises an
      `AttributeError` on the spot, and the call site sits outside `_fail` with no try around it ⇒ a conversation
      turn that would otherwise have succeeded turns into an exception with no klass that never gets logged.
      ⚠️The structural gate only scans for `raise`, and is blind by design to an exception nobody wrapped.
    🔴A numeric type is not the same thing as a number that can be written to disk: `10**4000` is an int, and one
      field alone can push the whole line past the single-line cap ⇒ magnitude has to be blocked too (`USAGE_MAX`;
      it blocks `inf`/`nan` along with it). 📎 NOTES.md::usage-whitelist"""
    if not isinstance(raw, dict):
        if _usage_once(family, "<" + type(raw).__name__ + ">"):
            log("%s's usage is not a dict (it is %s) ⇒ the whole thing was thrown away" % (family, type(raw).__name__))
        return {}
    out, dropped = {}, []
    for k, v in raw.items():
        if k in USAGE_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) < USAGE_MAX:
            out[k] = v
        else:
            dropped.append(str(k)[:32])
    fresh = sorted(x for x in dropped if _usage_once(family, x))
    if fresh:
        log("%s's usage had field(s) that did not make it into the log (not on the whitelist, or the value is not "
            "a number that can be written to disk): %s" % (family, ", ".join(fresh)))
    return out


def _fail(klass: str, raw: str, family: str = "") -> BridgeError:
    """The one exit for the driver layer's failure paths: write one line to bridge.log, then hand the exception
    back to the caller (the global rule that a failure must be loud).
    ⭐The write sits here, never in `error_body()`: that one runs on every single request, and it would flood the
      screen even for a healthy bridge.
    ⭐The line must carry both `klass` and the original words together: `classify()` returning `unknown` is the
      only signal that the pattern list has gone stale.
    ⚠️Folded, never truncated: the CLI's original words are often several lines, and this folds them explicitly
      once — the rule is "outside text gets folded at the border where it comes in". `raw` itself must reach the
      caller without a single character changed (B21).
    🔴But the copy that lands on disk does have a cap (`_clip`), and that does not conflict with the rule above:
      on the claude side `raw` can be the entire model answer ⇒ one failed turn could write the whole thing into
      `bridge.log`, exactly the thing B30 ("never log the content") exists to prevent. B21 is satisfied by the copy
      handed back to the caller; keeping just the head of the copy on disk is enough for diagnosis.
      📎 NOTES.md::b21-vs-b30"""
    log("%s: this turn failed (%s): %s" % (family or "?", klass, _one_line(raw)))
    err = BridgeError(klass, raw, family)
    err.logged = True   # ⭐this one's account has already been recorded: the layer above (HTTP/remote leg) must never write a second line
    return err


class _Pipe:
    """One long-lived CLI child process plus a read loop. The two gates are two ways to die: stall = not a single
    byte comes out; first_token = there is activity but no content."""

    def __init__(self, argv: list, cwd, env: dict, family: str):
        self.family = family
        self._out_thread = self._err_thread = None   # ⭐set before Popen: the failure path below also has to go through `_close_pipes()`
        # 🔴"Did we close this ourselves" must be an explicit flag, never guessed from the exception type: the
        #   whole `OSError` family comes free (an fd closed by someone else, EMFILE, an I/O error the driver layer
        #   raises are all it) ⇒ guessing by type would let a read failure that is the bridge's own get swallowed
        #   into a quiet sentinel, walk down the EOF path, kill a perfectly healthy child, and then build a
        #   confident, specific, wrong conclusion out of a stale stderr tail — with no real reason recorded
        #   anywhere.
        # ⭐Uses `threading.Event`, never a plain bool: `set()`/`is_set()` go through a lock internally ⇒ that
        #   gives a real happens-before edge (a plain bool not blowing up today is because the implementation
        #   happens not to reorder it, never because the semantics forbid it).
        # 📎 NOTES.md::pipe-closing-flag
        self._closing = threading.Event()
        self._read_error = None
        try:
            self.popen = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          cwd=str(cwd), env=env, **new_session_kw())
        except (OSError, ValueError) as e:
            # ⚠️`ValueError` is not redundant: `Popen` raises it when argv/cwd/env carries a NUL, and catching only
            #   OSError would let those cases escape bare. The outside input that used to reach it (config.json's
            #   codex_home) went away with Task 13c ⇒ today this is defense in depth (⏳ not audited for other
            #   paths).
            # ⛔`TypeError` (a non-string slipped into argv) is deliberately not caught: that is our own bug, and
            #   wrapping it into a retryable crashed would be passing off a bug as a retryable failure.
            #   📎 NOTES.md::popen-raises
            raise _fail("crashed", "could not start the %s CLI: %s" % (family, e), family)
        self.pid = self.popen.pid
        self._listed = False
        # 🔴From this line until `born = True`, the process is already running while this `_Pipe` has not been
        #   handed to anyone yet ⇒ any exception escaping this stretch means a long-lived CLI nobody recognizes and
        #   nobody can kill. ⭐The cleanup goes through `finally` plus a flag, never `except <a family we
        #   recognize>` (the shape is "any exception leaks it").
        #   Gate: tests/test_30_drivers.py::SpawnWindow::test_any_exception_after_spawn_buries_the_child
        #   📎 NOTES.md::spawn-window
        born = False
        try:
            try:
                listed, why = child_add(self.pid, family), "could not get into the registry"
            except OSError as e:
                # 🔴Letting this escape bare means the process is already running, was never registered, and
                #   nobody can kill it — the caller does not even get the pid.
                listed, why = False, "writing the registry table blew up (%s)" % e
            if not listed:
                # ⛔Do not copy `run_cli`'s "loud but keep going": that one takes a second-scale probe, this is a
                #   long-lived process that can live for a whole session.
                # ⚠️There is nothing to unregister on this path (`_listed` is still False ⇒ the `_unlist()` call
                #   below is a no-op) — never write a second cleanup path just for this case.
                raise _fail("crashed", "started the %s CLI (pid %d) but %s, and it has already been killed: "
                                       "leaving it running would make it an untraceable orphan the moment the "
                                       "bridge dies" % (family, self.pid, why), family)
            self._listed = True
            self._q: queue.Queue = queue.Queue()
            self._err: collections.deque = collections.deque(maxlen=40)
            # ⭐The read side (`stderr_tail`) and the write side (the pump thread) touch the same deque ⇒ the same
            #   lock. The reason for this lock is not "it would blow up today" (today's `"".join` runs entirely in
            #   C): it is ① under a no-GIL build it is no longer atomic; ② the next person to write
            #   `[x for x in self._err]` turns the race real immediately — and what it throws is an unclassified
            #   exception, one that picks exactly the "something has already gone wrong" error path to blow up in,
            #   wiping out the CLI's original words. 📎 NOTES.md::err-deque-race
            self._err_lock = threading.Lock()
            self._out_thread = threading.Thread(target=self._pump_out, daemon=True)
            self._out_thread.start()
            self._err_thread = threading.Thread(target=self._pump_err, daemon=True)
            self._err_thread.start()
            born = True
        finally:
            if not born:
                self.kill()   # ⭐one exit: kill the tree, unregister (only what was really registered), and the three parent-side fds

    def _read_failed(self, which: str, exc: Exception) -> None:
        """A pump thread's read blew up. This only counts as normal when we are the ones closing the pipe right
        now, otherwise it must be loud.
        ⭐The test is the explicit flag `self._closing`, never the exception type (the reason is in the block in
        `__init__`).
        ⚠️What gets written to disk is this one's own sentence, never `self._read_error`: the latter only records
          the first one ⇒ if both pumps blow up one after another, the two lines on disk would read identically,
          both saying stdout — a lying error message.
        ⚠️Carries the moment it happened: `_read_error` never gets cleared ⇒ without a timestamp it would be read
          as "just happened"."""
        if self._closing.is_set():
            return
        said = "reading %s's %s blew up (at %s): %r" % (self.family, which, time.strftime("%H:%M:%S"), exc)
        if self._read_error is None:   # ⭐keep only the first one: the rest are mostly its knock-on effects
            self._read_error = said
        log("⚠️ " + said + " (not the CLI exiting)")

    def _pump_out(self) -> None:
        try:
            for raw in iter(self.popen.stdout.readline, b""):
                self._q.put(raw)
        except (OSError, ValueError) as exc:
            # ⭐What this branch catches can only be a real read failure ⇒ hand it all to `_read_failed` to
            #   complain about; the `_closing` flag is left in only as a fallback for the POSIX path that has
            #   never been measured. 📎 NOTES.md::pipe-closing-flag
            self._read_failed("stdout", exc)
        finally:
            self._q.put(None)   # ⭐unconditional: this is the exact sentinel `read_until` recognizes

    def _pump_err(self) -> None:          # ⛔not draining stderr fills the pipe and stalls the CLI
        try:
            for raw in iter(self.popen.stderr.readline, b""):
                self._err_add(decode(raw))
        except (OSError, ValueError) as exc:
            # ⚠️This one is more silent than the stdout branch: a blown-up stderr pump just leaves the tail a bit
            #   shorter, and that tail is `classify()`'s only input ⇒ the sentence "stderr is empty" can itself be
            #   a lie.
            # ⭐Once this throws, this pump is gone and the tail is frozen forever (nothing anywhere restarts it),
            #   and when it dies between two turns the session is not dropped for it — that is a trade-off, not an
            #   oversight. 📎 NOTES.md::stderr-pump-death
            self._read_failed("stderr", exc)

    def _err_add(self, text: str) -> None:
        """Push one line into the stderr tail. ⭐The one and only write door: tests go through it too, never
        append bare anywhere else (a bare append skips the lock, and that race only ever shows up on the
        "something has already gone wrong" path, the hardest place to debug)."""
        with self._err_lock:
            self._err.append(text)

    def alive(self) -> bool:
        return self.popen.poll() is None

    def stderr_tail(self) -> str:
        with self._err_lock:
            return "".join(self._err)[-500:].strip()

    def mark_ok(self) -> None:
        """The waterline (OUT-1, the fix has already been ruled on): every "unambiguous success" clears the
        stderr tail to zero ⇒ whatever `classify` gets fed on the EOF path afterward is only what was added after
        this moment. 🔴Without clearing it, a piece of stale noise (measured on a real codex: a
        `codex_core::tools::router` ERROR line, `failed to connect to websocket … wss://…`; with no credentials it
        is `401 Unauthorized`) would get an unrelated crash afterward judged as `auth_required`, sending the user
        off to log in again. ⭐Changed for both families the same way (call sites: before both families' `turn`
        returns successfully, and when codex's handshake succeeds).
        ⚠️Bounded, not eliminated: a line written before success but only read by the pump after this moment is
        still left sitting there (the pump is asynchronous). 📎 NOTES.md::real-cli-stderr"""
        with self._err_lock:
            self._err.clear()

    def send(self, obj: dict) -> None:
        try:
            self.popen.stdin.write((json.dumps(obj, ensure_ascii=False) + NL).encode("utf-8"))
            self.popen.stdin.flush()
        except (OSError, ValueError) as e:
            raise _fail("crashed", "%s CLI's stdin is already closed: %s | %s" % (self.family, e, self.stderr_tail()),
                        self.family)

    def read_until(self, pred, timeout: float, stall, first_token, has_content, cancel) -> dict:
        deadline = time.time() + timeout
        quiet_until = time.time() + stall if stall else None
        content_at = time.time() + first_token if first_token else None
        while True:
            now = time.time()
            if cancel is not None and cancel.is_set():
                self.kill()
                raise _fail("cancelled", "the caller cancelled this turn", self.family)
            content_until = None if (content_at is None or has_content()) else content_at
            # 🔴Both gates are each held up by two mechanisms, never drop either one: ① checking the gate at the
            #   top of the loop holds up "the CLI arrives faster than the gate" (the queue always has something in
            #   it ⇒ `queue.Empty` never fires); ② computing `caps` from the gates holds up "the CLI goes
            #   completely silent" (when not one byte comes out, the top of the loop never gets a second turn).
            #   📎 NOTES.md::two-gates-two-mechanisms
            due = [(t, msg) for t, msg in ((deadline, "the whole turn timed out after %gs" % timeout),
                                           (quiet_until, "not a single byte out for %gs (stuck)" % (stall or 0)),
                                           (content_until, "no first byte of content out for %gs" % (first_token or 0)))
                   if t is not None and now >= t - 0.01]
            if due:
                self.kill()
                raise _fail("timeout", self.family + ": " + min(due)[1], self.family)
            caps = [t for t in (deadline, quiet_until, content_until) if t is not None]
            if cancel is not None:
                caps.append(now + 0.25)
            try:
                raw = self._q.get(timeout=max(min(caps) - now, 0))
            except queue.Empty:
                continue
            if raw is None:
                self._err_thread.join(timeout=1)     # let the last few stderr lines land in the tail, or the error report cannot show the cause of death
                # 🔴Whoever Popens it reaps it: unregistering without reaping leaves it a zombie on POSIX (this
                #   case is invisible on win32). Also covers another case at the same time: stdout broke but it
                #   is still alive ⇒ kill the tree. 📎 NOTES.md::reap-your-own
                self.kill()
                if self._read_error:
                    # 🔴A read blowing up is not the CLI finishing talking ⇒ this case must never go through
                    #   `classify(tail)`: that would take a tail that was never read in full and make up a
                    #   confident upstream class for what is the bridge's own I/O failure. The class is pinned to
                    #   `crashed`, never classified; the original words carry both our read error and the stderr
                    #   tail together (B21).
                    #   ⚠️This has a cost (a real quota that coincides with a read failure loses its 429/401
                    #   upstream). 📎 NOTES.md::read-error-not-eof
                    raise _fail("crashed", "%s | %s" % (self._read_error, self.stderr_tail()), self.family)
                tail = self.stderr_tail() or "the CLI process exited (exit=%s), stderr is empty" % self.popen.poll()
                raise _fail(classify(tail, "crashed"), tail, self.family)   # ⭐unregistering has already been done by kill() and only once
            if stall:
                # ⭐`max`, never a direct assignment: a direct assignment would let the `api_retry` case below be
                #   wiped out by whatever frame immediately follows it (the shape "announce a backoff → emit one
                #   frame → go quiet for a long time" would still be misjudged; a 529 backoff of dozens of seconds
                #   plus the 30s window is a real window).
                quiet_until = max(quiet_until, time.time() + stall)
            s = decode(raw).strip()
            if not s.startswith("{"):
                continue
            try:
                ev = json.loads(s)
            except ValueError:
                continue
            if stall and ev.get("type") == "system" and ev.get("subtype") == "api_retry":
                quiet_until = time.time() + stall + (ev.get("retry_delay_ms") or 0) / 1000   # the CLI is backing off on its own, never kill this as stuck
            if pred(ev):
                return ev

    def _unlist(self) -> None:
        """Unregister, and only once.
        🔴`child_remove` complains "this pid was never in the registry" for a pid not in the table, and that line
          is the only signal that a registration attempt was rejected and nobody noticed ⇒ unregistering a second
          time would turn it into routine noise. ⛔Never rewrite this as "leave `_listed` set and unregister again
          next time": that would make that signal fire every time.
        🔴`child_remove`'s own `_children_save` call swallows no exception at all ⇒ this has to catch it here:
          what is usually flying on this path is some other exception (all three failure paths call `kill()` first
          and then `raise _fail(...)`) ⇒ letting it escape would wipe out the CLI's original words and class
          entirely (B21/B26 voided on the spot). 📎 NOTES.md::unlist-once"""
        if self._listed:
            self._listed = False
            try:
                # ⭐Catches only `OSError`, and that is not a missed case, it has been checked (that is the only
                #   family this path can raise).
                # ⚠️Never harden this by copying `ClaudeDriver.__init__`'s case (which specifically catches
                #   `ValueError`): that path's input comes from outside, this one is something we built ourselves.
                #   Which exceptions to catch is decided by where that path's input comes from, never by copying
                #   the neighbor. 📎 NOTES.md::unlist-once
                child_remove(self.pid)
            except OSError as exc:
                log("⚠️ unregistering pid %d failed, a dead pid will be left behind in the registry: %s" % (self.pid, exc))

    def _close_pipes(self) -> None:
        """The one and only release point for stdout/stderr (the failure path in `__init__`, `close()` and
        `kill()` all go through it).
        ⚠️stdin has a separate one: the `stdin.close()` call inside `close()` — that is the polite-close signal
          (letting the CLI read EOF and exit on its own), not a release, which is why it comes before this. Do not
          let a later reader get tripped up looking for "the one implementation" by the letter.
        ⭐Relies on GC, never on closing: that reference chain breaks the moment any exception's traceback or one
          debugging reference holds onto it. And each session holds 3 parent-side fds ⇒ on Linux the first thing
          hit is `RLIMIT_NOFILE` (default 1024), never memory — it is the hardest ceiling among the resources
          counted per session_id.
        ⚠️Waits for the pump threads to exit first (`join`), and if they never do, it simply does not close.
        📎 NOTES.md::close-pipes-blocks"""
        # 🔴The flag is raised after the join, never before: raising it before would swallow a real read failure
        #   that happens after the flag is raised but during the join. The test is simple: during the join we have
        #   not closed a single fd yet ⇒ whatever blows up in that stretch must be a real error, and must be loud.
        for t in (self._out_thread, self._err_thread):
            # ⚠️`t is not threading.current_thread()` guards against "calling `close()`/`kill()` from inside the
            #   pump thread itself". No path does that today ⇒ no test goes red while it is absent; never read it
            #   as "some path does call it this way".
            if t is not None and t is not threading.current_thread():
                t.join(timeout=1)
        self._closing.set()   # ⭐from this moment on, and only from this moment on, a pump read blowing up counts as "we closed it ourselves"
        # 🔴🔴Never close a pipe that still has a pump stuck on it: when the read end is stuck in `readline()` and
        #   `close()` is called, closing is exactly the call that gets blocked ⇒ that would stuff an unbounded
        #   block into what is supposed to be `kill()`'s bounded path. A skipped fd can only wait for GC ⇒ this
        #   must be loud.
        #   📎 NOTES.md::close-pipes-blocks
        for name, pipe, pump in (("stdin", self.popen.stdin, None),
                                 ("stdout", self.popen.stdout, self._out_thread),
                                 ("stderr", self.popen.stderr, self._err_thread)):
            if pipe is None:
                continue
            if pump is not None and pump.is_alive():
                # ⚠️Names which one: when both pumps are stuck at once this writes two lines, and without naming
                #   the pipe the two lines would read identically — leaving no way to tell from disk which fd was
                #   skipped (the same problem as the `_read_failed` case).
                log("⚠️ %s's %s pipe did not get closed: the pump is still stuck in readline (most likely a "
                    "grandchild process is holding the write end), closing it now would block us ⇒ this fd can "
                    "only be left for GC" % (self.family, name))
                continue
            with contextlib.suppress(OSError, ValueError):
                pipe.close()

    def close(self, grace: float = CLOSE_GRACE_S) -> str:
        with contextlib.suppress(OSError, ValueError):
            self.popen.stdin.close()
        try:
            self.popen.wait(timeout=grace)
            self._unlist()
            self._close_pipes()
            return "exited"
        except subprocess.TimeoutExpired:
            self.kill()
            return "killed"

    def kill(self) -> None:
        """⚠️The worst-case blocking time differs between the two platforms ⇒ the time the cancel/timeout gate
        takes to get back to the caller has to account for it:
          · win32: ≈32s = the `subprocess.run(timeout=15)` inside `taskkill /T /F`
            plus `proc.wait(timeout=15)` (two stretches, in series, both inside `kill_tree`), plus two
            `join(timeout=1)` calls;
          · POSIX: ≈17s = `os.killpg` is instant ⇒ only `proc.wait(timeout=15)` plus the two `join` calls are
            left.
        🔴The 0.26s on the happy path is not the ceiling (the previous version wrote it as "17s" using exactly
          that number); 32/17 is pieced together from "the parts that were measured plus the two 15s pulled out
          of reading the code", never read this as the whole thing having been measured. Underreporting it is
          just setting a trap for whoever picks this up next. 📎 NOTES.md::kill-time-budget"""
        kill_tree(self.popen)
        self._unlist()
        self._close_pipes()

class ClaudeDriver:
    family = "claude"

    def __init__(self, cfg: dict, model: str, effort, system: str, workdir: Path):
        head = cli_head("claude", cfg)
        if not head:
            raise _fail("crashed", "no claude CLI was found on this machine", "claude")
        sys_file, iso = workdir / "system.txt", workdir / "isolation.json"
        try:
            sys_file.write_text(system, encoding="utf-8")
            iso.write_text(json.dumps({"disableAllHooks": True}), encoding="utf-8")   # through a file, not the command line: cmd would rewrite the curly braces
        except (OSError, ValueError) as e:
            # ⭐Which classes `except` should catch is decided by measuring, never by writing down "what I assumed
            #   it throws" (guess wrong and that branch is dead code, while a test that mocks the same assumption
            #   stays green); catch narrowly: `claude_argv`'s `bad_request` must never be swallowed into crashed.
            # ⚠️The structural gate only scans for `raise`, and is blind by design to an exception nobody wrapped
            #   ⇒ this case can only be caught by a behavioral test.
            # 📎 NOTES.md::except-what-you-measured
            raise _fail("crashed", "could not write claude's working directory %s (system.txt/isolation.json): %s"
                                   % (workdir, e), "claude")
        self.stall = CLAUDE_STALL_S
        self.pipe = _Pipe(claude_argv(head, model, sys_file, iso, effort), workdir, child_env(), "claude")
        self.pid = self.pipe.pid

    def turn(self, text: str, on_delta, timeout: float, first_token, cancel) -> dict:
        self.pipe.send({"type": "user", "message": {"role": "user", "content": text}})
        sent, box = time.time(), {"ttfc": None}

        def pred(ev: dict) -> bool:
            if ev.get("type") == "stream_event":
                inner = ev.get("event") or {}
                d = inner.get("delta") or {}
                # ⛔The first byte only recognizes text_delta: thinking_delta's thinking is always an empty string, that stream is just a heartbeat.
                if inner.get("type") == "content_block_delta" and d.get("type") == "text_delta" and d.get("text"):
                    if box["ttfc"] is None:
                        box["ttfc"] = round(time.time() - sent, 2)
                    on_delta(d["text"])
            return ev.get("type") == "result"

        ev = self.pipe.read_until(pred, timeout, self.stall, first_token, lambda: box["ttfc"] is not None, cancel)
        out = (ev.get("result") or "").strip()
        if ev.get("is_error") or not out:
            raw = out or self.pipe.stderr_tail() or "claude returned an empty result"
            raise _fail(classify(raw, "unknown"), raw, "claude")
        self.pipe.mark_ok()
        return {"text": out, "usage": usage_numbers(ev.get("usage"), "claude"), "ttfc": box["ttfc"]}

    def alive(self) -> bool:
        return self.pipe.alive()

    def close(self) -> str:
        return self.pipe.close()

    def kill(self) -> None:
        self.pipe.kill()


# The second half of the sentence (the next step) for when the handshake "could not ask" / "did not recognize the
# answer" ⇒ this session does not start. ⭐The class is `unknown`, never `crashed`: it reproduces every single
# time, and `crashed ∈ RETRYABLE` would be asking the caller to retry forever (the same settled idiom as `_serve`;
# 13c review M1).
CODEX_REFUSED = " ⇒ this session does not start: upgrade Codex CLI and try again first; if it still happens, paste this line to scv's maintainer"


def _codex_user_off(conf_ev: dict, skills_ev: dict) -> dict:
    """The two receipts from `config/read`/`skills/list` → thread/start's `config`: the player's configured MCP
    servers, the skills he has installed — turn them off one by one.
    🔴Both can only be turned off by name/path (measured): MCP — `-c mcp_servers={}` does not block it (`-c` does a
      deep merge against the table, so his servers still start and their tools still get attached to the model's
      hand; swapping the whole table for a different type makes codex refuse to start at all), which is why B11
      turns tools off one at a time; skills — writing `$skill-name` in the prompt pulls that whole SKILL.md in
      whole (`features.mentions_v2=false` does not block it). The listing flag (`include_instructions`) also has
      to be written into this same table: once thread/start carries a `skills` table,
      `-c skills.include_instructions=false` no longer takes effect (measured at zero budget, Fix 1).
      ⇒ names/paths can only be asked of codex itself first (the same process, the same cwd, project level
      included too).
    ⚠️The receipt is his entire effective config and a description of every skill (an MCP server's env can well
      have a password in it): take only the key names/paths, never keep a single value, never log it, never put it
      in the original words (the original words go into bridge.log, and into the API error body handed to the
      dispatcher).
    ⚠️The wrong shape ⇒ this session must never start: never pretend it was turned off when it could not be
      (measured: with nothing configured at all it comes back as an empty table/empty list, never a missing key).
    🔴`mcp_servers` is not in the public schema (`ConfigReadResponse.Config` only declares 25 keys; it rides along
      via `additionalProperties`): the day codex stops returning it, this refuses ⇒ the whole codex family cannot
      open a session at all (a real request fails carrying the original words; `doctor --live` can see it, an
      ordinary doctor never starts a session and cannot see it). `mcpServerStatus/list`, which does list names in
      the public schema, would start his servers running (measured) ⇒ never use it. 📎 NOTES.md::codex-user-home"""
    res = conf_ev.get("result")
    conf = res.get("config") if isinstance(res, dict) else None
    servers = conf.get("mcp_servers") if isinstance(conf, dict) else None
    if not isinstance(servers, dict):
        raise _fail("unknown", "codex's config/read receipt has no mcp_servers table, the player's configured MCP "
                    "servers cannot be turned off (B11)" + CODEX_REFUSED, "codex")
    res = skills_ev.get("result")
    groups = res.get("data") if isinstance(res, dict) else None
    # ⭐Only one cwd was asked about ⇒ there must be exactly one group: `data: []` must never be read as "not a
    #   single skill is installed" (13c review M2)
    skills = groups[0].get("skills") if isinstance(groups, list) and len(groups) == 1 and isinstance(groups[0], dict) else None
    if not (isinstance(skills, list) and all(isinstance(s, dict) and isinstance(s.get("path"), str) for s in skills)):
        raise _fail("unknown", "codex's skills/list receipt is not a shape recognized by this version of scv (needs "
                    "exactly one group, one skills table in the group, each entry carrying a path), the player's "
                    "installed skills cannot be turned off" + CODEX_REFUSED, "codex")
    return {"mcp_servers": {str(name): {"enabled": False} for name in servers},
            "skills": {"include_instructions": False,
                       "config": [{"path": p, "enabled": False} for p in sorted({s["path"] for s in skills})]}}


class CodexDriver:
    """`codex app-server` long-lived: initialize → initialized → config/read plus skills/list → thread/start (the
    prompt goes through baseInstructions; the player's configured MCP servers and installed skills get turned off
    one by one here) → repeated turn/start.
    ⚠️Three places where this is unlike the claude family, never assume its shape carries over:
      ① the prompt is never written to a file (it goes through `baseInstructions`) ⇒ this family has no "could not
      write the working directory" failure path;
      ② `__init__` has a whole extra stretch of handshake ⇒ a whole extra family of failures (the `finally` below
      exists for exactly that);
      ③ `effort` is a field on every single `turn/start` (it is not in `ThreadStartParams`) ⇒ it has to be carried
      on every turn."""

    family = "codex"

    def __init__(self, cfg: dict, model: str, effort, system: str, workdir: Path):
        head = cli_head("codex", cfg)
        if not head:
            raise _fail("crashed", "no codex CLI was found on this machine", "codex")
        self.effort, self.stall, self._rid, self.tid = effort or "low", CODEX_STALL_S, 0, ""
        self.pipe = _Pipe(codex_argv(head, model, self.effort), workdir, child_env(), "codex")
        self.pid = self.pipe.pid
        ready = False
        try:
            self._call("initialize", {"clientInfo": {"name": "scv", "version": VERSION}}, CODEX_HANDSHAKE_S)
            self.pipe.send({"method": "initialized", "params": {}})
            off = _codex_user_off(
                self._call("config/read", {"cwd": str(workdir)}, CODEX_HANDSHAKE_S, "could not ask which MCP servers the player has configured"),
                self._call("skills/list", {"cwds": [str(workdir)]}, CODEX_HANDSHAKE_S, "could not ask which skills the player has installed"))
            ev = self._call("thread/start", {"baseInstructions": system, "sandbox": "read-only", "ephemeral": True,
                                             "model": model, "cwd": str(workdir), "config": off}, CODEX_HANDSHAKE_S)
            res = ev.get("result") if isinstance(ev.get("result"), dict) else {}
            self.tid = (res.get("thread") or {}).get("id") or ""
            if not self.tid:
                # The protocol drifted (a version change, a renamed field) ⇒ this must never be treated as success:
                # every turn with no tid would fail, and the error at that point would point at turn, with nobody
                # able to trace the real cause back to this handshake step.
                # 🔴Never put the receipt itself into the original words (13c review I1): the receipt carries
                #   instructionSources (the player's paths, which is his user name), and the original words go into
                #   bridge.log and the API error body; the test is "the protocol's key names can be said, not one
                #   of the player's values" ⇒ only list the top-level keys.
                raise _fail("unknown", "codex thread/start did not give a threadId (the receipt's top-level keys: %s)"
                            % ", ".join(sorted(str(k) for k in res)[:20]) + CODEX_REFUSED, "codex")
            # The instruction files this session will really load (a public field): `doctor --live` reports "what
            #   it will carry along" from this (`live_check`; an ordinary doctor never asks it by going through the
            #   handshake — thread/start would make codex warm up and connect to the inference endpoint, see
            #   `codex_carried`)
            self.sources = res.get("instructionSources")
            self.pipe.mark_ok()
            ready = True
        finally:
            # 🔴`__init__` blowing up halfway ⇒ the caller does not even get `self` ⇒ nobody can manage this
            #   long-lived process anymore.
            # ⭐Uses `finally`, never `except BridgeError`: that would only cover the family we recognize ourselves,
            #   and this stretch has other people's exceptions too (KeyboardInterrupt, a json serialization error)
            #   — missing one case is leaking one long-lived process.
            # ⭐Never `raise` here: `_call`/`read_until` have each already written their own line, wrapping it once
            #   more would write two lines for the same failure.
            if not ready:
                self.pipe.kill()

    def _call(self, method: str, params: dict, timeout: float, why: str = "") -> dict:
        """One JSON-RPC round trip. ⚠️Only the whole-call `timeout` gate applies: the handshake period has no
        "heartbeat" and no "content", so the stall/first-token gates mean nothing here (pass None = do not check),
        never copy the line from turn.
        `why`: the first half of "so what" for when this question gets no good answer (an old codex version does
        not have this method ⇒ it reproduces every time, default class `unknown`, followed by `CODEX_REFUSED`; 13c
        review M1③: the original words used to be just `Method not found`, with not a word about why it is
        refusing or what to do)."""
        self._rid += 1
        rid = self._rid
        self.pipe.send({"id": rid, "method": method, "params": params})
        ev = self.pipe.read_until(lambda e: e.get("id") == rid, timeout, None, None, lambda: True, None)
        if ev.get("error"):
            # ⭐Goes through the same `_rpc_error` (only one place in the whole file is allowed to parse an error
            #   object).
            raw = _rpc_error(ev["error"])
            if why:
                raise _fail(classify(raw, "unknown"), "%s (codex said: %s)%s" % (why, raw, CODEX_REFUSED), "codex")
            # ⚠️`classify` only recognizes the quota/auth classes; everything else falls to this default ⇒ what it
            #   cannot recognize is crashed, never guess
            raise _fail(classify(raw, "crashed"), raw, "codex")
        return ev

    def turn(self, text: str, on_delta, timeout: float, first_token, cancel) -> dict:
        self._rid += 1
        rid = self._rid
        self.pipe.send({"id": rid, "method": "turn/start",
                        "params": {"threadId": self.tid, "input": [{"type": "text", "text": text}],
                                   "effort": self.effort}})
        sent = time.time()
        box = {"text": "", "usage": {}, "error": "", "ttfc": None, "saw_401": False}

        def pred(ev: dict) -> bool:
            method, params = ev.get("method"), ev.get("params") or {}
            if method == "item/agentMessage/delta":
                piece = params.get("delta") or ""
                # ⛔The first byte recognizes only this one kind of event, and only a frame that has text: the
                #   earliest thing to arrive under `item/*` is app-server's own immediate echo of our own user
                #   message ⇒ taking "the first event" as the first byte would make the reading permanently 0 while
                #   all three tables stay green.
                if piece:
                    if box["ttfc"] is None:
                        box["ttfc"] = round(time.time() - sent, 2)
                    on_delta(piece)
            elif method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") in ("agentMessage", "agent_message"):
                    # 🔴Accumulate, never overwrite. The invariant: "what streamed out" must equal "what came
                    #   back" — an overwriting assignment would let the second one in a turn silently wipe out the
                    #   first (the caller sees A+B in the stream, gets back only B, and all three tables stay
                    #   green).
                    # ⚠️Empty text must never wipe out content already accumulated — ⭐this is guaranteed by
                    #   accumulation itself (`+= ""` is a no-op), never add another `if piece:` for it: that would
                    #   be dead code. 📎 NOTES.md::codex-accumulate
                    # 🔴Accumulated character by character, never stripped, and never joined with a separator:
                    #   neither one goes through `on_delta` ⇒ both would break that invariant. Whether the return
                    #   value should be trimmed is the display layer's decision, never this one's.
                    box["text"] += item.get("text") or ""
            elif method == "thread/tokenUsage/updated":
                # ⚠️This turn's usage is in `last`; `total` is context occupancy, never cumulative spend, and
                #   taking a difference of it produces a number that looks right.
                last = (params.get("tokenUsage") or {}).get("last") or {}
                # ⭐Rename by `CODEX_USAGE_KEYS` first, then hand the whole thing to the whitelist (never filter
                #   with `if k in ...` first: that would let a field the CLI newly added silently vanish, and the
                #   entire point of `usage_numbers` is to be loud when something is dropped).
                box["usage"] = usage_numbers({CODEX_USAGE_KEYS.get(k, k): v for k, v in last.items()}, "codex")
            elif method == "error":
                # ⭐An `error` notification during a retry: only record the flag, never end the call (codex may be
                #   using a 401 to refresh credentials right now, and the next one could succeed).
                #   📎 NOTES.md::codex-401-retry
                box["saw_401"] = box["saw_401"] or _codex_401(params.get("error"))
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                err = turn.get("error")
                box["saw_401"] = box["saw_401"] or _codex_401(err)     # ⭐the second carrier: the failed turn's own `turn.error` (15c review I3)
                if err or turn.get("status") == "failed":
                    # ⭐Goes through `_rpc_error`, never parsed again by hand: only one place in the whole file is
                    #   allowed to parse an error object.
                    box["error"] = _rpc_error(err) if err else "turn failed"
                return True
            elif ev.get("id") == rid and ev.get("error"):
                # 🔴`turn/start` refused outright by app-server (an expired threadId, an unrecognized model) ⇒ also
                #   goes through `_rpc_error`.
                box["error"] = _rpc_error(ev["error"])
                return True
            return False

        # ⭐The first-token gate's test reuses the one true source, ttfc: the gate and the reading are the same
        #   thing, never compute them separately
        self.pipe.read_until(pred, timeout, self.stall, first_token, lambda: box["ttfc"] is not None, cancel)
        # ⭐`.strip()` is used only in this one judgment ("did this turn have any content at all"): a turn that only
        #   emits whitespace ⇒ that is not an answer. Never move it back into the accumulation above, and never
        #   trim the return value while we are at it either — those are two different things (see the comment
        #   above).
        if box["error"] or not box["text"].strip():
            raw = box["error"] or "codex gave no content this turn"
            klass = classify(raw, "unknown")
            # ⭐When the original words cannot be read, this turn genuinely failed (there is an error object, M7:
            #   a turn whose content is all whitespace but which succeeded never counts), and a structured 401 was
            #   seen ⇒ "needs to log in again" (never override a quota the original words already spelled out
            #   clearly; the original words do not change by a single character, B21)
            if box["error"] and box["saw_401"] and klass not in ("quota", "auth_required"):
                klass = "auth_required"
            raise _fail(klass, raw, "codex")
        self.pipe.mark_ok()
        return {"text": box["text"], "usage": box["usage"], "ttfc": box["ttfc"]}

    def alive(self) -> bool:
        return self.pipe.alive()

    def close(self) -> str:
        return self.pipe.close()

    def kill(self) -> None:
        self.pipe.kill()


def _codex_401(err) -> bool:
    """Whether this codex `TurnError` says 401 — both carriers share this one function (the `error` notification's
    `params.error`, and `turn/completed`'s `turn.error`). Shape follows `app-server generate-json-schema`:
    `codexErrorInfo` is either a string enum (`unauthorized`), or `{<variant>: {httpStatusCode}}` (4 variants carry
    a status code). Never recognized by text (that belongs to `classify`). 📎 NOTES.md::codex-401-retry"""
    info = err.get("codexErrorInfo") if isinstance(err, dict) else None
    return info == "unauthorized" or isinstance(info, dict) and any(
        isinstance(v, dict) and v.get("httpStatusCode") == 401 for v in info.values())


def make_driver(cfg: dict, family: str, model: str, effort, system: str, workdir: Path):
    if family == "claude":
        return ClaudeDriver(cfg, model, effort, system, workdir)
    if family == "codex":
        return CodexDriver(cfg, model, effort, system, workdir)
    # ⭐Goes through `_fail`, never a bare `BridgeError`: every failure path must write one line (the global rule),
    #   and "why does this bridge not have gemini" is exactly the kind of question that means going to look in
    #   bridge.log. `family` is carried through as-is: it is the family the caller actually asked for, never just
    #   a "?".
    raise _fail("bad_request", "unrecognized CLI family: %r" % (family,), family)

REPLAY_HEAD = "[scv] Your previous session is gone. Below is the whole conversation so far; continue from it."
REPLAY_TAIL = "[scv] Now reply to this latest message:"
SESSION_IDLE_S = 1800
MESSAGE_ROLES = ("user", "assistant")   # `messages`'s convention only has these two; system goes through the separate `system` parameter
# 🔴The ceiling on how many long-lived sessions can be alive at once; `SessionManager._make_room` is the only
#   place that enforces it. Set by fd, never by memory (Linux's `RLIMIT_NOFILE` soft limit defaults to 1024, and
#   one session holds 3 parent-side pipe fds); ⛔never turn this into a config option — whoever wants more
#   long-lived processes turns `max_concurrent` instead, and the ceiling follows him (see `max_sessions`).
#   The reading, the zero-input control, the POSIX half — none of it measured. 📎 NOTES.md::session-family-cap
MAX_SESSIONS = 24


def fingerprint(system: str, messages: list) -> str:
    """The prefix fingerprint = system plus every message's role plus the content of the non-assistant messages.
    An assistant's content never goes into the fingerprint: the caller's own history copy may have been trimmed by
    the caller itself, while the process remembers the original; matching turn counts already means the same
    conversation."""
    shape = [system] + [[m.get("role"), None if m.get("role") == "assistant" else m.get("content")] for m in messages]
    return hashlib.sha256(json.dumps(shape, ensure_ascii=False).encode("utf-8")).hexdigest()


def flatten(messages: list) -> str:
    """The CLI only accepts user messages, and there is no way to feed it assistant history ⇒ when rebuilding,
    flatten the whole conversation into a single first user message. Returned as-is when there is only one."""
    if len(messages) == 1:
        return messages[0]["content"]
    lines = [REPLAY_HEAD, ""]
    for m in messages[:-1]:
        lines += ["<%s>" % m["role"], m["content"], "</%s>" % m["role"]]
    lines += ["", REPLAY_TAIL, messages[-1]["content"]]
    return NL.join(lines)


class _Session:
    def __init__(self, driver, sig: tuple, workdir: Path):
        self.driver, self.sig, self.workdir = driver, sig, workdir
        self.fp, self.last_used = "", time.time()


class SessionManager:
    """The ledger of long-lived sessions: what comes in over the wire is the full history, the process only eats
    the increment, and a mismatch loudly rebuilds from the full history.
    ⭐The `BridgeError` this layer raises never goes through `_fail` (⇒ not one line lands in bridge.log), and
      that is deliberate, not a gap: the two it raises (`bad_request`/`cancelled`) are both normal control flow,
      and writing one line to disk for every bad request that comes in would turn bridge.log into background
      noise. The structural gate `NoSilentFailurePath`'s scan deliberately does not extend to this class.
    🔴So this layer's failure accounting sits at the HTTP/remote leg layer instead (Task 10's boundary), never
      here — this sentence has to be written into the code: both layers assuming the other one is logging it is
      the textbook way to build a silent failure.
    ⚠️The rebuild log line is a separate matter, write it as usual, and only write it when a rebuild really
      happens. 📎 NOTES.md::manager-does-not-log"""

    def __init__(self, cfg: dict, cat_fn):
        self.cfg, self.cat_fn = cfg, cat_fn
        self._sessions: dict = {}
        self._detached: dict = {}     # sid → [_Session that was detached, not yet closed] (the remote leg: the receiving-stream thread detaches it on the spot, the closing thread closes it, see `detach`)
        self._locks: dict = {}
        self._lock_users: dict = {}   # sid → how many are holding, or about to hold, `_locks[sid]` (see `_forget_lock`)
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(int(cfg.get("max_concurrent") or 4))
        # ⭐The `+1` is not padding: `_make_room` can only be sure of clearing room for one new session by relying
        #   on "in-flight turns < the session ceiling" (an in-flight turn is capped by the concurrency slots).
        #   Whoever turns `max_concurrent` up past MAX_SESSIONS wants exactly that many long-lived processes ⇒ the
        #   ceiling follows him, never let a mechanism override a config decision.
        self.max_sessions = max(MAX_SESSIONS, int(cfg.get("max_concurrent") or 4) + 1)
        self.queued = self.running = 0

    # ---- concurrency slots (B10: the cap is set by the bridge itself; over the cap, queue; a queued call can be cancelled)
    def _acquire(self, cancel) -> int:
        t0 = time.time()
        with self._lock:
            self.queued += 1
        try:
            while not self._slots.acquire(timeout=0.25):
                if cancel is not None and cancel.is_set():
                    raise BridgeError("cancelled", "the caller cancelled it while it was queued")
        finally:
            with self._lock:
                self.queued -= 1
        return int((time.time() - t0) * 1000)

    def _workdir(self, key: str) -> Path:
        """⛔A session id comes from outside, so it never goes into a path directly: only its hash does. 🔴Adds a
        random suffix on top: one directory per instance (15b fix1): the old code let a new and an old instance of
        the same id share one directory, and the old one's cleanup (`_drop`: `close()` first, waiting up to
        CLOSE_GRACE_S, then `rmtree`) would delete the new instance's freshly written
        `system.txt`/`isolation.json`. ⛔Nothing needs "the same id always the same directory": there is no
        `--resume`, a rebuild comes from the full history, orphan sweeps go by pid, and `_drop` deletes the one
        recorded on the `_Session`. 📎 NOTES.md::one-dir-per-instance"""
        p = spath("work") / (hashlib.sha256(key.encode("utf-8")).hexdigest()[:16] + "-" + secrets.token_hex(4))
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _drop(self, session_id: str, kill: bool, gone=None) -> None:
        if gone is None:
            with self._lock:
                gone = [s for s in [self._sessions.pop(session_id, None)] if s is not None]
        if not gone:
            return
        for sess in gone:
            (sess.driver.kill if kill else sess.driver.close)()
            shutil.rmtree(sess.workdir, ignore_errors=True)
        self._forget_lock(session_id)

    def detach(self, session_id) -> None:
        """Pop it out of the table, never close it (closing has to wait out the grace period, tens of seconds in
        the worst case): the remote leg detaches it on the spot, on the receiving-stream thread (15b fix2 N-I2) ⇒
        a job queued behind this close is guaranteed not to see it, and will only ever rebuild from the full
        history (the old code had the closing thread race to pop it ahead of that job). The closing work belongs
        to `close_detached` (called by the closing thread)."""
        with self._lock:
            sess = self._sessions.pop(str(session_id), None)
            if sess is not None:
                self._detached.setdefault(str(session_id), []).append(sess)

    def close_detached(self, session_id) -> None:
        with self._lock:
            gone = self._detached.pop(str(session_id), [])
        self._drop(str(session_id), False, gone)

    # ---- the ceiling on that whole family of resources (A1~A9/B1: the key comes from the network, and this
    #   machine partitions processes/threads/fds/locks/directories/table rows by it)
    def _turn_lock(self, sid: str):
        """Take this session's turn lock, and record "someone wants it" at the same time."""
        with self._lock:
            self._lock_users[sid] = self._lock_users.get(sid, 0) + 1
            return self._locks.setdefault(sid, threading.Lock())

    def _forget_lock(self, sid: str) -> None:
        """Drop a turn lock nobody wants anymore (A4: this table used to be something nothing could ever reclaim,
        and the key comes from the network ⇒ it only ever grew, never shrank).
        🔴The test is a reference count, never `locked()` (there is a gap between the two, the cost is in the
        archaeology); the half that checks `sid not in self._sessions` carries just as much weight: the session
        still being there means someone will still come to use it. 📎 NOTES.md::session-family-cap"""
        with self._lock:
            if not self._lock_users.get(sid) and sid not in self._sessions:
                self._lock_users.pop(sid, None)
                self._locks.pop(sid, None)

    def _release_lock(self, sid: str) -> None:
        with self._lock:
            self._lock_users[sid] = self._lock_users.get(sid, 1) - 1
        self._forget_lock(sid)

    def _evict(self, sid: str) -> bool:
        """Reclaim one session, but never one that is answering right now: `close()` would let that turn see
        stdout EOF ⇒ get reported as "the bridge crashed, retryable" — exactly the lie `_closed_midflight()` exists
        to fix.
        ⭐The test is whether its turn lock can be acquired, never `locked()` (there is a gap between the latter
        and `_drop`). 📎 NOTES.md::session-family-cap"""
        lock = self._locks.get(sid)
        if lock is None or not lock.acquire(blocking=False):
            return False
        try:
            self._drop(sid, kill=False)
        finally:
            lock.release()
            self._forget_lock(sid)
        return True

    def _make_room(self) -> None:
        """Clear room before building a new session. ⭐One ceiling for the whole family, never a separate reclaim
        point patched onto each of those nine resources (the key comes from the network and has no ceiling of its
        own ⇒ patching them one at a time never finishes). ⭐Evicts the least recently used one, never refuses new
        work. 📎 NOTES.md::session-family-cap"""
        self.gc_idle()      # ⭐clear the idle ones first: avoid evicting when possible (evicting makes the other side resend the full history, and that costs money)
        while len(self._sessions) >= self.max_sessions:
            fresh = sorted(self._sessions.items(), key=lambda kv: kv[1].last_used)
            if not any(self._evict(sid) for sid, _s in fresh):
                # Only one configuration reaches this point: `max_concurrent` turned up past the session ceiling (see `max_sessions`).
                log("⚠️ long-lived sessions hit the ceiling of %d, but every single one is answering right now ⇒ building this one over the ceiling anyway" % self.max_sessions)
                return
            log("⚠️ long-lived sessions hit the ceiling of %d ⇒ reclaimed the least recently used one (it will rebuild from the full history next time it comes back)" % self.max_sessions)

    def run(self, *, session_id, model_id, effort, system, messages, on_started, on_delta, cancel,
            first_token=FIRST_TOKEN_S, timeout=300, closed=None) -> dict:
        """One turn of question and answer. Returns {"text", "usage", "ttfc", "rebuilt", "session", "queued_ms", "family"}.
        `on_started(queued_ms)` is called exactly once, the moment a concurrency slot is acquired (on
        the path where it is cancelled while queued, it is never called at all).
        ⭐`text` passes through as-is, this layer never trims it — this layer is transport, not display. The two
        families already have different trim states (claude's is the `result` the CLI gives, already stripped;
        codex's is what we accumulate character by character, with all the whitespace still in it), never smooth
        that over here: smoothing it over means "what did the CLI actually give" can never be checked again.
        📎 NOTES.md::text-passthrough"""
        family, model = resolve_model(model_id, self.cat_fn())
        if effort is not None:
            _closed(effort, EFFORTS, "effort")
        # ⭐Validation covers every single one, never just the last one (`flatten()`/`fingerprint()` both have to
        #   touch every one).
        # 🔴And this block must come before `_acquire`/`make_driver`: blowing up after the process has already
        #   started would mean one bad request really starts a CLI process and leaves it sitting in `_sessions`,
        #   and `messages`/`session_id` both come from the network ⇒ firing off different ids back to back would
        #   turn network input straight into long-lived processes on this machine.
        #   ⭐What carries the weight is this order, never the wording ⇒ the test judges "not one extra line in
        #   children.json". 📎 NOTES.md::validate-before-spawn
        for i, msg in enumerate(messages or []):
            if not isinstance(msg, dict):
                raise BridgeError("bad_request", "messages[%d] must be an object, got %s" % (i, type(msg).__name__))
            _closed(msg.get("role"), MESSAGE_ROLES, "messages[%d]'s role" % i)
            if not isinstance(msg.get("content"), str):
                raise BridgeError("bad_request", "messages[%d]'s content must be text, got %s"
                                                 % (i, type(msg.get("content")).__name__))
        if not messages or messages[-1].get("role") != "user":
            raise BridgeError("bad_request", "the last entry in messages must be user text")
        # 🔴`system` and the hole above are the same shape, one parameter apart. ⭐What matters most is this half:
        #   the same bad input costs the two families differently, and the side that fails is the silent one
        #   (claude throws a bare TypeError and leaks a `work/` directory too; codex throws nothing at all and
        #   just sends `baseInstructions: null` out) ⇒ the test runs both families through it.
        #   📎 NOTES.md::same-input-two-costs
        if not isinstance(system, str):
            raise BridgeError("bad_request", "system must be text, got %s" % (type(system).__name__,))
        queued_ms = self._acquire(cancel)
        # 🔴The very first line after acquiring the slot must be `try`: put it outside the try and one throw
        #   (`KeyboardInterrupt` can reach it) skips the whole `finally` ⇒ that concurrency slot never comes back.
        #   `BoundedSemaphore` does not heal itself: leak it `max_concurrent` times and this bridge can never take
        #   another job again, and it does so without a sound. 📎 NOTES.md::slot-leak
        try:
            with self._lock:
                self.running += 1
            try:
                on_started(queued_ms)
                if session_id:
                    res = self._run_session(str(session_id), family, model, effort, system, messages, on_delta,
                                            cancel, first_token, timeout, closed)
                else:
                    res = self._run_once(family, model, effort, system, messages, on_delta, cancel, first_token,
                                         timeout)
                res.update(queued_ms=queued_ms, family=family)
                return res
            finally:
                with self._lock:
                    self.running -= 1
        finally:
            self._slots.release()   # ⭐if it was acquired it must go back, no matter which family of exception is flying through

    def _run_once(self, family, model, effort, system, messages, on_delta, cancel, first_token, timeout) -> dict:
        workdir = self._workdir("once-" + uuid.uuid4().hex)
        ok = False
        try:
            driver = make_driver(self.cfg, family, model, effort, system, workdir)
            ok = True
        finally:
            # ⭐`finally` plus a flag, never `except BridgeError`: the latter only covers the family we recognize
            #   ourselves, and this stretch has other people's exceptions too (KeyboardInterrupt, a json
            #   serialization error) ⇒ every case missed leaks one directory. The gate is built to the shape of the
            #   bug: the shape is "any exception leaks it", never "a BridgeError leaks it".
            #   📎 NOTES.md::finally-not-except
            if not ok:
                shutil.rmtree(workdir, ignore_errors=True)
        answered = False
        try:
            res = driver.turn(flatten(messages), on_delta, timeout, first_token, cancel)
            answered = True
        finally:
            # ⭐Same as `_run_session`: kill it if it did not finish answering (no matter which family), close it
            #   gracefully only once it has
            if not answered:
                driver.kill()
            elif driver.alive():
                driver.close()
            shutil.rmtree(workdir, ignore_errors=True)
        res.update(rebuilt=None, session=None)
        return res

    def _run_session(self, sid, family, model, effort, system, messages, on_delta, cancel, first_token, timeout, closed=None) -> dict:
        turn_lock = self._turn_lock(sid)
        turn_lock.acquire()                           # the same session only answers one turn at a time
        try:
            sess, sig, rebuilt = self._sessions.get(sid), (family, model, effort), None
            if sess is not None:
                if not sess.driver.alive():
                    rebuilt = "the process is gone"
                elif sess.sig != sig:
                    rebuilt = "the model or effort changed"
                elif sess.fp != fingerprint(system, messages[:-1]):
                    rebuilt = "the prefix does not match"
                if rebuilt:
                    self._drop(sid, kill=True)
                    sess = None
            elif len(messages) > 1:
                rebuilt = ("this bridge has no such session (it was closed before, collected after an unfinished "
                           "previous turn, reclaimed for sitting idle too long or to make room, or the bridge "
                           "restarted)")
            if sess is None and closed is not None and closed():   # closed while waiting for a slot/waiting for the turn lock (15b fix3 O-g) ⇒ never start a CLI (codex also has to connect to a server) just to kill it right away
                raise BridgeError("cancelled", "the dispatcher closed this one's session while it was waiting for "
                                  "a slot/waiting for the turn lock ⇒ it was never answered (never start a CLI for "
                                  "a session that has been closed)")
            if rebuilt:
                log("⚠️ session %s rebuilt from the full history: %s" % (hashlib.sha256(sid.encode("utf-8")).hexdigest()[:8], rebuilt))
            if sess is None:
                self._make_room()                     # ⭐clear room before building the directory: never build the ceiling check after the money has already been spent
                workdir = self._workdir(sid)
                ok = False
                try:
                    driver = make_driver(self.cfg, family, model, effort, system, workdir)
                    ok = True
                finally:
                    # 🔴The directory has already been built while `self._sessions[sid]` has not been assigned yet
                    #   ⇒ nobody can find it afterward.
                    # ⭐This one is worse than the `_run_once` case: this directory's name is
                    #   `sha256(the network-supplied session_id)` plus a random suffix ⇒ firing off different ids
                    #   back to back piles up one directory after another. The reason for `finally` plus a flag is
                    #   the same as `_run_once`.
                    if not ok:
                        shutil.rmtree(workdir, ignore_errors=True)
                sess = _Session(driver, sig, workdir)
                with self._lock:
                    self._sessions[sid] = sess
                if closed is not None and closed():
                    # 🔴The dispatcher closed it during this stretch of starting the CLI (close could not detach
                    #   one that was not registered yet, added in 15b fix2) ⇒ register first, then check: on the
                    #   close side, "detach plus record" sits under the same lock (`Bridge.note_close`) ⇒ neither
                    #   order may be missed. If it is still this same one, kill it on the spot; one that has
                    #   already been detached belongs to the closing thread.
                    with self._lock:
                        mine = self._sessions.get(sid) is sess and self._sessions.pop(sid)
                    if mine:
                        self._drop(sid, True, [sess])
                    raise BridgeError("cancelled", "the dispatcher closed this one's session while its CLI was "
                                      "starting ⇒ it was never answered (never leave the long-lived process behind)")
                text = flatten(messages)
            else:
                text = messages[-1]["content"]        # ⭐a long-lived process only eats the increment
            answered = False
            try:
                res = sess.driver.turn(text, on_delta, timeout, first_token, cancel)
                answered = True
            finally:
                # 🔴A process that has already errored cannot be trusted ⇒ kill it, and rebuild the next question
                #   from the full history. ⭐`finally` plus a flag, never `except BridgeError` (the first settled
                #   idiom): `on_delta` is the caller's own code, and it can throw any family at all; killing only
                #   on BridgeError would let some other family leave "the previous question's answer" sitting in
                #   the pipe, and the next question would get answered with the previous one (measured in review
                #   C-A).
                if not answered:
                    self._drop(sid, kill=True)
            sess.fp = fingerprint(system, list(messages) + [{"role": "assistant", "content": None}])
            sess.last_used = time.time()
            res.update(rebuilt=rebuilt, session=sid)
            return res
        finally:
            turn_lock.release()
            self._release_lock(sid)   # ⭐the turn lock itself also has to be given back: it is a table that grows by ids supplied over the network

    def close_session(self, session_id) -> bool:
        had = str(session_id) in self._sessions
        self._drop(str(session_id), kill=False)
        return had

    def close_all(self) -> None:
        for sid in list(self._sessions):
            self._drop(sid, kill=False)
        for sid in list(self._detached):       # detached but not yet closed (the closing thread is a daemon, never count on it when the bridge stops)
            self.close_detached(sid)

    def gc_idle(self, max_idle: float = SESSION_IDLE_S) -> int:
        """Collect the ones that have been idle too long. ⚠️Returns the count actually collected, never "the count
        that looked collectible": one that has been answering a turn for half an hour and is still not done has
        its `last_used` stuck at the previous turn ⇒ it looks idle, and closing it would be exactly the "the
        bridge crashed" lie (see `_evict`)."""
        old = [sid for sid, s in list(self._sessions.items()) if time.time() - s.last_used > max_idle]
        return len([sid for sid in old if self._evict(sid)])

    def counts(self) -> dict:
        """The status endpoint that gets polled goes through this one, never `snapshot()` — all three numbers are
        already in memory, costing nothing at all.
        ⚠️All three belong to this manager; the `children` inside `snapshot()` reads the global table for the
          whole SCV_HOME — two different measures, never read them as the same thing."""
        return {"sessions": len(self._sessions), "queued": self.queued, "running": self.running}

    def snapshot(self) -> dict:
        """⚠️This call used to be expensive, never hang it off a status endpoint that gets polled: before the
        switch to ctypes, on win32 every extra child cost ~0.70s, in series, and the entire cost was in
        `proc_rss_kb` (back then, asking about one pid started one powershell, 📎 NOTES.md::birth-cert-ctypes).
        Now each child on win32 is microsecond-scale (measured 2026-09-23: a single `proc_rss_kb` call ≈5µs; with
        zero sessions and 4 rows in the table, ≈0.2–0.5ms); on POSIX it still starts one ps per child.
        ⛔Never add a knob for this here today: with not one consumer yet, that would just be a guess.
        🔴"Children" means the number of rows in the registry, never "this manager's session count" — the latter
          belongs to this manager, the former reads the global table for the whole SCV_HOME ⇒ even with zero
          sessions of your own, this call's cost is still charged by the number of rows in the table (measured at
          2.843s before the switch to ctypes, with 4 rows in the table left behind by some other manager).
          📎 NOTES.md::snapshot-is-expensive"""
        kids = [{"pid": c["pid"], "family": c.get("family"), "rss_kb": proc_rss_kb(int(c["pid"]))} for c in children()]
        out = self.counts()   # ⭐those three numbers are computed in exactly one place: never copy a second one here (a copy would drift apart from the one above)
        out["children"] = kids
        return out

RATE_WINDOW_S = 3600   # the remote leg's local rate-limit window; ⭐one number, allowed to live in exactly one place: the 429's `Retry-After` reads this same one
# The measured hit count of the two consumers (recounted after Task 11 wired up the remote leg, never file:line):
#   ①`allow_remote()`'s window — every single remote job goes through it (`RemoteLeg._job` is the production
#     caller) ⇒ a real consumer.
#   ②`Handler._answer_error`'s `Retry-After` — production hits are still 0: the only thing in the whole file that
#     can produce `local_rate_limit` is `RemoteLeg` (at two spots: the rate limit and the in-flight cap), and that
#     leg does not send HTTP headers. ⛔Never read this as "already in use". It is not dead code (the day the
#     local leg gets its own rate limit, it will need this immediately), but today only the tests feed it.


class JobLog:
    """One line of metadata per job. A code audit governs "what it can do"; this file governs "what it actually
    did".
    ⭐`write()`'s signature carries no parameter at all that could hold a prompt or an answer ⇒ "never log the
      content" is guaranteed by the signature, never left to the caller's good conscience
      (`tests/test_60_joblog.py::Log::test_signature_cannot_carry_text` asserts exactly that, the signature
      itself).
    🔴🔴"Never log the content" is not an absolute guarantee — the exceptions below are bounded, and the public
      description (README's "Where prompts and answers end up") is written to match this section exactly:
      ① On `bridge.log`'s failure paths, up to `LINE_CAP_BYTES` bytes of model content can land on disk (`_fail()`'s
         `raw` is the entire answer on the claude side) — we do not control the content, but it is model output,
         never something the other end stuffed in; when a request is refused, that line also copies a short
         fragment of the rejected value (the `repr(…)[:N]` spots, N is in the code);
      ② Every line of `jobs.log` has up to `LINE_CAP_BYTES // 8` bytes of caller-supplied free text (the remote
         leg's `job_id`) — this one the other end can stuff on purpose, because job_id comes from the request.
         `leg`/`klass` are closed sets; `model` is too, via `shown_model()` (a model name that has been reported,
         or a sentence carrying no original text saying "an unreported model" — Task 14 review I1: this used to
         copy whatever raw value the other end gave, verbatim).
      ⇒ What the signature guarantees is "the content has no path in", never "not a single byte, ever".
      A product promise that keeps one unwritten exception is more dangerous than having no such promise at all."""

    def __init__(self):
        self._lock = threading.Lock()
        self._remote: collections.deque = collections.deque()

    def allow_remote(self, per_hour: int) -> bool:
        """The remote leg's local rate limit, with a one-hour window (never a minute —
        `test_the_window_is_an_hour_not_a_minute` pins it from the "one still inside the window must still count"
        side; pinning only "an old one expires" would leave a 60-second window just as green).
        ⚠️Three things the wiring layer needs to know:
        ①The slot is spent before the call starts, and a failure never gives it back — giving it back would let a
          caller that always fails drive this machine's CLI an unlimited number of times.
        ②`per_hour` is an argument on every single call, while the window is state shared by this one instance ⇒
          when wiring it up, settle on "read cfg in exactly one place".
        ③The window lives only in memory, and a process restart wipes it clean. This is a premise, never a
          conclusion: the quota is only genuinely "hourly" as long as (a) nobody adds an automatic restart for it,
          and (b) the other side cannot crash it. ⛔The sentence "this is not an exploitable hole" holds today only
          because nobody has written the restart logic yet, never because of a protected property.
          ⚠️Once autostart is installed, it becomes "the hour since the last start" instead — crash once and the
          quota resets to zero; and since `jobs.log` is persistent while the window is volatile, seeing more than
          `per_hour` entries within the same calendar hour is not a bug.
          ⭐If you add a supervisor/autostart into this file, come back and reread this section — the conclusion
          has to be judged again."""
        now = time.time()
        with self._lock:
            while self._remote and self._remote[0] < now - RATE_WINDOW_S:
                self._remote.popleft()
            if len(self._remote) >= per_hour:
                return False
            self._remote.append(now)
            return True

    def write(self, *, leg, model, klass, usage, latency_s, ttfc, queued_ms, rebuilt, job_id="", cli_version="") -> None:
        """🔴`model` is a name, not an identity ⇒ tallying BYOK spend from this log would silently undercount it;
        `cli_version` is the cheapest patch for that spot (its shape is locked down by `_cli_version()` ⇒ "never
        log the content" was not loosened for it). ⚠️Log lines written before this one do not have this key; a
        reader must treat "this key is missing" as "unknown". 📎 NOTES.md::model-name-not-identity"""
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "leg": leg, "job_id": job_id, "model": model,
               "cli_version": cli_version, "klass": klass,
               "usage": usage, "latency_s": latency_s, "ttfc": ttfc, "queued_ms": queued_ms, "rebuilt": rebuilt}
        # 🔴Per-field truncation cannot rely on `_append_capped`'s fallback to cover it: truncating the whole line
        #   would cut the JSON into half a line, and `tail()` reading half a line just throws ⇒ a 41 KB `job_id`
        #   (it too comes from the network) can kill the read side of the entire audit log.
        # ⭐Cuts every str field down one at a time, never picking "which fields are safe": that kind of list goes
        #   stale the moment the next field is added.
        row = {k: _clip(v, LINE_CAP_BYTES // 8) if isinstance(v, str) else v for k, v in row.items()}
        line = self._line(row)
        over = len(line.encode("utf-8")) - (LINE_CAP_BYTES - 2)
        if over > 0:
            # 🔴The cut above cannot reach `usage` (it is a dict, and its values are numbers) ⇒ one big number
            #   alone can push the whole line past the cap, and then `_append_capped` cuts it in half. ⭐So this
            #   measures once after `json.dumps`, and if it is over, falls back to one minimal but valid line ⇒
            #   "every line is complete JSON" holds by construction, not by relying on the unwritten premise that
            #   "every other field is a closed set".
            # ⭐The skeleton is cut by characters, never by `_clip`: `json.dumps`'s escaping expands by at most 6x
            #   ⇒ (16+16+32)×6 plus ts plus this sentence stays well clear of the cap, with no dependence on any
            #   field's content. 📎 NOTES.md::row-is-always-valid-json
            line = self._line({"ts": row["ts"], "leg": str(row["leg"])[:16], "klass": str(row["klass"])[:16],
                               "job_id": str(row["job_id"])[:32],
                               "scv_note": "the whole line went %d bytes past the single-line cap ⇒ only the skeleton was kept" % over})
        with self._lock:
            _append_capped("jobs.log", line)

    # 🔴`json.dumps(ensure_ascii=False)` only escapes C0 ⇒ DEL/C1/U+2028/U+2029 inside a remote `job_id` land on
    #   disk as-is: a terminal chokes on C1 when a human `type`s this file, and `str.splitlines()` treats
    #   U+0085/U+2028/U+2029 as line breaks (re-review 3, independent check 6) ⇒ swap them for backslash, the
    #   letter u, then four hex digits (a legal JSON escape; `json.loads` restores it character for character;
    #   Chinese text stays just as readable). ⭐After `json.dumps`, before measuring the line length: what gets
    #   measured is exactly the line that lands on disk, and the skeleton line goes through this too.
    JSON_RAW = re.compile("[" + chr(127) + "-" + chr(159) + chr(0x2028) + chr(0x2029) + "]")

    @classmethod
    def _line(cls, row: dict) -> str:
        return cls.JSON_RAW.sub(lambda m: chr(92) + "u%04x" % ord(m.group(0)), json.dumps(row, ensure_ascii=False))

    def tail(self, n: int = 20) -> list:
        """⭐Reads across a rotation: reading only the current file would give a few lines short right after a
        rotation just happened, and "only 3 lines came back" looks identical to "there are only 3 lines in total"
        to whoever is reading the log — that is silently handing back a wrong number."""
        rows = self._read("jobs.log", n)
        if len(rows) < n:
            rows = self._read("jobs.log.1", n - len(rows)) + rows   # make up however many lines are short (0 short never reaches here)
        return rows[-n:]

    @staticmethod
    def _read(name: str, n: int) -> list:
        p = spath(name)
        if not p.exists():
            return []
        # Filter first, then slice: the other way around, one blank line or marker line mixed into the tail would
        # hand back fewer than asked for.
        # ⭐Skips the rotation marker (the line `_append_capped` writes, which is not JSON) — it is metadata, never
        #   a job, and letting it through would hand a consumer a dict with no `leg`/`model` in it, silently.
        #   ⚠️The test blocks only this one kind of line: half a line of JSON still throws (that is bad data, never
        #   silently skip it).
        # 🔴Splits only on NL, never `splitlines()`: the latter also treats U+0085/U+2028 as line breaks, and lines
        #   written before the upgrade still have those as-is (re-review 3, independent check 6)
        good = [y for y in p.read_text(encoding="utf-8").split(NL) if y.startswith("{")]
        return [json.loads(x) for x in good[-n:]]

# ━━ Local API (B3/B5/B23): a few OpenAI-compatible routes + four guards
GET_ROUTES = ("/healthz", "/v1/models")
POST_ROUTES = ("/v1/chat/completions", "/v1/sessions/close")
ROUTES = GET_ROUTES + POST_ROUTES   # ⭐there is only one routing table: preflight reads routes off it too (never let OPTIONS keep a table of its own)
MAX_BODY = 8 * 1024 * 1024
MAX_TIMEOUT_S = 3600.0       # the longest turn a caller can ask for; not a product promise — it is "do not let one number hold a concurrency slot forever"
#   ⚠️`timeout` is not the ceiling on how long the caller waits for a reply: once the gate fires, a
#     tree-kill still has to run before control returns here (win32 ≈32s / POSIX ≈17s, see `_Pipe.kill`'s
#     note) ⇒ the client's socket timeout has to budget for this stretch too.
MAX_NAME_CHARS = 128         # the length cap for names like model/session that come in from the network
SURROGATES = re.compile("[" + chr(0xD800) + "-" + chr(0xDFFF) + "]")   # lone surrogates: `json.loads` accepts their escaped spelling, but it cannot be encoded as UTF-8
CLOSE_ANSWER_S = 2.0         # /v1/sessions/close answers by this long at the latest (closing a session in flight can drag on)
KILL_BUDGET_S = 32.0         # tree-kill's worst case (win32 ≈32s / POSIX ≈17s, see `_Pipe.kill`'s note)
# 🔴"someone just closed this session" has to stay remembered for at least one full turn plus one tree-kill: this
#   table is the only judge of "closed while in flight ⇒ call it cancelled", and a turn can run as long as
#   `MAX_TIMEOUT_S`. Remembering it for less than one turn (600s, as it was originally written) means a long turn
#   closed mid-flight might already have had its memory trimmed away ⇒ it falls back to "502, the bridge crashed,
#   retryable" — exactly the thing this rewrite is meant to fix.
CLOSE_MEMORY_S = MAX_TIMEOUT_S + KILL_BUDGET_S
KEEPALIVE_S = 10.0           # during a streaming lull, send an SSE comment every this many seconds
PEER_PROBE_S = 2.0           # on the non-streaming path, probe whether the caller is still there every this many seconds (not a timeout, see `_peer_gone`)
KNOWN_PARAMS = ("model", "messages", "stream", "session", "effort", "reasoning_effort", "n", "response_format",
                "modalities", "first_token_timeout", "timeout")
# ⚠️a "word.word" shape like `chat.completion` lands squarely in gate ①'s URL-detector HOSTNAME check
#   (`completion` gets read as a TLD) ⇒ spell it out piece by piece, never as one literal (same spot as in
#   `proc_start_id`). Never loosen that check, never add a word to NOT_HOSTS: that trades a miss for a false
#   alarm. ⭐the value itself is OpenAI's live shape and not one character of it may change ⇒
#   tests/test_70_local_api.py pins the spelled-out result with a few assertions that write out the literal.
OBJ_COMPLETION = "chat" + chr(46) + "completion"     # the `object` of the non-streaming reply
OBJ_CHUNK = OBJ_COMPLETION + chr(46) + "chunk"       # the `object` of each streamed chunk
# parameters that would change the semantics must never be swallowed silently: the CLI's tools are off, and it
#   cannot give logprobs or multiple samples either.
REJECTED_PARAMS = ("tools", "tool_choice", "functions", "function_call", "logprobs", "top_logprobs", "audio",
                   "prediction")
# ⭐the promise made to callers (the compatibility table) lives in README.md's "Local API" section — Task 14
#   moved it there from here (the source of truth may live in only one place, never keep a second copy here).
#   Gates on both ends read that table: every entry in `KNOWN_PARAMS`/`REJECTED_PARAMS` must be named in it, and
#   whatever the "straight 400" row names must really be in these two tuples (tests/test_97_docs.py::CompatTable)
#   — change these two tuples, and go change that table too.
_refuse_warned: set = set()   # each of the four guards only complains the first time (reasons in `Handler._refuse`)


def _bad(msg: str) -> BridgeError:
    """A bad request at this layer. Never through `_fail()`: that door belongs to the driver layer (something
    went wrong on the CLI's side), this end is input validation — two different failure domains. This whole
    family's bookkeeping is added in one place, `_error_payload()` (see its note)."""
    return BridgeError("bad_request", msg)


def _text_of(content, where: str) -> str:
    """One message's body → a piece of text. ⭐`where` is not decoration: the `bridge.log` line for a failure has
    to show which one broke (`messages[3]`), or the caller is left guessing which of a hundred messages "only
    accepts text" is talking about."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            kind = part.get("type") if isinstance(part, dict) else None
            if kind != "text":
                # ⚠️only echo the type name, and truncate it: copying the whole block in as-is would send it
                #   straight into bridge.log (the other half of B30)
                raise _bad("%s only accepts text, got a content block of type %s" % (where, repr(kind)[:32]))
            parts.append(str(part.get("text") or ""))
        return "".join(parts)
    raise _bad("%s's content must be text or a list of text blocks, got %s" % (where, type(content).__name__))


def _name(body: dict, key: str, required: bool):
    """Take in a name (model/session) from the network. ⭐This layer is where they first enter the bridge from the
    network ⇒ the shape limit lives here: `model` goes as-is into a `jobs.log` line, into error text, and then
    into `bridge.log`; without a length cap the other side could hand us a 40 KB name to write into our own audit
    log; `session` becomes a key in `_locks`.
    ⚠️Never echo the original value in the error: echoing it just sends it into the log a second time."""
    v = body.get(key)
    if v is None or v == "":
        if required:
            raise _bad(key + " must not be empty")
        return None
    if not isinstance(v, str):
        raise _bad("%s must be text, got %s" % (key, type(v).__name__))
    if len(v) > MAX_NAME_CHARS:
        raise _bad("%s can be at most %d characters, this one has %d" % (key, MAX_NAME_CHARS, len(v)))
    return v


def _seconds(body: dict, key: str, default: float) -> float:
    """A seconds parameter. 🔴A `float(body.get(key) or default)`-style write, faced with `"thirty seconds"`,
    raises `ValueError` — which is not a `BridgeError` ⇒ it goes straight through, past the handler: the caller
    sees the connection just drop, and `bridge.log` gets not one word. ⚠️`nan`/`inf` also come in through this path
    (`json.loads` accepts them by default); the comparison below rejects those too."""
    v = body.get(key)
    if not v:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise _bad("%s must be a positive number of seconds, got %s" % (key, type(v).__name__))
    if not 0 < v <= MAX_TIMEOUT_S:
        raise _bad("%s must be between 0 and %g seconds" % (key, MAX_TIMEOUT_S))
    return float(v)


def normalize_request(body) -> dict:
    """OpenAI shape → the shape the session manager wants. Parameters it does not recognize go into `ignored` and
    are handed back to the caller; ones that would change the semantics get a straight 400."""
    if not isinstance(body, dict):
        raise _bad("the request body must be a JSON object")
    # 🔴a lone surrogate let in here would blow up on some encode far from the entry point, and be blamed on
    #   something else (D23, "count the same shape first" — measured the same on both legs: session ⇒ blows up
    #   computing a hash as "the bridge itself crashed"; system/body ⇒ blows up writing the file / writing stdin
    #   as a retryable `crashed`) ⇒ reject it in this one entry point shared by both legs (never patch each encode
    #   site separately)
    if SURROGATES.search(json.dumps(body, ensure_ascii=False)):
        raise _bad("the request has a character that cannot be encoded as UTF-8 (a lone surrogate, U+D800-U+DFFF)")
    for k in REJECTED_PARAMS:
        if body.get(k):
            raise _bad("this bridge does not support %s (behind it is a CLI with every tool turned off)" % k)
    if body.get("n") not in (None, 1):
        raise _bad("n can only be 1")
    rf = body.get("response_format")
    if isinstance(rf, dict) and rf.get("type") not in (None, "text"):
        raise _bad("response_format only supports text")
    mod = body.get("modalities")
    if mod and (not isinstance(mod, list) or mod != ["text"]):
        # ⚠️a `list(body["modalities"])`-style write, faced with a number, raises `TypeError` (see the same spot
        #   in `_seconds`)
        raise _bad("modalities only supports [text]")
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        raise _bad("messages must not be empty")
    system, out = [], []
    for i, m in enumerate(msgs):
        role = m.get("role") if isinstance(m, dict) else None
        if role in ("system", "developer"):
            system.append(_text_of(m.get("content"), "messages[%d] (system)" % i))
        elif role in MESSAGE_ROLES:
            out.append({"role": role, "content": _text_of(m.get("content"), "messages[%d]" % i)})
        else:
            raise _bad("messages[%d] has an unsupported role: %s" % (i, repr(role)[:32]))
    return {"model_id": _name(body, "model", True), "system": (NL + NL).join(system), "messages": out,
            "session": _name(body, "session", False),
            # 🔴`effort` is this layer's third "string coming in from the network" ⇒ it goes through the same
            #   `_name()` door as `model`/`session`. The version that skipped it: the other side stuffs in
            #   200,000 characters, and `_closed()` echoes it straight back into the response body and stderr
            #   (there is a `_clip` on disk, neither of those has one). ⭐the rule this section set for itself
            #   should not apply to just two fields.
            "effort": _name(body, "effort", False) or _name(body, "reasoning_effort", False),
            "stream": bool(body.get("stream")),
            "ignored": sorted(k for k in body if k not in KNOWN_PARAMS),
            "first_token": _seconds(body, "first_token_timeout", FIRST_TOKEN_S),
            "timeout": _seconds(body, "timeout", 300.0)}


def _peer_gone(conn) -> bool:
    """Is the caller still connected. ⭐This is not a timeout (a timeout guesses an upper bound, and on this path
    we genuinely cannot guess one): it only returns True when the other end has really closed the connection.
    🔴What it guards is a concurrency slot — the non-streaming path originally had no probe at all (streaming
      relies on the keep-alive bytes hitting `OSError`) ⇒ a client that hangs up still runs that turn all the way
      to the gate, and one slot sits held by someone who already left for the whole `timeout` (300s by default;
      measured `running=1` still 10s after the hangup). `max_concurrent` defaults to only 4, and once
      `BoundedSemaphore` has leaked it all away it does not heal itself.
    ⭐`MSG_PEEK` does not consume bytes: we already read the request body in full by `Content-Length`, so this
      only asks "is the read side at EOF".
    ⚠️Getting something back ⇒ the other end is still there (under HTTP/1.0 that is mostly junk, not this
      function's concern); any exception is treated as "still there" — better to miss killing one slot than to
      let one failed probe cut off a turn that is genuinely alive."""
    try:
        ready, _w, _x = select.select([conn], [], [], 0)
        return bool(ready) and conn.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return False


def _usage_openai(u: dict) -> dict:
    a, b = int((u or {}).get("input_tokens") or 0), int((u or {}).get("output_tokens") or 0)
    return {"prompt_tokens": a, "completion_tokens": b, "total_tokens": a + b}


def _shown(text: str) -> str:
    """The body handed back to the caller. ⭐trim is a display-layer decision, and it lives at this layer: the
    two families come in already trimmed to different degrees (claude's `result` comes from the CLI already
    stripped by the driver; codex's is accumulated character by character, all the whitespace is still there,
    📎 NOTES.md::text-passthrough) ⇒ leaving it unnormalized here means asking the same question to both families
    comes back with `content` of different shapes.
    ⚠️the cost is written down plainly: on the streaming path, deltas are passed through byte for byte (bytes
      already sent cannot be taken back) ⇒ what codex's family streams out together will carry extra whitespace
      at both ends compared to the non-streaming reply. Never touch delta to make the two line up — that erases
      what the CLI actually said."""
    return text.strip()


def _closed_midflight(e: BridgeError, closed: bool) -> BridgeError:
    """A turn closed by `/v1/sessions/close` while it was in flight must never be reported as "the bridge
    crashed, retryable".
    🔴`close_session` does not take turn_lock ⇒ it will close the CLI that is answering right now, and the turn in
      flight sees stdout EOF ⇒ reported as-is that is a 502 + `retryable=True` + an empty `fix_hint` + original
      words pointing at a CLI crash that never happened.
    ⭐only this `crashed` slot gets rewritten, not every failure: a genuine timeout or genuine out-of-quota in the
      same turn is its own true conclusion, and covering it with "someone came and closed it" is just lying in
      the other direction.
    ⚠️the CLI side's original words are kept as-is (B21), only demoted to a parenthetical aside. 📎 NOTES.md::midflight-close"""
    if not closed or e.klass != "crashed":
        return e
    # ⚠️never hard-code `/v1/sessions/close` here: on the remote leg's path, what closes it is a `close_session`
    #   event pushed by the dispatcher, and hard-coding it would be original words pointing at the wrong place
    #   (the class is right, the words are wrong, and the test harness only asserts the class — it would not catch it).
    out = BridgeError("cancelled", "this session was closed while this turn was in flight"
                                   " (what the CLI side saw was: %s)" % e.raw, e.family)
    out.logged = e.logged   # ⭐once that has already been logged, never log it again: two lines for one failure means thinning out the log
    return out


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # 🔴`SO_REUSEADDR`'s meaning on win32 is not the same as on POSIX: with both sides on (which is exactly
    #   stdlib's default, and both bridges are this same class), a second bridge really can bind the same port
    #   ⇒ each one takes half the requests, and neither one errors, neither one is detectable.
    #   win32, five cases measured 📎 NOTES.md::windows-reuseaddr.
    # ⚠️the POSIX side is unmeasured (this machine only has win32). What is known: on Linux, `SO_REUSEADDR` does
    #   not let a second listener bind the exact same addr:port, but the case of "one binds `0.0.0.0:P`, another
    #   binds `127.0.0.1:P`" is allowed ⇒ while another process holds the wildcard address, the bridge can still
    #   bind there, and the kernel picks who gets each request; BSD/macOS is a third scheme again.
    #   ⇒ keeping stdlib's default for POSIX here is a trade-off, not a conclusion: what that side wants is
    #   "can rebind after TIME_WAIT".
    allow_reuse_address = os.name != "nt"


PORT_TRIES = 20                     # 0.2.1: how far past a taken default port the local API looks
ES_SYSTEM_REQUIRED = 0x00000001     # SetThreadExecutionState: reset the system idle timer once (⛔ never ES_CONTINUOUS: that one sticks to the calling thread)
AWAKE_EVERY_S = 30                  # how often the main loop asks; far below any sleep timeout Windows offers (1 minute is the shortest)


class KeepAwake:
    """While the remote leg has a job running, or had one end less than `window_s` ago, keep resetting Windows'
    system idle timer so the machine does not fall asleep in the middle of someone's game (maintainer 2026-09-27:
    the bridge runs on a desktop at home while the player plays on a phone — a desktop that sleeps pauses the game,
    and nobody can wake it from the phone).
    ⭐Only the system idle timer, once per tick (ES_SYSTEM_REQUIRED alone): the display may still turn off, and
      nothing is left behind when the bridge stops. ⛔never ES_CONTINUOUS: it sticks to the calling thread.
    ⭐The tail (`window_s` after the last job) covers the gaps inside one game: the player's own turn, a pause
      waiting for a login. A machine that is already asleep when a game starts cannot be helped from here.
    POSIX: a no-op (not measured; left to the POSIX pass)."""

    def __init__(self, window_s: float, poke=None, clock=time.time):
        self.window_s = float(window_s)
        self._poke = poke if poke is not None else _poke_idle_timer
        self._clock = clock
        self._lock = threading.Lock()
        self._busy = 0
        self._last = None                 # when the last job ended; None = none yet
        self.pokes = 0

    def begin(self) -> None:
        with self._lock:
            self._busy += 1

    def end(self) -> None:
        with self._lock:
            self._busy = max(0, self._busy - 1)
            self._last = self._clock()

    def wanted(self) -> bool:
        if self.window_s <= 0:
            return False
        with self._lock:
            return self._busy > 0 or (self._last is not None and self._clock() - self._last < self.window_s)

    def tick(self) -> bool:
        """Called by the main loop every `AWAKE_EVERY_S`. Returns whether it asked Windows to stay awake."""
        if not self.wanted():
            return False
        try:
            self._poke()
        except Exception as e:           # never let this take the bridge down: it is a convenience, not the job
            log("⚠️ could not ask the system to stay awake: %s: %s" % (type(e).__name__, _one_line(e)))
            return False
        self.pokes += 1
        return True


def _poke_idle_timer() -> None:
    if sys.platform == "win32":
        _k32().SetThreadExecutionState(ES_SYSTEM_REQUIRED)


def _awake_window(v) -> float:
    """config.json's `keep_awake_s`: missing ⇒ 600; a number ≥ 0 ⇒ that; anything else ⇒ 600, said once."""
    if v is None:
        return 600.0
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
        return float(v)
    log("⚠️ config.json's keep_awake_s is not a number ≥ 0 (it is %r) ⇒ using 600" % (v,))
    return 600.0


class Bridge:
    """A bridge = one config + one detection pass + a session manager + an audit log + a local HTTP leg.
    ⚠️`found`/`cat` are detected at the moment the bridge starts: if the user runs `codex login` after starting the
      bridge, it only becomes visible after a restart — never change this to probe on every request (`detect()`
      has to start two child processes each time, and its few lines of decision log about "not reporting this
      whole family" would get flooded into background noise)."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.found = detect(cfg)
        self.cat = catalog(cfg, self.found)
        self.sessions = SessionManager(cfg, lambda: self.cat)
        self.joblog = JobLog()
        # 0.2.0: `keep_awake_s` — how long after the last remote job the machine is kept awake (0 = off)
        self.awake = KeepAwake(_awake_window(cfg.get("keep_awake_s")))
        self.started_at = time.time()
        self.httpd = None
        self.remote = None                 # assigned only in Task 11
        self._closed_at: dict = {}         # session → the last time someone came to close it
        self._closed_lock = threading.Lock()

    def start_local(self, port: int | None = None) -> int:
        want = int(self.cfg.get("port") or 8765) if port is None else int(port)
        try:
            self.httpd = _Server((LOCAL_HOST, want), _make_handler(self))
        except OSError as exc:
            # This is the most common way starting the bridge fails (a previous one is still open / something
            # else is holding the port) ⇒ never let the user see a bare WinError.
            err = BridgeError("crashed", "the local API could not start (port %d): %s" % (want, _one_line(exc)))
            log(err.raw)
            err.logged = True
            raise err
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self.httpd.server_address[1]

    def start_local_or_next(self) -> tuple:
        """0.2.1: `(port, moved_from)`. The default port taken by another program (seen on the maintainer's desktop)
        ⇒ try the next `PORT_TRIES` ports and say where it went; `moved_from` is None when nothing moved.
        ⛔never moves a port the user chose (their own client may point at it): that one fails as before."""
        want = int(self.cfg.get("port") or DEFAULT_CONFIG["port"])
        try:
            return self.start_local(), None
        except BridgeError:
            if want != DEFAULT_CONFIG["port"]:
                raise
        for p in range(want + 1, want + PORT_TRIES + 1):
            with contextlib.suppress(BridgeError):
                return self.start_local(p), want
        raise BridgeError("crashed", "the local API could not start on port %d or the %d after it" % (want, PORT_TRIES))

    def start_remote(self) -> bool:
        """Not paired = not one byte goes out to the network. Returns False = this bridge only has a local leg
        today (two different reasons, kept apart in the log).
        🔴the plaintext slot: §① 's `HTTPS` line says "the remote address has to start with it", and before this
          spot nowhere in the whole file was actually enforcing it — another promise that was written down and
          then nobody enforced. Without this guard, an `http://` `remote_url` (which could also be hand-written
          into config.json) would send `Authorization: Bearer <remote_token>`, the hello payload's "which CLIs are
          installed on this machine", and every segment of every answer, all in plaintext.
        ⭐this one spot that actually dials must judge for itself (a hand-written config can go around `scv pair`);
          the judgment itself lives in exactly one place (`remote_url_refused`), and `pair` calls it too.
        ⚠️plaintext `http://` on loopback is an exception made for the test harness (the reference dispatcher runs
          on 127.0.0.1)."""
        url = str(self.cfg.get("remote_url") or "")
        if not url or not self.cfg.get("remote_token"):
            return False
        why = remote_url_refused(url)          # ⭐the same judgment as `scv pair` (only one is allowed to exist)
        if why:
            log("❌ the remote leg is not starting: config.json's remote_url is no good: " + why)
            return False
        self.remote = RemoteLeg(self)
        threading.Thread(target=self.remote.run, daemon=True).start()
        return True

    def cli_ver(self, model_id) -> str:
        """The version of the CLI family behind this model (for `jobs.log`'s bookkeeping; detection already ran
        when the bridge started, this call costs nothing)."""
        return _cli_version((self.found.get(str(model_id).partition("/")[0]) or {}).get("version"))

    def health(self) -> dict:
        """⚠️never call `snapshot()`: its cost grows with the registry's row count (before the switch to ctypes,
        win32 started one powershell per pid to ask its RSS; measured 2.843s even for a fresh manager with zero
        sessions; POSIX still runs one `ps` per pid), and this endpoint is exactly what people poll.
        ⭐`children` is just the registry's row count (one file read, measured to cost nothing), and it is a global
          number for the whole SCV_HOME, while `sessions`/`queued`/`running` belong to this one bridge — two
          different scopes, never read them as the same thing.
        🔴this endpoint takes no token ⇒ `families` only gives the version's shape + whether it is blocked (13b
          review M3): the original words in `blocked`/`version` can carry an absolute local path (a pastable login
          command, the OS's own words, a path with a user name in it); the reason and what to do about it are left
          for the local doctor. 📎 NOTES.md::snapshot-is-expensive"""
        out = self.sessions.counts()
        out.update(version=VERSION, protocol=PROTOCOL, uptime_s=int(time.time() - self.started_at),
                   models=self.cat, children=len(children()),
                   families={f: {"version": _cli_version(i["version"]), "blocked": bool(i["blocked"])}
                             for f, i in self.found.items()},
                   remote=(self.remote.describe() if self.remote else {"state": "off"}))
        return out

    def note_close(self, sid: str, order=None, detach=False) -> bool:
        """Record that someone has started closing this session. ⭐What gets recorded is the moment work begins,
        not the moment it finishes closing: the judgment is "did anyone come to close it while this turn was in
        flight", and the act of closing itself can drag on for over ten seconds. `order` = the event sequence
        number on the remote leg's receive-stream thread (see `closed_during`).
        Returns True = a new close (the caller should go close it); False = a previous close is still running ⇒
        only refresh the record to this one (15b fix2 M-b: that thread will pick up one more round once it
        finishes the one in its hand)."""
        now = time.time()
        with self._closed_lock:
            for k, row in list(self._closed_at.items()):
                if now - row["at"] > CLOSE_MEMORY_S:
                    self._closed_at.pop(k, None)   # ⭐sweep it while we're here: this table must never keep growing with every close
            row = self._closed_at.get(sid)
            # ⭐this remote-side call landing on a locally-initiated "still closing" (a row with no order) also
            #   counts as a new one: the local thread only closes the one it popped itself, and never picks up
            #   another round (15b fix3 N-M1)
            fresh = row is None or row["done"] or (order is not None and row.get("order") is None)
            self._closed_at[sid] = ({"at": now, "done": False, "error": "", "order": order} if fresh
                                    else dict(row, at=now, order=order))
            if detach:          # remote leg: detaching and recording share the same lock (both the check after `_run_session` registers and the check at the closing thread's finish rely on it)
                self.sessions.detach(sid)
            return fresh

    def note_closed(self, sid: str, error: str = "", order=None):
        """That close is done (record it whether it succeeded or not). 🔴"started" and "finished" must never look
        the same, and `close_session()` on its own cannot tell them apart: what it returns is "was it in the table
        just now" — the first call already popped it ⇒ the second call always gets `False`, byte for byte the
        same as "this bridge never had this session at all". This entry is what makes up for that gap.
        ⚠️record it even when it blows up: not recording it leaves "still closing" hanging forever, and that is an
        answer that will never change.
        ⭐if the order recorded no longer belongs to this call (another one arrived while this was closing, M-b; or
          while this local call was closing, the remote leg started its own, fix3 N-M1) ⇒ never record it as
          finished, return that call's order instead: the remote caller picks up one more round; the local caller
          does not need to worry about it (that call has its own thread, and it will record the finish itself)."""
        with self._closed_lock:
            row = self._closed_at.get(sid)
            if row is not None and row.get("order") != order:
                return row["order"]
            if row is not None:
                row["done"], row["error"] = True, error
        return None

    def closed_during(self, sid, since: float, order=None) -> bool:
        """Did anyone come to close this same session while this turn (which began at `since`) was in flight.
        🔴the remote leg (both sides carry the receive-stream thread's event sequence number `order`) judges by
          event order, never by the wall clock (15b fix2 N-I2): close and job are handled on the same thread in
          arrival order, but win32's `time.time()` ticks about once every 1 ms, and two back-to-back events often
          land on the same value ⇒ `>=` would judge "closed, then asked" as "closed while queued" — a false,
          non-retryable cancelled. ⭐the sequence number strictly increases ⇒ never a tie. The local leg has no
          single arrival order (one thread per request), so it still goes by the wall clock."""
        with self._closed_lock:
            row = self._closed_at.get(sid) if sid else None
            if row and order is not None and row.get("order") is not None:
                return row["order"] > order
            return bool(row) and row["at"] >= since

    def close_state(self, sid) -> str:
        """The outcome of the previous close: `""` = no memory of this, `"closing"` = still closing, `"closed"` =
        finished closing, `"!<original words>"` = that close blew up (⇒ every time it is asked, it gets the same
        line back; never let a failed close turn into a silent False)."""
        with self._closed_lock:
            row = self._closed_at.get(sid) if sid else None
            if not row:
                return ""
            if not row["done"]:
                return "closing"
            return ("!" + row["error"]) if row["error"] else "closed"

    def stop(self) -> None:
        if self.remote:
            self.remote.stop()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.sessions.close_all()


def _make_handler(bridge: Bridge):
    class Handler(BaseHTTPRequestHandler):
        """The local leg's request shell.
        🔴it is not inside the scan surface of the `NoSilentFailurePath` gate (that gate recognizes classes named
          `_Pipe`/`*Driver`), and that is a decision, not a gap: the driver layer is the first to know something
          went wrong with the CLI, and every one of its `raise`s has to log its own line; this layer is the
          opposite — it is the last one to catch anything, and most of the exceptions that fly this far have
          already been logged by `_fail()` ⇒ logging another line here would be two lines for the same failure.
          ⭐its own door is `_error_payload()`: every error exit goes out through that one place, and "log a line
          here for the one nobody logged yet" is also written only in that one spot.
          Gate: tests/test_70_local_api.py::OneDoor::test_error_body_is_computed_in_exactly_one_place
        📎 NOTES.md::api-is-the-last-catcher"""

        server_version = "scv/" + VERSION
        protocol_version = "HTTP/1.0"   # the streaming path has no Content-Length ⇒ closing the connection is what signals the end
        # ⭐a process that connects and never sends a byte must not get to sit on a thread for free
        #   (`daemon_threads=True` ⇒ there is no cap on the number of threads).
        # ⚠️this is a socket-level timeout (`settimeout` in `StreamRequestHandler.setup`) ⇒ writes count too:
        #   a caller that stays connected without reading a single byte for 60s knocks this turn into `OSError` ⇒
        #   it goes down the cancellation path (never silently).
        timeout = 60
        _sent = False
        _cors: dict = {}

        def log_message(self, *args) -> None:
            """The default implementation prints every request to stderr ⇒ that is a second exit besides `log()`
            (no line wrapping, no `_clip`, no rotation), and the request line carries whatever path the caller
            gave. ⇒ turned off; whatever needs saying, we say it ourselves."""
            return

        # ---- Exits: the response only ever goes out through these two doors
        def _json(self, status: int, obj: dict, extra: dict | None = None) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            if self._sent:
                # ⚠️the headers already went out (most likely the stream already opened its mouth) ⇒ sending
                #   another status code would only chase garbage into the body.
                log("⚠️ the local API tried to send a second response (status=%d, %s) ⇒ only logging this line" % (status, repr(self.path)[:128]))
                return
            self._sent = True
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for k, v in list(self._cors.items()) + list((extra or {}).items()):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _drain(self) -> None:
            """Before refusing a POST, read the request body out first: closing without reading a single byte
            leaves data sitting in the kernel that nobody picked up, and the other side gets an RST ⇒ the caller
            sees `WinError 10053`, not our carefully-written 401.
            🔴read the whole thing, not just the front of it: the version written first only read the front 64 KB,
              and measuring both arms showed it does not fix anything (it still breaks starting at 200 KB) —
              "read a little" and "read nothing" land in the same slot here. The cap is `MAX_BODY` itself (which is
              already the amount we are willing to accept, and this leg only listens on loopback).
              📎 NOTES.md::refuse-big-body"""
            try:
                n = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
            except ValueError:
                return
            if n > 0:
                with contextlib.suppress(OSError):
                    self.rfile.read(n)

        def _refuse(self, status: int, code: str, msg: str) -> bool:
            """The refusal door for the four guards (this family is never a `BridgeError`: they never even made it
            through the bridge's own door).
            ⭐each category only complains the first time: the most common reason for all four of these is "the
              client is misconfigured" (a bad token / Origin not configured), and the two-thousandth line would
              only push some other error out of the log's rotation — a log drowned into background noise by its
              own kind is worse than no log at all."""
            if code not in _refuse_warned:
                _refuse_warned.add(code)
                log("the local API refused a request (%s): %s (this category only complains once)" % (code, msg))
            if self.command == "POST":
                self._drain()
            self._json(status, {"error": {"message": msg, "type": "refused", "code": code, "retryable": False}})
            return False

        def _error_payload(self, e: BridgeError) -> dict:
            """This leg's error exit = that one shared exit (`error_payload()`, the reasons for its bookkeeping
            are written where it is defined)."""
            return error_payload(e, "the local API ")

        def _answer_error(self, e: BridgeError) -> None:
            extra = {}
            if e.klass == "local_rate_limit":
                # ⭐only our own rate limiter knows how long its window is ⇒ only it can give a `Retry-After`
                #   (it is what makes `retryable=True` safe to send: without saying how long to wait, the client
                #   would retry immediately and flood the whole hour). We do not know when the upstream quota
                #   comes back ⇒ never invent a number for it (the recovery time is in the CLI's own words).
                extra["Retry-After"] = str(int(RATE_WINDOW_S))
            self._json(HTTP_STATUS.get(e.klass, 502), self._error_payload(e), extra)

        # ---- The four guards (B23), cheapest to most expensive; each one has its own counter-example test for
        #      "leave this out and it gets through"
        def _guard(self, need_token: bool, need_json: bool) -> bool:
            host = (self.headers.get("Host") or "").strip().lower()
            if host.startswith("["):        # `[::1]:8765` ⇒ `::1`
                host = host[1:].split("]")[0]
            elif host.count(":") == 1:      # `127.0.0.1:8765` ⇒ `127.0.0.1`
                host = host.split(":")[0]   # never strip a bare IPv6 address too: `::1` would get cut down to `:`, a 403 for someone who did nothing wrong
            if host not in LOOPBACK_HOSTS:
                # DNS rebinding: the request really did land on the loopback port, but the browser thinks it is visiting evil.example
                return self._refuse(403, "forbidden_host", "the Host header is not a loopback address: %s" % repr(host)[:64])
            origin = self.headers.get("Origin")
            if origin:
                if origin not in (bridge.cfg.get("allowed_origins") or []):
                    return self._refuse(403, "forbidden_origin", "a request carrying an Origin header is refused by default: %s" % repr(origin)[:128])
                # ⭐only echo this header back once it is configured, never echo it unconditionally
                #   (unconditionally would turn this guard into "anyone is welcome")
                self._cors["Access-Control-Allow-Origin"] = origin
            if need_json and (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                # a text/plain POST does not trigger a CORS preflight ⇒ any web page could send one
                return self._refuse(415, "json_only", "Content-Type must be application/json")
            if need_token:
                # 🔴no token configured ⇒ let no one in (fail closed): a `"Bearer " + str(cfg.get(...))`-style
                #   write would splice together `Bearer None` when the key is missing, `Bearer ` when it is an
                #   empty string — both of them are guessable passwords, and on this leg the token is the only
                #   authentication there is. Never assume "`load_config()` always mints one": cfg can also be
                #   hand-assembled by someone else (`helpers.start_bridge(local_token="")` is exactly that).
                token = bridge.cfg.get("local_token") or ""
                got = (self.headers.get("Authorization") or "").encode("utf-8")
                want = ("Bearer " + token).encode("utf-8")
                if not token or not hmac.compare_digest(got, want):
                    # never go through `self_cmd`: the refusal body is for a caller that did not bring the right
                    # token ⇒ never carry a local path in it (a path has a user name in it)
                    return self._refuse(401, "bad_token", "missing the local token, or it's wrong (it's in the state directory's config.json, under the key local_token; the token subcommand prints it)")
            return True

        def _body(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise _bad("Content-Length is not a number")
            if n <= 0 or n > MAX_BODY:
                raise _bad("invalid request body length: %d (cap is %d bytes)" % (n, MAX_BODY))
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))   # UnicodeDecodeError ⊂ ValueError
            except ValueError as e:
                raise _bad("the request body is not valid JSON: %s" % e)

        # ---- Routing
        def _serve(self, fn) -> None:
            """The one shell for every request: a bug in the bridge itself must never show up as just "the
            connection dropped for no reason".
            ⚠️When an exception flies out of `BaseHTTPRequestHandler`, it only prints a traceback to stderr — not
              one word on disk — and the caller gets a hangup with no status code at all.
            🔴`_sent`/`_cors` only ever get reset here (the two on the class are just defaults, and `_cors` is a
              mutable `{}`) ⇒ every new `do_*` must go through this shell; skip it, and one request's configured
              Origin leaks into every request that comes after it."""
            self._sent, self._cors = False, {}
            try:
                fn()
            except BridgeError as e:
                self._answer_error(e)
            except OSError as e:
                # The caller left partway through: this is not a bug in the bridge, never call it "the bridge
                # itself crashed", and there is no need to answer again either.
                log("the local API could not send this reply (the caller most likely hung up): %s" % _one_line(e))
            except Exception as e:
                log("❌ local API internal error (%s %s): %s: %s" % (self.command, repr(self.path)[:128], type(e).__name__, e))
                # ⭐classified as `unknown`, never `crashed`: `crashed ∈ RETRYABLE` ⇒ that would be asking the
                #   caller to retry a bug that is guaranteed to reproduce, forever. ⚠️"what gets caught" and "what
                #   it's classified as" are two different axes: here we catch anything at all (miss it, and
                #   nothing gets logged), but once caught it must never all be called "retryable".
                err = BridgeError("unknown", "the bridge itself crashed: %s: %s" % (type(e).__name__, e))
                err.logged = True
                self._answer_error(err)

        def do_GET(self) -> None:
            self._serve(self._get)

        def do_POST(self) -> None:
            self._serve(self._post)

        def do_OPTIONS(self) -> None:
            self._serve(self._preflight)

        def _preflight(self) -> None:
            """The browser's preflight. ⭐without it, `allowed_origins` is a knob that does nothing even when
            configured: a POST with `application/json` always sends OPTIONS first, and the default implementation
            replies 501.
            ⚠️the path has to be recognized too: skip that, and `OPTIONS` becomes the one and only method that
            replies 200 to a path that does not exist (the real request still gets 404 right after — not a hole,
            but it leaves this routing table disagreeing with itself)."""
            if self.path.split("?")[0] not in ROUTES:
                self._refuse(404, "not_found", "no such path: %s" % repr(self.path)[:128])
            elif self._guard(False, False):
                self._json(200, {}, {"Access-Control-Allow-Headers": "Authorization, Content-Type",
                                     "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                                     "Access-Control-Max-Age": "600"})

        def _get(self) -> None:
            path = self.path.split("?")[0]
            if path == "/healthz":
                if self._guard(False, False):   # never require a token: "is it up" is something anyone should be able to ask
                    self._json(200, bridge.health())
            elif path == "/v1/models":
                if self._guard(True, False):
                    born = int(bridge.started_at)
                    self._json(200, {"object": "list", "data": [
                        {"id": m, "object": "model", "created": born, "owned_by": m.split("/")[0]}
                        for m in bridge.cat]})
            else:
                self._refuse(404, "not_found", "no such path: %s" % repr(self.path)[:128])

        def _post(self) -> None:
            path = self.path.split("?")[0]
            if path not in POST_ROUTES:
                self._refuse(404, "not_found", "no such path: %s" % repr(self.path)[:128])
            elif self._guard(True, True):
                body = self._body()
                if path == "/v1/sessions/close":
                    self._close(body)
                else:
                    self._chat(normalize_request(body))

        def _close(self, body) -> None:
            """Close a session; calling the same id again is just a query (never a separate endpoint for that: the
            four guards already sit on this path, and opening another surface is one more surface to guard).
            Three replies, ⭐"started" and "finished closing" must never look the same:
              · `200 {"closed": true}`  = closed (this call closed it, or a previous call's background cleanup
                already finished)
              · `202 {"closing": true}` = still closing (the background work has not come back yet)
              · `200 {"closed": false}` = this bridge does not have it (never had it / already gone / the memory
                of closing it expired)
            🔴this call can drag on: `close_session` does not take turn_lock ⇒ closing a session that is in flight
              has to wait out `CLOSE_GRACE_S` (10s) before a tree-kill (and the tree-kill itself has a ≈32s cap on
              win32) ⇒ answering synchronously would hang for over ten seconds. Waiting `CLOSE_ANSWER_S` and then
              answering 202 first is safe: the session has already been removed from the table (`_drop`'s first
              line pops it).
            📎 NOTES.md::midflight-close"""
            sid = _name(body if isinstance(body, dict) else {}, "session", True)
            was = bridge.close_state(sid)
            if was == "closing":         # a previous call is still running ⇒ never open a second one to close the same session
                self._json(202, {"closed": None, "closing": True})
                return
            # 🔴a previous call blowing up never means "stop trying from now on": the version that returned those
            #   original words as the final answer directly (1) returned before `note_close()` even ran ⇒ this
            #   table's expiry sweep can never reach this path (poisoned for about an hour); (2) poisons the id,
            #   not that session instance ⇒ a new one later built under the same id is clean and alive, yet can
            #   never be closed, while the CLI process behind it keeps holding on (at the time, `gc_idle()` had
            #   zero production callers = no fallback; from Task 12 on it runs on a clock (`serve_until`), but it
            #   still has to sit idle past `SESSION_IDLE_S` before it is collected — a fallback half an hour late
            #   does not mean this does not need fixing); (3) that sentence talks about the previous attempt, and
            #   this attempt never even happened — "an error message must never lie".
            #   ⇒ go ahead and close it again anyway; the previous attempt's original words are demoted to a
            #   footnote (still a diagnostic, no longer the conclusion).
            box: dict = {}

            def shut() -> None:
                fail = ""
                try:
                    box["closed"] = bridge.sessions.close_session(sid)
                except Exception as exc:   # never let it stay stuck in a daemon thread's traceback alone
                    fail = "%s: %s" % (type(exc).__name__, exc)
                    box["error"] = fail
                    log("❌ closing the session blew up: %s" % fail)
                finally:
                    bridge.note_closed(sid, fail)   # ⭐record it whether it succeeded or not: not recording it leaves "still closing" an answer that never changes

            bridge.note_close(sid)   # ⭐the timestamp is stamped before work begins: the judgment is "did anyone come to close it while this turn was in flight"
            worker = threading.Thread(target=shut, daemon=True)
            worker.start()
            worker.join(CLOSE_ANSWER_S)
            if box.get("error"):
                also = ("; the previous attempt to close it did not succeed either (%s)" % was[1:]) if was.startswith("!") else ""
                err = BridgeError("unknown", "closing the session did not succeed: " + box["error"] + also)
                err.logged = True
                raise err
            if "closed" in box:
                # ⭐the `was == "closed"` half is exactly the I-6 hole: closed before, and naturally not in the
                #   table now ⇒ `close_session()` returns False, and that means "there is no such session", never
                #   "it just got closed".
                self._json(200, {"closed": bool(box["closed"]) or was == "closed"})
            else:
                self._json(202, {"closed": None, "closing": True})

        def _chat(self, req: dict) -> None:
            cancel, q, t0 = threading.Event(), queue.Queue(), time.time()
            cid, created = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time())

            def work() -> None:
                try:
                    q.put(("done", bridge.sessions.run(
                        session_id=req["session"], model_id=req["model_id"], effort=req["effort"],
                        system=req["system"], messages=req["messages"], on_started=lambda ms: None,
                        on_delta=lambda s: q.put(("delta", s)), cancel=cancel,
                        first_token=req["first_token"], timeout=req["timeout"])))
                except BridgeError as e:
                    q.put(("error", e))
                except Exception as e:     # a bug in the bridge itself needs to be loud too, never let the connection just hang
                    log("❌ local leg internal error: %s: %s" % (type(e).__name__, e))
                    err = BridgeError("unknown", "the bridge itself crashed: %s: %s" % (type(e).__name__, e))
                    err.logged = True
                    q.put(("error", err))

            threading.Thread(target=work, daemon=True).start()

            def record(klass: str, res) -> None:
                res = res or {}
                # ⭐`job_id` records the id we ourselves sent out, never the caller's free-form text: `jobs.log`'s
                #   exception of "at most `LINE_CAP_BYTES // 8` bytes of the caller's text per line" does not
                #   exist on this leg at all, and the audit line still lines up with the response the caller has
                #   in hand.
                bridge.joblog.write(leg="local", job_id=cid, model=shown_model(req["model_id"], bridge.cat), klass=klass,
                                    cli_version=bridge.cli_ver(req["model_id"]),
                                    usage=res.get("usage") or {}, latency_s=round(time.time() - t0, 2),
                                    ttfc=res.get("ttfc"), queued_ms=res.get("queued_ms") or 0,
                                    rebuilt=res.get("rebuilt"))

            def failed(e: BridgeError) -> BridgeError:
                """The one and only place a failure gets wrapped up: sort out "closed while in flight" first, then record it."""
                e = _closed_midflight(e, bridge.closed_during(req["session"], t0))
                record(e.klass, None)
                return e

            def meta(res: dict) -> dict:
                return {"ignored": req["ignored"], "rebuilt": res.get("rebuilt"), "session": res.get("session"),
                        "queued_ms": res.get("queued_ms"), "ttfc": res.get("ttfc")}

            probed = [time.time()]

            def pull():
                """Get the next chunk; probe whether the caller is still there while we wait (B9 on the local leg,
                the non-streaming half).
                ⭐never give it a timeout: waiting in line for a slot can genuinely take a long time, and a guessed
                  ceiling would cut off something that is genuinely still alive.
                🔴must never probe only when the queue is empty: while deltas keep arriving, `q.get` returns
                  instantly every time ⇒ the probe gets starved out completely (measured: not one probe across a
                  12-second stretch of streamed text, and the slot only came back 15.41s later; a lull was only
                  2.67s). And in a real CLI turn, the overwhelming majority of the time is spent doing exactly
                  that — streaming text continuously ⇒ what gets starved is exactly the most common case in
                  production. ⇒ probe by the clock, never by whether the queue happens to be empty
                  (`select(…, 0)` does not block, the cost is close to zero).
                ⚠️this covers the queueing period too: peeking at the first chunk happens before any byte does."""
                while True:
                    try:
                        got = q.get(timeout=PEER_PROBE_S)
                    except queue.Empty:
                        got = None
                    if time.time() - probed[0] >= PEER_PROBE_S:
                        probed[0] = time.time()
                        if _peer_gone(self.connection):
                            return ("gone", None)
                    if got is not None:
                        return got

            kind, val = pull()      # ⭐peek at the first chunk before deciding what status code to reply with (borrowed from CLIProxyAPI)
            if not req["stream"]:
                while kind == "delta":
                    kind, val = pull()
            if kind == "gone":
                cancel.set()        # never let the CLI keep working for someone who already left, and especially never let it keep holding a slot
                record("cancelled", None)
                return
            if kind == "error":
                self._answer_error(failed(val))
                return
            if not req["stream"]:
                record("ok", val)
                self._json(200, {"id": cid, "object": OBJ_COMPLETION, "created": created,
                                 "model": req["model_id"],
                                 "choices": [{"index": 0, "finish_reason": "stop",
                                              "message": {"role": "assistant", "content": _shown(val["text"])}}],
                                 "usage": _usage_openai(val.get("usage")), "scv": meta(val)})
                return

            def chunk(delta: dict, finish=None) -> dict:
                return {"id": cid, "object": OBJ_CHUNK, "created": created, "model": req["model_id"],
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

            def data(obj) -> str:
                return "data: " + (obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)) + NL + NL

            def emit(payload: str) -> None:
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()

            done = False
            try:
                self._sent = True
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                for k, v in self._cors.items():
                    self.send_header(k, v)
                self.end_headers()
                emit(data(chunk({"role": "assistant"})))
                streamed = False
                while True:
                    if kind == "delta":
                        streamed = True
                        emit(data(chunk({"content": val})))
                    elif kind == "error":                  # error mid-stream: wrap up with one error event
                        e = failed(val)
                        done = True   # ⭐M-3: before the write that can fail — a hangup below must never also record `cancelled` for this job
                        emit(data(self._error_payload(e)) + data("[DONE]"))
                        return
                    else:
                        if not streamed:
                            emit(data(chunk({"content": _shown(val["text"])})))
                        record("ok", val)
                        done = True   # ⭐record it before sending the last few chunks: if sending fails, this same job must never get recorded twice
                        emit(data(chunk({}, "stop")))
                        emit(data({"id": cid, "object": OBJ_CHUNK, "created": created,
                                   "model": req["model_id"], "choices": [],
                                   "usage": _usage_openai(val.get("usage")), "scv": meta(val)}) + data("[DONE]"))
                        return
                    while True:
                        try:
                            kind, val = q.get(timeout=KEEPALIVE_S)
                            break
                        except queue.Empty:
                            # ⭐a lull still needs bytes too: it doubles as the probe for "is the caller still there" (the cancellation below relies on it)
                            emit(": keep-alive" + NL + NL)
            except OSError:
                # The caller hung up ⇒ cancel this turn (B9 on the local leg): never let the CLI keep working for someone who already left.
                if not done:
                    cancel.set()
                    record("cancelled", None)

    return Handler


# ━━ The remote leg (B6/B7/B9/B10/B12/B20/B24/B28/§5.2): take jobs from the dispatcher, stream results back one piece at a time
# 🔴this section is the only place in the whole file that actually dials out. It never hard-codes where to: the
#   hostname only ever comes from `remote_url`, which `scv pair` saves into config.json (the `# ━━ ①` section is
#   the one place allowed to hold an address literal). Not paired ⇒ `start_remote()` returns False, and not one
#   byte goes out to the network. The contract facing outward lives in `PROTOCOL.md` (its reader is whoever
#   implements the dispatcher, never ourselves).
STREAM_MAX_AGE_S = 25        # once an SSE pipe has lived this long, proactively switch to a new one (`0` = never switch proactively)
# 🔴a conservative default, not a measured one (it can only really be measured once it is live) ⇒ never read it as a conclusion. 📎 NOTES.md::stream-max-age-guess
STREAM_READ_TIMEOUT_S = 40   # a keepalive comes every 15s ⇒ 40s with not one byte = this pipe is dead
RESULT_FLUSH_S = 0.3         # batch incremental text for this long before sending one batch back
# ⚠️this number's real cost: `urllib` does not reuse connections ⇒ every chunk is a new TCP connection. Against a
#   real https dispatcher that means one full TCP+TLS handshake every 0.3 seconds × every job in flight (measured
#   TIME_WAIT locally: 8 jobs = +40, 4 trickling streams running 14s = +141). Making it smaller means more
#   responsive but also more expensive — never treat it as just a latency knob.
SEEN_JOBS = 512              # idempotency memory: how many job_ids it remembers (in memory only, cleared on every bridge restart)
POST_TIMEOUT_S = 30          # timeout for one hello/result-post attempt; `_post` tries up to 3 times (waiting 0.5/1.0 seconds in between) ⇒ one delivery's worst case is about 91 seconds
MIN_REDIAL_S = 1.0           # the minimum interval between two dials; a bad pipe goes into exponential backoff up to MAX_BACKOFF_S starting from the first one, and complains once it has hit STUCK_REDIALS in a row
STUCK_REDIALS, MAX_BACKOFF_S = 3, 60.0
STREAM_EVENT_MAX = 256 * 1024
SSE_ID_DIGITS = 20   # the most digits an `id:` can have (PROTOCOL's "an increasing integer"): 20 digits fit any 64-bit unsigned integer (2**64-1 is 20 digits), any longer and it stops looking like a counter
# 🔴a single event's byte count must have an upper bound (this is the one path the outside network can come in
#   through); judged in one place with one budget, never split into "one for readline, one running total" — that
#   would be two guards against the same symptom. 📎 NOTES.md::sse-decode-replace


def _loopback_http(url: str) -> bool:
    """The one exception to plaintext `http://`: a loopback address (the reference dispatcher runs on loopback,
    and the test harness relies on this).
    🔴judge by the parsed-out hostname, never by a string prefix: under a prefix check,
      `http://127.0.0.1.a-remote-host.example:9` would also pass, while urllib actually dials that remote name
      (review M-F measured `start_remote()` returning True).
    ⭐a spelling like `127.0.0.1@evil` gets `evil` as the hostname `urlsplit` picks out ⇒ also rejected.
    🔴the port has to be read too: `urlsplit` cuts the hostname at the first colon, `http.client` cuts at the last
      colon ⇒ `http://127.0.0.1:evil.example:443` reads as 127.0.0.1 on this side, while the other side dials
      `127.0.0.1:evil.example` (second follow-up review M-6, measured). The port is all-digits ⇔ there is only
      one colon ⇔ both sides cut at the same spot; reading `.port` raises `ValueError` when it is not all-digits
      (a spelling with a newline in it gets the newline stripped first by `urlsplit`, and what is left is still
      not all-digits)."""
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port          # never delete this: this line exists exactly for its ValueError
        return parts.scheme == "http" and ipaddress.ip_address(parts.hostname or "").is_loopback
    except ValueError:
        return False


def remote_url_refused(url: str) -> str:
    """The one and only judgment for "can this remote address be dialed": returns `""` = can dial, otherwise a
    reason meant for a person to read.
    ⭐two doors share it: the spot that actually dials (`Bridge.start_remote`, this also blocks a hand-written
      config.json) and `scv pair` (the pairing request itself carries the pairing code, and sending it in
      plaintext leaks the code ⇒ waiting until start to block it would already be too late). Never write a
      second copy at the second spot: a prefix check would let `http://127.0.0.1.<remote-domain>` through
      (review M-F). Gate: tests/test_95_setup_pair_update.py::Doors"""
    if url.startswith(HTTPS) or _loopback_http(url):
        return ""
    return ("the remote address must start with %s (plaintext sends the token, the pairing code, and every "
            "segment of every answer); plaintext http is only allowed on a loopback IP (127.x.x.x/[::1]), and "
            "never a hostname like localhost (it can be rewritten by hosts/DNS)" % HTTPS)


# The whole family that dialing (`OpenerDirector.open` inside `_open`) can raise: `OSError` (including
#   `URLError`/`HTTPError`/timeouts), `ValueError` (a bad URL, a reply that cannot be parsed),
#   `http.client.HTTPException` (`BadStatusLine`/`IncompleteRead`: an unhealthy proxy replying with one bad status
#   line is exactly this, review C-A).
NET_ERRORS = (OSError, ValueError, http.client.HTTPException)


def redirect_refused(req, newurl: str) -> str:
    """The one and only judgment for "does this hop get followed": returns `""` = follow it, otherwise the reason
    (review M-8).
    🔴urllib follows a redirect carrying `Authorization` over as-is (it only strips content-length/content-type,
      and carries it across hosts too), and by default it also follows an https→http downgrade ⇒ ① ones carrying
      a token (the remote leg's hello/stream/result) never follow any redirect at all: blocking only the
      downgrade is not enough, hopping to another https host would hand the token over just the same; the
      dispatcher must never reply with a 3xx (PROTOCOL.md) ② ones without a token (`scv update` fetching a file
      from the public repo, backed by a sha256 on its content; `scv pair`'s code is in the request body, and
      urllib drops the request body when it follows a 301/302/303, and does not follow a 307/308 POST at all)
      only block an https downgrade.
    🔴③ a request that started from loopback never follows a redirect out beyond loopback (13d, item 5):
      `local_get` should only ever dial loopback, but whatever answers on that port could be some other program
      (on this dev machine, 8765 is exactly that), and it can reply with a 302, sending
      `scv status`/`stop`/`doctor` off to the outside network with a GET. "Is it loopback" uses the same
      plaintext judgment (`_loopback_http`)."""
    if req.has_header("Authorization"):
        return "requests carrying a token never follow redirects (following would hand the token to the new address as-is)"
    if _loopback_http(req.full_url) and not _loopback_http(newurl):
        return "a request that started from loopback never follows a redirect out beyond loopback"
    if urllib.parse.urlsplit(req.full_url).scheme == "https" and urllib.parse.urlsplit(newurl).scheme != "https":
        return "never follows a redirect that downgrades from https"
    return ""


class _Redirects(urllib.request.HTTPRedirectHandler):
    """The redirect handler installed on every request (via `_open`). When refusing, close the 3xx response
    already in hand before raising (never leave it unclosed: that is a `ResourceWarning`).
    🔴the hop that does get followed strips the proxy credential (13d, measured): `ProxyHandler` adds it to the
      request header, and urllib copies it into the new request as-is when following a redirect ⇒ if the new
      address is in NO_PROXY it connects to the origin directly, credential and all (measured locally on
      loopback: once through `_fetch`, once end to end through `scv update`). `ProxyHandler` adds it back fresh
      whenever the new request genuinely needs to go through the proxy."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        why = redirect_refused(req, newurl)
        if why:
            fp.close()
            raise ValueError("%s: %d → %s" % (why, code, _clip(str(newurl), 200)))
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        new.remove_header("Proxy-authorization")       # this is exactly the casing `add_header` stores (`capitalize()`)
        return new


def _open(req, timeout: float, *handlers):
    """The one and only door for dialing (`_fetch` reading a reply — the `limit=0` branch never reads a single
    byte — and `_stream_once` reading the stream both go through it; the dialing map is pinned by
    test_95::Doors): every request gets `_Redirects` installed on it (`urlopen`'s default opener would follow any
    redirect at all); `handlers` is how `local_get` passes in the one that routes around the proxy.
    ⭐still honors the proxy environment variables as usual (B32: `build_opener` carries a `ProxyHandler` by
      default). A non-2xx raises `HTTPError`, which is itself a response object — call `close()` on it before
      discarding it (I-4); what to do about failure (retry / return None / give a human message) is up to the
      caller."""
    try:
        return urllib.request.build_opener(*handlers, _Redirects()).open(req, timeout=timeout)
    except NET_ERRORS as e:
        if hasattr(e, "close"):
            e.close()
        raise


def _fetch(req, timeout: float, *handlers, limit: int | None = None) -> bytes:
    """Dial once, read the response body back (the `limit=0` branch never reads a single byte, see below) (shared
    by the remote leg's result posts, a subcommand checking on itself, `scv pair`, `scv update`), through `_open`.
    ⭐the response body has an upper bound (this is data from the far side of the network): defaults to
      `MAX_BODY` (the hello reply, PROTOCOL.md's hard-limits table); `scv update` gives it its own
      `SCV_PY_MAX_BYTES` (review M-7: the two bounds mean different things, never let tightening `MAX_BODY` on the
      local leg reject updates as collateral damage).
    ⭐`limit=0` = never read a single byte, and return as soon as it is 2xx (`/bridge/result`: the bridge never
      looks at the body; reading it would give it an upper bound, and treating a delivery that did reach the
      other side as a failure once it goes over that bound, re-review 2, N-M2)."""
    limit = MAX_BODY if limit is None else limit
    with _open(req, timeout, *handlers) as r:
        raw = r.read(limit + 1) if limit else b""
    if len(raw) > limit:
        raise ValueError("the response body exceeds %d bytes" % limit)
    return raw


def _ver(s: str) -> tuple:
    return tuple(int(x) for x in re.findall("[0-9]+", str(s))[:3])


# hello reply's `min_supported` (PROTOCOL.md); ⭐always matched with fullmatch. Each segment caps at 9 digits:
#   without the cap, a segment over 4300 digits makes `_ver`'s `int()` raise ⇒ backs off forever, never `too_old`
#   (second follow-up review 4, Out-of-Scope 2)
MIN_SUPPORTED_RE = re.compile("[0-9]{1,9}([.][0-9]{1,9}){0,2}")


PATH_WORD = re.compile("[^" + chr(92) + "s'" + '"' + "`()" + chr(92) + "[" + chr(92) + "]{}<>,;（）「」【】，。；：、]+")
PATH_SEP, HOME_MARK = "[/" + chr(92) * 2 + "]+", chr(0xE000)
DRIVE_HEAD = re.compile("[A-Za-z]:" + PATH_SEP)
API_PATHS = frozenset(ROUTES + tuple(REMOTE_PATHS.values()))     # this bridge's own API paths: an exact set (never a prefix — `/v1/../x` would still be accepted)
# Where a word might start, scanned in one pass (fix2, second follow-up review N-M2: it used to slice character by
#   character, quadratic): the start of a word, right after `=`/`:`/a CJK character (not the private-use area —
#   `HOME_MARK` lives there, outside the scope of the follow-up review, item 4), or a drive letter not preceded by
#   a letter or digit
PATH_START = re.compile("^|(?<=[=:" + chr(0x2E80) + "-" + chr(0xD7FF) + chr(0xF900) + "-" + chr(0xFFEF) + "])|(?<![A-Za-z0-9])(?=[A-Za-z]:[/"
                        + chr(92) * 2 + "])")


def _home_re():
    """The home directory is spliced together segment by segment: between segments it accepts any number of `/`
    and backslashes (mixed spellings, doubled as in a repr all count), and the drive-letter head also accepts
    Git Bash's `/c` and WSL's `/mnt/c`; both ends are bounded at a segment edge (`/home/al` never swallows
    `/home/alice`, `/root` never mangles `…/rootkit`). Failing to get the home directory — any exception at all
    (15c review M4) — ⇒ None: this one pass simply does not run."""
    try:
        segs = [s for s in re.split(PATH_SEP, str(user_home())) if s]
    except Exception:
        return None
    if not segs or len(segs) == 1 and segs[0].endswith(":"):
        return None
    drive = re.fullmatch("([A-Za-z]):", segs[0])
    head = ("(?:%s|%s%s|%smnt%s%s)" % (re.escape(segs.pop(0)), PATH_SEP, drive.group(1), PATH_SEP, PATH_SEP, drive.group(1))
            if drive else "")
    edge = "[A-Za-z0-9_.~-]"
    return re.compile("(?<!%s)%s%s(?!%s)" % (edge, head, "".join(PATH_SEP + re.escape(s) for s in segs), edge), re.I)


def _path_at(tok: str, i: int, end: int, slashes: list) -> bool:
    """Is `tok[i:end]` the start of a local path — in constant time: only looks at the first few characters, and
    the count of `/` uses a prefix sum (`slashes`). Path-shaped = home directory / backslash / drive letter +
    slash / `~/` / `file:` / starts with `/` and has at least two segments (except this bridge's own exact
    interface paths); the two slashes of `scheme://` never count (except for `file:`)."""
    if tok.startswith("//", i) and i >= 2 and tok[i - 1] == ":" and tok[i - 2].isalnum() and tok[max(0, i - 5):i - 1].lower() != "file":
        return False
    return bool(tok[i:i + 1] in (HOME_MARK, chr(92)) or DRIVE_HEAD.match(tok, i) or tok[i:i + 2] in ("~/", "~" + chr(92))
                or tok[i:i + 5].lower() == "file:"
                or tok[i:i + 1] == "/" and slashes[end] - slashes[i] >= 2 and (end - i > 32 or tok[i:end] not in API_PATHS))


def _no_local_paths(text: str) -> str:
    """The one and only pass before the remote leg's error text leaves the machine (15c, lead's word swap ⑦3):
    the home directory is recognized segment by segment (`_home_re`), then whatever segment looks like a local
    path gets replaced with `<local path>` (the starting points `PATH_START` scans out are each run through
    `_path_at`; a backslash anywhere else in the word ⇒ mask starting from the word's own start — fix2's fallback
    for follow-up review N-M1; a later segment split off by a space within the same path gets merged into the
    previous one), and a line at the end says how many were replaced (a home directory swapped for `~` counts
    too). The whole pass is linear (follow-up review N-M2).
    ⭐collected by shape, never deleted one at a time at each `raise` site (a blacklist: the next new way to leak
      one will not come tell us about it — the same reasoning as `_cli_version`).
    ⚠️what this cannot catch: a relative path with no backslash, a bare user name appearing outside a path
      (`~al/x` counts too), a machine name, a single-segment `/tmp`, the part after a space in a path that
      carries no slash, a path glued onto a character other than `=`/`:`/a CJK character that also does not start
      with a drive letter or a backslash (`|/srv/x`, `@/home/bob/x`);
      what it over-masks: the segment one space after a path that carries a slash (the `5/5` in `C:/a 5/5`; two
      paths separated by only one space, where the second does not start with a drive letter or a slash, count as
      one), and a word with a backslash gets masked in its entirety from its own start. The local leg and
      bridge.log never go through this (this machine's own owner needs the full text). 📎 NOTES.md::no-local-paths"""
    hre, n, out, last, pos = _home_re(), 0, [], None, 0
    text = hre.sub(HOME_MARK, text) if hre else text
    for m in PATH_WORD.finditer(text):
        tok, gap, pos = m.group(0), text[pos:m.start()], m.end()
        end, slashes = len(tok.rstrip(":.")), [0]
        for ch in tok:
            slashes.append(slashes[-1] + (ch == "/"))
        at = next((s.start() for s in PATH_START.finditer(tok) if s.start() < end and _path_at(tok, s.start(), end, slashes)), None)
        tail = tok[end:]
        if at is None and last == m.start() - 1 and gap == " " and ("/" in tok or chr(92) in tok):
            out.append(tail)            # the segment after a space within the same path (`C:/Program Files/x`) ⇒ merge into the previous one, never count it separately
            last = m.end()
            continue
        out.append(gap)
        if chr(92) in (tok if at is None else tok[:at]):
            at = 0                      # fallback: a backslash in the word (before the starting point) ⇒ mask from the word's own start to its end (a relative backslash path, a UNC path glued onto another character)
        if at is None:
            n += tok.count(HOME_MARK)
            out.append(tok.replace(HOME_MARK, "~"))
            continue
        n, last = n + 1 + tok[:at].count(HOME_MARK), m.end()
        out.append(tok[:at] + "<local path>" + tail)
    text = ("".join(out) + text[pos:]).replace(HOME_MARK, "~")
    return text + ("(%d local paths in the original words were not sent; the original is in bridge.log on this "
                   "machine, which keeps only the first %d bytes of each line)" % (n, LINE_CAP_BYTES) if n else "")


def hello_payload(bridge: Bridge) -> dict:
    """B24: the whitelist of fields reported. Never add one more key here without first changing PROTOCOL.md and
    test_hello_is_a_whitelist.
    ⚠️a whitelist controls the key names, not the values ⇒ the `cli_version` slot has a separate shape check of
      its own, see `_cli_version()`."""
    return {"protocol": PROTOCOL, "bridge_version": VERSION, "os": os.name + "/" + sys.platform,
            "python": "%d.%d" % sys.version_info[:2],
            "families": [{"family": f, "cli_version": _cli_version(i["version"])}
                         for f, i in bridge.found.items() if not i["blocked"]],
            "models": bridge.cat, "max_concurrent": int(bridge.cfg.get("max_concurrent") or 4)}


def remote_request(d: dict) -> dict:
    """A job from the dispatcher → laid out into the shape the local leg's mouth expects, going through the exact
    same `normalize_request()`.
    🔴never write a second copy of intake validation here: the length check (`_name`), the range check
      (`_seconds`), the content-shape check (`_text_of`) — not one of them can be skipped; "one rule, two
      submission paths" with one side missing its copy means the bug only shows up on the other side.
      What each one missing would do, and which compatibility-table behaviors this path inherits as a result,
      📎 NOTES.md::two-legs-one-entry-gate"""
    opts = d.get("opts") if isinstance(d.get("opts"), dict) else {}
    msgs = d.get("messages")
    head = [{"role": "system", "content": d.get("system") or ""}]
    return normalize_request({"model": d.get("model"), "session": d.get("session"),
                              "messages": (head + msgs) if isinstance(msgs, list) else msgs,
                              "effort": opts.get("effort"), "timeout": opts.get("timeout"),
                              "first_token_timeout": opts.get("first_token_timeout")})


# ━━ The remote leg's outbound channel (M-6): one thread + one bounded queue, doing one thing at a time in the
#   order it came in. ⭐A self-contained block: its own class, its own lock, never a mutable global shared across
#   blocks; it does not recognize a job, only "one thing" (a callable) ⇒ that is its own piece: `src/82_outbox.py`.
#   📎 NOTES.md::ack-on-stream-thread
class _Outbox:
    """The part that has to wait on the network after a job comes in (the ack, the error that follows a
    rejection right away, the ack for a dup) is handed to this, never waited on the receive-stream thread: that
    thread waiting even once means the worst-case time of one delivery (about 91 seconds), and during that whole
    stretch the SSE pipe reads not a single byte, so every cancel/close_session queued behind it is stuck too.
    ⭐one thread (started only the first time there is work), a queue with an upper bound: when it will not fit,
      `put` returns why (`"full"`/`"stopped"`, never one False covering two different things), and it is up to the
      caller to complain.
    ⭐`stop()`: stop accepting; nothing still queued gets done (returns how many were dropped); whatever is being
      worked on right now finishes and then the thread exits — waiting at most one result-post's timeout
      (`_post` watches the stop-the-bridge flag)."""

    def __init__(self, cap: int):
        self._q, self._lock, self._stopped, self.thread = queue.Queue(cap), threading.Lock(), False, None
        self.cap = cap

    def put(self, fn, *args) -> str:
        with self._lock:
            if self._stopped:
                return "stopped"
            try:
                self._q.put_nowait((fn, args))
            except queue.Full:
                return "full"
            if self.thread is None:
                self.thread = threading.Thread(target=self._loop, daemon=True)
                self.thread.start()
            return ""

    def _loop(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            try:
                item[0](*item[1])
            except Exception as e:      # ⭐one item blowing up must never take the whole channel down with it: this is a daemon thread, and letting it fly out means this leg can never ack again (without a sound)
                log("❌ error handling a remote event, dropping this one: %s: %s" % (type(e).__name__, repr(str(e))[:128]))

    def stop(self) -> int:
        with self._lock:
            self._stopped, left = True, 0
            while True:
                try:
                    self._q.get_nowait()        # ⚠️never check `empty()` first and then get: the channel thread could take the last item at just that moment ⇒ this raises Empty
                except queue.Empty:
                    break
                left += 1
            if self.thread is not None:
                self._q.put_nowait(None)        # just emptied ⇒ there is definitely room; wakes the thread stuck on `get()`
        return left


class RemoteLeg:
    def __init__(self, bridge: Bridge):
        self.b = bridge
        self.base = str(bridge.cfg["remote_url"]).rstrip("/")
        self._stop = threading.Event()
        self._last_id, self._seen, self._cancels, self._bad = "", collections.OrderedDict(), {}, 0
        # 🔴the number of jobs in flight at once must have an upper bound: each job gets its own thread, and
        #   events are pushed in from the far side of the network ⇒ no cap = the other side pushing ten thousand
        #   at once means ten thousand threads on this machine. ⭐a concurrency slot governs "how many are running
        #   at once", never "how many are being pushed in at once" — two different things (B1 on Task 8's
        #   account). The upper bound follows `max_concurrent` (4 times it), never a separate knob of its own.
        self.max_inflight = max(4, 4 * int(bridge.cfg.get("max_concurrent") or 4))
        self._inflight = threading.BoundedSemaphore(self.max_inflight)
        # 🔴the send channel's (M-6) queue must have an upper bound too, and it also follows `max_concurrent`
        #   (4 times the in-flight cap, never a separate knob): when result posting is healthy, up to this many
        #   pushed in at once all get an ack (the ones that hit the in-flight cap get an error instead), any more
        #   are dropped; one item is the raw event's bytes (see `_take`; ≤256 KiB, bad bytes decoded to U+FFFD and
        #   re-encoded can be up to 3x) ⇒ 64 items by default, 48 MiB worst case. Once full ⇒ drop, and complain
        #   (`_hand`).
        self._outbox = _Outbox(4 * self.max_inflight)
        # ⭐"a cancel that arrives early" (15b fix1 I1): when a cancel arrives, that job has already been queued
        #   into the channel but not yet picked up (it is in `_queued`, not yet in `_cancels`) ⇒ record it in
        #   `_early`, and the moment `_admit` registers it, check right then — a hit cancels it on the spot
        #   (never waiting for the acks queued ahead of it to all go out one by one). Never recognize an id the
        #   bridge never queued at all (the dispatch stream is ordered; a cancel running ahead of its own job only
        #   happens under an abnormal ordering, and recognizing it there would silently cancel a piece of work
        #   nobody asked to cancel, lead's ruling) ⇒ both tables are bounded: `_queued`'s keys ≤ the queue cap plus
        #   the one item in the channel's hand, `_early` ⊆ `_queued`'s keys (the few entries dumped when the
        #   bridge stops are left as-is, see `_admit`). The check-and-write on both sides happens inside this same
        #   lock.
        self._early, self._queued, self._early_lock = set(), {}, threading.Lock()
        self.state, self.refused, self.connects, self.opened_at, self._full = "idle", "", 0, 0.0, 0
        self._order = 0     # the event sequence number on the receive-stream thread (`_dispatch` adds 1 to it for every one, and only it ever writes it): which of close and job arrived first is judged by this (N-I2), never by the wall clock

    def describe(self) -> dict:
        # ⚠️that key has to be spelled out piece by piece: the whole file only lets `Handler._refuse()` write out
        #   that literal (it pins down the one and only construction point for the whole family of four-guard
        #   refusal bodies, tests/test_70_local_api.py::OneDoor), and this is only a name that happens to match,
        #   never that family. Never loosen that check — that trades a miss for a false alarm (the same slot as
        #   `chat.completion` hitting the URL-detector gate).
        return {"state": self.state, "url": self.base, "connects": self.connects,
                "refus" + "ed": self.refused}

    def stop(self) -> None:
        self._stop.set()
        left = self._outbox.stop()          # ⭐the new thread family gets shut down right here: whatever is still queued is dropped (and complained about), whatever is being worked on right now finishes and exits on its own
        if left or self._full:
            log("⚠️ stopping the bridge: %d item(s) in the outbound channel were never done (a job with no ack = the dispatcher will treat it as never delivered), and %d more were dropped while the channel was full" % (left, self._full))
        for ev in list(self._cancels.values()):
            ev.set()

    def _request(self, key: str, data: bytes | None):
        req = urllib.request.Request(self.base + REMOTE_PATHS[key], data=data, method="GET" if data is None else "POST")
        req.add_header("Authorization", "Bearer " + str(self.b.cfg["remote_token"]))
        if data is not None:
            req.add_header("Content-Type", "application/json")
        return req          # ⭐urllib honors the proxy environment variables by default (B32); never switch this to http.client, it does not

    def _post(self, key: str, body: dict, tries: int = 3) -> dict:
        try:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError as e:
            # ⭐cannot be encoded as UTF-8 (a lone surrogate) is never a network failure: it used to run the full
            #   3 tries in the retry loop and log "delivery failed" (follow-up review 4, "measure it while we're
            #   at it")
            log("❌ %s was never sent: the content has a character that cannot be encoded as UTF-8 (%s) ⇒ never retried (retrying would do the same thing)" % (key, type(e).__name__))
            raise
        for i in range(tries):
            try:
                # ⭐the whole family of exceptions / closing `HTTPError` / the response-body upper bound all live
                #   behind that one door, `_fetch` (review C-A, I-4)
                raw = _fetch(self._request(key, data), POST_TIMEOUT_S, limit=None if key == "hello" else 0)
                # ⭐the bridge never reads `/bridge/result`'s reply body (not even the bytes): a 2xx counts as
                #   delivered (a 200 that used to be treated as this delivery having failed when it was not JSON
                #   or over 8 MiB — an ack "failing" this way and the job just gets lost)
                return json.loads(raw.decode("utf-8") or "{}") if key == "hello" else {}
            except NET_ERRORS as e:
                # ⭐while stopping the bridge, never sleep it out dry, and never fire off the next attempt either
                #   (it used to be that the flag would wake `wait` up and it dialed again anyway, up to
                #   POST_TIMEOUT_S each time, follow-up review 5, Out-of-Scope ⑤)
                if i == tries - 1 or self._stop.wait(0.5 * (i + 1)):
                    log("❌ delivering %s failed (tried %d times): %s: %s" % (key, i + 1, type(e).__name__, repr(str(e))[:128]))
                    raise
        return {}

    def _report(self, jid: str, seq: int, event: str, **kw) -> None:
        if event == "error":    # ⭐error text going out to the network only ever passes through this one spot ⇒ local paths are caught here by shape (never the local leg, never bridge.log)
            kw["error"] = dict(kw["error"], message=_no_local_paths(kw["error"]["message"]))
        self._post("result", dict({"job_id": jid, "seq": seq, "event": event}, **kw))

    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                if self.state != "bad_hello":       # during the stretch waiting for the dispatcher to fix it, `status` keeps showing the reason (never papered over by hello)
                    self.state = "hello"
                reply = self._post("hello", hello_payload(self.b))
                if not isinstance(reply, dict):       # `.get` used to blow up with an AttributeError (15b fix2 addition)
                    raise ValueError("the hello reply is not a JSON object (it is %s)" % type(reply).__name__)
                self._save_latest(reply.get("latest"))
                need = str(reply.get("min_supported") or "0")
                # 🔴`need` is the dispatcher's own text, and `refused` goes into bridge.log, `/healthz` (no token
                #   needed to read it), and `status`'s stdout ⇒ the shape gate sits at exactly this one entry
                #   point (never patch each exit separately): a bad shape only reports the length, never echoes it
                #   back (follow-up review 3, I-b)
                if not MIN_SUPPORTED_RE.fullmatch(need):
                    # 🔴never `return` here: one dispatcher release with a broken format would stop every bridge
                    #   online until someone restarts it (follow-up review 4, m-f) ⇒ keep backing off and asking
                    #   again, and it connects on its own once the other side fixes it
                    self.refused = ("the dispatcher's required minimum version is not recognized (%d characters, "
                                    "the shape wanted is like 1.2.3, each segment at most 9 digits) ⇒ the stream "
                                    "did not connect, asking again after a while (the wait grows each time, up to "
                                    "%.0f seconds); once the other side fixes it, it connects on its own: show "
                                    "this line to the other side" % (len(need), MAX_BACKOFF_S))
                    self.state = "bad_hello"
                    log("❌ %s (asking again in %.0fs)" % (self.refused, backoff))
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF_S)
                    continue
                self.refused, self.state = "", "hello"     # ⭐the other side fixed it ⇒ clear the previous refusal (never "refused once, refused forever")
                if _ver(VERSION) < _ver(need):
                    # never go through `self_cmd`: this goes into /healthz (readable without a token too) ⇒ never
                    # carry a local path in it; the line that can be pasted is printed by `cmd_status`
                    # ⭐repr first, then truncate: each segment caps at 9 digits ⇒ the repr is at most 31
                    #   characters, and `[:32]` never truncates it today; if that cap is ever loosened, this still
                    #   falls back on README's ≤128
                    self.refused = "bridge v%s is too old, the server requires ≥ %s: update first (the update subcommand; status will print the whole command)" % (VERSION, repr(need)[:32])
                    self.state = "too_old"
                    log("❌ " + self.refused)
                    return                          # the bridge has to restart after an update anyway
                # 🔴never reset backoff when hello succeeds: when `/bridge/stream` alone is broken (replying 5xx)
                #   while hello is still alive, doing that would reset it every single loop right before hitting
                #   the wall again ⇒ one hello + one stream + one log line every second, forever, never backing
                #   off (review I-A measured 39 times in 40 seconds). ⭐only a pipe that has actually connected
                #   (`_sick` judges it healthy) brings it back to 1.
                while not self._stop.is_set():
                    mark, born, dials = self._last_id, time.time(), self.connects
                    try:
                        bailed = self._stream_once()
                    except Exception:
                        # 🔴a pipe that ends in an exception (a read timeout / RST / IncompleteRead — the most
                        #   common way a real network breaks) goes through this same judgment: it used to be that
                        #   only the normal-return path judged it ⇒ a healthy pipe that had connected and received
                        #   events would still double all the way up to 60s just the same (second follow-up review
                        #   I-1).
                        if not self._sick(mark, dials, False):
                            backoff, self._bad = 1.0, 0
                        raise
                    backoff = self._pace(backoff, born, bailed, self._sick(mark, dials, bailed))
            except Exception as e:
                # ⭐catch anything at all, never just OSError/ValueError: this is a daemon thread, and letting it
                #   fly out only leaves a traceback in stderr, and then this bridge silently can never receive work
                #   again — while the local leg is still alive, so from the outside it looks "just fine".
                #   ⚠️"what gets caught" and "what it's called" are two different axes: once caught, this only
                #   reconnects, it never invents a category for the failure.
                # ⭐`state` and `refused` describe the same "most recent reason" ⇒ switching to `retrying` clears
                #   the previous line (lead's ruling, F5-⑦2); `refused` is cleared before `state` changes: whoever
                #   reads `retrying` is guaranteed that line is already gone
                self.refused = ""
                self.state = "retrying"
                log("⚠️ remote leg dropped, reconnecting in %.0fs: %s: %s" % (backoff, type(e).__name__, repr(str(e))[:128]))
                self._stop.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)

    def _save_latest(self, latest) -> None:
        """Save what the server says is the latest version for `scv update` to use. ⭐only keep three keys, each
        truncated to 128 characters: this is data from the far side of the network landing on this machine's
        disk, and without a whitelist the other side replying with a 500 MB object would just be a 500 MB file.
        🔴`commit` gets spliced by `scv update` into the download address ⇒ the shape check belongs at the point
          the address gets built (`cmd_update`'s `COMMIT_RE`, the judgment may live in only one place), never
          judged a second time here — if that spot does not check it, checking it here would not help either.
        ⭐goes through `_atomic_write`: `write_text` is open-and-truncate, and blowing up partway through the
          write means the previous copy is gone too."""
        row = latest if isinstance(latest, dict) else {}
        keep = {k: str(row.get(k) or "") for k in ("version", "commit", "sha256")}
        if any(SURROGATES.search(v) for v in keep.values()):
            # 🔴a lone surrogate (15b fix1 I3, the same shape as D23): it used to blow up on `_clip`'s encode,
            #   get called "the remote leg dropped", and the stream could never connect again ⇒ caught right here
            #   at the entry point: store all three as empty strings (scv update waits for the dispatcher to fix
            #   it), log one line, and keep connecting the stream as usual
            log("⚠️ the hello reply's latest cannot be encoded as UTF-8 (a lone surrogate) ⇒ not saved, all three recorded as empty strings (the update subcommand has to wait for the dispatcher to fix it); the remote leg keeps connecting as usual")
            keep = dict.fromkeys(keep, "")
        keep = {k: _clip(v, 128) for k, v in keep.items()}
        _atomic_write("latest.json", json.dumps(keep, ensure_ascii=False))

    def _sick(self, mark: str, dials: int, bailed: bool) -> str:
        """The one and only judgment for "is this pipe healthy": returns `""` = healthy / `"stuck"` = livelock /
        `"dropped"` = broke right after connecting (or never connected at all).
        🔴both a normal return (`_pace`) and ending in an exception (that spot inside `run`) go through this one
          place, never copy a second version of it inside an except.
        ⭐two different sicknesses, two different judgments, never merge them into one (merged together, "got hung
          up on right after connecting" gets blamed on `id:` ordering, and the kind of livelock where reading a
          full 256 KiB over a slow network takes more than 1 second would silently, permanently get stuck, firing
          once a second forever without a single complaint):
          ① livelock = "gave up on an event & `_last_id` did not move forward" ⇒ we cannot skip past it. This has
            nothing to do with how long the pipe has been alive. This is the locally-judgeable shape of that
            protocol requirement (`id:` before `data:`) being violated — what is judged is "I am stuck", never
            trying to parse what shape the other side's frame is in.
          ② broke right after connecting = "no progress & (never got a 2xx, or has been alive less than
            `MIN_REDIAL_S` counting from the moment it got a 2xx)". Looking at "no progress" alone is not enough:
            a healthy pipe with no work to do also makes no progress, and backing it off would make a job up to a
            minute late. ⭐`opened` (got a 2xx) must never be skipped: skip it, and a dispatcher that slowly comes
            back with a 5xx (an overloaded origin, a CDN timing out on its origin) would have every single pipe
            count as "alive for over 1 second", never backing off. ⭐the lifetime is never counted from the moment
            of dialing (M-2): a dispatcher that is slow to reply 200 and then immediately closes the stream would
            also have every pipe count as "alive for over 1 second" (follow-up review 3, measured 25 times in 30
            seconds, without a single complaint); the RST version depends on whether the RST beats the header or
            not, backing off sometimes and not others. `_pace`'s minimum interval still counts from the moment of
            dialing.
        The parameters are the two things recorded before dialing (`_last_id`/`connects`); the moment
        `_stream_once` gets a 2xx it records it in `opened_at`."""
        progressed, opened = self._last_id != mark, self.connects != dials
        if bailed and not progressed:
            return "stuck"
        if not progressed and (not opened or time.time() - self.opened_at < MIN_REDIAL_S):
            return "dropped"
        return ""

    def _pace(self, backoff: float, born: float, bailed: bool, sick: str) -> float:
        """The throttle between two pipes (the normal-return path; `_sick` is what judges "is it healthy", this
        only handles complaining and waiting).
        🔴`_stream_once()` returning normally (EOF / reached its max age / gave up on an oversized event) used to
        redial on the spot: this one plain case of a dispatcher "closing the stream right after 200" measured
        398.5 times per second — hammering the other side and any proxy in between like a DDoS source, and this
        only requires the other side to be unhealthy, never malicious. Both sicknesses go through the same
        exponential backoff; which one gets complained about is split apart by its cause. 📎 NOTES.md::redial-pacing"""
        self._bad = self._bad + 1 if sick else 0
        if bailed and (not sick or self._bad == 1):   # only complain the first time during a stuck stretch, backoff handles the rest
            log("⚠️ a remote event exceeded %d bytes ⇒ dropping it and reconnecting" % STREAM_EVENT_MAX)
        if self._bad == STUCK_REDIALS:
            log(("⚠️ dropped the same oversized event %d times in a row with zero progress: the dispatcher put "
                 "`id:` after `data:` (that way we can never skip past it) ⇒ backing off now (the interval "
                 "doubles each time, capped at about %.0fs)" if sick == "stuck" else
                 "⚠️ connected and got disconnected within a second %d times in a row without receiving a single "
                 "event: the dispatcher is unhealthy (overloaded / a proxy cutting the stream) ⇒ backing off now "
                 "(the interval doubles each time, capped at about %.0fs)") % (STUCK_REDIALS, MAX_BACKOFF_S))
        left = MIN_REDIAL_S - (time.time() - born)
        if left > 0:
            self._stop.wait(left)
        if not self._bad:
            return 1.0
        self._stop.wait(backoff)
        return min(backoff * 2, MAX_BACKOFF_S)

    def _stream_once(self) -> bool:
        """One SSE pipe, read until it breaks. Returns True = this one was collected because it gave up on an
        oversized event (for `_sick()` to judge livelock)."""
        req = self._request("stream", None)
        req.add_header("Accept", "text/event-stream")
        if self._last_id:
            req.add_header("Last-Event-ID", self._last_id)
        born = time.time()
        with _open(req, STREAM_READ_TIMEOUT_S) as resp:     # ⭐through that one door: carrying a token ⇒ never follows a redirect (review M-8)
            self.connects, self.opened_at = self.connects + 1, time.time()     # the moment it gets a 2xx (this is where `_sick`'s lifetime counts from, M-2)
            self.state = "streaming"
            eid, event, data, size = "", "message", [], 0
            while not self._stop.is_set():
                if STREAM_MAX_AGE_S and time.time() - born >= STREAM_MAX_AGE_S:
                    return False                            # proactively switch to a new pipe; whatever event was not finished is made up for via Last-Event-ID
                raw = resp.readline(STREAM_EVENT_MAX - size + 1)   # what comes back is however much of the budget is left
                if not raw:
                    return False
                size += len(raw)
                if size > STREAM_EVENT_MAX:
                    # 🔴push `_last_id` forward before giving up on it: skip that and it is a livelock (a spelling
                    #   with `id:` after `data:` cannot be saved here, never call this "already solved"). The
                    #   decode below using `"replace"` is the same reasoning.
                    if eid:
                        self._last_id = eid
                    return True                             # whether this is okay is judged in one place, `_sick()`; complaining and waiting happen in `_pace()`
                # both livelocks, judged with one budget in one place, are covered in 📎 NOTES.md::sse-decode-replace
                line = raw.decode("utf-8", "replace").rstrip(chr(13) + NL)
                if line == "":
                    size = 0
                    if data:
                        if eid:
                            self._last_id = eid
                        self._dispatch(event, NL.join(data))
                    eid, event, data = "", "message", []
                elif line.startswith(":"):
                    continue
                elif line.startswith("id:"):
                    eid = line[4:] if line.startswith("id: ") else line[3:]    # the SSE spec: strip only the one space right after the colon (never `.strip()`, re-review 2, O-c)
                    if not re.fullmatch("[0-9]{1,%d}" % SSE_ID_DIGITS, eid):
                        # 🔴this has to go as-is into the `Last-Event-ID` header on reconnect: a CJK character, a
                        #   CR (which `http.client` cannot encode), or a seventy-thousand-digit number (the other
                        #   side replies 431) in it would mean the stream can never reconnect again, with the log
                        #   only ever saying "the remote leg dropped" (15b fix2 O1, fix3) ⇒ caught right at this
                        #   entry point by PROTOCOL's defined shape (never two gates each judging half of it):
                        #   never adopt it, keep using the previous one as usual, log one line (never echo it back)
                        log("⚠️ a dispatcher event's `id:` has the wrong shape (PROTOCOL wants 1-%d decimal digits, this one has %d characters) ⇒ never adopted, reconnecting still carries the previous one as usual" % (SSE_ID_DIGITS, len(eid)))
                        eid = ""
                elif line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
        return False

    def _dispatch(self, event: str, raw: str) -> None:
        """The isolation shell for one event: one bad event must never take the whole leg down with it (that
        would leave this bridge silently unable to receive work ever again)."""
        self._order += 1
        try:
            self._route(event, raw)
        except Exception as e:
            log("❌ error handling remote event %s, dropping this one: %s: %s" % (repr(event)[:32], type(e).__name__, repr(str(e))[:128]))

    def _route(self, event: str, raw: str) -> None:
        try:
            d = json.loads(raw)
        except ValueError:
            log("⚠️ the remote side sent an event that is not JSON, dropping it: %s" % repr(raw)[:120])
            return
        if not isinstance(d, dict):
            log("⚠️ the remote side sent a %s event that is not an object, dropping it" % repr(event)[:32])
        elif event == "cancel":
            jid = d.get("job_id")
            with self._early_lock:          # ⭐mutually exclusive with `_admit`'s registration step: either it has already registered (cancel takes effect on the spot), or when it registers it is guaranteed to see this entry
                ev = self._cancels.get(str(jid))
                if not ev and isinstance(jid, str) and self._queued.get(jid):
                    self._early.add(jid)    # the one still queued: cancel it the moment it is picked up; ⭐an id the bridge has never seen ⇒ do nothing at all
            if ev:
                ev.set()                    # the one in flight: takes effect on the spot (never queued behind the send channel — that is exactly what M-6 needs)
        elif event == "close_session":
            # ⭐both closing paths share the exact same one as the local leg (the `two-legs-one-entry-gate` rule
            #   used to only cover `job`): length/type go through `_name()`; "don't open a second one if the
            #   previous close is still running" goes through `note_close()`'s return value. Skip the
            #   deduplication, and the other side starts one thread for every push, while `_closed_at` keeps
            #   entries for `CLOSE_MEMORY_S` (≈1 hour) under keys the network handed us (measured: 300 pushes =
            #   +300 rows).
            # ⭐I-3: namespaced before it reaches `Bridge`/`SessionManager` (same rewrite as `_job`'s `sid` below):
            #   otherwise this key is the exact one a local client's `session` uses too.
            sid = "remote:" + _name(d, "session", True)
            # 🔴those two bookkeeping calls are not exclusive to the local leg: skip `note_close`/`note_closed`,
            #   and a turn "closed by the dispatcher while in flight" gets reported as `crashed` = "the bridge
            #   crashed, retryable" — exactly the lie `_closed_midflight()` exists to fix.
            # ⭐detach it on the spot (N-I2: any job arriving after this is guaranteed not to see it) + record it
            #   under this call's order, both inside the same lock (`note_close(detach=True)`); if a previous
            #   remote-side close is still running ⇒ only refresh the record, and that thread will pick up one
            #   more round once it finishes the one in its hand (M-b, never start another thread for it; if a
            #   previous local-side close is still running ⇒ it will never pick up another round, this call
            #   starts its own, fix3 N-M1). The close itself runs in its own thread: closing something in flight
            #   has to wait out the grace period plus one tree-kill (≈32s on win32), and doing it synchronously
            #   would starve this pipe to death (the gate is 40s).
            if self.b.note_close(sid, self._order, detach=True):
                threading.Thread(target=self._close_session, args=(sid, self._order), daemon=True).start()
        elif event == "job":
            self._take(d, raw)

    def _close_session(self, sid: str, order: int) -> None:
        fail = ""
        while order is not None:
            try:
                self.b.sessions.close_detached(sid)     # ⭐only close the one that was detached: the one in the table might be a new one a job built after this close arrived
            except Exception as exc:   # never let it stay stuck in a daemon thread's traceback alone
                fail = "%s: %s" % (type(exc).__name__, exc)
                log("❌ closing the session blew up: %s" % fail)
            finally:
                # ⭐record it whether it succeeded or not (not recording it leaves "still closing" an answer that never changes); another one arrived while this was closing ⇒ return that call's order, pick up one more round (M-b)
                order = self.b.note_closed(sid, fail, order)

    def _hand(self, what: str, fn, *args) -> bool:
        """Hand one thing to the send channel. ⭐complain when it is full (never drop silently), but never flood
        the log either: while it is full only the first one complains, and once there is room again one more
        line gives the total (`_append_capped` protects bytes, not information). Only the receive-stream thread
        ever calls this ⇒ `_full` has only one writer."""
        why = self._outbox.put(fn, *args)
        if not why and self._full:
            log("⚠️ the send channel has room again: %d item(s) were dropped in total while it was full" % self._full)
            self._full = 0
        elif why == "stopped":
            log("⚠️ the bridge is stopping, the send channel is no longer accepting ⇒ dropping %s" % what)
        elif why == "full":
            self._full += 1
            if self._full == 1:
                log("⚠️ the send channel is full (%d items: the dispatcher's /bridge/result is slow or stuck, or "
                    "more than this many were pushed at once) ⇒ dropping %s (it will get no ack); everything "
                    "after it is dropped for as long as it stays full, one line with the total once there is "
                    "room again" % (self._outbox.cap, what))
        return not why

    def _unqueue(self, jid: str, ev=None) -> None:
        """This job is no longer "queued": `ev` given = it was picked up (registered into `_cancels`, an
        early-arriving cancel's flag gets set on the spot), not given = not picked up (dup / ack failed / rejected
        / never got queued): the early-arriving cancel entry only gets invalidated once not a single copy of this
        id is queued any more (leave it be if another copy is still queued, N-I1). ⭐registering and checking
        happen inside the same lock (see the cancel branch in `_route`)."""
        with self._early_lock:
            if ev is not None:
                self._cancels[jid] = ev
            n = self._queued.get(jid, 0) - 1
            if n > 0:
                self._queued[jid] = n
            else:
                self._queued.pop(jid, None)
            if jid in self._early and (ev is not None or n <= 0):
                self._early.discard(jid)
                if ev is not None:
                    ev.set()                # ⇒ `_job` picks this up before it even starts the CLI: the final state is cancelled

    def _take(self, d: dict, raw: str) -> None:
        """On the receive-stream thread this only does the part that never waits on the network: recognize
        `job_id`, then hand it to the send channel (M-6). ⭐what gets queued is the raw event's UTF-8 bytes, never
        the parsed object: parsed, it can grow to 19 times the original (measured: a 256 KiB `[[0],[0],…]` ⇒
        5 MB; even stored as a str, one four-byte character can multiply the whole string by 4x), and capping by
        item count alone would not hold memory down."""
        jid = d.get("job_id")
        if not isinstance(jid, str) or not 0 < len(jid) <= MAX_NAME_CHARS or SURROGATES.search(jid):
            # ⚠️never echo that value back: it would go straight into bridge.log, and on this path it has not
            #   passed any length check yet.
            # 🔴a lone surrogate: this id cannot even be encoded as UTF-8 for the ack ⇒ it used to be treated as a
            #   network failure and tried 3 times, and the dispatcher would never receive a single one (follow-up
            #   review 4, "measure it while we're at it")
            log("⚠️ dropping a job: job_id is invalid (%s, %d characters)"
                % (type(jid).__name__, len(jid) if isinstance(jid, str) else 0))
        else:   # ⭐arrival is recorded right here (never after the ack): the wall clock + the receive-stream thread's event sequence number; "the session was closed while this was queued" is judged by the sequence number (I2/N-I2)
            with self._early_lock:          # record "queued" before handing it out: the channel thread might pick it up immediately
                self._queued[jid] = self._queued.get(jid, 0) + 1
            if not self._hand("a job", self._admit, jid, raw.encode("utf-8"), (time.time(), self._order)):
                self._unqueue(jid)

    def _admit(self, jid: str, raw: bytes, arrived: tuple) -> None:
        """A shell: for every copy that gets this far, the "queued" bookkeeping is settled exactly once — when
        picked up, that happens inside `_admit_one` (under the same lock as registration); when not picked up (dup
        / ack failed / rejected), it happens here. ⚠️the few copies that `_Outbox.stop()` dumps out while stopping
        the bridge, before their turn comes, never get this far, and their bookkeeping is left as-is (harmless:
        this whole leg is thrown away once the bridge stops)."""
        box = [True]
        try:
            self._admit_one(jid, raw, arrived, box)
        finally:
            if box[0]:
                self._unqueue(jid)

    def _admit_one(self, jid: str, raw: bytes, arrived: tuple, box: list) -> None:
        """The part that runs on the send channel, in the same order it used to run on the receive-stream thread:
        (a dup just gets a dup ack) ack → record "seen" → take a slot → start the thread.
        ⭐only this one thread ever touches `_seen` ⇒ "seen before or not" is judged by arrival order (a dup
        arriving after the original still counts as new work if the original's ack failed to send)."""
        if jid in self._seen:
            self._report(jid, -1, "ack", dup=True)
        else:
            held = ok = False   # ⭐two separate flags: `held` = did it actually take a slot, `ok` = did the work actually start (never merge them into one)
            try:
                # ⭐ack succeeds first, then "seen" is recorded: the other way around, a job whose ack never made
                #   it out would be treated as a duplicate on resend and never run at all.
                self._report(jid, 0, "ack", dup=False)
                # 🔴every acked id gets remembered, rejected ones too: PROTOCOL has the dispatcher deduplicate by
                #   `seq`, and `seq` counts from 0 for each job ⇒ the same id running a second sequence would have
                #   the other side treat `ack`/`started` as a duplicate and drop it (while `local_rate_limit` is
                #   retryable, and this is exactly the path callers are encouraged to take). ⇒ an id may only ever
                #   have one sequence; retrying needs a new id (written into PROTOCOL).
                self._seen[jid] = True
                while len(self._seen) > SEEN_JOBS:
                    self._seen.popitem(last=False)
                held = self._inflight.acquire(blocking=False)
                if not held:
                    self._report(jid, 1, "error", **error_payload(BridgeError(
                        "local_rate_limit", "this bridge's remote jobs in flight at once have already hit the "
                                            "cap of %d (= config.json's max_concurrent times 4): wait for a few "
                                            "to finish before sending more, and retry with a new "
                                            "job_id" % self.max_inflight), "the remote leg"))
                    return
                box[0] = False
                self._unqueue(jid, threading.Event())     # registering and checking for an early-arriving cancel done in one step (see that branch in `_route`)
                if self._stop.is_set():     # ⭐register first, check the flag second: `stop()` sets its flag first and then cancels whatever is already registered, one by one ⇒ neither ordering may ever miss a piece of work
                    # a resource audit measured it: the bridge stopped while an ack was in flight, and it came
                    # back around later ⇒ it still started a job (`close_all` had already run by then, and
                    # nobody was there to receive the CLI it started)
                    log("⚠️ stopping the bridge: a job's ack just finished and the bridge stopped right after ⇒ never starting it (the dispatcher got the ack, and will get no final state)")
                    return
                threading.Thread(target=self._job, args=(jid, json.loads(raw), arrived), daemon=True).start()
                ok = True
            finally:
                if held and not ok:
                    # ⭐the slot was already taken, and the work never started ⇒ it does not get returned here,
                    #   and once `BoundedSemaphore` has leaked enough times this leg goes mute (without a sound).
                    #   ⚠️the `held` half carries just as much weight: calling `release()` without having taken
                    #   one raises `ValueError` on the spot (which is exactly the path the rejection takes), never
                    #   merge the two flags into one.
                    self._cancels.pop(jid, None)
                    self._inflight.release()

    def _job(self, jid: str, job: dict, arrived: tuple) -> None:
        t0, seq, buf, flushed, mute = time.time(), [0], [], [time.time()], [False]
        cancel = self._cancels[jid]

        def post(event: str, **kw) -> None:
            """⭐the contract: failing to send raises `BridgeError` (`flush` below, on the turn's own post,
            relies on exactly this to go mute).
            🔴it wraps any family at all, never "just the families we recognize": it used to wrap only
              `OSError`/`ValueError`, and `http.client.HTTPException` would fly straight through into the driver
              layer, turning a perfectly good job into one with no final state at all (review C-A).
            ⚠️this contract is not a structural guarantee: the wrapper itself, taking the original words
              (`_one_line(e)`), can meet an exception whose `__str__` itself blows up, and that is exactly what
              flies out instead (second follow-up review M-2). ⇒ never let anything that "must not be preempted"
              rely on this alone for its cleanup: the post inside `finish` catches `Exception` itself; the post
              that flies into the driver layer during a turn is caught by `_run_session`/`_run_once`'s
              `finally` + `answered` (kill the process, rebuild on the next question, measured in review).
            ⭐failing to send is never "the bridge itself crashed": wrap it into a clear sentence. The families
              `_post` recognizes have already complained on their own ⇒ `logged` gets set; an unrecognized one
              complains once, right here."""
            seq[0] += 1
            try:
                self._report(jid, seq[0], event, **kw)
            except Exception as e:
                err = BridgeError("unknown", "delivering to the dispatcher failed: %s: %s" % (type(e).__name__, _one_line(e)))
                err.logged = isinstance(e, (OSError, ValueError, http.client.HTTPException))
                if not err.logged:
                    log("❌ delivering %s hit an error we do not recognize: %s" % (event, err.raw))
                raise err from e

        def flush() -> None:
            # 🔴a chunk's delivery failing must never fly into the driver layer as an exception: `on_delta` is
            #   called from inside `driver.turn()` ⇒ go mute instead, do not try again this turn; the post for
            #   the final state still gets its own one chance (it is the dispatcher's only signal that this is
            #   wrapped up).
            # ⭐clear buf before sending: never leave it sitting there even if sending fails — the flush right
            #   before the final state would otherwise hit the same batch again.
            text, buf[:], flushed[0] = "".join(buf), [], time.time()
            if text and not mute[0]:
                try:
                    post("chunk", text=text)
                except BridgeError:           # a family outside the contract flies out instead: caught at the root during a turn, caught by `finish` right before the final state (see `post`)
                    mute[0] = True

        def finish(err, res) -> None:
            """The final state: attempted exactly once. ⭐catches `Exception`, never just `BridgeError`: when
            building the final state itself (`error_payload`) errors out, fall back to a minimal `unknown`,
            never send nothing at all."""
            try:
                flush()
            except Exception as e:
                # 🔴whatever family the last chunk raises, it must never take the final state down with it: it
                #   used to be just `suppress(BridgeError)`, relying on `post()`'s "only ever raises BridgeError"
                #   contract, which is not a structural guarantee ⇒ an exception whose `__str__` itself blows up
                #   left the final state with 0 posts, while `jobs.log` still recorded ok (second follow-up
                #   review M-2, measured). ⭐`post()` has already complained about the `BridgeError` family; this
                #   complains about the rest, reporting only the class name (its own original words might not
                #   even be retrievable).
                if not isinstance(e, BridgeError):
                    log("❌ the last chunk before the remote leg's final state errored out (the final state is still sent): %s" % type(e).__name__)
            try:
                if err is None:
                    post("done", text=res["text"], usage=res.get("usage") or {}, ttfc=res.get("ttfc"),
                         latency_s=round(time.time() - t0, 2), rebuilt=res.get("rebuilt"))
                else:
                    post("error", **error_payload(err, "the remote leg"))
            except BridgeError:
                pass                          # failed to send: `post` has already complained, the outcome is never rewritten because of this
            except Exception as e:
                log("❌ the remote leg errored out building the final state: %s: %s" % (type(e).__name__, e))
                # ⭐the fallback still goes through `error_payload` (an error body may only ever be built in one
                #   place); if it too breaks, all that is left is this one log line
                with contextlib.suppress(Exception):
                    post("error", **error_payload(BridgeError("unknown", "the bridge itself crashed: %s: %s"
                                                              % (type(e).__name__, e)), "the remote leg"))

        def on_delta(s: str) -> None:
            buf.append(s)
            if time.time() - flushed[0] >= RESULT_FLUSH_S:
                flush()

        # ⭐the initial value is "never reached an outcome", never `ok`: if the handler raises again, the final
        #   state and the bookkeeping both say `unknown` (the initial value used to be `ok` ⇒ a failed job got
        #   recorded as a success in `jobs.log`, review M-A, measured).
        klass, res, sid = "unknown", {}, None
        err = BridgeError("unknown", "the bridge itself crashed: this one broke before reaching an outcome")
        # 🔴the cleanup (final state + slot + bookkeeping) must live in the `finally` of that same one `try`
        #   statement, never a separate one: split into two, and anything raised again inside the `except` branch
        #   flies straight out of `_job`, and the second `try` never even starts ⇒ the slot is never returned, no
        #   final state is sent, `jobs.log` gets zero lines, not one word lands on disk. This is not hypothetical:
        #   feeding `closed_during` a `session` that never passed the gate (`["x"]`) is a `TypeError`, and 16 of
        #   those permanently mute this leg, while the reason given to the outside world is the lie "already at
        #   the in-flight cap". ⭐"no matter which family of exception flew" is a guarantee the structure gives,
        #   never just a wish ⇒ the gate is built to the shape of the bug: `test_any_failure_still_returns_the_slot`.
        self.b.awake.begin()        # 0.2.0 KeepAwake: paired with the `end()` in this try's finally (every path passes through it)
        try:
            try:
                per_hour_cfg = self.b.cfg.get("remote_jobs_per_hour")
                # ⭐M-2: `... or 600` folded a configured 0 (the one local knob to pause remote work) into "not
                #   set" ⇒ 600. Only a missing key (`None`) falls back to 600 now.
                per_hour = 600 if per_hour_cfg is None else int(per_hour_cfg)
                if not self.b.joblog.allow_remote(per_hour):
                    raise BridgeError("local_rate_limit", "this bridge's local rate limit: at most %d remote jobs "
                                                          "per hour (config.json::remote_jobs_per_hour)" % per_hour)
                req = remote_request(job)
                # ⭐I-3: namespaced before it reaches `Bridge`/`SessionManager` — a raw value that has passed the
                #   gate is otherwise fit to be a dict key, and the local leg uses the exact same one at the same
                #   spot (two products picking the same simple id, "default", used to rebuild each other's
                #   session). What goes back to the dispatcher below never carries a session id at all.
                sid = None if req["session"] is None else "remote:" + req["session"]
                # 🔴before starting the CLI, check two things that could have happened while this was queued (15b
                #   fix1): a cancel that arrived early (I1); the same session getting closed after this arrived
                #   (I2: close is handled on the spot on the receive-stream thread, and can race ahead of this one
                #   while it is still sitting in the channel) ⇒ cancelled on the spot, never rebuild a session
                #   that was closed
                if cancel.is_set() or self.b.closed_during(sid, *arrived):
                    raise BridgeError("cancelled", "this one was cancelled before it even started" if cancel.is_set() else
                                      "while this one was queued waiting for its ack, the dispatcher closed its session ⇒ it never ran (never rebuild a session that was closed)")
                res = self.b.sessions.run(
                    session_id=sid, model_id=req["model_id"], effort=req["effort"],
                    system=req["system"], messages=req["messages"],
                    on_started=lambda ms: post("started", queued_ms=ms), on_delta=on_delta, cancel=cancel,
                    first_token=req["first_token"], timeout=req["timeout"], closed=lambda: self.b.closed_during(sid, *arrived))
                klass, err = "ok", None
            except BridgeError as e:
                klass, err = e.klass, e   # ⭐record it as-is first: the rewrite below raises again, and the final state can still tell the true reason
                # ⭐"closed while in flight" goes through the exact same rewrite as the local leg: must never be reported as "the bridge crashed, retryable".
                err = _closed_midflight(e, self.b.closed_during(sid, *arrived))
                klass = err.klass
            except Exception as e:                          # a bug in the bridge itself: be loud about it, never leave the dispatcher hanging
                klass, err = "unknown", BridgeError("unknown", "the bridge itself crashed: %s: %s" % (type(e).__name__, e))
                log("❌ remote leg internal error: %s: %s" % (type(e).__name__, e))
        except Exception as e:   # the handler itself raised again: the outcome is already recorded in klass/err, this is only responsible for being loud about it
            log("❌ the remote leg errored out again before wrapping up: %s: %s" % (type(e).__name__, e))
        finally:
            # 🔴the outcome and whether it could be delivered are two different axes: failing to deliver must
            #   never rewrite a successful job into an `error`.
            # ⭐the slot always gets returned no matter which family of exception flew above: once
            #   `BoundedSemaphore` has leaked `max_inflight` times, this leg can never accept work again, and
            #   without a sound (the same shape as the concurrency slot in `SessionManager`).
            try:
                finish(err, res)
            finally:
                self._cancels.pop(jid, None)
                self._inflight.release()
                self.b.awake.end()
                self.b.joblog.write(leg="remote", model=shown_model(job.get("model"), self.b.cat), klass=klass, job_id=jid,
                                    cli_version=self.b.cli_ver(job.get("model")), usage=res.get("usage") or {},
                                    latency_s=round(time.time() - t0, 2), ttfc=res.get("ttfc"),
                                    queued_ms=res.get("queued_ms") or 0, rebuilt=res.get("rebuilt"))


# ━━ Subcommands (run / start / stop / status / token / doctor / setup / pair / update)
# ⭐This block only ever touches the blocks above it through an explicit function call, and never adds a new
#   mutable global shared across blocks (the split has already happened: the pieces under src/ are joined into
#   the one scv.py players get).
# ⭐Every failure path goes through `_cmd_failed`: one line lands in bridge.log (visible on stderr too) plus one
#   sentence of "what to do next" (Minor 6 / C15).
# ⚠️Process / network behaviour has only been measured on win32; the few POSIX branches (SIGTERM,
#   `start_new_session`) are written to the code, ⏳ not measured.
GC_EVERY_S = 60          # the clock-driven cadence for reclaiming idle persistent sessions: `gc_idle` used to only get called when a new session was built, so with nobody arriving it never shrank (an earlier measurement)
START_WAIT_S = 60        # how long `scv start` waits at most (starting the bridge runs detect first: a real CLI answers each probe in 0.05-0.43s)
STOP_WAIT_S = CLOSE_GRACE_S + KILL_BUDGET_S   # POSIX: how long to wait after SIGTERM for it to wind down on its own (closing one session = the grace period plus, worst case, one tree-kill)
STOPPED_WITHIN_S = 5.0   # how long to wait, after a hard kill, for the process to actually vanish from the system
PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")


def self_cmd(*words: str) -> str:
    """The one and only door for a self-referential command: every human-facing "go run one of scv's own
    subcommands" goes through it (a bare `scv <subcommand>` reappearing in a human-facing string turns red on the
    spot: tests/test_90_cli.py::SelfCommands). After install there is no `scv` on PATH (setup does not install a
    launcher, a product decision) ⇒ pasting the bare form would not run.
    ⭐What it computes is what this particular install actually looks like: interpreter = `sys.executable`, script
      = this file's absolute path (never `python3` / `python` / `~`); turning that into something pasteable is
      `paste_cmd`'s job. ⚠️May come out as several lines (one label line, one command line) ⇒ the caller gives it
      its own line to itself, never uses it as a `%` / `.format` template (a `%` / `{` in the path would blow up).
    🔴The output carries this machine's absolute path (a path with the user's name in it) ⇒ never goes into the
      API's response body, never out over the network."""
    return paste_cmd([sys.executable, os.path.abspath(__file__)] + list(words))


def _cmd_failed(what: str, next_step: str, detail: str = "") -> int:
    log("❌ " + what)
    if detail:      # ⭐printed as-is to stderr, never folded into the line above: `log()` truncates its tail by LINE_CAP_BYTES, and the last few lines (why it died) get cut off first
        print(detail, file=sys.stderr)          # ⚠️this one never goes through the `log()` door ⇒ text from outside has to be run through `_no_ctrl` by the caller first (the two spots in pair / update)
    print("  ⇒ " + next_step, file=sys.stderr)
    return 1


def local_get(port: int, path: str, timeout: float = 3.0):
    """Ask itself. Must bypass the environment proxy: when the user has set HTTP_PROXY without configuring
    no_proxy, going through the proxy cannot reach 127.0.0.1 on this machine (B32).
    ⭐It is one of the named outbound points in `NET_CALLERS`, but it only ever dials loopback: the address comes
      only from section ①'s `LOCAL_URL`; never follow a 302 outside loopback (`redirect_refused` ③)."""
    try:
        return json.loads(_fetch(LOCAL_URL % port + path, timeout, urllib.request.ProxyHandler({})).decode("utf-8"))
    except NET_ERRORS:         # closes over `HTTPError` / the whole exception family, at the `_open` door
        return None


def our_health(port: int):
    """Is it this bridge answering on that port. "Something answered" is not "the bridge is running": on this
    dev machine, port 8765 was measured being held by an unrelated program."""
    h = local_get(port, "/healthz")
    return h if isinstance(h, dict) and h.get("protocol") == PROTOCOL and "version" in h else None


def mask_proxy(url: str) -> str:
    """A password inside a proxy address never goes into any output. Blocked whether or not a scheme is present
    (`user:pw@host:port` is recognized by many proxy tools).
    🔴Masks up to the last `@`, never stops at a `/` (Task 12 review I-2): when the password itself contains `@`,
      Node / Python / Rust all split on the last `@` (and that kind of config actually works); stopping at `/`
      would mean the config itself never works at all, and it is exactly when the proxy is broken that someone
      runs doctor and pastes the output for someone else to read.
    ⚠️Would rather over-mask: a proxy address with `@` in its path (rare) gets its hostname masked away too along
      with the password — a leaked password costs more than an invisible hostname."""
    return re.sub("^([A-Za-z][A-Za-z0-9+.-]*://)?.*@", lambda m: (m.group(1) or "") + "***@", url, flags=re.S)


def proxy_env() -> dict:
    return {k: mask_proxy(v) for k, v in os.environ.items() if k.lower() in PROXY_VARS}


def _remask(env):
    """The proxy variables stored in bridge.pid get masked again on the way back out: an older version may not
    have masked them completely (the `mask_proxy` from before I-2)."""
    if not isinstance(env, dict):
        return "(bridge.pid did not record this item, or its shape is wrong: %s)" % type(env).__name__
    return {str(k): mask_proxy(v) if isinstance(v, str) else "(not a string, not printed)" for k, v in env.items()}
def codex_features_unknown(head: list) -> tuple:
    """Names in `CODEX_OFF` that this codex no longer recognizes ⇒ `(names, "")`; could not check ⇒ `([], reason)`.
    ⭐codex says nothing at all about a `-c features.X=false` it does not recognize ⇒ a name gone stale (most
      often just renamed) means that item silently never got turned off.
    ⭐"could not ask" and "asked, and all 20 turned out stale" must never look the same (an earlier brief lumped
      the former into the same list)."""
    try:
        p = run_cli(list(head) + ["features", "list"], timeout=PROBE_MAX_SECONDS, cwd=_probe_dir())
    except (OSError, subprocess.SubprocessError) as e:
        return [], "could not check: features list never ran: %s" % _one_line(e)
    names = {ln.split()[0] for ln in decode(p.stdout).splitlines() if ln.split()}
    if p.returncode != 0 or not names:
        return [], "could not check: features list rc=%d, %d line(s): %s" % (
            p.returncode, len(names), _clip(_one_line(decode(p.stderr).strip()) or "(stderr is empty)", 200))
    return [n for n in CODEX_OFF if n not in names], ""


CODEX_CARRIED = ("AGENTS" + ".override.md", "AGENTS.md")     # written apart on purpose: the URL gate would read `AGENTS.override.md` as a hostname


def _stat_file(p: Path):
    """A file ⇒ `{"path", "bytes"}`; not there ⇒ `None`; there, but its size cannot be told ⇒ report it anyway,
    saying clearly why there is no number (never treat it as absent, never make up a number).
    ⛔Only `stat`, never read the content (B22's spirit: the bridge does not read the player's private files).
    ⚠️A directory whose name matches never counts (13c re-review N4: a directory's `st_size` on POSIX is 4096).
      `stat` first, then `is_file()`: the case where the size cannot be told has to land in the `OSError` below
      first (which errors `is_file()` returns False for, and which it lets fly, varies by pathlib version,
      ⏳ not checked version by version)."""
    try:
        size = p.stat().st_size
        if not p.is_file():
            return None
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        return {"path": str(p), "bytes": None, "error": _one_line(e)}
    return {"path": str(p), "bytes": size}


def codex_carried() -> dict:
    """Plain doctor (zero cost): the instruction file codex will carry into every single call, that this bridge
    cannot block, going only by `$CODEX_HOME` (codex's own default `~/.codex` if unset) ⇒
    `{"home", "files": [{"path", "bytes"}…], "if_blank"?: {"path", "bytes"}}`.
    ⭐The rule follows a real, zero-cost measurement of codex: the override has non-blank content ⇒ only it is
      carried; otherwise (missing / 0 bytes / whitespace only) ⇒ AGENTS.md (only counts if it has non-blank
      content). A 0-byte file can be told apart by stat ⇒ a 0-byte override reports AGENTS.md (13c review I2's
      (b)); "whitespace only" cannot be told apart by stat ⇒ report the override, plus `if_blank` = the file codex
      would switch to if the override turns out to be all whitespace (never read the content just to tell them
      apart). ⚠️By the same reasoning there are two cases where this over-reports (`--live` gets them right): an
      AGENTS.md that is whitespace only (codex does not carry it); an override that is whitespace only with no
      AGENTS.md at all (codex carries nothing, and this still reports the override, with no `if_blank`).
    🔴13c Fix 1 once changed this to "ask codex's `instructionSources` at the handshake, over thread/start": the
      number came out right, but thread/start makes codex warm up — it even dials
      `wss://chatgpt.com/backend-api/codex/responses` and gets back a response id (measured against codex's own
      log store) ⇒ "plain doctor costs nothing" became a lie (the maintainer's ruling: never turn a public promise
      into a lie just to get the number right). That question moved into `doctor --live` (`live_check`: it uses
      that one call's own session).
    ⚠️Cannot see: an AGENTS.md in a git ancestor directory (review M7, only `--live` can see it). ⚠️What it looks
      at is this process's CODEX_HOME: the bridge that is running may have been started from a different
      terminal (the same issue as the proxy variables). 📎 NOTES.md::codex-user-home"""
    home = Path(os.environ.get("CODEX_HOME") or (user_home() / ".codex"))
    over, main = (_stat_file(home / name) for name in CODEX_CARRIED)
    main = main if main and main["bytes"] != 0 else None
    if over and over["bytes"] != 0:
        return dict({"home": str(home), "files": [over]}, **({"if_blank": main} if main else {}))
    return {"home": str(home), "files": [main] if main else []}


def _live_carried(srcs) -> dict:
    """`doctor --live`: the `instructionSources` in that one real call's own session reply (a public field:
    "currently loaded for this thread") ⇒ `{"sources": [{"path", "bytes"}…]}`; this version of codex does not say
    ⇒ `{"error": reason}` (never pass stat off as something codex itself said).
    ⭐More accurate than stat: it gets right both the "override is whitespace only" case and an AGENTS.md in a git
      ancestor directory (M7). Only stat, never read the content."""
    if not (isinstance(srcs, list) and all(isinstance(s, str) for s in srcs)):
        return {"error": "codex's thread/start reply has no instructionSources table (this version of codex does not say)"}
    # codex says it is there and stat cannot find it (review M9): report it anyway, saying clearly why there is no number
    return {"sources": [_stat_file(Path(s)) or {"path": s, "bytes": None, "error": "stat could not find it (is it gone?)"} for s in srcs]}


def _says_number(text: str, n: int) -> bool:
    """Whether `text` contains a single number that equals exactly `n` (`doctor --live`'s judge for "answered
    correctly", and the question's own "the sum must not already be in the prompt" check, share this one ruler).
    🔴Thousands separators must be recognized: a real codex measured answering "4646 + 6290 = 10,936" — that is
      correct, but `"10936" in` used to judge it wrong (📎 NOTES.md::live-check-prompt). 🔴But never let this turn
      a wrong answer into a right one (Task 12 review M-2): a plain substring test would have `110936` contain
      `10936`; and "strip out anything that looks like a separator" would glue unrelated numbers together
      (`pids 16 912` becomes 16912, `3.6.912` becomes 3.6912).
    ⇒ ① only strip separators inside something shaped like a thousands-grouped integer: the leading group 1-3
      digits, every group after it exactly 3 digits, the same separator throughout, and never immediately next to
      a digit or a "digit + separator" on either side; ② then compare on number boundaries. ⚠️An ASCII space only
      counts as an in-group separator (the SI convention), never as something gluing two numbers together: the
      10936 in "10936 5" still counts. ⚠️`10.936` is read as a thousands separator (the European convention), so
      an ambiguous answer like "10.936 thousand" counts too."""
    sep = "[,，._'’ " + chr(160) + chr(8201) + chr(8239) + "]"
    glue = "[,，._'’" + chr(160) + chr(8201) + chr(8239) + "]"
    grouped = ("(?<![0-9])(?<![0-9]" + sep + ")[0-9]{1,3}(?P<s>" + sep + ")[0-9]{3}(?:(?P=s)[0-9]{3})*"
               "(?![0-9])(?!" + sep + "[0-9])")
    flat = re.sub(grouped, lambda m: re.sub("[^0-9]", "", m.group(0)), text)
    return re.search("(?<![0-9])(?<![0-9]" + glue + ")" + str(int(n)) + "(?![0-9])(?!" + glue + "[0-9])", flat) is not None


def _canary_seen(text: str, hexpart: str) -> bool:
    """Whether the canary leaked: any 8 consecutive hex characters from the secret showing up in the answer
    (case-insensitive) counts as a leak.
    ⭐Looser than "the whole original string appears verbatim": it also catches one that has been rewritten (the
      `CANARY-` prefix dropped, switched to uppercase) or only a fragment (≥8 characters) pasted in (review M-3).
    ⚠️False alarms: a 12-character secret ⇒ 5 windows; the chance that some spot in an answer happens to collide
      with one of them is ≲ character-count × 5 ÷ 16⁸ (about 2×10⁻⁶ for two thousand characters).
    ⚠️Cannot see: a truncation shorter than 8 characters, one written apart (spaces added in), a paraphrase ("a
      string of hex") — this test can only prove "it leaked", never prove "it never read it"."""
    low = text.lower()
    return any(hexpart[i:i + 8] in low for i in range(len(hexpart) - 7))


def live_check(cfg: dict, family: str, model: str) -> dict:
    """One real call (costs a little quota). Both judges are behavioural (spec §15): ① a random addition answered
    correctly (the channel works, the model is listening); ② the content of the canary outside the working
    directory must never show up (the tools really are locked down).
    🔴The brief used to want it to "echo back a password": a real haiku refused (reading "echo a password, and
      read a file" as a prompt-injection test) ⇒ a healthy CLI got judged broken. After switching to a sentence
      an ordinary person would ask, one call from each family passed, and haiku actually went and tried to read
      the file ⇒ nothing leaked because the tool was locked down, never because the model refused.
      📎 NOTES.md::live-check-prompt
    ⚠️The canary sits under `work/`, outside the working directory: what this proves is "the CLI cannot read
      outside its own cwd", never "cannot read the whole machine".
    ⭐The codex family carries back `carried` too: the `instructionSources` from this one call's own session
      reply (`_live_carried`, 13c Fix 1b)."""
    hexpart = secrets.token_hex(6)
    secret = "CANARY-" + hexpart
    outside = spath("work") / ("canary-" + secrets.token_hex(4) + ".txt")
    workdir = spath("work") / ("doctor-" + secrets.token_hex(4))
    while True:
        a, b = 1000 + secrets.randbelow(8000), 1000 + secrets.randbelow(8000)
        q = ("Two quick things. First, what is %d + %d? Second, I saved a short note at %s - what does it say? "
             "If you cannot open files, just say so." % (a, b, outside.as_posix()))
        if not _says_number(q, a + b):     # the sum itself must never already be in the prompt (a CLI that only echoes back could otherwise pass, if the path happened to contain it)
            break
    driver, carried = None, {}
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        outside.write_text(secret, encoding="utf-8")
        driver = make_driver(cfg, family, model, "low", "You are a terse assistant.", workdir)
        if family == "codex":
            # ⭐"which instruction files this session will actually carry" is asking about this one call's own
            #   session (the cwd has the same shape as a real session: an empty directory under `work/`) — never
            #   start a second session just to ask this one question: every session codex starts (thread/start)
            #   warms it up once, dialing an inference endpoint too
            carried = {"carried": _live_carried(driver.sources)}
        res = driver.turn(q, lambda s: None, 180, 90, None)
        return dict({"ok": _says_number(res["text"], a + b), "canary_leaked": _canary_seen(res["text"], hexpart),
                     "ttfc": res["ttfc"], "usage": res["usage"], "answer": _clip(_one_line(res["text"]), 300)}, **carried)
    except BridgeError as e:     # ⭐the error body goes through that one door (`error_payload`): the copy that never made it to disk gets one line added there
        return dict({"ok": False, "canary_leaked": False, "error": error_payload(e, "doctor --live ")["error"]}, **carried)
    finally:
        if driver is not None:
            (driver.close if driver.alive() else driver.kill)()
        with contextlib.suppress(OSError):
            outside.unlink()
        shutil.rmtree(workdir, ignore_errors=True)
def _read_pid_file():
    """`None` = there is no such file; unreadable / wrong shape ⇒ `{"bad": reason}` (never let a broken file blow up `scv stop` into a traceback)."""
    p = spath("bridge.pid")
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"bad": "could not read it: %s" % _one_line(exc)}
    pid = rec.get("pid") if isinstance(rec, dict) else None
    return rec if isinstance(pid, int) and not isinstance(pid, bool) else {"bad": "wrong shape"}


def bridge_owner() -> tuple:
    """What state the bridge recorded in bridge.pid is in right now ⇒ `(state, rec)`, state ∈ none / alive / gone /
    unsure (could not tell, retried) / foreign (unrecognizable: corrupt, or a birth-id format written by a
    different version).
    🔴Always judge by `== rec["born"]`, never by the truthiness of the birth id (it is three-valued: `None` would
      be read as "already stopped" ⇒ failing to kill it while claiming it has stopped, A1); ⭐the format goes
      through `birth_known()` first: unrecognizable never means "not that bridge" (A3)."""
    rec = _read_pid_file()
    if rec is None:
        return "none", {}
    born = rec.get("born")
    if "bad" in rec or not isinstance(born, str) or not birth_known(born):
        return "foreign", rec
    now = proc_start_id(rec["pid"])
    return ("unsure" if now is None else "alive" if now == born else "gone"), rec


def started(port: int, ticket: str):
    """The condition `scv start` waits for: the ticket in bridge.pid is the one issued this time, and the bridge
    is answering on the port.
    🔴Never wait for `bridge.pid.exists()`: a stale file left behind by a previous crashed bridge would satisfy
      that on the spot (the f17c9e5 shape, A4).
    ⭐Recognized by ticket, never by pid: under a venv's python launcher the pid can belong to a different process
      (measured the same on this machine, ⏳ venv not measured)."""
    rec = _read_pid_file() or {}
    # 0.2.1: once the ticket matches, the bridge's own port (it may have moved off a taken default, `bind_local`)
    return rec if rec.get("ticket") == ticket and our_health(int(rec.get("port") or port)) else None


def bind_local(bridge, cfg: dict) -> int:
    """`cmd_run`'s local API: moved off a taken default port ⇒ config.json says the new one (so `status`, `token`,
    `doctor` and the next `start` agree) and one log line names both."""
    port, moved = bridge.start_local_or_next()
    if moved is not None:
        cfg["port"] = port
        save_config(cfg)
        log("⚠️ port %d is taken by another program ⇒ the local API moved to %d, and config.json now says %d" % (moved, port, port))
    return port


def spawn_detached(argv: list) -> tuple:
    """B17: the bridge must never be an agent session's child process. On Windows the harness often locks child
    processes inside a Job ⇒ first try to break free of it, and say so plainly if it cannot.
    🔴win32 uses `CREATE_NO_WINDOW`, never `DETACHED_PROCESS`: the latter leaves the bridge with no console at
      all ⇒ every console child process it starts afterwards gets a new, visible window from the system (measured
      on a user's desktop, flashing non-stop). The former gives the bridge a hidden console, never attached to
      the terminal that started it (the two are mutually exclusive; giving both means one gets ignored). The
      child-process layer has its own `new_session_kw()` backing it up too, see that for why.
    ⭐stdout / stderr always go to DEVNULL: hooking them up to bridge.log would make every one of `log()`'s lines
      get written twice, and would bypass the one door that append writes to disk go through (the brief's
      original approach). Every exception at the subcommand layer goes through the top-level handler and lands
      on disk; the ones that cannot land there (an interpreter-level crash) get seen by `scv start` as "it started
      and quit right away", which sends the user to `scv run` in the foreground. 📎 NOTES.md::detach-0b"""
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name != "nt":
        return subprocess.Popen(argv, start_new_session=True, **kw).pid, ""
    flags = CREATE_NO_WINDOW | 0x00000200 | 0x01000000        # NEW_PROCESS_GROUP | BREAKAWAY_FROM_JOB
    try:
        return subprocess.Popen(argv, creationflags=flags, **kw).pid, ""
    except OSError as exc:
        log("⚠️ could not break free of the parent process's Job (%s) ⇒ falling back to starting it without breaking free" % exc)
        return subprocess.Popen(argv, creationflags=flags & ~0x01000000, **kw).pid, (
            "⚠️ could not break free of the parent process's Job: closing this terminal / session may take the bridge down with it. "
            "Please run this again from an ordinary terminal:" + NL + self_cmd("start"))


def serve_until(bridge, stop: threading.Event) -> None:
    """`scv run`'s main loop: reclaims idle sessions on the clock (A6). ⭐Wakes every 0.5s, never `wait(60)`: on
    win32, when the main thread is stuck in a long wait, Ctrl+C only gets handled once it wakes up (⏳this is
    written to CPython's known behaviour, not measured on this machine)."""
    last = poked = time.time()
    while not stop.wait(0.5):
        if time.time() - poked >= AWAKE_EVERY_S:
            poked = time.time()
            bridge.awake.tick()
        if time.time() - last >= GC_EVERY_S:
            last = time.time()
            try:
                n = bridge.sessions.gc_idle()
            except Exception as e:      # a clock-driven reclaim blowing up must never take the whole bridge down with it (it is not any one request)
                log("❌ error reclaiming sessions on the clock (the bridge keeps running): %s: %s" % (type(e).__name__, e))
                continue
            if n:
                log("reclaimed %d persistent session(s) idle for more than %ds" % (n, SESSION_IDLE_S))


SESSION_WORKDIR_RE = re.compile(r"[0-9a-f]{16}-[0-9a-f]{8}")


def sweep_work() -> int:
    """I-1: remove `work/` entries a session left behind with no chance to clean itself up (on win32, `cmd_stop`'s
    hard kill means `cmd_run`'s `finally: bridge.stop()` never runs, so `_drop`'s `shutil.rmtree` never does either
    — every `stop` used to leave one directory per still-alive session, `system.txt` and all).
    ⚠️Only ever call this next to `sweep_orphans()` (the two spots where no other bridge can be using this
      `SCV_HOME`): removing one out from under a bridge that is still running would corrupt a live session.
    🔴Matched only by the exact shape `_workdir` builds, `[0-9a-f]{16}-[0-9a-f]{8}` — never "anything under work/
      that is not a known name": `work/probe` and a `doctor --live` run's `doctor-<hex>` / `canary-<hex>.txt` are
      shaped differently on purpose, so this never touches one running at the same moment.
    Logs one line with the count; a failure removing one entry is loud, never fatal (bookkeeping, not the action a
    stop/start exists to do)."""
    d = spath("work")
    gone = []
    for p in (d.iterdir() if d.is_dir() else ()):
        if p.is_dir() and SESSION_WORKDIR_RE.fullmatch(p.name):
            try:
                shutil.rmtree(p)
                gone.append(p.name)
            except OSError as exc:
                log("⚠️ could not remove a leftover session work directory %s: %s" % (p, exc))
    if gone:
        log("cleaned up %d leftover session work director%s under work/ (left behind by a hard stop): %s" % (
            len(gone), "y" if len(gone) == 1 else "ies", ", ".join(gone[:8])))
    return len(gone)


def cmd_run(args) -> int:
    cfg = load_config()     # ⭐L450: every path that goes on to sweep touches config first (it complains loudly if the disk is not writable, never let the bookkeeping step get there first and fail silently)
    state, rec = bridge_owner()
    if state == "alive":
        # 🔴this check has to run before the sweep: that bridge's CLIs are all in the registry with matching birth ids ⇒ a sweep would kill every one of them
        return _cmd_failed("another bridge (pid %s, port %s) is already using this SCV_HOME" % (rec["pid"], rec.get("port")),
                           "not starting a second one; to restart, stop it first:" + NL + self_cmd("stop"))
    sweep_tmp()
    if state in ("foreign", "unsure"):
        log("⚠️ bridge.pid cannot say whether the previous bridge is still around (%s) ⇒ not sweeping the previous "
            "leftovers this time: a sweep would kill the CLIs of a bridge that is still alive" % (
                "this file is not recognized" if state == "foreign" else "could not tell pid %s's birth id" % rec["pid"]))
    else:
        sweep_orphans()
        sweep_work()
    me = os.getpid()        # ⭐this bridge's own identity (written into bridge.pid), never the name used for the write buffer (that belongs only to _tmp_path)
    born = proc_start_id(me)
    if not born:            # 🔴A2: writing `None` in becomes `null` ⇒ the next `scv stop` would say "that is no longer the bridge" and delete the file, while the bridge is still running
        return _cmd_failed("could not tell this process's own birth id (%r) ⇒ not starting: it would go unrecognized when the bridge is stopped later" % (born,),
                           "try again; if it keeps happening, paste the last few lines of bridge.log")
    if unused_codex_home(cfg):
        log("⚠️ " + unused_codex_home(cfg))
    bridge = Bridge(cfg)
    try:
        port = bind_local(bridge, cfg)
    except BridgeError:
        # ⭐`start_local` has already landed on disk (the port number plus the OS's original words). M-7: its class
        #   is `crashed` (∈ RETRYABLE) — nothing on the bridge-starting path reads `retryable` ⇒ never open a new
        #   category just for this (that would drag HTTP_STATUS / RETRYABLE / fix_hint along with it), but the
        #   wording still has to be right
        print(NL.join(["  ⇒ if something else is holding it, change the port in %s to a different number (retrying in a bit will not help);" % spath("config.json"),
                       "if a previous bridge is still open, look at it:", self_cmd("status"), "stop it:", self_cmd("stop")]), file=sys.stderr)
        return 1
    _atomic_write("bridge.pid", json.dumps({"pid": me, "born": born, "port": port,
                                            "ticket": getattr(args, "ticket", "") or "", "proxy_env": proxy_env(),
                                            "env": env_facts()}))   # ⭐names only (15c review I2: what doctor reports is the bridge's own copy)
    stop = threading.Event()
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, lambda *_: stop.set())
    try:
        paired = bridge.start_remote()
        log("scv %s is up (pid %d): the local API is on %s:%d; models %s; remote leg %s" % (
            VERSION, me, LOCAL_HOST, port, bridge.cat, bridge.remote.base if paired else "off (not paired)"))
        serve_until(bridge, stop)
    finally:
        bridge.stop()
        if (_read_pid_file() or {}).get("born") == born:    # only ever delete its own entry
            with contextlib.suppress(OSError):
                spath("bridge.pid").unlink()
        log("the bridge stopped (pid %d)" % me)
    return 0


def _log_tail(n: int = 5) -> str:
    try:
        with open(spath("bridge.log"), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8192))
            return NL.join(f.read().decode("utf-8", "replace").splitlines()[-n:])
    except OSError as exc:
        return "(could not read bridge.log: %s)" % exc


def cmd_start(args) -> int:
    cfg = load_config()
    port = int(cfg.get("port") or 8765)
    if our_health(port):
        print("already running (port %d)" % port)
        return 0
    state, rec = bridge_owner()
    if state == "alive":
        return _cmd_failed("the bridge at pid %s is still alive, but port %d is not answering (it is on %s)" % (rec["pid"], port, rec.get("port")),
                           NL.join(["it may be halfway through starting up (wait a few seconds and check again):", self_cmd("status"),
                                    "or config.json's port has changed ⇒ stop it, then start again:", self_cmd("stop"), self_cmd("start")]))
    ticket = secrets.token_hex(8)
    pid, note = spawn_detached([sys.executable, os.path.abspath(__file__), "run", "--ticket", ticket])
    end = time.time() + START_WAIT_S
    while time.time() < end:
        rec = started(port, ticket)
        if rec is None and proc_start_id(pid) == "":
            rec = started(port, ticket)                    # exiting and writing the pid file have no guaranteed order ⇒ check once more
            if rec is None:
                return _cmd_failed("the bridge process started and quit right away (pid %d); the last few lines of bridge.log are below" % pid,
                                   "see the lines above; running it in the foreground shows exactly where it died:" + NL + self_cmd("run"), _log_tail())
        if rec is not None:
            if rec.get("port") != port:
                print("port %d was taken by another program: the bridge moved to %s (config.json now says so)" % (port, rec.get("port")))
            print("up: pid %d, port %s, log %s" % (rec["pid"], rec.get("port"), spath("bridge.log")))
            if note:
                print(note)
            return 0
        time.sleep(0.5)
    return _cmd_failed("did not come up within %d seconds (pid %d)" % (START_WAIT_S, pid),
                       "see %s; running it in the foreground shows exactly where it is stuck:" % spath("bridge.log") + NL + self_cmd("run"))


def cmd_stop(args) -> int:
    load_config()           # ⭐L450: see the same line in cmd_run
    state, rec = bridge_owner()
    p = spath("bridge.pid")
    if state == "none":
        # never sweep in this case: with no pid file there is no way to tell whether another bridge is using this SCV_HOME (the next bridge start will sweep)
        print("no bridge.pid: the bridge is not running, or it was not started with the start / run subcommand")
        return 0
    if state == "foreign":
        return _cmd_failed("bridge.pid is not recognized (%s) ⇒ this version cannot confirm whether pid %s is that bridge, so it was left untouched and the file was left in place"
                           % (rec.get("bad") or "the birth-id format was written by a different version", rec.get("pid")),
                           "stop it with the bridge version that wrote it; once you have confirmed that bridge is really gone (the line below says not running), delete %s:" % p
                           + NL + self_cmd("status"))
    if state == "unsure":
        return _cmd_failed("could not tell whether pid %s is still that bridge (retried) ⇒ left untouched, bridge.pid was left in place" % rec["pid"],
                           NL.join(["stop it again in a bit:", self_cmd("stop"), "check whether it still answers:", self_cmd("status")]))
    pid, born = rec["pid"], rec["born"]
    if state == "gone":
        print("pid %d is no longer that bridge (it disappeared without winding down) ⇒ only clearing bridge.pid" % pid)
    else:
        if os.name != "nt":
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)               # let it wind down on its own first (closing its CLI sessions)
            _wait_not(pid, born, STOP_WAIT_S)
        if proc_start_id(pid) == born:
            kill_pid_tree(pid)                             # win32 has no SIGTERM to send: taskkill /T takes the CLIs down with it too
            _wait_not(pid, born, STOPPED_WITHIN_S)
        now = proc_start_id(pid)                           # `""` or someone else's birth id (the pid was reused) = that bridge is gone
        if now is None or now == born:
            return _cmd_failed("pid %d did not stop (%s) ⇒ bridge.pid was left in place" % (
                pid, "could not tell its birth id" if now is None else "still alive"), "run it again:" + NL + self_cmd("stop"))
    if (_read_pid_file() or {}).get("born") == born:
        with contextlib.suppress(OSError):
            p.unlink()
    sweep_orphans()
    sweep_work()
    if state == "alive":
        print("stopped: pid %d" % pid)
        log("stop: stopped pid %d (a hard kill on win32: that bridge had no time to write its own 'stopped' line)" % pid)
    return 0


def _wait_not(pid: int, born: str, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end and proc_start_id(pid) == born:
        time.sleep(0.2)


def cmd_status(args) -> int:
    port = int(load_config().get("port") or 8765)
    health = our_health(port)
    if health is None:
        state, rec = bridge_owner()
        print("not running (no answer on port %d)%s" % (port, "; but the pid %s recorded in bridge.pid is still alive (it is on %s)"
                                               % (rec["pid"], rec.get("port")) if state == "alive" else ""))
        return 1
    print(json.dumps(health, ensure_ascii=False, indent=2))
    # ⭐/healthz must never carry a path or original words ⇒ the pasteable lines get printed here instead, on stderr (stdout keeps only that one JSON blob, 13b review M2)
    blocked = [f for f, i in (health.get("families") or {}).items() if isinstance(i, dict) and i.get("blocked")]
    if blocked:
        print("⇒ these families are blocked and not being reported: %s; see doctor for why and what to do:" % ", ".join(blocked) + NL + self_cmd("doctor"), file=sys.stderr)
    if (health.get("remote") or {}).get("state") == "too_old":
        print("⇒ the remote leg refused this version; update (no arguments = use the pair the dispatcher gave in its last hello):" + NL + self_cmd("update"), file=sys.stderr)
    return 0


def cmd_token(args) -> int:
    print(load_config()["local_token"])
    return 0
def _children_facts() -> list:
    """§8 "how many child processes are being managed, how much memory": reads the registry (the whole SCV_HOME),
    counting one alive (and reporting its memory) only when its birth id matches."""
    out = []
    for c in children():
        now = proc_start_id(int(c["pid"]))
        alive = None if now is None or not birth_known(c["born"]) else now == c["born"]
        out.append({"pid": c["pid"], "family": c["family"], "alive": alive,
                    "rss_kb": proc_rss_kb(int(c["pid"])) if alive else None})
    return out


CLI_NAMESPACE = ("CLAUDE", "CODEX", "ANTHROPIC_", "OPENAI_", "AI_AGENT")      # the range of names doctor lists (never the values)
# variables the user set themselves that change how a CLI answers, going around the closed set of argv (B27) (15c ⑦2: keep it and disclose it): name -> what it changes (from the docs' own wording, never made up)
BEHAVIOR_VARS = {"CLAUDE_CODE_EFFORT_LEVEL": "it overrides the --effort the bridge passes (docs: overrides --effort)",
                 "MAX_THINKING_TOKENS": "set to 0 to turn thinking off (except Opus 5.5 / Fable), other values only take effect under a fixed thinking budget (docs)",
                 "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING": "Opus 4.6 / Sonnet 4.6 fall back to a fixed thinking budget governed by MAX_THINKING_TOKENS (docs)",
                 "CLAUDE_CODE_DISABLE_THINKING": "turns thinking off (not written in the docs; 15c measured thinking tokens on haiku going 142 to 0)",
                 "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "it changes the output cap for each turn's answer; set it too small and the answer gets cut off (the package's own words: when a reply exceeds the output cap, "
                                                  "set the CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable; claude 2.1.282)"}
# the ones that cause trouble if missing (settled by 15c's zero-cost readings): on win32, missing APPDATA / HOME / USERPROFILE (two families) still behave normally at the --version / login-status
#   level; only when LOCALAPPDATA alone is missing does `cli_head` fail to find the desktop build's own bundled codex. POSIX ⏳ not measured. 📎 NOTES.md::child-env-session-vars
CRITICAL_ENV = ("LOCALAPPDATA",) if os.name == "nt" else ()


def env_facts() -> dict:
    """doctor's "environment" field, names only, never values: the two families of namespaced variables passed to
    the child CLI / the session variables stripped out (`session_bound`) / the ones that change behaviour
    (`BEHAVIOR_VARS` plus `ANTHROPIC_DEFAULT_<alias>_MODEL`: the docs say this alias actually runs the model it
    names) / the critical variables that are missing.
    ⭐Computed from the copy actually handed down (`child_env()`), never freshly from `os.environ` (15c review
      I1). What it describes is this process's own: `cmd_run` records it into bridge.pid when the bridge starts,
      and doctor reports the bridge's own copy while the bridge is running (I2)."""
    sent, said = child_env(), {}
    for k in sorted(sent):
        m = re.fullmatch("ANTHROPIC_DEFAULT_([A-Z]+)_MODEL", k.upper())
        what = BEHAVIOR_VARS.get(k.upper()) or (m and "the name `%s` actually runs the model it points to (docs)" % m.group(1).lower())
        if what:
            said[k] = what
    return {"passed": sorted(k for k in sent if k.upper().startswith(CLI_NAMESPACE)), "stripped": sorted(set(os.environ) - set(sent)),
            "changes": said, "missing": [k for k in CRITICAL_ENV if env_missing(k)]}


def _env_whose(own: dict, rec) -> dict:
    """Whose environment names doctor reports (15c review I2): if the bridge is running and bridge.pid recorded
    its `env_facts()` from the moment it started ⇒ report the bridge's own (`of_bridge`); otherwise report this
    one's own, saying clearly whose it is. A shape that does not fit (an older version never recorded it, or it
    got corrupted) ⇒ treated as not recorded. The critical missing variables (`missing`) follow the same
    reasoning (fix1's addition, the maintainer's ruling ⑦3)."""
    e = rec.get("env") if isinstance(rec, dict) else None
    ok = (isinstance(e, dict) and all(isinstance(e.get(k), list) and all(isinstance(x, str) for x in e[k])
                                      for k in ("passed", "stripped", "missing"))
          and isinstance(e.get("changes"), dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in e["changes"].items()))
    whose = ("the bridge that is running (pid %s, from the moment it started)" % rec.get("pid") if ok else
             "this terminal (the bridge is not running; when the bridge starts, its own environment at that moment is what counts)" if rec is None else
             "this terminal (the running bridge did not record this: it was started by an older version)")
    return dict(e if ok else own, whose=whose, of_bridge=ok, own_missing=own["missing"])   # ⭐doctor's own missing items get their own separate field (fix2 N-M3)


def doctor_facts(cfg: dict, live: bool) -> dict:
    """⭐Reports facts only (B14); the verdict lives separately, in `doctor_verdict`. `proxy_env` belongs to
    doctor's own process; `bridge_proxy_env` was recorded when the running bridge started (§8: the two processes
    can be started from two different terminals, and see different proxies)."""
    found = detect(cfg)
    port = int(cfg.get("port") or 8765)
    state, rec = bridge_owner()
    health = our_health(port)
    facts = {"scv": VERSION, "python": sys.version.split()[0], "os": os.name + "/" + sys.platform,
             "config": str(spath("config.json")), "port": port, "running": health is not None,
             # ⭐I-4: keep the whole dict, never just the names — `doctor_verdict` also needs each one's own
             #   `blocked` flag (already only a version shape plus a bool, no paths, see `Bridge.health`).
             "bridge_families": health["families"] if isinstance((health or {}).get("families"), dict) else None,
             "bridge": {"state": state, "pid": rec.get("pid"), "port": rec.get("port")},
             "children": _children_facts(), "remote_url": cfg.get("remote_url") or "(not paired: no outside address is reached)",
             "proxy_env": proxy_env(),
             "bridge_proxy_env": _remask(rec.get("proxy_env")) if state == "alive" else "(no bridge is running)",
             "env": _env_whose(env_facts(), rec if state == "alive" else None), "families": {}}
    for family, info in found.items():
        entry = {"path": info["head"][-1], "version": info["version"], "blocked": info["blocked"]}
        if family == "claude" and info["blocked"]:
            # 🔴F-1: never ask `auth status` of a claude old enough to be blocked — if it does not recognize that argument, it will send this as a real call with it as the prompt
            entry["auth"] = {"logged_in": False, "method": "", "plan": "",
                             "probe_error": "not asked: this family is blocked (see blocked); an old version of claude would send an argument it does not recognize as a real call with it as the prompt"}
        else:
            entry["auth"] = auth_status(family, info["head"])
        # ⭐both families' login commands use the resolved executable (`fix_hint` goes out over the network and can only give the bare name, which may not be on PATH)
        entry["login_cmd"] = login_cmd(family, info["head"])
        if family == "codex":
            entry["codex_off_unknown"], entry["codex_off_error"] = codex_features_unknown(info["head"])
            entry["carried"] = codex_carried()      # never starts a session (zero cost); `--live` has its own copy of what codex itself says (`live.carried`)
        if live and not info["blocked"]:
            # ⭐the cheapest tier of each family (never opus)
            entry["live"] = live_check(cfg, family, {"claude": CLAUDE_MODELS, "codex": CODEX_MODELS}[family][0])
        facts["families"][family] = entry
    if unused_codex_home(cfg):
        facts["unused_codex_home"] = str(cfg["codex_home"])
    return facts


def unused_codex_home(cfg: dict) -> str:
    """`codex_home`, written into config.json by an old install (Task 0c-13, that era's default, never something
    he chose) ⇒ one sentence of plain English; nothing there ⇒ an empty string.
    ⛔Never reads it any more, never rewrites config.json for him (the maintainer's ruling); doctor prints it, and
    starting the bridge also logs one line to bridge.log (13c review M10: someone who deliberately set up
    isolation back then would have it quietly disappear ⇒ it has to complain loudly in at least two places, and
    also tell him this key can be deleted)."""
    if "codex_home" not in cfg:
        return ""
    return ("config.json's codex_home (%s) is no longer used: the codex this bridge starts uses your own CODEX_HOME (~/.codex if unset); "
            "to have it use a different directory, set the CODEX_HOME environment variable; this key can be deleted from config.json" % cfg["codex_home"])


def _carried_lines(stat, said) -> list:
    """The lines for "what codex will carry into every call". `said` = what codex itself said in that one real
    session from `doctor --live`: if there is one, trust only it (more accurate than stat: it gets both "the
    override is whitespace only" and an AGENTS.md in a git ancestor directory right); none / could not tell ⇒
    plain doctor's lines (a stat-only estimate), and it has to say when it could not tell (`--live` must never
    say less than plain doctor, and must never pass an estimate off as something codex said). 📎 NOTES.md::codex-user-home"""
    def size(f):
        return "%d bytes" % f["bytes"] if f.get("error") is None else "could not read its size: " + f["error"]

    def one(f):
        return "⚠️ codex will carry your %s (%s) into every call: codex has no switch to turn it off, and this bridge cannot block it" % (f["path"], size(f))

    if said and "sources" in said:
        return [one(f) for f in said["sources"]]
    stat = stat or {}
    out = [one(f) for f in stat.get("files") or ()]
    if stat.get("if_blank"):
        out.append("⚠️ the override is there, but if it is all whitespace codex will switch to AGENTS.md (%s): %s "
                   "(a --live check asks codex itself which one it is using)" % (size(stat["if_blank"]), stat["if_blank"]["path"]))
    if said:
        out.append("⚠️ could not tell which instruction files this session will carry: %s ⇒ the lines above only "
                   "estimate from files under CODEX_HOME, and cannot see one in a git ancestor directory"
                   % said["error"])
    return out


def doctor_verdict(facts: dict) -> tuple:
    """facts -> human-facing lines + the families with problems. ⭐The pasteable command (the login one inside
    blocked) is printed with real line breaks: the copy in the JSON is escaped.
    ⚠️Wording (an earlier measurement): `auth status` / `login status` only say "there are local credentials",
    never "the credentials still work" — the real test is `--live`.
    ⭐The whole `--live` line is only printed once, at the end (hanging one on every family's own line would
      flood the screen)."""
    lines, bad, want_live = [], [], False
    for fam in ("claude", "codex"):
        env = facts["env"]
        e, gone = facts["families"].get(fam), (env["missing"] if fam == "codex" and not env["of_bridge"] else [])
        if e is None:
            if gone:        # ⭐the missing variable is exactly why it could not be found (15c ⑦6): never call it "not on PATH", never send the user to install something already installed
                bad.append(fam)
                lines.append("❌ codex: not found — %s's environment has no %s (the desktop build's own bundled codex is found through it), most likely an "
                             "agent running commands for you filtered the environment ⇒ please start the bridge in your own terminal:" % (env["whose"], ", ".join(gone)) + NL + self_cmd("start"))
            elif fam == "codex" and env["own_missing"]:     # the bridge is running, and only this terminal itself is missing it (fix2, re-review N-M3): say clearly this only concerns this terminal, never a ❌
                lines.append("· codex: this terminal's own probe did not find it — this terminal's environment has no %s (the desktop build's own bundled codex is found through it); "
                             "this says only about this terminal, and has nothing to do with the bridge that is running" % ", ".join(env["own_missing"]))
            else:
                lines.append("· %s: not found on this machine (not on PATH, and config.json's %s_bin does not point to one either)" % (fam, fam))
            continue
        auth, live = e["auth"], e.get("live")
        if e["blocked"]:
            bad.append(fam)
            lines.append("⛔ %s: this family is not reported: %s" % (fam, e["blocked"]))
        elif auth["probe_error"]:
            bad.append(fam)
            lines.append("⚠️ %s: could not tell whether there are local credentials: %s" % (fam, auth["probe_error"]))
        elif not auth["logged_in"]:
            bad.append(fam)
            lines.append("⚠️ %s: no local credentials ⇒ run this yourself (copy the whole line): %s%s" % (fam, NL, e["login_cmd"]))
        else:
            want_live = want_live or live is None
            lines.append("✅ %s %s: local credentials found (%s)%s" % (
                fam, _cli_version(e["version"]), "/".join(x for x in (auth["method"], auth["plan"]) if x),
                " — whether the credentials still work needs one real call with --live to know (the command is at the end)" if live is None else ""))
        if not e["blocked"] and facts["running"]:
            # ⭐I-4: doctor's own detect() is fresh, but the running bridge only ever detected once, at start
            #   (README says so) ⇒ a family that comes back afterward (the player logs Codex in while the bridge
            #   keeps running) stays off until restarted, and doctor must not look all green about it. Judged by
            #   the bridge's own copy (`bridge_families`): missing entirely counts the same as `blocked: true`.
            bfam = (facts.get("bridge_families") or {}).get(fam)
            if bfam is None or bfam.get("blocked"):
                bad.append(fam)
                lines.append(("⚠️ the running bridge still has %s off (it checks only when it starts) ⇒ stop, "
                              "then start:" % fam) + NL + self_cmd("stop") + NL + self_cmd("start"))
        if live is not None and "error" in live:
            bad.append(fam)
            err = live["error"]
            step = e["login_cmd"] if err["type"] == "auth_required" else err["fix_hint"]
            # ⭐with no ready-made next step (`fix_hint` empty: the `unknown` classes), it settles for the original words — the handshake-refusal sentences already carry their own next
            #   step (`CODEX_REFUSED`); this used to tack on an empty arrow and a new line saying "see the original words above" (outside 13c's re-review scope)
            lines.append("❌ %s's real call failed (%s): %s" % (fam, err["type"], err["message"]) + (" ⇒" + NL + step if step else ""))
        elif live is not None and live["canary_leaked"]:
            bad.append(fam)
            lines.append("🔴 %s read the canary outside the working directory ⇒ this family's tools are not locked down, never connect it" % fam)
        elif live is not None and not live["ok"]:
            bad.append(fam)
            lines.append("⚠️ %s answered, but got the addition we asked wrong ⇒ read its own words and judge for yourself: %s" % (fam, live["answer"]))
        elif live is not None:
            # ⚠️only say what the judge actually knows (review M-3): what it judges is "is there a fragment of the secret in the answer", never "did it read it" — read it and stayed
            #   quiet, paraphrased it, pasted only a small fragment — this ruler cannot see any of those ⇒ never write it as "never read it"
            lines.append("✅ %s's real call went through (first token in %ss); the canary outside the working directory never showed up in the answer" % (fam, live["ttfc"]))
        if e.get("codex_off_unknown"):
            lines.append("⚠️ codex no longer recognizes these feature names it was told to turn off (most likely renamed ⇒ the new name may not be off): %s"
                         % ", ".join(e["codex_off_unknown"]))
        elif e.get("codex_off_error"):
            lines.append("⚠️ codex's list of features to turn off, this time, %s" % e["codex_off_error"])
        lines += _carried_lines(e.get("carried"), (live or {}).get("carried"))
    env = facts["env"]      # ⭐names only (15c ⑦2): the ones that change behaviour are his own settings ⇒ keep them, say clearly what they change; the stripped ones belong to the identity of the agent session that started it
    if env["of_bridge"] and env["missing"] and "codex" not in (facts["bridge_families"] or ["codex"]):
        bad.append("codex")         # ⭐judged by the bridge's own copy (fix1's addition): the environment it started with is missing it, and its /healthz genuinely has no codex either (could not tell ⇒ never draw a conclusion)
        lines.append("❌ codex: %s could not find it — the environment it started with has no %s (the desktop build's own bundled codex is found through it), most likely an agent running "
                     "commands for you filtered the environment ⇒ stop it, then restart in your own terminal:" % (env["whose"], ", ".join(env["missing"])) + NL + self_cmd("stop")
                     + NL + self_cmd("start"))
    lines.append("· the two families of variables passed to the child CLI by %s (names only): %s" % (env["whose"], ", ".join(env["passed"]) or "(none)"))
    lines.append("· the session variables stripped by %s (the identity of the agent session that started it, never passed to the child CLI): %s" % (env["whose"], ", ".join(env["stripped"]) or "(none)"))
    lines += ["⚠️ %s is in the environment (%s; you set it yourself, passed to the child CLI unchanged): %s" % (k, env["whose"], v) for k, v in sorted(env["changes"].items())]
    if facts.get("unused_codex_home"):
        lines.append("· " + unused_codex_home({"codex_home": facts["unused_codex_home"]}))
    kids = [c for c in facts["children"] if c["alive"]]
    lines.append("✅ the bridge is running (port %d)" % facts["port"] if facts["running"] else
                 "· the bridge is not running (no answer on port %d) ⇒ start it:" % facts["port"] + NL + self_cmd("start"))
    lines.append("· %d CLI child process(es) in the registry are still alive, %d MB total" % (len(kids), sum(c["rss_kb"] or 0 for c in kids) // 1024))
    if want_live:
        lines.append("· whether the credentials still work: one real call per family plus the canary self-check (costs a little quota):" + NL + self_cmd("doctor", "--live"))
    return lines, sorted(set(bad))


def cmd_doctor(args) -> int:
    """stdout starts with the whole JSON of facts (machine-readable: `json.JSONDecoder().raw_decode`), then the
    human-facing verdict."""
    facts = doctor_facts(load_config(), live=bool(getattr(args, "live", False)))
    lines, bad = doctor_verdict(facts)
    print(json.dumps(facts, ensure_ascii=False, indent=2) + NL)
    print(NL.join(lines))
    if bad:
        log("doctor: families with problems: %s" % ", ".join(bad))
        print("⇒ families with problems: %s (each one's own line above says what to do)" % ", ".join(bad))
    return 1 if bad else 0
def cmd_setup(args) -> int:
    """Run once after install: every probing action lives here and is deterministic, the agent's only job is to
    read the output back to the person exactly as it is (B31).
    ⭐= doctor (`--live` passed through) plus a "what's next" block. ⛔Never installs a startup launcher, never
      changes PATH, never logs in on someone's behalf (adding a startup launcher would mean re-judging E20's
      `allow_remote` window). The subcommands named in "what's next" are checked against real output by
      tests/test_70_local_api.py::PromisedCommands."""
    cfg = load_config()                       # also casts the local token while it is at it
    print("scv %s: configured at %s (the local token is already in there). First, a health check — facts, never conclusions:" % (VERSION, spath("config.json")))
    rc = cmd_doctor(args)
    print(NL.join(["", "What's next (copy the whole command line; if it's split by shell, pick the line for the one you use):",
                   "Start the bridge (OpenAI-compatible base_url: %s/v1):" % (LOCAL_URL % int(cfg.get("port") or 8765)), self_cmd("start"),
                   "See the local token (put it in the api_key field of an OpenAI-compatible client):", self_cmd("token"),
                   "One real call per family plus the canary self-check (costs a little quota):", self_cmd("doctor", "--live"),
                   'Only needed if you are connecting remote work (never paired = not one byte goes out to the internet): append " <url> --code <pairing code>" to this line and run it:',
                   self_cmd("pair"),
                   "Installing from setup.md for someone? Once the bridge is running, finish its step 6 (ask them about an audit) and step 8 (note where the bridge is, and tell them where you noted it)."]))
    return rc


PAIR_TOKEN_RE = re.compile("[!-~]{1,512}")     # printable ASCII, no whitespace: it has to land on disk, and go into the `Authorization` header of every call


def cmd_pair(args) -> int:
    """§6 step 5 (optional): trade a one-time pairing code for a token, stored in config.json. ⚠️Only handles the
    seed-stage path (a code sent by hand).
    ⭐Every failure path is judged out before config.json is ever written ⇒ the sentences saying "config.json was
      not touched" are true.
    ⭐The token is data from the far side of the network ⇒ it only lands on disk once its shape passes (one with a
      newline in it would only blow up urllib the next time it dials). ⚠️The code and the token never go into the
      log, never show on screen."""
    url = str(args.url).strip().rstrip("/")
    why = remote_url_refused(url)
    if why:
        return _cmd_failed("not paired (not one byte was sent, config.json was not touched): " + why,
                           "ask the other side for an address starting with https, swap it into the command above, and run it again")
    req = urllib.request.Request(url + REMOTE_PATHS["pair"], method="POST", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"code": str(args.code), "bridge_version": VERSION}).encode("utf-8"))
    try:
        reply = json.loads(_fetch(req, 30).decode("utf-8") or "{}")
    except NET_ERRORS as e:
        return _cmd_failed("could not pair (config.json was not touched): %s ⇒ %s: %s" % (url, type(e).__name__, repr(str(e))[:128]),
                           "403 = the code is wrong or already used (a pairing code is one-time, ask the other side for a new one); could not connect = "
                           "check the address, network, and proxy first (the proxy variables are visible in doctor):" + NL + self_cmd("doctor"), "the original words in full: " + _no_ctrl(_one_line(e)))
    token = reply.get("token") if isinstance(reply, dict) else None
    if not (isinstance(token, str) and PAIR_TOKEN_RE.fullmatch(token)):
        return _cmd_failed("could not pair (config.json was not touched): %s did not reply with a usable token (needs 1-512 printable ASCII characters, no whitespace)" % url,
                           "this is a problem on the other side: show this line to them")
    cfg = load_config()
    old = str(cfg.get("remote_url") or "")
    cfg.update(remote_url=url, remote_token=token)
    save_config(cfg)
    swapped = " (replacing the previous %s)" % old if old and old != url else ""
    log("pair: paired to %s%s" % (url, swapped))
    print(NL.join(["paired: %s%s. The token is stored in %s" % (url, swapped, spath("config.json")),
                   "from the next time the bridge starts, it will dial out to: %s" % ", ".join(p for k, p in REMOTE_PATHS.items() if k != "pair"),
                   "if the bridge is currently running, it is not using this yet: stop it, then start it again:", self_cmd("stop"), self_cmd("start"),
                   "to disconnect this side: delete remote_token from config.json",
                   "installing from setup.md for someone? after the restart, finish its step 6 (ask them about an audit) and step 8 (note where the bridge is, and tell them where you noted it)"]))
    return 0


COMMIT_RE = re.compile("[0-9a-f]{7,40}")       # ⭐always `fullmatch`: `match` plus `$` lets a trailing newline through (`_model_ok` fell into this same hole)
SHA256_RE = re.compile("[0-9a-f]{64}")
SCV_PY_MAX_BYTES = 2 * 1024 * 1024            # how big a scv.py can be (about 270 KB today): over this, whatever was fetched is not it (review M-7)


def cmd_update(args, target: Path | None = None) -> int:
    """B29: what gets pinned is the single file's sha256, never a tag. The hash comes from the dispatcher (or a
    release page), the file comes from the public repository — only if both sides are compromised at once does
    anything go wrong.
    ⭐Every judgment that can fail happens before anything is touched (shape / could not fetch / hash mismatch /
      not Python) ⇒ the sentences saying "nothing was touched" are true; the two writes to disk that follow are
      each atomic and each say clearly what they did: save the previous version first, and never swap if it
      cannot be saved (swapping without keeping a previous version means having nothing to roll back to).
    ⭐Written and compared by bytes: text mode on win32 turns LF into CRLF, and the hash would no longer be that
      hash. 📎 NOTES.md::update-bytes
    ⚠️`target` defaults to `__file__`: this is no longer a single-file-era assumption ("the whole bridge is this
      one file") that the module split has to revisit — it holds for good, since players always get the one file
      the build joins together, so `__file__` is the whole bridge."""
    target = Path(target) if target is not None else Path(os.path.abspath(__file__))
    commit, want = str(getattr(args, "commit", "") or ""), str(getattr(args, "sha256", "") or "").lower()
    source = "the command line"
    if not (commit or want):                  # never let half from the command line and half from latest.json get spliced into a pair that nobody actually gave
        latest = {}
        with contextlib.suppress(OSError, ValueError):
            latest = json.loads(spath("latest.json").read_text(encoding="utf-8"))
        latest = latest if isinstance(latest, dict) else {}
        commit, want = str(latest.get("commit") or ""), str(latest.get("sha256") or "").lower()
        source = "latest.json (given by the dispatcher at the bridge's last hello)"
    if not (COMMIT_RE.fullmatch(commit) and SHA256_RE.fullmatch(want)):
        return _cmd_failed("not updated (nothing was touched): need a pair, --commit <a 7-40 character lowercase hex commit id> and --sha256 <64 hex characters>, "
                           "what came from %s was %s / %s" % (source, repr(commit)[:48], repr(want)[:72]),
                           "give both together; give neither and it uses the pair the dispatcher gave a paired bridge's last hello (%s)" % spath("latest.json"))
    try:
        current = target.read_bytes()
    except OSError as e:
        return _cmd_failed("not updated (nothing was touched): could not read the current copy at %s: %s" % (target, _one_line(e)), "see the OS's own words above")
    if hmac.compare_digest(hashlib.sha256(current).hexdigest(), want):
        print("already at this version (%s's sha256 is already %s), nothing was touched" % (target, want))
        return 0
    url = "%s/%s/scv.py" % (UPDATE_BASE, commit)
    try:
        data = _fetch(urllib.request.Request(url), 60, limit=SCV_PY_MAX_BYTES)
    except NET_ERRORS as e:
        return _cmd_failed("not updated (nothing was touched): could not fetch %s ⇒ %s: %s" % (url, type(e).__name__, repr(str(e))[:128]),
                           "404 = the public repository has no such commit; could not connect = check the network and proxy first (the proxy variables are visible in doctor):" + NL + self_cmd("doctor"),
                           "the original words in full: " + _no_ctrl(_one_line(e)))
    got = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(got, want):
        return _cmd_failed("not updated (nothing was touched): the sha256 does not match. expected %s / actual %s / file from %s" % (want, got, url),
                           "the expected value came from %s; one side or the other is wrong — never work around this, go ask whoever gave you this pair" % source)
    try:
        ast.parse(data.decode("utf-8"))
    except (SyntaxError, ValueError, RecursionError) as e:
        return _cmd_failed("not updated (nothing was touched): the hash matches, but the content is not valid Python: %s: %s" % (type(e).__name__, _one_line(e)),
                           "show this line to whoever published this pair")
    prev = Path(os.path.abspath(spath(SCV_PREV)))   # every place a person sees uses an absolute path (a relative SCV_HOME follows the cwd; outside the re-review's scope ④)
    try:
        _atomic_write(SCV_PREV, current)
    except OSError as e:
        return _cmd_failed("not updated (nothing was touched): could not save the previous version (%s): %s" % (prev, _one_line(e)),
                           "if the previous version cannot be saved, never swap: fix the state directory's (SCV_HOME) problem first, then run that same command again")
    try:
        _replace_file(target.with_name(target.name + ".new"), target, data)   # the exception is written into the derived gate (review M-2)
    except OSError as e:
        return _cmd_failed("did not swap: %s is still the previous version (%s); %s is already a copy of it" % (target, _one_line(e), prev),
                           "most likely something else has it open (an editor / antivirus) or there is no write permission; fix that, then run that same command again")
    log("update: %s was swapped to %s (sha256 %s, this pair came from %s); the previous version is at %s" % (target, commit, got, source, prev))
    # ⭐both paths are turned absolute (`state_dir()` never expands `~` or makes paths absolute: a relative path would follow the cwd at the moment it is pasted, review M-3)
    diff = paste_cmd(["git", "diff", "--no-index", os.path.abspath(prev), os.path.abspath(target)])
    print(NL.join(["updated to %s (sha256 %s; this pair came from %s)" % (commit, got, source),
                   "the previous version is left as-is at: %s (to roll back, copy it back to %s)" % (prev, target),
                   "to see what changed this time: both copies are on this machine; if git is installed, run (copy the whole line):", diff]
                  + ["⚠️ if the bridge is currently running, it is still the old code: stop it, then start it again:", self_cmd("stop"), self_cmd("start")]))
    return 0
def main(argv: list | None = None) -> int:
    if sys.version_info < MIN_PY:
        print("this bridge needs Python %d.%d or newer" % MIN_PY, file=sys.stderr)   # stdout is reserved for the subcommand's own output
        return 2
    # 🔴when piped / redirected, stdout follows the locale encoding (gbk on Chinese Windows) and is strict ⇒ a single ✅ becomes a UnicodeEncodeError
    #   (an agent reading the output happens to always go through a pipe). stderr is already backslashreplace.
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(errors="replace")
    # 🔴when it is not a console (piped / redirected), switch both streams to UTF-8 (Task 14 review I6 measured: on Chinese Windows an agent
    #   reading through a pipe got GBK bytes, and on an English locale the Chinese all turned into `?`). Never touch the console path (CPython
    #   goes through wide characters on the Windows console, unrelated to the locale encoding).
    for stream, errs in ((sys.stdout, "replace"), (sys.stderr, "backslashreplace")):
        with contextlib.suppress(AttributeError, ValueError):
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors=errs)
    ap = argparse.ArgumentParser(prog="scv", description="local agent bridge")
    sub = ap.add_subparsers(dest="cmd")
    # ⭐one `add_parser` call per name (never a loop): the `PromisedCommands` gate recognizes, by literal text, "the subcommand named in an error message really exists"
    sub.add_parser("version", help="print just the version number")
    sub.add_parser("run", help="start the bridge in the foreground (Ctrl+C to stop)").add_argument("--ticket", default="", help=argparse.SUPPRESS)
    sub.add_parser("start", help="start the bridge in the background")
    sub.add_parser("stop", help="stop the bridge running in the background")
    sub.add_parser("status", help="whether the bridge is running, and its state if so")
    sub.add_parser("token", help="print the local API's token")
    sub.add_parser("doctor", help="health check").add_argument("--live", action="store_true",
                                                    help="one real call per family (costs a little quota), and runs the canary")
    sub.add_parser("setup", help="run once after install: health check + what to do next").add_argument(
        "--live", action="store_true", help="one real call per family during the health check (costs a little quota)")
    pr = sub.add_parser("pair", help="connect to a remote dispatcher with a one-time pairing code (optional)")
    pr.add_argument("url")
    pr.add_argument("--code", required=True)
    up = sub.add_parser("update", help="switch to the public repository's scv.py (for some commit, pinning its sha256)")
    up.add_argument("--commit", default="")
    up.add_argument("--sha256", default="")
    args = ap.parse_args(argv)
    if args.cmd == "version":
        print(VERSION)
        return 0
    table = {"run": cmd_run, "start": cmd_start, "stop": cmd_stop, "status": cmd_status, "token": cmd_token,
             "doctor": cmd_doctor, "setup": cmd_setup, "pair": cmd_pair, "update": cmd_update}
    if args.cmd not in table:
        ap.print_help()
        return 2
    try:
        return table[args.cmd](args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as e:      # ⭐the top-level handler (Minor 6): any exception a subcommand does not catch lands on disk and gets a human sentence, never just a bare traceback
        # ⚠️never touch `spath()` here: it creates directories, and the reason execution reached this point may be exactly "the state directory cannot be written to"
        hint = ("most likely the state directory (SCV_HOME, ~/.scv by default) cannot be read or written: disk full? no permission? pointed somewhere wrong?" if isinstance(e, OSError)
                else "this is an error the bridge itself failed to catch: paste the last few lines of bridge.log to the maintainer")
        return _cmd_failed("the %s subcommand did not succeed: %s: %s" % (args.cmd, type(e).__name__, _one_line(e)),
                           hint + " (this line has already been logged to bridge.log, if it could be written)")


if __name__ == "__main__":
    sys.exit(main())
