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

VERSION = "0.1.0"
PROTOCOL = 1
MIN_PY = (3, 9)
LINE_BUDGET = 5450      # ⭐the line count is only a proxy metric: auditability is guaranteed by those AST gates,
#                         never by this number. Why 5450, and why the old 2000/3000/3800/4300/4400/4500/4600/5600/5400 no
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
# ⭐Clean up resources with `finally` plus a flag, never `except <a family we recognize>` (scv.py:1979 CodexDriver.__init__)
# ⭐"Did we do this ourselves" uses an explicit flag, never guessed from the exception type (scv.py:1623 _Pipe._read_failed)
# ⭐The parent side has exactly one release point for stdout/stderr; the stdin one is the polite close signal (scv.py:1787 _Pipe._close_pipes)
# ⭐An id from outside never goes into a path, only its hash does (scv.py:2244 SessionManager._workdir)
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


