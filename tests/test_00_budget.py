# -*- coding: utf-8 -*-
"""Bulk gate + the top three lockdown sections. ⭐What this file tests is "can an auditor answer three questions
just by grepping", ⛔ not code style.

The later half — the few Contract classes — tests the **runtime** half of those same three questions: does
spath() really blow up, does the token really only ever get minted once, does it stay loud when it cannot write
to the log. ⭐The AST gates only ever look at the source text, they never execute scv.py — so both halves have to
exist.
"""
import ast
import collections
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
NL = chr(10)
with io.open(os.path.join(ROOT, "scv.py"), encoding="utf-8") as _f:   # ⛔ never a bare open: a leaked handle throws a ResourceWarning
    SRC = _f.read()
TREE = ast.parse(SRC)

import scv  # noqa: E402  ⭐just an import (touches no disk); SCV_HOME is always set inside setUpModule

# Both branches use the interpreter's own standard library (the list on 3.10+, its directories on 3.9) ⇒ what it
# blocks is exactly what B2 cares about — a **third-party dependency** — never "the names someone predicted". (A
# hand-written fallback used to stand here; it drifted twice: first missing `select`/`shlex`/`socket`, then
# blind to every stdlib module scv.py does not use.)
def _stdlib_names():
    """The standard library's top-level names. 3.10+ ships the list (`sys.stdlib_module_names`); on 3.9 it is read
    off the interpreter's own standard-library directories. (The hand-written fallback this replaced knew only
    scv.py's own modules, so the positive control's `import webbrowser` looked third-party on 3.9 — the first CI
    run.) Third-party packages live in site-packages, which has no `__init__.py` and is never listed."""
    names = getattr(sys, "stdlib_module_names", None)
    if names:
        return frozenset(names)
    import pkgutil
    import sysconfig
    bases = {sysconfig.get_paths()["stdlib"], sysconfig.get_paths()["platstdlib"]}
    dirs = [d for b in bases for d in (b, os.path.join(b, "lib-dynload"))] + [os.path.join(sys.base_prefix, "DLLs")]
    found = {m.name for m in pkgutil.iter_modules([d for d in dirs if os.path.isdir(d)])}
    return frozenset(found | set(sys.builtin_module_names))


STDLIB = _stdlib_names()

# ⭐**Every** module/name scv.py brings in is named here (this is the reading `imported_names()` uses). ⛔One extra
#   one has to be an explicit decision each time: every other AST gate only recognizes the modules on its own
#   table, and never looks past it (the network gate's own boundary is in `net_call_pred`) ⇒ this is the one door
#   that "nothing outside the table gets in". 🔴It and `test_stdlib_only` do ⛔ not guard the same thing: that one
#   guards against a **third-party dependency** (B2), and it is blind to one extra stdlib module coming along for
#   the ride (`logging.handlers`/`webbrowser`/`multiprocessing.connection` all reach out to the network) — this
#   one guards against exactly that. Each has its own positive control.
SCV_IMPORTS = {
    "__future__.annotations", "argparse", "ast", "collections", "contextlib", "ctypes", "functools", "hashlib",
    "hmac", "http.client", "http.server.BaseHTTPRequestHandler", "http.server.ThreadingHTTPServer", "ipaddress",
    "json", "os", "pathlib.Path", "queue", "re", "secrets", "select", "shlex", "shutil", "signal", "socket",
    "subprocess", "sys", "threading", "time", "urllib.error", "urllib.parse", "urllib.request", "uuid"}

# ⭐Adding a network call = have to name that function right here, ⛔ never sneak one in.
# Before Task 11 this was an **empty set** (i.e. "scv.py makes zero network calls" — a real assertion, not a real
# vacuous green); the remote leg was the first thing to reach outward, and all three names live inside
# `RemoteLeg`: building the request, sending it back, receiving the stream. ⛔One extra name has to be an explicit
# decision each time.
# Task 12 added `local_get`: the subcommand asking its own `/healthz`, dialing **loopback only** (the address is
# section ①'s `LOCAL_URL`), and bypassing the environment proxy. 0.3.0 split it: the dialing half is now
# `local_fetch` (raw bytes: the wake subcommand knocks on `GET /wake`, which answers a page), and `local_get` only
# reads those bytes as JSON — so `local_fetch` is the name on this list.
# Task 13 (carry-forward 2/3): "dial once, read back the response body" was folded into **one door**, `_fetch`
#   (the whole exception family + `HTTPError.close()` + a cap on how much of the response body it reads —
#   `_post`/`local_get` each used to copy their own version, and this batch would have had to copy two more) ⇒
#   `_post` no longer dials on its own (it builds the request through `_request`, dials through `_fetch`) — it
#   goes out from here; the two new outbound calls, `cmd_pair` (POST the pairing code) / `cmd_update` (GET the
#   public repo's scv.py), each build their own `Request` and are named here.
# Task 13 fix1 (review M-8): dialing itself gained one more layer ⇒ **`_open` is the one and only place that
#   builds an opener, the one and only place that dials** (it fits every call with `_Redirects`: one carrying a
#   token never follows a redirect, ⛔ never follows an https downgrade either); `_fetch` (reads the reply — the
#   `limit=0` branch ⛔ never reads) and `_stream_once` (reads the stream) both go through it, so they are out from
#   here too. "Who calls `_open`/`_fetch`" is pinned down by tests/test_95_setup_pair_update.py::Doors (⛔
#   otherwise this gate would be blind to a new caller reaching the dial through the door).
NET_CALLERS = {"_request", "local_fetch", "_open", "cmd_pair", "cmd_update"}
# Network-facing modules: after `from X import Y`, `Y(...)`'s dotted full name **no longer has the module name in
# it** ⇒ these names have to be collected first.
# ⭐The last five were merged in by re-review two, I-2 (a pure tightening: today's scv.py uses none of them):
# sending/receiving mail or newsgroup posts, reaching out through the browser,
# `multiprocessing.connection.Client` dialing an address directly.
NET_MODULES = ("socket", "ssl", "http", "urllib", "ftplib", "smtplib", "telnetlib", "asyncio", "xmlrpc",
               "imaplib", "poplib", "nntplib", "webbrowser", "multiprocessing")

# The **loader** of native code: every ctypes name that can turn a DLL/shared library into something callable,
#   plus kernel32's own `LoadLibrary*`. 🔴`ctypes` can reach any native network/process/file API directly
#   (`WinDLL('winhttp')`, `ws2_32`) — every gate above is **completely blind** to it ⇒ the whole file is only
#   allowed one door: the one `WinDLL("kernel32")` call inside `_k32`, and the only thing it hands back out is the
#   six functions in `K32_EXPORTS`. ⚠️This is a door against **slipping**, ⛔ not against a deliberate bypass:
#   every ctypes function object keeps its DLL alive in its own private `_objects` (re-review three, D2), and both
#   the crack next to the door and the one square that still has no gate over it are written into `native_door`'s
#   boundary.
NATIVE_LOADERS = ("WinDLL", "CDLL", "OleDLL", "PyDLL", "windll", "cdll", "oledll", "pydll", "pythonapi",
                  "LibraryLoader")
K32_EXPORTS = {"OpenProcess", "GetProcessTimes", "WaitForSingleObject", "K32GetProcessMemoryInfo", "CloseHandle",
               "SetThreadExecutionState"}
# ⭐Every `ctypes` name scv.py ever touches (this is the reading `ctypes_surface()` uses) is named here. ⛔One extra
#   one has to be an explicit decision each time: `ctypes._dlopen`/`WINFUNCTYPE`/`CFUNCTYPE`/`cast` are **not
#   called loaders**, yet they can just as easily turn a DLL or a bare address into something callable ⇒
#   `native_door` is blind to them (re-review three, D3, measured: stuffed into the real source, all 49 stayed
#   green).
CTYPES_SURFACE = {"POINTER", "Structure", "WinDLL", "byref", "c_int", "c_size_t", "c_uint32", "c_uint64", "c_void_p",
                  "get_last_error", "sizeof"}

IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
HOSTNAME = re.compile(r"\b[a-z0-9-]+\.[a-z]{2,}\b", re.I)
# ⭐**A hostname with no dot in it is entirely outside both judges' scanning surface below**: `HOSTNAME` needs a
#   dot plus a TLD, `IPV4` needs four groups of digits ⇒ a bare literal `"localhost"`/`"::1"` written outside
#   section ① sails right through this gate (measured). And those two are exactly what the next person adding a
#   feature is most likely to write down as a literal. ⇒ Add them by **exact equality** (⛔ not substring: that
#   would judge prose like "running on localhost" as an address too — trading a miss for a false alarm, and this
#   very gate's own help text says not to do that).
BARE_HOSTS = {"localhost", "::1", "0:0:0:0:0:0:0:1", "ip6-localhost"}
# ⭐These suffixes are **filenames**, not hostnames: config.json/bridge.log would hit HOSTNAME dead center
#   (cleaning up this one took a positive control on both ends)
NOT_HOSTS = {"json", "log", "py", "pid", "tmp", "txt", "md", "lock", "exe", "sh", "bat", "ini", "toml", "yml", "yaml"}

# Whoever trips this gate usually reacts by thinking "the gate is just dumb" ⇒ and goes and loosens the two
# judges above. ⭐Being wrong has to be loud, and loud has to be useful: put the right move right in the failure
# message, or this friction gets worn away the next time someone hits it.
STRAY_ADDR_HELP = (
    "An address literal is only allowed to appear in the `# ━━ ①` section. "
    "If this is a false positive (e.g. PowerShell's `X.Y` property chain lands dead center on HOSTNAME, with the "
    "TLD read as `Y`) ⇒ **rewrite your literal**: put a `)` in front of the dot, or switch to "
    "`| Select-Object -ExpandProperty Y`. "
    "⛔Never add a word to NOT_HOSTS, ⛔never loosen the HOSTNAME regex — that trades a miss for a false alarm, "
    "and a gate that misses is worse than no gate at all (a CamelCase exemption added on a whim would let a real "
    "address like `Api.Example.Com` slip through with it).")

_HOME = None
_OLD_HOME = None


def setUpModule():
    """⛔Never build it at import time: discover imports every test module first, and changing SCV_HOME at import
    time means "whichever module is imported last wins"."""
    global _HOME, _OLD_HOME
    _OLD_HOME = os.environ.get("SCV_HOME")
    _HOME = tempfile.mkdtemp(prefix="scv-test-")
    os.environ["SCV_HOME"] = _HOME


def tearDownModule():
    if _OLD_HOME is None:
        os.environ.pop("SCV_HOME", None)
    else:
        os.environ["SCV_HOME"] = _OLD_HOME
    shutil.rmtree(_HOME, ignore_errors=True)


def _docstrings(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def string_constants(tree):
    """[(the function it sits in, line number, value)]. Docstrings do not count — the engine's own shape-gate once
    tripped over "it matched its own docstring"."""
    docs, found = _docstrings(tree), []

    def visit(node, fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node.name
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            found.append((fn, node.lineno, node.value))
        for child in ast.iter_child_nodes(node):
            visit(child, fn)

    visit(tree, "<module>")
    return found


def call_sites(tree, want):
    """[(the function it sits in, line number, dotted full name)]; `want(full name) -> bool` says which calls
    count."""
    found = []

    def dotted(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            head = dotted(node.value)
            return (head + "." + node.attr) if head else node.attr
        return ""

    def visit(node, fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node.name
        if isinstance(node, ast.Call):
            name = dotted(node.func)
            if name and want(name):
                found.append((fn, node.lineno, name))
        for child in ast.iter_child_nodes(node):
            visit(child, fn)

    visit(tree, "<module>")
    return found


def attr_sites(tree, names):
    """[(the function it sits in, line number, attribute name)]. ⭐Scans Attribute nodes, not Call nodes ⇒ both
    `p.with_name(x)` and `p.parent / x` are covered by one judge."""
    found = []

    def visit(node, fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node.name
        if isinstance(node, ast.Attribute) and node.attr in names:
            found.append((fn, node.lineno, node.attr))
        for child in ast.iter_child_nodes(node):
            visit(child, fn)

    visit(tree, "<module>")
    return found


def _no_backslash(src):
    """The backslash gate's **one and only judge**: both the real source and a synthetic sample have to be fed to
    **this one** function.

    🔴⛔Never copy the expression again inside a test case (`SRC.count(chr(92)*2)`/`SRC[:0].count(...)`): when the
      copy gets broken (measured on a real `scv.py` that really does have backslashes, review found 28 cases where
      both rewrites stayed green), the gate's own line stays green right along with it.
    ⭐Sharing the exact same judge means that if it ever breaks, **the synthetic sample's line goes red first**, so
      the gate can never silently stop working."""
    return src.count(chr(92))


GATE_RE = re.compile(r"(tests/[A-Za-z0-9_]+\.py)::([A-Za-z_][A-Za-z0-9_]*)::([A-Za-z_][A-Za-z0-9_]*)")


def gate_missing(path, klass, test):
    """Whether a `tests/x.py::Class::test_y` pointer points at nothing: returns a reason, `""` = it really is
    there.

    🔴**Both call sites share this one judge** (the Retired block, and the ones the bulk docstring lists), ⛔ never
      copy a second one: if the copy gets broken, the other call site stays green right along with it (this same
      file's `_no_backslash`/`pointer_ok` exist for exactly this reason).
    ⭐It exists for the same reason as the "Retired" block: **a pointer to something that does not exist is worse
      than no pointer at all — it lets someone believe it has already been checked.**"""
    full = os.path.join(ROOT, path.replace("/", os.sep))
    if not os.path.exists(full):
        return "points at a file that does not exist: " + path
    with io.open(full, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    cls = next((n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == klass), None)
    if cls is None:
        return "points at a class that does not exist: " + klass
    if test not in [n.name for n in ast.walk(cls) if isinstance(n, ast.FunctionDef)]:
        return "points at a test that does not exist: " + klass + "::" + test
    return ""


def pointer_ok(want, span):
    """The settled-idiom pointer's **one and only judge**: the gate and both controls are all fed **this one**
    function.

    🔴⛔Never copy it again inside a test case: when the copy gets broken, the gate's own line stays green right
      along with it (this same file's `_no_backslash` exists for exactly this reason, and I made the same
      mistake here again)."""
    lo, hi = span
    return lo <= want <= hi


def net_addrs(v):
    """The parts of a string that look like a network address. ⛔A plain path (/bridge/hello) and a filename
    (config.json) do not count.

    🔴🔴**This judge's boundary — it being green does not mean "every address literal is inside section ①"**
      (copied straight from `test_state_dir_only_reached_through_spath`'s own disclaimer): it covers ①a scheme
      ②an IPv4 address ③a dotted hostname ④one of the bare names in `BARE_HOSTS` (with or without a port, both
      count — `localhost:8765` is the one re-review caught). **What it does not cover**: any other undotted
      hostname (`myhost`), an address pieced together (`"local" + "host"`), the **bracket form**
      (`[::1]:8765`), a bare numeric port. ⇒ Whoever adds a network feature **⛔ must never rely on this gate
      alone**.
    ⚠️The bare-name check only recognizes "**the whole value is exactly that** (optionally with one port)" ⛔ not a
      substring: a substring match would judge prose like "running on localhost" as an address too — **trading a
      miss for a false alarm**, and this gate's own `STRAY_ADDR_HELP` says not to do that."""
    hits = []
    if "http://" in v or "https://" in v:
        hits.append("scheme")
    hits += IPV4.findall(v)
    hits += [h for h in HOSTNAME.findall(v) if h.rsplit(".", 1)[1].lower() not in NOT_HOSTS]
    bare = v.strip().lower()
    if bare in BARE_HOSTS or (bare.count(":") == 1 and bare.split(":")[0] in BARE_HOSTS):
        hits.append(bare)
    return hits


# Which module-qualified calls count as "reaching out from the very first segment": ⭐**derived from
#   `NET_MODULES`** ⛔ never a separate table written by hand (the two tables used to each be written separately,
#   and `asyncio` was in one but not the other ⇒ `asyncio.open_connection(...)` slipped through, review I-B).
#   The whole `http` family ⛔ does not count (`http.server` is a local listener) — only the `http.client` branch
#   counts (see `is_net_module`).
NET_HEADS = tuple(m for m in NET_MODULES if m != "http")
# The judge's **false-positive side**: these branches only ever do string work or listen locally, ⛔ they never
#   reach outward. ⚠️Every one added here needs a reason, and a false-positive-side control to go with it
#   (`local*` inside `test_the_net_call_judge_sees_the_ways_around_it`).
#   `urllib.parse`: only slices strings (the plaintext gate `_loopback_http` needs it to read a hostname);
#   `http.server`: the local API itself.
NOT_NET = ("urllib.parse", "http.server")


def is_net_module(name):
    if any(name == p or name.startswith(p + ".") for p in NOT_NET):
        return False
    return name.split(".")[0] in NET_HEADS or name == "http.client" or name.startswith("http.client.")


def net_imports(tree):
    """The bound names counted as "reaching out from the first segment", from two sources: ①the Y in
    `import <a network module> as Y` ②the X in `from <package> import X` where **`<package>.X` is itself
    network-facing**.
    ⚠️② is judged by **the full name** (⛔ not just the package name): `from urllib import request` ⇒
      `urllib.request` is ⇒ afterward `request.urlretrieve(...)`'s first segment is `request`, and it still
      counts; `from http import client` the same way; `from http.server import ThreadingHTTPServer` ⇒
      `http.server.…` ⛔ does not ⇒ calling it bare **does not count** as reaching out (review M-E's false-positive
      case).
    ⚠️Both ① and ② were added later: ① on the morning of 2026-09-23 (`import urllib.request as _u`), ② that
      afternoon (review I-B): after `from urllib import request`/`from http import client`, a call on
      `submodule.*`, plus `asyncio.open_connection`, stuffed into `scv.py` ⇒ 42 cases in this gate all stayed
      green)."""
    heads = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            heads |= {(a.asname or a.name) for a in node.names
                      if is_net_module((node.module or "") + "." + a.name)}
        elif isinstance(node, ast.Import):
            heads |= {a.asname for a in node.names if a.asname and is_net_module(a.name)}
    return heads


def net_call_pred(tree):
    """The **one and only judge** of "is this call reaching out" (the gate and every control are all fed this
    one).

    🔴**The boundary is stated in the positive** (⛔ never rewrite it as "the only thing it still cannot see is X
      and Y": that sentence has already overreached three separate times — re-review two, I-2 stuffed 5 unrelated
      writing styles into scv.py and all 43 cases stayed green). **What it covers, and only this**: modules on the
      `NET_MODULES` table, reached through one of the four writing styles below —
      ① the tail name is `urlopen`/`Request`/`create_connection`/`connect`
      ② module-qualified: `socket.*`/`urllib.*`/`asyncio.*`/`webbrowser.*`/`multiprocessing.*`…… (`NET_HEADS`,
        derived from `NET_MODULES`) plus `http.client.*`
      ③ `from <package> import X` where `<package>.X` is on the table ⇒ afterward `X(...)`/`X.*` (this includes
        `from urllib import request`)
      ④ `import <a module on the table> as Y` ⇒ afterward `Y.*`
      The false-positive side only lets through the two branches in `NOT_NET` (`urllib.parse`/`http.server`).
    ⛔**Nothing else is covered at all.** For example (each one measured, and each one really does slip through):
      · a module off the table: `logging.handlers.HTTPHandler`/`SocketHandler`, `ctypes` (`WinDLL('winhttp')`) —
        the door they would come in through is watched by `test_the_import_list_is_pinned` (the import set is
        pinned) and `test_native_code_has_one_door` (native code has one door), ⛔ never this one;
      · an aliased assignment: `o = urllib.request.urlretrieve; o(u)`, `m = urllib.request; m.urlretrieve(u)`;
      · reflection: `__import__(...)`, `importlib.import_module(...)`, `getattr(module, "name")`;
      · a name re-exported through another module: `http.server.socket.socket()`,
        `http.server.http.client.HTTPSConnection(h)`;
      · starting a child process to reach out (curl/wget/`Invoke-WebRequest`): the real gate here is `run_cli`
        having only one door + `test_cli_subcommand_literals_only_in_argv_builders`.
      ⇒ Whoever adds a network feature **⛔ must never rely on this gate alone**. Aliased assignments,
      `__import__`/`getattr` reflection, and re-exports — **no gate today can see any of the three**
      (`importlib`-style reflection would first need `import importlib`, which trips the import-set door).
    ⚠️③④ and `urllib.*` were all added later (before that, a bare call to
      `from http.client import HTTPSConnection`, `urllib.request.urlretrieve(url)`,
      `import urllib.request as _u; _u.build_opener().open(url)` all slipped through entirely).
      ⭐Adding a module to `NET_MODULES` is a **pure tightening** (today's scv.py only uses a table module inside
      those three whitelisted functions), ⛔ there is no "trade a miss for a false alarm" exchange happening here.
      Each writing style has its own positive control."""
    heads = net_imports(tree)

    def want(name):
        head, tail = name.split(".")[0], name.split(".")[-1]
        return (tail in ("urlopen", "Request", "create_connection", "connect")
                or is_net_module(name) or head in heads)

    return want


is_net_call = net_call_pred(TREE)


def imported_names(tree):
    """What this source imports (**the whole file**, including imports written inside a function): `import a.b`
    ⇒ `"a.b"`; `from a.b import X` ⇒ `"a.b.X"`. ⭐A from-import carries **the name it bound**, ⛔ the module name
    alone is not enough: `from http.server import socket` (a re-export riding through) would look exactly like
    today's line if only the module name were recorded."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            out |= {"." * node.level + (node.module or "") + "." + a.name for a in node.names}
    return out


def non_stdlib(tree):
    """B2's **one and only judge** (the gate and its positive control are both fed this one): the top-level
    packages brought in by an import that are not on the stdlib list."""
    return {n.split(".")[0] for n in imported_names(tree)} - STDLIB


def _is_loader(name):
    return name in NATIVE_LOADERS or name.startswith("LoadLibrary")


def _tail(func):
    return func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else "")


def native_door(tree):
    """The **one and only judge** of "which door does native code come in through" (the gate and its positive
    control are both fed this one). Returns three things:
    ①every place a loader is mentioned, `{(the function it sits in, name)}` (a Name, an Attribute, or a name from
      `from ctypes import X` all count)
    ②each time a loader is **called**, its first argument: a literal records that value, ⛔ anything not a literal
      records `None`
    ③how whatever `_k32` catches from calling the loader **gets used**: `dll.X` records `X`, using it bare
      (`return dll`, passing it to someone else) records `"<used bare>"`
    ⚠️Boundary (this only recognizes a loader **by name** — the following are all invisible to it; re-review
      three, D2/D3 measured that the first two can really open the door):
      ①**a private attribute**: `_k32().OpenProcess._objects["0"]` is the whole of kernel32 (every ctypes
        function object keeps its DLL alive inside it), `ctypes._dlopen("ws2_32")`; ②**instantiating a function
        prototype**: `ctypes.WINFUNCTYPE(...)(("WSAStartup", an object carrying a _handle))`; ③taking a bare
        address and `ctypes.cast`ing it into a function, then calling it; ④reflection:
        `getattr(ctypes, "Win" + "DLL")`.
      ⇒ ②③④ on `ctypes.*` and `_dlopen` are watched by `test_the_ctypes_surface_is_pinned` (using `ctypes` itself
        bare counts too); a private attribute on any other object (`_objects`/`_handle`) is watched by
        `test_no_private_attribute_is_reached_off_self`.
      **Still no gate at all for**: reflection pieced together from a string (`getattr(f, "_obj" + "ects")`,
        `__import__("ctyp" + "es").cast`)."""
    sites, args, uses = set(), [], set()

    def visit(node, fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node.name
        name = node.id if isinstance(node, ast.Name) else (node.attr if isinstance(node, ast.Attribute) else "")
        if _is_loader(name):
            sites.add((fn, name))
        if isinstance(node, ast.ImportFrom):
            sites.update((fn, a.name) for a in node.names if _is_loader(a.name))
        if isinstance(node, ast.Call) and _is_loader(_tail(node.func)):
            first = node.args[0] if node.args else None
            args.append(first.value if isinstance(first, ast.Constant) else None)
        for child in ast.iter_child_nodes(node):
            visit(child, fn)

    visit(tree, "<module>")
    for k32 in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_k32"):
        held = {t.id for n in ast.walk(k32) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                and _is_loader(_tail(n.value.func)) for t in n.targets if isinstance(t, ast.Name)}
        dotted = set()
        for n in ast.walk(k32):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in held:
                uses.add(n.attr)
                dotted.add(id(n.value))
        uses.update("<used bare>" for n in ast.walk(k32) if isinstance(n, ast.Name) and n.id in held
                    and isinstance(n.ctx, ast.Load) and id(n) not in dotted)
    return sites, args, uses


def ctypes_surface(tree):
    """The **one and only judge** of "which ctypes things has scv.py touched" (the gate and its positive control
    are both fed this one):
    ①`ctypes.X` (including `c.X` after `import ctypes as c`) records `X`; ②`from ctypes import X` records `X`;
    ③the module object used **bare** (`c = ctypes`, `getattr(ctypes, …)`, passed as an argument) records
    `"<used bare>"` — once it is used bare, however it gets used afterward becomes invisible.
    ⭐Only records **the first level**: `ctypes.X.Y` records `X` (a submodule like `ctypes.util.…` would need its
      own `import ctypes.util`, which trips the import-set door).
    ⚠️Boundary: a module object obtained through `__import__("ctypes")` ⛔ is not recognized (see `native_door`'s
      boundary, "still no gate at all for")."""
    names = {"ctypes"} | {a.asname for n in ast.walk(tree) if isinstance(n, ast.Import)
                          for a in n.names if a.name == "ctypes" and a.asname}
    out, dotted = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in names:
            out.add(n.attr)
            dotted.add(id(n.value))
        elif isinstance(n, ast.ImportFrom) and n.module == "ctypes":
            out.update(a.name for a in n.names)
    out.update("<used bare>" for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in names and id(n) not in dotted)
    return out


def _private(name):
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def private_reaches(tree):
    """The **one and only judge** of "who is touching somebody **else's** private attribute" (the gate and its
    positive control are both fed this one). Returns two things:
    ①every `[(the function it sits in, line number, attribute name)]` where the attribute name is private (`_x`/
      `__x`, ⛔ never a dunder `__x__`) and the receiver is **not** `self`;
    ②how many times `self._x` occurs (used by the "the scanner has not gone blind" line — the real source has
      plenty of these).
    ⭐Judged by **the receiver's name**: `self._x` is allowed through; `cls._x` has zero occurrences today, ⛔ it is
      not pre-emptively allowed (allowing it would be an explicit decision made when it is actually needed); a
      module-level `_x`/`_k32()` is a Name, ⛔ not an Attribute, so it is outside the scanning surface (that is
      **its own** private name). Both reads and writes count (D3's `h._handle = …` is a write, and it counts).
    ⚠️Boundary: reflection pieced together from a string (`getattr(f, "_obj" + "ects")`) is invisible to it; so is
      a receiver that happens to be **named** `self` but is a different object."""
    found, mine = [], 0

    def visit(node, fn):
        nonlocal mine
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node.name
        if isinstance(node, ast.Attribute) and _private(node.attr):
            if isinstance(node.value, ast.Name) and node.value.id == "self":
                mine += 1
            else:
                found.append((fn, node.lineno, node.attr))
        for child in ast.iter_child_nodes(node):
            visit(child, fn)

    visit(tree, "<module>")
    return found, mine


def is_state_dir_call(name):
    return name.split(".")[-1] == "state_dir"


# Disk-write sinks (write / delete / create a directory / rename / change permissions). ⭐Recognized by **the
# shape of the call**, ⛔ never by the target path (AST cannot tell what the target is).
PATH_SINKS = ("write_bytes", "write_text", "mkdir", "unlink", "rmdir", "rename", "touch", "chmod", "lchmod", "symlink_to",
              "hardlink_to", "link_to")     # `link_to`: builds a hard link on 3.9-3.11 (removed from 3.12 on; scv supports 3.9; 13d fix1, review M4)
# ⭐13d (re-review N-1) added the whole `os.utime` family (changing a timestamp/permissions/owner/creating a
#   special file) and `shutil.chown`/`make_archive`/`unpack_archive` — a pure tightening: today's scv.py uses none
#   of them. 13d fix1 (review M4) added `os.setxattr`/`os.removexattr`, `Path.link_to`, also a pure tightening.
MODULE_SINKS = {"os": ("replace", "rename", "renames", "remove", "unlink", "mkdir", "makedirs", "rmdir", "removedirs",
                       "link", "symlink", "chmod", "lchmod", "fchmod", "chown", "lchown", "fchown", "chflags", "lchflags",
                       "utime", "truncate", "ftruncate", "mkfifo", "mknod", "setxattr", "removexattr", "open", "write"),
                "shutil": ("rmtree", "copy", "copy2", "copyfile", "copytree", "move", "copymode", "copystat", "chown",
                           "make_archive", "unpack_archive")}


# ⭐Every place in scv.py that **directly** calls a disk-write sink is named here (`disk_sites()`'s reading:
#   `(the function, with its class name, sink)` counted by occurrence). ⛔One extra one has to be an explicit
#   decision each time (following `NET_CALLERS`'s example). Task 13 fix1 (review M-2): `spath()`'s two gates only
#   look at **how a path was built** — a path built with a bare `Path(...)` is outside their scanning surface at
#   all — review stuffed one line, `Path(__file__ + ".oops").write_bytes(b"")`, into `cmd_setup`, and that whole
#   batch of gates stayed green while an extra file really landed in the repo directory. "Writing a file outside
#   the state directory" always eventually has to land on a disk-write sink ⇒ pin down **who** right at the sink.
#   ⚠️`cmd_update` ⛔ is not here: both of its writes go through `_atomic_write`/`_replace_file`, and
#     `_replace_file`'s own callers have their own separate gate (tests/test_95_setup_pair_update.py::Doors). The
#     two together are the whole picture of "who can write, through which door".
#   ⚠️Task 13c crossed out `_ensure_codex_home` (the one that used to build codex's home directory and chmod it
#     0o700): codex now goes back to the player's own home, and scv ⛔ never builds anything there again — one
#     fewer name here is a **tightening** (it would go red the moment it came back).
#   ⚠️13c Fix 1 once added `codex_carried` (a plain doctor building an empty directory under `spath("work")` just
#     to run the handshake); Fix 1b crossed it back out: the handshake would pre-warm codex and dial the
#     inference endpoint ⇒ a plain doctor went back to just stat-ing, and `--live` uses `live_check`'s own real
#     session receipt directly — one fewer name here is a **tightening** (it would go red the moment it came
#     back).
#   ⭐13d (re-review N-1): tightened from "a set of functions" to **`(function, sink)` counted by occurrence**
#     (following tests/test_90_cli.py::NoConsoleWindows's own style) — judged one function at a time, adding one
#     more disk write inside one of the 17 functions already on the list (`cmd_run`/`cmd_stop`/`live_check`, the
#     big ones) would still stay green (re-review's mutation MG′).
#   ⚠️`("_open", ".open(?)")` is a **pinned false positive**: that line inside `_open` is `OpenerDirector.open(req)`
#     (dialing out) ⛔ not a disk write — the judge cannot tell it apart from `p.open(m)` where mode is not a
#     literal, and it would rather over-report (see `disk_writers()`). It is pinned here so that a second
#     `.open(<a variable>)` showing up somewhere else would turn this red.
DISK_WRITES = collections.Counter({
    ("ClaudeDriver.__init__", ".write_text"): 2, ("SessionManager._drop", "shutil.rmtree"): 1,
    ("SessionManager._run_once", "shutil.rmtree"): 2, ("SessionManager._run_session", "shutil.rmtree"): 1,
    ("SessionManager._workdir", ".mkdir"): 1, ("_append_capped", "os.replace"): 1, ("_append_capped", "open(a)"): 1,
    ("_children_keep_bad", "os.replace"): 1, ("_mint_token", ".write_text"): 1, ("_mint_token", "os.link"): 1,
    ("_mint_token", "os.chmod"): 1, ("_mint_token", "os.unlink"): 1, ("_open", ".open(?)"): 1, ("_probe_dir", ".mkdir"): 1,
    ("_replace_file", ".write_bytes"): 1, ("_replace_file", ".write_text"): 1, ("_replace_file", "os.replace"): 1,
    ("_replace_file", ".unlink"): 1, ("cmd_run", ".unlink"): 1, ("cmd_stop", ".unlink"): 1, ("live_check", ".mkdir"): 1,
    ("live_check", ".write_text"): 1, ("live_check", ".unlink"): 1, ("live_check", "shutil.rmtree"): 1,
    ("save_config", "os.chmod"): 1, ("spath", ".mkdir"): 1, ("sweep_tmp", ".unlink"): 1,
    ("sweep_work", "shutil.rmtree"): 1})


def _writes_mode(node):
    """Whether a mode argument "writes": for a literal, look inside it for w/a/x/+; ⛔ anything not a literal
    always counts as writing (better to over-report)."""
    if node is None:
        return False
    return not isinstance(node, ast.Constant) or any(c in str(node.value) for c in "wax+")


def _mode_arg(call, pos):
    """The `open`-family call's mode argument: the `pos`-th positional argument, or `mode=`; if neither is there
    but there is a `*`/`**` unpacking, hand back that unpacking instead (a mode that **cannot be seen** counts as
    "not a literal", 13d fix1 review M4: `open(p, **kw)`/`p.open(**kw)` used to be treated as read-only); if there
    is nothing at all ⇒ `None` (read-only)."""
    if len(call.args) > pos:
        return call.args[pos]
    named = next((k.value for k in call.keywords if k.arg == "mode"), None)
    if named is not None:
        return named
    return next((a for a in call.args if isinstance(a, ast.Starred)), None) or \
        next((k.value for k in call.keywords if k.arg is None), None)


def disk_writers(tree):
    """The **one and only judge** of "who is touching the disk" (the gate and its positive control are both fed
    this one): `{qualified function name: [(line number, sink)]}`, the function name carries its class name
    (`A.f`). Recognizes these calls: ①`<anything>.write_bytes/write_text/mkdir/unlink/rmdir/rename/touch/chmod/…(…)`
    ②`Path.replace`: `<anything>.replace(exactly one positional argument, no keywords)`, `<anything>.replace(target=…)`,
      or the unbound form `Path.replace(p, q)` (`str.replace` needs at least two positional arguments and no
      `target` keyword ⇒ `s.replace(a, b, count=1)` ⛔ does not count)
    ③`<anything>.open(mode)`: mode is taken from the first positional argument or `mode=`; a literal containing
      w/a/x/+, or **anything that is not a literal**, both count (same rule as ④: better to over-report) ⇒
      ⚠️`opener.open(req)` gets named as `.open(?)` — the judge cannot tell it apart from `p.open(m)`
      syntactically; `p.open()`/`p.open("rb")` ⛔ do not count
    ④the builtin `open(path, mode)`, where mode writes or is not a literal ⑤the disk-write functions in
    `MODULE_SINKS` (`os.*`/`shutil.*`).
    For ③④, a mode that **cannot be seen** (a `*`/`**` unpacking) also counts as "not a literal" (`_mode_arg`).
    🔴**Boundary (what this cannot see)**: an alias (`import os as o`, `from os import remove`, `o = os`,
      `w = p.write_text` — the file-split batch would have had to fold this family in), reflection (`getattr`),
      starting a child process to write (codex writing its own home — that is the child-process door's
      business), ctypes (`CreateFileW` — that is the native-code door's business), `io.open`/`tempfile.*` (not
      imported; the import-set door blocks it), `shutil.copyfileobj`-style calls that only take an already-open
      file object (the moment it was opened was already counted by ③④).
      False-positive side (all loud): `os.open(p, os.O_RDONLY)`, `.open("<a literal containing w/a/x/+>")` (say,
      a URL), `.open(<a variable>)`.
      ⛔What this pins down is **who** writes, **how many places**, **which sink**, ⛔ never **where it writes to**:
      AST cannot tell whether the target is inside the state directory."""
    found = collections.defaultdict(list)

    def dotted(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            head = dotted(node.value)
            return (head + "." + node.attr) if head else node.attr
        return ""

    def visit(node, where):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            where = where + [node.name]
        if isinstance(node, ast.Call):
            name, hit = dotted(node.func), None
            parts = name.split(".") if name else []
            if len(parts) == 2 and parts[1] in MODULE_SINKS.get(parts[0], ()):
                hit = name
            elif isinstance(node.func, ast.Attribute) and parts[:1] not in (["os"], ["shutil"]):
                attr = node.func.attr
                if attr in PATH_SINKS:
                    hit = "." + attr
                elif attr == "replace" and ((len(node.args) == 1 and not node.keywords) or parts[:1] == ["Path"]
                                            or any(k.arg == "target" for k in node.keywords)):
                    hit = ".replace"
                elif attr == "open":
                    mode = _mode_arg(node, 0)
                    if _writes_mode(mode):
                        hit = ".open(%s)" % (mode.value if isinstance(mode, ast.Constant) else "?")
            elif name == "open":
                mode = _mode_arg(node, 1)
                # (mode is the 2nd positional argument or `mode=`; if it cannot be seen, it counts as "not a literal", see `_mode_arg`)
                if _writes_mode(mode):
                    hit = "open(%s)" % (mode.value if isinstance(mode, ast.Constant) else "?")
            if hit:
                found[".".join(where) or "<module>"].append((node.lineno, hit))
        for child in ast.iter_child_nodes(node):
            visit(child, where)

    visit(tree, [])
    return dict(found)


def disk_sites(tree):
    """`disk_writers()`'s **counted-by-occurrence** view: `Counter((qualified function name, sink))`. ⭐Just a
    different shape — the judge underneath is still `disk_writers()`."""
    return collections.Counter((fn, sink) for fn, hits in disk_writers(tree).items() for _ln, sink in hits)


# ⭐Starting one CLI session = spending quota: every time codex starts one session (thread/start) it does one
#   startup prewarm — it dials `wss://…/codex/responses` and gets back a response id (13c Fix 1b, measured
#   against codex's own logging library, 📎 NOTES.md::codex-user-home); one call to claude is one call, full stop.
#   ⇒ "who can reach all the way to starting a session" is pinned down as this table (following `NET_CALLERS`'s
#   example): `{door: {qualified caller}}`, one extra one has to be an explicit decision each time.
#   🔴13c Fix 1 once had an extra `codex_carried` here (a plain doctor's handshake asking about
#   instructionSources) ⇒ a plain doctor/setup stopped costing zero quota.
#   · `sessions.run`'s two callers are the two legs of "one real request": the local HTTP one
#     (`/v1/chat/completions`) and the remote job.
#   · `live_check` is only ever allowed to be called by `doctor_facts`, and only wrapped inside `if … live …`
#     (`scv doctor --live`/`scv setup --live`: the user explicitly said they want to spend it).
#   · `SessionManager`'s construction is only ever allowed inside `Bridge.__init__` (13d/13c re-review N3):
#     `sessions.run`'s door is recognized **by instance** — building a separate instance somewhere else turns red
#     right here first.
SESSION_DOORS = {"ClaudeDriver": {"make_driver"}, "CodexDriver": {"make_driver"},
                 "make_driver": {"SessionManager._run_once", "SessionManager._run_session", "live_check"},
                 "_run_once": {"SessionManager.run"}, "_run_session": {"SessionManager.run"},
                 "sessions.run": {"_make_handler.Handler._chat.work", "RemoteLeg._job"},
                 "live_check": {"doctor_facts"}, "SessionManager": {"Bridge.__init__"}}


def session_door_callers(tree, doors):
    """`{door: {qualified caller: [one entry per call site: the names that showed up in any wrapping if-condition…]}}`
    (**split by call site**: merging them into one would let a call wrapped in `if live` vouch for another one in
    the same function that is not wrapped). A call counts as "called this door": its dotted full name equals it,
    or ends with `.door` (`self._run_once(…)`, `bridge.sessions.run(…)` both count). The qualified name carries
    its class name, and a nested function carries it too (`A.f.g`).
    ⭐The `sessions.run` door recognizes **a `SessionManager` instance's `.run`**, ⛔ never just a name suffix (13d/
      13c re-review N3's mutation S: a new subcommand doing `mgr = SessionManager(…); mgr.run(…)` stayed green on
      both halves). An expression "yields" an instance (`yields`): `SessionManager(…)`, `sessions`/`….sessions`, a
      local name already recognized; plus a few shells that pass one of these along unchanged — either branch of
      a conditional expression, either side of `or`/`and`, any element of a tuple/list/set literal (counts
      wherever it shows up, ⛔ not only "taken out of it"). A local name is recognized through these bindings
      (chased until it stops changing): `=` (including unpacking: a tuple matched item-for-item against a tuple,
      and the whole group counts if they cannot be matched up), an annotated assignment, `:=`, a `for` target, a
      comprehension target, a `with … as` target (13d fix1, review M3's mutation S3
      `for m in (bridge.sessions,): m.run()` used to stay green on both halves). ⭐The set of names is collected **separately for each
      `def`/`class` (walking its whole subtree, including names bound inside a nested `def`), then merged with
      the outer scope's** (13b tightened this, 13d re-review N1's mutation R3cl: bound in the outer scope, called
      inside an inner `def` — a closure — used to go unrecognized without a sound). `lambda` does not change
      scope, and was already visible. The receiver itself being one of these expressions also counts
      (`(b.sessions if x else y).run()`). Construction has its own separate door
      (`SESSION_DOORS["SessionManager"]`) ⇒ building an extra instance somewhere else turns red right there on
      its own.
    ⚠️A "wrapping if" only counts the `if`'s body (⛔ never the else), only up to the nearest enclosing function.
      🔴**Boundary (what this cannot see)**: an alias (`d = make_driver`, `r = m.run; r()`), `getattr`, **an
      instance passed across functions** (a parameter, a return value, stashed into another attribute/dict/set
      and taken back out, a global variable — a name bound at module level never enters any function's set),
      taken **from a non-literal container** (`for m in managers`) — the construction door catches "building a
      separate one", it does not catch "passing the one bridge already has out and calling it elsewhere".
      False-positive side (loud, zero occurrences in the real source today; each one pinned down in
      `test_the_session_door_judge_is_not_blind`): when an unpacking cannot be matched up, the whole group counts
      (`a, m = b.sessions` ⇒ `a` counts too); if one element of a tuple is it, the whole `for` target counts
      (`for a, m in [(x, b.sessions)]` ⇒ `a` counts too); **iterating** over something named
      `sessions`/`….sessions` counts every item taken from it (`for p in sessions: p.run()`); the literal
      container itself also counts (`pair = (b.sessions, 1); pair.run()`); a name bound inside a nested `def`
      counts in the outer scope too (a different, unrelated `m` in the outer scope also gets named); an inner
      **parameter** shadowing an outer name with the same name also counts (`def inner(m): m.run()`)."""
    found = {d: {} for d in doors}

    def dotted(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            head = dotted(node.value)
            return (head + "." + node.attr) if head else node.attr
        return ""

    def yields(node, mgrs):
        """Whether this expression yields a `SessionManager` instance (see the reading above)."""
        if isinstance(node, ast.Call):
            return dotted(node.func).split(".")[-1] == "SessionManager"
        if isinstance(node, ast.Name) and node.id in mgrs:
            return True
        if isinstance(node, (ast.Name, ast.Attribute)):
            name = dotted(node)
            return name == "sessions" or name.endswith(".sessions")
        if isinstance(node, ast.IfExp):
            return yields(node.body, mgrs) or yields(node.orelse, mgrs)
        if isinstance(node, ast.BoolOp):
            return any(yields(v, mgrs) for v in node.values)
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return any(yields(e.value if isinstance(e, ast.Starred) else e, mgrs) for e in node.elts)
        if isinstance(node, ast.NamedExpr):
            return yields(node.value, mgrs)
        return False

    def names(target):
        return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}

    def bound(target, value, mgrs):
        """The local names recognized by binding `target = value`: tuple matched item-for-item against tuple
        (equal length), otherwise the whole target follows value."""
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)) \
                and len(target.elts) == len(value.elts):
            return set().union(*(bound(t, v, mgrs) for t, v in zip(target.elts, value.elts)))
        return names(target) if yields(value, mgrs) else set()

    def mgr_names(fn):
        out = set()
        while True:                                   # chase repeatedly: `m = b.sessions; m2 = m` needs to reach m2
            more = set(out)
            for n in ast.walk(fn):
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        more |= bound(t, n.value, out)
                elif isinstance(n, (ast.AnnAssign, ast.NamedExpr)) and n.value is not None:
                    more |= bound(n.target, n.value, out)
                elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)) and yields(n.iter, out):
                    more |= names(n.target)
                elif isinstance(n, ast.withitem) and n.optional_vars is not None and yields(n.context_expr, out):
                    more |= names(n.optional_vars)
            if more == out:
                return out
            out = more

    def visit(node, where, guards, mgrs):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # ⭐merge with the outer scope's (13b, 13d re-review N1): calling a name bound in the outer scope from inside a closure (mutation R3cl) used to go unrecognized without a sound
            where, guards, mgrs = where + [node.name], [], mgrs | mgr_names(node)
        if isinstance(node, ast.Call):
            name = dotted(node.func)
            recv = node.func.value if isinstance(node.func, ast.Attribute) and node.func.attr == "run" else None
            mgr_run = recv is not None and yields(recv, mgrs)
            for d in doors:
                if name == d or name.endswith("." + d) or (d == "sessions.run" and mgr_run):
                    found[d].setdefault(".".join(where) or "<module>", []).append(sorted(set(guards)))
        for field, value in ast.iter_fields(node):
            for child in (value if isinstance(value, list) else [value]):
                if isinstance(child, ast.AST):
                    visit(child, where, guards + [n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)]
                          if isinstance(node, ast.If) and field == "body" else guards, mgrs)

    visit(tree, [], [], set())
    return found


SPAWN_DOORS = {"run_cli": 4, "_Pipe": 2}     # the two doors that start a CLI → which position the `env` argument sits at in the signature (0-based)


def spawn_env_violations(tree):
    """15c review I1: every call site that starts a CLI (a call to `run_cli(`/`_Pipe(`) is only ever allowed to
    pass an `env` that is `child_env()`, a local name bound by it, or (only for `run_cli`) omitted entirely — its
    own default is already `child_env()` ⇒ returns `["function name@line number", …]`. "Bound by it" = within the
    same scope (each `def` counted on its own, ⛔ never merged into a nested `def`/`lambda`), **every** write to
    that name is `name = child_env()` (a parameter, a `for`/`with` target, or any other assignment all count as
    not bound correctly).
    Anything passed through `*args`/`**kw` always counts as unclear, and gets named.
    🔴Boundary (what this cannot see): an alias (`f = run_cli; f(…)`), `getattr`, starting a process by going
      around both doors entirely (where a process gets started is pinned down by occurrence in
      tests/test_90_cli.py::NoConsoleWindows, and what a child process actually sees for its environment is
      pinned end-to-end by tests/test_90_cli.py::ChildProcessesNeverSeeTheSession), the dict getting mutated after
      being bound (`e = child_env(); e["X"] = 1`), `child_env` itself getting broken (that half belongs to
      tests/test_20_argv.py::ChildEnv), an `except … as e`/`import … as e` style re-binding (these are not a
      `Name` write, so the judge still treats it as bound to `child_env()`; 15c re-review, outside scope 1)."""
    bad = []

    def own(scope):
        todo = list(ast.iter_child_nodes(scope))
        while todo:
            n = todo.pop()
            yield n
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                todo.extend(ast.iter_child_nodes(n))

    def is_child_env(v):
        return isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "child_env" and not (v.args or v.keywords)

    scopes = [tree] + [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef))]
    for scope in scopes:
        nodes = list(own(scope))
        good = {id(t) for n in nodes if isinstance(n, (ast.Assign, ast.NamedExpr)) and is_child_env(n.value)
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if isinstance(t, ast.Name)}
        bound = {}
        for n in nodes:
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                bound[n.id] = bound.get(n.id, True) and id(n) in good
            elif isinstance(n, ast.arg):
                bound[n.arg] = False
        if not isinstance(scope, (ast.Module, ast.ClassDef)):
            for a in ast.walk(scope.args):
                if isinstance(a, ast.arg):
                    bound[a.arg] = False
        for n in nodes:
            name = (n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")) if isinstance(n, ast.Call) else ""
            if name not in SPAWN_DOORS:
                continue
            kw = [k.value for k in n.keywords if k.arg == "env"]
            val = kw[0] if kw else (n.args[SPAWN_DOORS[name]] if len(n.args) > SPAWN_DOORS[name] else None)
            blind = any(isinstance(a, ast.Starred) for a in n.args) or any(k.arg is None for k in n.keywords)
            ok = not blind and ((val is None and name == "run_cli") or is_child_env(val)
                                or isinstance(val, ast.Name) and bound.get(val.id) is True)
            if not ok:
                bad.append("%s@%d" % (getattr(scope, "name", "<module>"), n.lineno))
    return sorted(bad)


def _line_of(marker):
    hits = [i + 1 for i, line in enumerate(SRC.splitlines()) if line.startswith(marker)]
    assert len(hits) == 1, (marker, hits)
    return hits[0]


class Budget(unittest.TestCase):
    def test_line_budget(self):
        """⭐The line count is a **proxy metric**, ⛔ not the goal. The goal (B2) is "a wary developer can read it
        in five minutes and decide to trust it" — and this proxy has **already stopped tracking that goal**: even
        with zero comments, the bare floor is already **1997-2180 lines** — Task 0-9's plain code alone is
        **1029 lines** (⛔ not counting any comment, measured), plus the last 6 legs' projected 780-960. ⇒ **2000
        cannot hold, and that has nothing to do with how comments get handled** (deleting all 441 lines of
        explanation would only bring it down to 1227, and the last 6 legs alone would push it back over the
        line).
        2026-09-22, the maintainer ruled: `LINE_BUDGET` 2000 → **3000** (a bare floor of ~2180 + roughly 800 lines
        of headroom for explanation), and **auditability is now guaranteed by the AST gates below, ⛔ no longer by
        the line count** — what a wary reader has to check is exactly these things, 🔴**auditability is pinned on
        these ten gates** (each question is pinned on its own line): whichever one goes blind, gets deleted, or
        gets renamed, nobody is watching that question anymore, **and the line count will never notice for you**:
          · where does the network send data       → tests/test_00_budget.py::Budget::test_urls_only_in_section_one
            (who is reaching outward)              → tests/test_00_budget.py::Budget::test_network_calls_only_in_whitelisted_functions
          · where does it write to disk            → tests/test_00_budget.py::Budget::test_state_dir_only_reached_through_spath
            (who is touching the disk)             → tests/test_00_budget.py::Budget::test_disk_writes_only_in_named_functions
          · the tool-off flag / `app-server` only inside the two argv builders
                                                    → tests/test_00_budget.py::Budget::test_cli_subcommand_literals_only_in_argv_builders
            (it only covers these two literals; "which programs it can start" is pinned by occurrence in
            tests/test_90_cli.py::NoConsoleWindows, ⛔ not on this list)
          · any non-stdlib dependency               → tests/test_00_budget.py::Budget::test_stdlib_only
          · which modules it brings in              → tests/test_00_budget.py::Budget::test_the_import_list_is_pinned
          · where does native code come in          → tests/test_00_budget.py::Budget::test_native_code_has_one_door
            (the two cracks beside that door)       → tests/test_00_budget.py::Budget::test_the_ctypes_surface_is_pinned
                                                     → tests/test_00_budget.py::Budget::test_no_private_attribute_is_reached_off_self
        ⚠️A gate's **boundary** (what it cannot see) is sometimes in its own docstring, sometimes in the judge
          function it calls, and a few still have none written down at all (Task 14 review I2:
          `test_urls_only_in_section_one` has no docstring, `test_cli_subcommand_literals_only_in_argv_builders`
          never wrote down its boundary) — a gate being green ⛔ does not mean that question's rule is fully being
          kept: whoever reads a gate has to read the judge function that backs it too.
        ⚠️The list originally named only four (one each for network/disk/child process/non-stdlib); 2026-09-23 fix
          round 6 added five more: the "who is reaching outward" gate's own docstring already claimed to be
          load-bearing, yet it was missing from the list; the import set and the native-code door were added by
          re-review two and were already load-bearing; the two cracks beside the door were added by re-review
          three, M-1. Task 13d added the tenth, "who is touching the disk" (the maintainer's ruling): it backs
          the README's outward-facing claim about "what it writes"; the file-split batch would have had to
          rescope every gate's scanning surface, and this list is the insurance against "a gate silently
          disappearing while it moves".
        ⚠️These names are pinned by `test_the_gates_it_leans_on_really_exist` (renaming one turns it into a
          **dead link**, and a reader would believe they had already checked).
        2026-09-23, the maintainer ruled a second time: 3000 → **3800**. Measured ratio holds steady at 2.27x
        (implementation increment ÷ the plan's implementation-code line count, Task 10 = 261→594, Task 11 =
        191→440) × Tasks 12-15's planned implementation code of 326 lines ≈ 740 lines, and it was already at 2944
        at the time ⇒ the endpoint was estimated at ~3684, leaving a headroom of a bit over a hundred lines.
        ⭐The same ruling also settled: **once Plan 1 is done and the whole suite is green, split the independent
        functions/classes out into modules** (B2's single-file rule / B29's pinned sha256 get re-set along with
        it) ⇒ this number only governs up to the file-split batch.
        2026-09-23 evening, 3800 → **4300** (following the same ruling's "not enough, just raise it, ⛔ never ask
        again", the maintainer's own words): Task 12 measured 3145→3721 (+576) ÷ the Brief's implementation code
        of 191 (non-blank) lines = **3.0x**, higher than 2.27x ⇒ recalculated at 3.0x: Task 13's scv.py
        implementation of 69 (non-blank) lines ≈ +210, `scv codex-login` and codex's `fix_hint` pointing at it ≈
        +40, Task 14/15 odds and ends ≈ +60 ⇒ the endpoint was estimated at ~4030, leaving headroom of a couple
        hundred lines.
        2026-09-25, lead raised 4300→4400: 15b measured 4277 (estimated +70, actual +105); 15b's review fix1
        (I1-I3 + root cause) and 15c (estimated +38-54) both need lines; the file split later joined the pieces
        back into one scv.py, and the gates still scan that one file — this number was never rescoped because of
        it.
        2026-09-25, lead raised 4400→4500: 15b closed out at 4388 (three rounds of fixes + root cause); 15c
        Phase A estimated +38-54, plus about +65 for the doctor-disclosure and missing-variable line, leaving
        about 45 lines for the review round; the file split later joined the pieces back into one scv.py, and
        the gates still scan that one file — this number was never rescoped because of it.
        2026-09-25, lead raised 4500→4600: 15c Phase B measured 4482; 15c's review fix1 (I1 the wiring gate and
        the end-to-end test, I2 the bridge's environment getting recorded into bridge.pid, I3 the two carriers,
        M2/M3 the path-collection fix) estimated +60-90; the file split later joined the pieces back into one
        scv.py, and the gates still scan that one file — this number was never rescoped because of it.
        2026-09-25 night, lead raised 4600→5600 (a temporary value for S2's English pass): after the file split,
        what the player gets is still one joined-together scv.py, and this number still governs it the same way;
        English comments run longer than Chinese ones (translating src/20_kill.py measured 49→55 lines = +12%,
        and half of that block is code; estimating each line of Chinese comment becomes 1.3-1.4 lines, over 1,726
        lines ⇒ an endpoint of roughly 5100-5300), the code does not change by one line (S2's reconciliation tool
        checks the AST); S2's own close-out will re-set this from what gets measured.
        2026-09-26, lead re-set 5600→5400 at S2's close-out, from the measurement: with every comment, docstring and
        message translated the file is 5344 lines (4564 before the English pass at 872e829, +17%; the 5100-5300 estimate above
        was a little low); 56 lines of headroom are left for the fixes of the final whole-branch review.
        2026-09-26, lead raised 5400→5450 after the final whole-branch review's fixes: they measured +55 lines
        (5344→5399: the work-directory sweep, the remote-leg session namespace, doctor reporting the running
        bridge's families, the remote rate limit of 0), leaving one line; the implementer shortened comments rather
        than stop, which is exactly what this number must never cause. 51 lines of headroom now.
        2026-09-27, lead raised 5450→5500 for 0.2.0: the CI fixes after publishing measured +10 (5399→5409),
        KeepAwake measured +77 (5409→5486: the class, its config reader, the sixth `_k32` function, the remote
        leg's begin/end and the main loop's tick); 14 lines of headroom.
        2026-09-27, lead raised 5500→5550 for 0.2.1 (the Plan 2B walkthrough with a real Codex): measured +32
        (5486→5518: the local API moving off a taken default port, `start` waiting on the port the bridge reports,
        `pair` and `setup` pointing back at setup.md steps 6 and 8); 32 lines of headroom.
        2026-09-28, lead raised 5550→5750 for 0.3.0 (the sleeping bridge, spec 2026-09-28-bridge-sleep-wake): measured
        +174 (5518→5692: the sleep/wake switch and its clock, `GET /wake` and its page, the wake subcommand, `status`
        saying asleep/awake, hello's `local_port`); 58 lines of headroom.
        ⛔**The budget loosening ⛔ does not mean the archaeology a compression pass moved out should move back
        in** — the judge has not changed by one word (see NOTES.md::line-budget-3000)."""
        self.assertLessEqual(len(SRC.splitlines()), scv.LINE_BUDGET)
        # ⭐Pin the number down exactly: B33's original point was "if it goes over, delete something that is not
        #   pulling its weight first, ⛔ never just raise the budget" ⇒ raising the budget has to be an
        #   **explicit** decision (with the reasoning above changed right along with it), ⛔ never someone quietly
        #   bumping 2000 up a bit.
        self.assertEqual(scv.LINE_BUDGET, 5750)

    def test_the_gates_it_leans_on_really_exist(self):
        """The docstring above now carries the weight that used to belong to the line count ⇒ every gate it names
        has to really be there.
        ⭐The judge **shares** `GATE_RE`/`gate_missing` with the Retired block, ⛔ never copy a second one here.
        ⭐The count is pinned exactly (both directions: listing one fewer also turns it red) ⇒ adding or removing a
        gate from the list has to come with changing this number too — an explicit decision."""
        named = GATE_RE.findall(Budget.test_line_budget.__doc__ or "")
        self.assertEqual(len(named), 10, named)
        for path, klass, test in named:
            with self.subTest(gate=klass + "::" + test):
                self.assertEqual(gate_missing(path, klass, test), "")

    def test_no_backslash_anywhere_in_the_source(self):
        """A global constraint: `scv.py` ⛔ must never write an escape sequence — a newline goes through
        `NL = chr(10)`, a backslash through `chr(92)`, **not even in a comment**.

        ⭐The reason is that a heredoc can make a backslash **collapse a level, without ever raising an error**
          (this machine hit it four times in one day) ⇒ the judge can only ever be "there are zero of them", ⛔
          never "it looks right".
        ⚠️**Carries a negative control**: this gate used to be kept up entirely by someone running
          `s.count(chr(92))` by hand — and "it is 0 right now" looks exactly the same as "this ruler can never go
          red at all".
        ⭐Three sentences, each proving a **different** thing, ⛔ not one of them can be dropped (each of the first
          two versions was missing one, see `_no_backslash`)."""
        # ① the real source qualifies
        self.assertEqual(_no_backslash(SRC), 0,
                         "a backslash showed up in scv.py: use NL/chr(10) for a newline, chr(92) for a backslash, ⛔ never write an escape sequence")
        # ② feed **the same judge** a synthetic source text that deliberately carries one backslash ⇒ it must be judged as failing
        #    (if the judge ever gets broken, this line goes red first ⇒ the line above can never silently turn into a statement that is always true)
        bad = "NL = chr(10)" + chr(10) + "x = 1  # there is a " + chr(92) + "n"
        self.assertNotEqual(_no_backslash(bad), 0,
                            "the judge is broken: it cannot even catch a sample that **deliberately carries a backslash** ⇒ "
                            "the assertion above against the real source has already turned into a statement that is always true "
                            "(and scv.py might really have a backslash in it)")
        # ③ `SRC` really is scv.py's own bytes (⛔ not an empty string / a half-read file) — this is the precondition for ① being able to go red at all
        self.assertGreater(len(SRC), 10000)
        self.assertIn("LINE_BUDGET", SRC)

    def test_stdlib_only(self):
        """B2: ⛔ no third-party dependency. ⚠️It is **blind by design** to "one extra stdlib module coming along"
        (that is `test_the_import_list_is_pinned`'s business)."""
        self.assertEqual(sorted(non_stdlib(TREE)), [])

    def test_the_stdlib_judge_is_not_blind(self):
        """Positive control: a third-party package (one of each writing style, including one hidden inside a
        function) has to be named."""
        probe = ast.parse(NL.join(("import requests", "def f():", "    from yaml import safe_load", "    return 1")))
        self.assertEqual(non_stdlib(probe), {"requests", "yaml"})
        self.assertEqual(non_stdlib(ast.parse("import os" + NL + "from pathlib import Path")), set())

    def test_the_import_list_is_pinned(self):
        """🔴Re-review two, I-2's **structural gate**: every module/name scv.py brings in is **pinned exactly as
        `SCV_IMPORTS`**.
        ⭐`assertEqual`, ⛔ not `assertLessEqual`: missing one also has to turn it red (**an empty set is a subset of
          any set** — a subset-style judgment stays green even once the scanner has gone blind).
        ⚠️What it covers is "**getting in the door**": once a module is already on the table, how it gets used
          (an aliased assignment, reflection, a re-export) is outside its reach — see `net_call_pred`'s
          boundary."""
        self.assertEqual(imported_names(TREE), SCV_IMPORTS)

    def test_the_import_pin_is_not_blind(self):
        """Positive control: ①one extra line, `import webbrowser`, tacked onto the end of the real source (stdlib,
        and it reaches outward) ⇒ the difference is exactly that one; ②a re-export riding through a function
        (`from http.server import socket`) ⇒ it gets caught (recording only the module name would miss it);
        ③**division of labor**: on this very same `webbrowser`, the stdlib judge **by design** does not name it —
          proving that only this one gate is watching for it, ⛔ the two are not covering for each other."""
        more = ast.parse(SRC + NL + "import webbrowser" + NL)
        self.assertEqual(imported_names(more) - SCV_IMPORTS, {"webbrowser"})
        sneaky = ast.parse(NL.join(("def f():", "    from http.server import socket", "    return socket")))
        self.assertEqual(imported_names(sneaky), {"http.server.socket"})
        self.assertEqual(non_stdlib(more), set())

    def test_native_code_has_one_door(self):
        """🔴Re-review two, I-2: `ctypes` had just been added to scv.py in this round, and it can reach any native
        API directly, while the network/disk/child-process gates are **all three completely blind** to it.
        ⭐Three sentences, each pinning one fact (⛔ missing any one leaves a hole): ①a loader only ever shows up
          inside `_k32`, and only once ②its argument is the literal `"kernel32"` ③what `_k32` hands back out is
          only the five functions in `K32_EXPORTS`, and the DLL object itself ⛔ is never handed out bare — kernel32
          itself already has CreateProcessW/CreateFileW/LoadLibraryW/GetProcAddress (start a process, write to
          disk, load yet another DLL).
        ⭐`assertEqual` pins the whole set: missing one also turns it red (`_k32` being deleted entirely, or the
          scanner going blind, ⛔ neither is allowed to stay all-green)."""
        sites, args, uses = native_door(TREE)
        self.assertEqual(sites, {("_k32", "WinDLL")})
        self.assertEqual(args, ["kernel32"])
        self.assertEqual(uses, K32_EXPORTS)

    def test_the_native_door_judge_is_not_blind(self):
        """Positive control: seven ways around it each get their own synthetic sample, and the judge has to catch
        every one (the first three are ①, the middle two are ②, the last two are ③)."""
        def door(*lines):
            return native_door(ast.parse(NL.join(lines)))

        self.assertIn(("f", "CDLL"), door("def f():", "    return ctypes.CDLL('ws2_32')")[0])
        self.assertIn(("<module>", "windll"), door("from ctypes import windll")[0])
        self.assertIn(("g", "LoadLibraryW"), door("def g(k):", "    return k.LoadLibraryW('ws2_32')")[0])
        self.assertEqual(door("def _k32():", "    dll = ctypes.WinDLL('ws2_32')", "    return dll.OpenProcess")[1],
                         ["ws2_32"])
        self.assertEqual(door("def _k32(n):", "    dll = ctypes.WinDLL(n)", "    return dll.OpenProcess")[1], [None])
        self.assertEqual(door("def _k32():", "    dll = ctypes.WinDLL('kernel32')", "    return dll")[2], {"<used bare>"})
        self.assertEqual(door("def _k32():", "    dll = ctypes.WinDLL('kernel32')",
                              "    return (dll.OpenProcess, dll.CreateProcessW)")[2], {"OpenProcess", "CreateProcessW"})

    def test_the_ctypes_surface_is_pinned(self):
        """🔴Re-review three, M-1: `native_door` only recognizes a loader by name ⇒ stuffing
        `ctypes._dlopen`+`WINFUNCTYPE((name, an object carrying a _handle))` into the real source used to leave all
        49 cases green, and it really can reach `ws2_32`'s `WSAStartup` (measured). ⇒ what `ctypes` gets touched
        with is **pinned exactly as `CTYPES_SURFACE`**.
        ⭐`assertEqual`, ⛔ not `assertLessEqual` (**an empty set is a subset of any set**: a subset-style judgment
          stays green once the scanner has gone blind).
        ⭐Division of labor: `test_native_code_has_one_door` covers "where is the loader, what does it load,
          what does it hand back out"; this one covers "what else has `ctypes` been touched with"."""
        self.assertEqual(ctypes_surface(TREE), CTYPES_SURFACE)

    def test_the_ctypes_surface_judge_is_not_blind(self):
        """Positive control: review D3 (`_dlopen`+`WINFUNCTYPE`), `cast`, `import ctypes as c`,
        `from ctypes import X`, and using it bare (review D1's `getattr(ctypes, …)`, `c = ctypes`) each get their
        own case. The last case proves **division of labor**: D2 (`_objects`) ⛔ is not this one's business, and
        it comes back empty here ⇒ only `private_reaches` watches it, and the two are not covering for each
        other."""
        def surface(*lines):
            return ctypes_surface(ast.parse(NL.join(lines)))

        self.assertEqual(surface("import ctypes", "def f(h):", "    h._handle = ctypes._dlopen('ws2_32')",
                                 "    return ctypes.WINFUNCTYPE(ctypes.c_int)(('WSAStartup', h))"),
                         {"_dlopen", "WINFUNCTYPE", "c_int"})
        self.assertEqual(surface("import ctypes", "def f(a):", "    return ctypes.cast(a, ctypes.CFUNCTYPE(None))()"),
                         {"cast", "CFUNCTYPE"})
        self.assertEqual(surface("import ctypes as c", "def f(a):", "    return c.cast(a, None)"), {"cast"})
        self.assertEqual(surface("from ctypes import cast"), {"cast"})
        self.assertEqual(surface("import ctypes", "def f():", "    return getattr(ctypes, 'WinDLL')('ws2_32')"),
                         {"<used bare>"})
        self.assertEqual(surface("import ctypes", "c = ctypes"), {"<used bare>"})
        self.assertEqual(surface("def f():", "    return _k32().OpenProcess._objects['0'].CreateProcessW"), set())

    def test_no_private_attribute_is_reached_off_self(self):
        """🔴Re-review three, M-1: `_k32()` only ever hands out five functions, but every ctypes function object
        keeps its DLL alive inside its own private `_objects` ⇒ stuffing
        `_k32().OpenProcess._objects["0"].CreateProcessW` into the real source used to leave all 49 cases green,
        and it really is callable (measured).
        ⇒ the whole file **⛔ must never touch another object's private attribute** (zero occurrences today, zero
        false positives).
        ⭐Two sentences: ①not one occurrence of someone else's private attribute ②the scanner has not gone blind:
          `self._x` really can be counted in the real source (⛔ otherwise ① would stay green on a judge that
          always returns empty)."""
        found, mine = private_reaches(TREE)
        self.assertEqual(found, [])
        self.assertGreater(mine, 0)

    def test_the_private_attribute_judge_is_not_blind(self):
        """Positive control: review D2 (`_objects`), D3's two private-attribute reaches (reading
        `ctypes._dlopen`, writing `h._handle`), `cls._x`, `o.__x` each get their own case; false-positive side:
        `self._x`, a dunder (`type(e).__name__`), a module-level private name (`_k32()`) ⛔ must never be named,
        and `self._x` has to be counted."""
        def reach(*lines):
            found, mine = private_reaches(ast.parse(NL.join(lines)))
            return {a for _fn, _ln, a in found}, mine

        self.assertEqual(reach("def f():", "    return _k32().OpenProcess._objects['0'].CreateProcessW")[0], {"_objects"})
        self.assertEqual(reach("def f(h):", "    h._handle = ctypes._dlopen('ws2_32')")[0], {"_handle", "_dlopen"})
        self.assertEqual(reach("class A:", "    @classmethod", "    def f(cls):", "        return cls._x")[0], {"_x"})
        self.assertEqual(reach("def f(o):", "    return o.__x")[0], {"__x"})
        self.assertEqual(reach("class A:", "    def f(self, e):", "        return self._x, type(e).__name__, _k32()"),
                         (set(), 1))

    def test_urls_only_in_section_one(self):
        lo, hi = _line_of("# ━━ ①"), _line_of("# ━━ ②")
        self.assertLess(hi - lo, 20)   # the real way this gate dies is window creep: ② slides further down, and the legal zone quietly grows
        stray = [(fn, ln, v) for fn, ln, v in string_constants(TREE)
                 if net_addrs(v) and not (lo < ln < hi)]
        self.assertEqual(stray, [], STRAY_ADDR_HELP)

    def test_network_calls_only_in_whitelisted_functions(self):
        """⭐`assertEqual`, ⛔ not `assertLessEqual`: **an empty set is a subset of any set** — this exact same
        file's own `test_state_dir_only_reached_through_spath` says so in so many words, and before Task 11,
        `NET_CALLERS` was an empty set, so the two judgments happened to be equivalent — the moment names got
        filled in, this gate turned into a shape that **stays green even after going completely blind**
        (measured: swapping `is_net_call` for a constant `False` ⇒ all 41 cases stay green; running it against an
        old `scv.py` that makes **zero** network calls also stays green).
        🔴B33 pins auditability on the AST gates `test_line_budget` lists (this one is the other half of "where
          does the network send data": who is reaching outward) ⇒ **not one** of them is allowed to be a gate
          that can idle along doing nothing."""
        self.assertEqual({fn for fn, _ln, _n in call_sites(TREE, is_net_call)}, NET_CALLERS)

    def test_state_dir_only_reached_through_spath(self):
        """The gate for lockdown section ②: the one and only entry point for computing a path that lands on disk
        is spath(), ⛔ nowhere else is allowed to build one from state_dir() on its own.

        ⭐`assertEqual`, ⛔ not `assertLessEqual`: **an empty set is a subset of any set** ⇒ the moment the scanner
          goes blind (or `state_dir()` someday stops being called from inside `spath`), `assertLessEqual` would
          stay green regardless.
        🔴This same file **already fixed this exact shape once** at the `--disallowedTools` spot (see
          `test_cli_subcommand_literals_only_in_argv_builders`'s docstring) — these two cases are tripping over it
          a **second** time.
        ⚠️Disk-path discipline is the one gate every one of the last seven legs leans on the whole way through ⇒
          it going blind for the length of the whole plan would cost more than any single bug.

        🔴🔴**This gate only covers the two known escape shapes** (a call to `state_dir()`, plus a
          `with_name`/`with_suffix`/`parent` derivation), ⛔ **it is not the complete rulebook**: a bare
          `Path("...")` literal, or `os.path.join`, are **both outside its scanning surface** (before Task 13c,
          the one `Path(home)` inside `_ensure_codex_home` was a ruled-on legitimate exception; today the only
          bare `Path(...)` left is read-only — `cli_head` checks whether the executable named in config exists,
          `codex_carried` stats the player's home's AGENTS.md — writing to disk is pinned by the sink gate
          below).
          ⇒ **It being green ⛔ does not mean "every disk path only ever goes through spath()" is being fully
          kept**. This sentence has to travel with the gate: the settled-idioms section had exactly this entry
          retired precisely because this gate exists, and whoever reads the gate needs to know where its boundary
          sits."""
        self.assertEqual({fn for fn, _ln, _n in call_sites(TREE, is_state_dir_call)}, {"spath"})

    def test_paths_are_not_derived_outside_spath(self):
        """Built to match the shape of the bug: the original bug was `p.with_name("config.json.tmp")` — it calls
        neither state_dir() nor goes through spath(), ⇒ the gate above stays green regardless. This one scans
        Attribute nodes, which also happens to cover the same family's `p.parent / "x"` shape along the way.
        ⭐Same floor as before: `assertEqual`, ⛔ not `assertLessEqual`.
        ⭐**An explicit, written-down exception**: `cmd_update` (Task 13 fix1, review M-2) — `scv update` swaps
          out **the scv.py that got installed** (not inside the state directory, the one exception at the very
          top of § ②), and the buffer it uses has to sit right next to it (an `os.replace` across drives would
          blow up) ⇒ `target.with_name(target.name + ".new")`. Writing it as `Path(str(target) + ".new")` would
          have been hiding inside this gate's blind spot instead; an exception belongs **inside the gate that
          governs this rule**, where it is visible.
        ⭐**Counted by occurrence** (13d/re-review N-1 tightened this along the way): it used to compare a set at
          the function level ⇒ `cmd_update` deriving one more path (another `.parent`, another `with_name`) would
          stay green regardless. Now `spath`'s one `.parent` and `cmd_update`'s one `with_name` are each pinned at
          1.
          ⚠️This gate only covers "how a path gets derived"; **who writes to disk** is pinned by
          `test_disk_writes_only_in_named_functions`."""
        hits = collections.Counter((fn, a) for fn, _ln, a in attr_sites(TREE, ("with_name", "with_suffix", "parent")))
        self.assertEqual(hits, collections.Counter({("spath", "parent"): 1, ("cmd_update", "with_name"): 1}))

    def test_disk_writes_only_in_named_functions(self):
        """The gate for the bug shape "wrote a file outside the state directory" (Task 13 fix1, review M-2's
        mutation MG): **every single place** that directly calls a disk-write sink is **pinned exactly as
        `DISK_WRITES`** — `(function, sink)` counted by occurrence (13d/re-review N-1: it used to compare a set
        at the function level, and one more write inside a function already on the list would still stay green).
        `assertEqual`: missing one also turns it red — a subset-style judgment stays green once the scanner has
        gone blind.
        ⚠️Boundary, see `disk_writers()`: it covers **who**, **how many places**, **which sink** — ⛔ never
          **where it writes to** (the same function using the same kind of sink but a different target is
          invisible to it); anything written through the doors `_atomic_write`/`_replace_file` is covered by
          their own callers' gates (tests/test_95_setup_pair_update.py::Doors)."""
        self.assertEqual(disk_sites(TREE), DISK_WRITES)

    def test_the_disk_writer_judge_is_not_blind(self):
        """Positive control: every writing style for a sink gets its own case (review MG's exact case is included
        as-is); false-positive side: `str.replace` (with or without `count=`), read-only `open`/`p.open()`/
        `p.open('rb')`, `os.path.join` — ⛔ none of these are ever allowed to be named.
        ⭐The few 13d (re-review N-1) added are shapes re-review's synthetic samples measured as missed: `.open`'s
          keyword mode/a non-literal mode, `Path.replace(target=…)` (including the unbound form
          `Path.replace(p, q)`), `os.utime`, `shutil.chown`/`make_archive`/`unpack_archive`.
        ⭐**Counted by occurrence**: a second occurrence of the same sink inside the same function has to count as
          2 (`two`) — comparing a set at the function level would make it invisible (re-review's mutation MG′).
        ⚠️`opener.open(req)` now **does** get named as `.open(?)`: the judge cannot tell it apart from `p.open(m)`
          (a `Path.open` with a mode that is not a literal), so it would rather over-report (the same rule as the
          builtin `open(p, m)`); the one real occurrence in the real source (`_open`'s dialing) is explicitly
          pinned inside `DISK_WRITES`."""
        probe = ast.parse(NL.join((
            "def mg():", "    Path(os.path.abspath(__file__) + '.oops').write_bytes(b'')",
            "def a(p):", "    p.write_text('x')",
            "def b(p, q):", "    p.replace(q)",
            "def c(p):", "    return p.open('wb')",
            "def d(p, m):", "    return open(p, m)",
            "def e(p):", "    return open(p, mode='a')",
            "def f(p):", "    os.remove(p)",
            "def g(p):", "    shutil.rmtree(p)",
            "class K:", "    def h(self, p):", "        p.mkdir()",
            "def kw(p):", "    return p.open(mode='w')",
            "def var(p, m):", "    return p.open(m)",
            "def tgt(p, q):", "    p.replace(target=q)",
            "def unbound(p, q):", "    Path.replace(p, q)",
            "def ut(p):", "    os.utime(p)",
            "def sh(p):", "    shutil.chown(p, 'u'); shutil.make_archive('a', 'zip', p); shutil.unpack_archive(p, 'd')",
            "def two(p, q):", "    p.unlink(); q.unlink()",
            # 13d fix1 (review M4): `**`/`*` unpacking (a mode that cannot be seen = not a literal), `Path.link_to` (builds a hard link on 3.9-3.11), xattr
            "def star(p, kw, a):", "    p.open(**kw); open(p, **kw); open(*a)",
            "def link(p, q):", "    p.link_to(q)",
            "def xattr(p):", "    os.setxattr(p, 'user.x', b'1'); os.removexattr(p, 'user.x')",
            "def ok(s, p):",
            "    s.replace('a', 'b'); s.replace('a', 'b', count=1); open(p); open(p, 'rb'); p.open(); p.open('rb');"
            " p.open(mode='r'); os.path.join('a', 'b')")))
        self.assertEqual(disk_sites(probe), collections.Counter({
            ("mg", ".write_bytes"): 1, ("a", ".write_text"): 1, ("b", ".replace"): 1, ("c", ".open(wb)"): 1,
            ("d", "open(?)"): 1, ("e", "open(a)"): 1, ("f", "os.remove"): 1, ("g", "shutil.rmtree"): 1, ("K.h", ".mkdir"): 1,
            ("kw", ".open(w)"): 1, ("var", ".open(?)"): 1, ("tgt", ".replace"): 1, ("unbound", ".replace"): 1,
            ("ut", "os.utime"): 1, ("sh", "shutil.chown"): 1, ("sh", "shutil.make_archive"): 1,
            ("sh", "shutil.unpack_archive"): 1, ("two", ".unlink"): 2, ("star", ".open(?)"): 1, ("star", "open(?)"): 2,
            ("link", ".link_to"): 1, ("xattr", "os.setxattr"): 1, ("xattr", "os.removexattr"): 1}))

    def test_a_second_write_in_a_listed_function_is_seen(self):
        """Re-review N-1's mutation MG′ fed the exact **real source**: adding one more disk write inside
        `cmd_stop` (already on the list) ⇒ the counted-by-occurrence judge shows exactly one extra entry for it.
        The old set-at-the-function-level judge stays green here (re-review measured: Ran 61 OK) — this case
        exists exactly for that reason."""
        lines = SRC.splitlines()
        at = [i for i, x in enumerate(lines) if x.startswith("def cmd_stop(")]
        self.assertEqual(len(at), 1, at)
        lines.insert(at[0] + 1, "    Path(os.path.abspath(__file__) + '.oops').write_bytes(b'')")
        more = disk_sites(ast.parse(NL.join(lines)))
        self.assertEqual(more - DISK_WRITES, collections.Counter({("cmd_stop", ".write_bytes"): 1}))
        self.assertEqual(DISK_WRITES - more, collections.Counter())

    def test_only_real_requests_and_doctor_live_start_a_cli_session(self):
        """🔴13c Fix 1b's bug shape: a zero-quota path (a plain doctor/setup) went all the way to starting a
        session — codex's thread/start pre-warms and dials the inference endpoint (measured against its own
        logging library). ⇒ every door and every caller of starting a session is **pinned exactly as
        `SESSION_DOORS`** (`assertEqual`: missing one also turns it red — a subset-style judgment stays green
        once the scanner has gone blind); the one call site for `live_check` must have an `if` mentioning `live`
        wrapped around it.
        ⚠️This gate covers "who calls it"; the other half, "a zero-quota command really does not start a single
          session", is pinned by a behavioral test:
          tests/test_90_cli.py::Lifecycle::test_zero_quota_commands_start_no_session (really starts a child
          process, and checks the fake CLI's own record)."""
        got = session_door_callers(TREE, SESSION_DOORS)
        self.assertEqual({d: set(c) for d, c in got.items()}, SESSION_DOORS)
        self.assertEqual([g for g in got["live_check"]["doctor_facts"] if "live" not in g], [])

    def test_the_session_door_judge_is_not_blind(self):
        """Positive control: the shape from Fix 1 (a plain doctor's path calling `make_driver` directly) must be
        named; an `if live:`'s body counts as wrapped, the else ⛔ does not; two call sites in the same function
        each count on their own (one wrapped, one not ⇒ the unwrapped one still shows up); a call through an
        attribute (`self._run_once`, `x.sessions.run`), a method inside a class, and a nested function all have
        to have their qualified names recognized."""
        probe = ast.parse(NL.join((
            "def codex_carried(cfg):", "    return make_driver(cfg)",
            "def doctor_facts(cfg, live):", "    if live and ok:", "        live_check(cfg)", "    else:", "        make_driver(cfg)",
            "    live_check(cfg)",
            "class SessionManager:", "    def run(self):", "        return self._run_once()",
            "def outer():", "    def work():", "        bridge.sessions.run()",
            # 13c re-review N3's mutation S: an instance not named `sessions` — a local name, called right on the construction, taken back out of the bridge and then called
            "def cmd_probe(cfg):", "    mgr = SessionManager(cfg, None)", "    mgr.run()",
            "def cmd_probe2(cfg):", "    SessionManager(cfg, None).run()",
            "def via_attr(self):", "    s = self.b.sessions", "    s.run()",
            "def not_a_manager(p):", "    subprocess.run(['x']); p.run(); q = other.thing; q.run()",
            # 13d fix1 (review M3): other bindings in the same function — for (review's mutation S3), with, unpacking, a conditional expression, or, passed along one more hop, a comprehension, called right on a conditional expression
            "def s3(bridge):", "    for m in (bridge.sessions,):", "        m.run()",
            "def with_(b):", "    with b.sessions as m:", "        m.run()",
            "def unpack(b):", "    cfg, m = b.cfg, b.sessions", "    m.run(); cfg.run()",
            "def ifexp(b, x):", "    m = b.sessions if x else None", "    m.run()",
            "def boolop(b, x):", "    m = x or b.sessions", "    m.run()",
            "def relay(b):", "    m = b.sessions", "    m2 = m", "    m2.run()",
            "def comp(b):", "    return [m.run() for m in [b.sessions]]",
            "def recv_ifexp(b, x, y):", "    (b.sessions if x else y).run()",
            # 13b (13d re-review N1's mutation R3cl): a closure — bound in the outer scope, called inside an inner `def`; `nonlocal` binds it back from the inner scope, then called in the outer scope
            "def closure(b):", "    m = b.sessions", "    def go():", "        return m.run()", "    return go()",
            "def via_nonlocal(b):", "    m = None", "    def grab():", "        nonlocal m", "        m = b.sessions",
            "    grab()", "    m.run()",
            # false-positive side: taking the **return value** of a manager method, a different container, the one that is not the manager inside an unpacking
            "def not_one(bridge, procs):", "    n = bridge.sessions.gc_idle()", "    n.run()",
            "    for p in procs:", "        p.run()",
            # false-positive side (loud, listed in the docstring; 13d re-review N2 + the extra one 13b's tightening brought along): iterating something named `sessions`, a literal container used for more than just "taking something out of it", a name bound inside a nested `def` counting in the outer scope too, an inner parameter of the same name shadowing the outer one — all of these **do** get named
            "def fp_iter(sessions):", "    for p in sessions:", "        p.run()",
            "def fp_literal(b):", "    pair = (b.sessions, 1)", "    pair.run()",
            "def fp_leak(b, other):", "    def inner():", "        m = b.sessions", "    m = other", "    m.run()",
            "def fp_shadow(b):", "    m = b.sessions", "    def inner(m):", "        return m.run()", "    return inner(1)",
            # 13b review M6: two more old cases already listed in the docstring — when an unpacking cannot be matched up the whole group counts, and a tuple `for` target counts as a whole (there used to be no case pinning this down)
            "def fp_unpack(b):", "    a, m = b.sessions", "    a.run()",
            "def fp_for_tuple(b, x):", "    for a, m in [(x, b.sessions)]:", "        a.run()")))
        got = session_door_callers(probe, ("make_driver", "live_check", "_run_once", "sessions.run", "SessionManager"))
        self.assertEqual({d: set(c) for d, c in got.items()},
                         {"make_driver": {"codex_carried", "doctor_facts"}, "live_check": {"doctor_facts"},
                          "_run_once": {"SessionManager.run"},
                          "sessions.run": {"outer.work", "cmd_probe", "cmd_probe2", "via_attr", "s3", "with_", "unpack",
                                           "ifexp", "boolop", "relay", "comp", "recv_ifexp", "closure.go",
                                           "via_nonlocal", "fp_iter", "fp_literal", "fp_leak", "fp_shadow.inner",
                                           "fp_unpack", "fp_for_tuple"},
                          "SessionManager": {"cmd_probe", "cmd_probe2"}})
        self.assertEqual(len(got["sessions.run"]["unpack"]), 1)        # only m is it inside the unpacking: `cfg.run()` ⛔ does not count
        self.assertEqual(got["live_check"]["doctor_facts"], [["live", "ok"], []])
        self.assertEqual(got["make_driver"]["doctor_facts"], [[]])

    def test_the_two_spath_scanners_are_not_blind(self):
        """Positive control: the two scanners the pair of gates above lean on each get fed a **synthetic
        violating sample**, and both have to catch it.

        🔴Without this case, "today it is only ever spath" and "the scanner does not work at all" would look
          exactly the same — and that is exactly why the `assertLessEqual` style could sail through the entire
          plan without catching anything. The false-positive side gets its own case too."""
        probe = ast.parse(NL.join((
            "def legit():", "    return spath('work')",
            "def sneaky():", "    return state_dir() / 'oops.txt'",
            "def sneakier(p):", "    return p.with_name('x.tmp')",
            "def alsobad(p):", "    return p.parent / 'y'")))
        self.assertEqual({fn for fn, _ln, _n in call_sites(probe, is_state_dir_call)}, {"sneaky"})
        self.assertEqual({fn for fn, _ln, _a in attr_sites(probe, ("with_name", "with_suffix", "parent"))},
                         {"sneakier", "alsobad"})

    def test_writes_is_the_whole_list_and_every_log_has_its_backup(self):
        """§②'s list of "which paths it writes" **can silently grow**: review stuffed one `"exfil.json"` into
        `WRITES` ⇒ all 151 cases stayed green. `spath()`'s own gate only guarantees "a name **not** on the list
        blows up" — **the list itself**, anyone can add to.

        ⭐`assertEqual` pins the whole set: the only assertion about `WRITES` anywhere in the whole repo used to be
          `tests/test_20_argv.py:301`'s `assertIn` — a **single-element lower bound**.
        ⭐The second sentence is the **static version** of the property behind
          `tests/test_60_joblog.py::Bounded::test_rotation_name_is_checked_on_the_first_write`: every append-only
          name has to have its `.1`, ⛔ never wait until it fills up to find out."""
        # ⭐`children.json.bad` (Task 12 fix1, ledger L110 M1): when the registry gets corrupted, the copy moved aside as-is before a new table is written, keeping only the most recent one
        # ⭐`scv.prev.py` (Task 13): before `scv update` swaps out scv.py, the previous version is stored here as-is (to roll back, just copy it back)
        self.assertEqual(set(scv.WRITES), {
            "config.json", "bridge.pid", "children.json", "children.json.bad", "jobs.log", "jobs.log.1",
            "bridge.log", "bridge.log.1", "latest.json", "work", "tmp", "scv.prev.py"})
        logs = {n for n in scv.WRITES if n.endswith(".log")}
        self.assertEqual(logs, {"jobs.log", "bridge.log"})
        for n in sorted(logs):
            self.assertIn(n + ".1", scv.WRITES, n + " is append-only, but its .1 is not in WRITES")

    def test_tmp_name_carries_pid(self):
        """A tmp name has to carry the pid (writing to the same target file concurrently throws a bare
        WinError 32 on Windows).

        ⭐The original version of this case **only** asserted "who is allowed to call `getpid`" — that can
        **miss**: `_tmp_path` could perfectly well call `getpid` and still not fold it into the name, and the
        gate would still stay all green. A judge has to be falsifiable ⇒ go look directly at whether the name has
        the pid in it. The AST gate below stays too, and it covers something else: that **only that one**
        constructor is allowed to know this at all."""
        self.assertIn(str(os.getpid()), scv._tmp_path("config.json").name)     # the one sentence that can really be falsified
        # ⭐Task 12 added one more, `cmd_run`: that one is **the bridge's own identity** (written into bridge.pid, checking its own birth id against itself), ⛔ not the name of a write buffer.
        #   This is an explicit decision to let it through: one more name here means coming back and saying clearly what it needs the pid for.
        self.assertEqual({fn for fn, _ln, _n in call_sites(TREE, lambda n: n.split(".")[-1] == "getpid")},
                         {"_tmp_path", "cmd_run"})

    def test_home_dir_touched_once(self):
        """The user's home directory is only ever read in one place: the `user_home()` door (both the state
        directory's default `~/.scv` and a plain doctor's stat on codex's default home `~/.codex` come from it).
        ⚠️13c's initial version opened this door, Fix 1 (doctor switched to asking codex itself) closed it, Fix 1b
        (a plain doctor went back to just stat-ing) opened it again — the judge has not changed a word, and it has
        always been exactly one occurrence."""
        homes = [n for n in ast.walk(TREE) if isinstance(n, ast.Attribute) and n.attr in ("home", "expanduser")]
        self.assertEqual(len(homes), 1)

    def test_cli_subcommand_literals_only_in_argv_builders(self):
        """⭐`assertEqual`, ⛔ not `assertLessEqual`: **an empty set is a subset of any set**.

        The original two sentences only had an **upper bound** ("must not show up in a third place"), and the
        lower bound was entirely empty ⇒ **deleting `--disallowedTools` from `claude_argv` altogether** (= B11's
        tools-always-off silently breaking) would stay green regardless.
        After Task 4 these two literals really do exist, so there is finally something for a lower bound to pin
        down: they must never run off somewhere else, and they must never disappear either."""
        where = {v: set() for v in ("--disallowedTools", "app-server")}
        for fn, _ln, v in string_constants(TREE):
            for key in where:
                if key in v:   # ⭐substring: an exact-equality check would miss `--flag=value` forms like `--disallowedTools=Bash`
                    where[key].add(fn)
        self.assertEqual(where["--disallowedTools"], {"claude_argv"})
        self.assertEqual(where["app-server"], {"codex_argv"})

    def test_no_forbidden_knobs(self):
        consts = [v for _fn, _ln, v in string_constants(TREE)]
        for banned in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                       "CLAUDE_CODE_OAUTH_TOKEN", "forced_login_method", ".credentials.json", "auth.json"):
            self.assertEqual([c for c in consts if banned in c], [], banned)

    def test_scanner_is_not_blind(self):
        """Positive control: the scanner itself has to catch something, has to really skip a docstring, **and the
        line-number attribution has to be right** (both gates lean on this)."""
        probe = ast.parse('''
def f():
    "doc http://in-docstring"
    return "http://stray"
''')
        self.assertEqual(string_constants(probe), [("f", 4, "http://stray")])

    def test_net_scanners_are_not_blind(self):
        """Positive control: a bare hostname / bare IP / network call site all have to be caught, ⛔ while a plain
        path and a filename must never be false-positived.

        ⭐The two cases `localhost`/`::1` were **added later**: they have no dot ⇒ the old `HOSTNAME`/`IPV4`
          judges were **completely blind** to them (a bare literal `"localhost"` written outside the section
          measured all-green), and those are exactly what the next person adding a network feature is most likely
          to write down. The false-positive side pins down at the same time: **prose mentioning localhost must
          never be judged as an address**."""
        probe = ast.parse('''
import urllib.request
HTTPS = "https://"

def leg():
    host = "evil.example.com"
    ip = "203.0.113.9"
    bare = "localhost"
    v6 = "::1"
    withport = "localhost:8765"
    prose = "the local API only ever listens on this one port, localhost"
    prose2 = "configured on localhost:8765"
    path = "/bridge/hello"
    fname = "config.json"
    return urllib.request.urlopen(f"{HTTPS}{host}{path}")
''')
        self.assertEqual({v for _fn, _ln, v in string_constants(probe) if net_addrs(v)},
                         {"https://", "evil.example.com", "203.0.113.9", "localhost", "::1", "localhost:8765"})
        self.assertEqual(call_sites(probe, net_call_pred(probe)), [("leg", 15, "urllib.request.urlopen")])

    def test_the_net_call_judge_sees_the_ways_around_it(self):
        """The other half of the positive control: **the few ways around it** also have to be caught, while a
        purely local call must never be named.

        🔴This case was added on 2026-09-22, because the one above **only ever fed it one writing style,
          `urllib.request.urlopen`** ⇒ "the judge recognizes this one" and "the judge recognizes reaching outward"
          could not be told apart. Writing styles a/b/c all really slipped past the old judge (measured all-green).
        🔴d/e/f were added on 2026-09-23 (review I-2): **`urllib.request`'s own family** — a module this file uses
          every day — the judge used to only apply module-qualification to `socket`/`ssl`/`http.client`, leaving
          it out. ⚠️Every one of these cases **⛔ must never** be caught through the two tail names
          `urlopen`/`Request` (that would mean the judge is recognizing the tail name, ⛔ not the module), so each
          one picks a different entry point.
        ⭐`local2` is the false-positive side: `http.server` is a **local listener**, ⛔ not reaching outward, ⛔
          it must never be named."""
        probe = ast.parse(NL.join((
            "from http.client import HTTPSConnection",
            "import socket",
            "import urllib.request",
            "import urllib.request as _u",
            "import http.server",
            "def a():",
            "    return HTTPSConnection('x')",          # a bare call after a from-import
            "def b():",
            "    return socket.create_connection(('x', 1))",
            "def c():",
            "    s = socket.socket()",
            "    return s.connect(('x', 1))",
            "def d():",
            "    return urllib.request.build_opener().open('x')",
            "def e():",
            "    return urllib.request.urlretrieve('x')",
            "def f():",
            "    return _u.build_opener().open('x')",   # after an alias, the first segment is `_u`, ⛔ not urllib
            "def local():",
            "    return open('x').read()",
            "def local2():",
            "    return http.server.ThreadingHTTPServer(('127.0.0.1', 0), None)")))
        self.assertEqual({fn for fn, _ln, _n in call_sites(probe, net_call_pred(probe))},
                         {"a", "b", "c", "d", "e", "f"})

    def test_the_net_call_judge_sees_submodules_bound_by_from_import(self):
        """🔴Review I-B (2026-09-23 re-review): once the case above was patched, three more writing styles still
        slipped past — stuffed into `scv.py` ⇒ this gate stayed all-green across 42 cases: g/h are the **two most
        idiomatic** ones (`from urllib import request` then `request.*`, `from http import client` then
        `client.*` — the first segment is the submodule name, ⛔ not the package name), i is `asyncio` (it is on
        `NET_MODULES` but was not on the old `NET_HEADS` — the two tables were each written separately). ⭐The
        false-positive side (review M-E): a **bare call** on X after `from http.server import X` is a local
        listener; `urllib.parse` only slices strings — each writing style (module-qualified / bare after
        from-import) gets its own case."""
        probe = ast.parse(NL.join((
            "from urllib import request",
            "from http import client",
            "from urllib import request as r",
            "import asyncio",
            "from http.server import ThreadingHTTPServer",
            "import urllib.parse",
            "from urllib.parse import urlsplit",
            "def g():",
            "    return request.urlretrieve('x')",
            "def h():",
            "    return client.HTTPSConnection('x')",
            "def g2():",
            "    return r.build_opener().open('x')",
            "def i():",
            "    return asyncio.open_connection('x', 1)",
            "def local3():",
            "    return ThreadingHTTPServer(('127.0.0.1', 0), None)",
            "def local4():",
            "    return urllib.parse.urlsplit('x').hostname",
            "def local5():",
            "    return urlsplit('x').hostname")))
        self.assertEqual({fn for fn, _ln, _n in call_sites(probe, net_call_pred(probe))}, {"g", "h", "g2", "i"})

    def test_the_net_call_judge_sees_the_modules_added_to_the_table(self):
        """The five modules re-review two, I-2 merged into `NET_MODULES` — both the module-qualified and the
        from-import writing style have to be caught (m1-m6).
        ⚠️⛔Never pin down "off the table is invisible to it" here: that would be pinning down a hole, and it would
        turn this case red the moment someone tightens things by adding a module to the table later."""
        probe = ast.parse(NL.join((
            "import imaplib, poplib, nntplib, webbrowser, multiprocessing.connection",
            "from multiprocessing.connection import Client",
            "def m1():", "    return imaplib.IMAP4_SSL('x')",
            "def m2():", "    return poplib.POP3('x')",
            "def m3():", "    return nntplib.NNTP('x')",
            "def m4():", "    return webbrowser.open('x')",
            "def m5():", "    return multiprocessing.connection.Client(('x', 1))",
            "def m6():", "    return Client(('x', 1))")))
        self.assertEqual({fn for fn, _ln, _n in call_sites(probe, net_call_pred(probe))},
                         {"m1", "m2", "m3", "m4", "m5", "m6"})

    def test_the_binding_the_gate_actually_uses_is_not_blind(self):
        """🔴Both cases above feed it a judge **built fresh on the spot** (`net_call_pred(probe)`) ⇒ blinding the
        **binding** the gate actually uses (`is_net_call`) would not turn either one red — yet that binding is
        exactly what the gate itself uses. ⭐**A ruler's positive control has to be fed the ruler itself**. (The
        disk gate has always done it this way: its own "not blind" case feeds it the exact module-level
        binding.)"""
        probe = ast.parse(NL.join(("import urllib.request", "def f():",
                                   "    return urllib.request.urlopen('x')", "def g():", "    return 1")))
        self.assertEqual({fn for fn, _ln, _n in call_sites(probe, is_net_call)}, {"f"})


class Idioms(unittest.TestCase):
    """The top section `# ━━ Settled idioms`'s **pointers must never rot**.

    ⭐The entire value of that section lives in its pointers ("go there to see why, at the time") ⇒ one wrong
      pointer sends the next person off to the wrong code — **worse than not having this section at all**. And
      "keeping line numbers by hand" is a thing that rots on its own: review caught three off-by-ones across two
      rounds on this very batch.
    ⚠️Built to match **the shape of the bug**: what it judges is "does this line number land inside the symbol it
      names", ⛔ never "does this line look right"."""

    PTR = re.compile(r"scv[.]py:(\d+)\s+([A-Za-z_][A-Za-z0-9_.]*)")
    GATE = GATE_RE          # ⭐points at **the one copy** inside the module, ⛔ never write the same regex a second time here
    RETIRED_HEAD = "# ━━ Retired"

    def _block(self, head):
        """Returns (the lines inside the block, **every line after the one that terminates it**). A block =
        everything after `# ━━ <head>`, up to the next `# ━━` or the first non-`#` line. ⭐Handing back "what
        comes after" too is because **the terminating line itself also has to be asserted** (⛔ never find it with
        `lines.index(stopper)`: blank lines are everywhere in the file, and that would find the first one)."""
        lines = SRC.splitlines()
        heads = [i for i, x in enumerate(lines) if x.startswith(head)]
        self.assertEqual(len(heads), 1, head + " this section is missing (or there is more than one)")
        for k, line in enumerate(lines[heads[0] + 1:]):
            if not line.startswith("#") or line.startswith("# ━━ "):
                return lines[heads[0] + 1:heads[0] + 1 + k], lines[heads[0] + 1 + k:]
        return lines[heads[0] + 1:], []

    def _rows(self):
        """⚠️**Every line inside this section must carry a pointer, with not one exemption.**

        🔴This "silently skipped" hole has been plugged **three times**, each time in the same shape (a judge lets
          one class of line through ⇒ writing it in that shape quietly puts it outside anyone's watch): ① the
          first version only recognized lines starting with `# ⭐` ⇒ forgetting to type one ⭐ let it slip through;
          ② the second version exempted an indented continuation line (`#   `) ⇒ writing one settled idiom as a
          continuation line let it slip through;
          ③ adding the "Retired" block almost used a **blank line** as the separator ⇒ writing an idiom line right
          below a blank line would have let it escape the pointer check entirely.
        ⇒ The separator is now an **explicit marker** (`# ━━ Retired`), and the line below asserts it **really is**
          the line that terminates this section — ⛔ it must never be a blank line, and ⛔ it must never be just any
          comment. Explanatory prose always goes **above** the `# ━━ Settled idioms` line."""
        rows, rest = self._block("# ━━ Settled idioms")
        stopper = rest[0] if rest else ""
        self.assertTrue(stopper.startswith(self.RETIRED_HEAD),
                        "the settled-idioms section must be followed directly by the `" + self.RETIRED_HEAD +
                        "` line (⛔ never use a blank line as the separator: that would become a third escape "
                        "hatch, and an idiom line written below a blank line would be outside anyone's watch "
                        "entirely). It actually was: " + stopper)
        out = []
        for line in rows:
            m = self.PTR.search(line)
            self.assertIsNotNone(m, "this line in the settled-idioms section has no scv.py:line-number pointer "
                                    "(write explanatory prose above this section instead): " + line)
            out.append((int(m.group(1)), m.group(2)))
        return out

    def _retired(self):
        """The Retired block: one sentence per line + a gate given by **a fully qualified name**
        (`tests/x.py::Class::test_y`).

        ⭐The pointer writes a name, ⛔ never a line number: nothing inside `tests/` is watching over line numbers,
          and "a hand-kept line number rots by construction" is exactly the lesson the section above already
          learned (the settled-idioms gate has caught 13 drifted line numbers so far).
        🔴**This section needed its own terminating judge too** — this is the same family's **fourth** escape
          hatch: `_rows()` asserts that it is terminated by `# ━━ Retired`, while this block **used to assert
          nothing about its own terminating line** ⇒ writing a rule right after the blank line below it would be
          outside the reach of both blocks (the first three: only recognizing lines starting with `# ⭐` /
          exempting an indented continuation line / using a blank line as the separator).
        ⚠️**The one spot still nobody's business is the preamble above `# ━━ Settled idioms`**: by design it is
          just prose ("write it above when there is more to say than fits"), and that prose also uses ⭐ for
          emphasis ⇒ judging by "does it have a ⭐" would catch innocent bystanders. ⭐This boundary is written down
          here honestly, ⛔ so the next person does not assume these two blocks' gates cover the entire top of the
          file."""
        rows, rest = self._block(self.RETIRED_HEAD)
        self.assertGreaterEqual(len(rest), 2, "there is nothing at all after the Retired block?")
        self.assertEqual(rest[0].strip(), "", "the Retired block must be terminated by a blank line: " + rest[0])
        self.assertTrue(rest[1].startswith("# ━━ "),
                        "nothing is allowed to sit between the Retired block and the next `# ━━` section (a rule "
                        "sitting there is outside the reach of both blocks). It actually was: " + rest[1])
        out = []
        for line in rows:
            m = self.GATE.search(line)
            self.assertIsNotNone(m, "this line in the Retired block does not point at a gate (needs "
                                    "tests/x.py::Class::test_y): " + line)
            out.append((m.group(1), m.group(2), m.group(3)))
        return out

    def _span(self, dotted):
        scope, node = TREE, None
        for part in dotted.split("."):
            node = next((n for n in ast.iter_child_nodes(scope)
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                         and n.name == part), None)
            self.assertIsNotNone(node, "the settled-idioms section points at a name that does not exist: " + dotted)
            scope = node
        return node.lineno, node.end_lineno

    def test_nothing_is_retired_without_leaving_a_pointer(self):
        """**Retired != gone.** Once a settled idiom is retired (because a gate now mechanically guarantees it),
        the Retired block has to keep one line saying "which gate governs it now" — otherwise, three legs later,
        someone asking "why is there no settled idiom about `spath`" will end up **re-deriving the whole thing**,
        and that is the most expensive kind of repeated work on this whole path.

        ⭐The lower bound changed from "count how many settled idioms there are" to **count how many the two
          blocks add up to**: this way, "freeing up a slot" and "quietly deleting one" can be told apart (the
          original `>= 4` referred to "the four the lead named", and two of those are now exactly the ones that
          got retired — that sentence is now stale)."""
        rows, retired = self._rows(), self._retired()
        self.assertGreaterEqual(len(rows) + len(retired), 8,
                                "the two blocks together came up short: a retired entry must leave a pointer, ⛔ it must never just disappear")
        self.assertGreaterEqual(len(retired), 1, "the Retired block is empty ⇒ this gate is idling")

    def test_every_retired_pointer_names_a_gate_that_really_exists(self):
        """⭐The format alone is not enough: once a gate gets renamed, its line in the Retired block **silently
        turns into a dead link** — exactly the shape we have been fixing the whole way through.
        ⇒ Really go search that file's AST for this `Class::test`."""
        for path, klass, test in self._retired():
            with self.subTest(gate=path + "::" + klass + "::" + test):
                self.assertEqual(gate_missing(path, klass, test), "", "Retired block " + path)

    def test_the_retired_pointer_ruler_is_not_blind(self):
        """Positive control: the format judge and the "the gate really exists" sentence each get fed a
        **synthetic bad sample**, and both have to catch it."""
        self.assertIsNone(self.GATE.search("# ⭐ just an example (tests/test_00_budget.py:226)"))   # a line number does not count as a pointer
        self.assertIsNotNone(self.GATE.search("# ⭐ just an example → tests/test_00_budget.py::Budget::test_line_budget"))
        with io.open(os.path.join(ROOT, "tests", "test_00_budget.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        names = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        self.assertIn("Budget", names)
        self.assertNotIn("NoSuchClass", names)          # the ruler can tell "there" from "not there"

    def test_every_pointer_lands_inside_what_it_names(self):
        rows = self._rows()
        self.assertLessEqual(len(rows) + 3, 8 + 3, "the settled-idioms section promised ≤8 lines")
        for want, dotted in rows:
            with self.subTest(target=dotted):
                lo, hi = self._span(dotted)
                self.assertTrue(lo <= want <= hi,
                                "a settled-idiom pointer has rotted: scv.py:%d is not inside %s (it spans %d-%d)" % (want, dotted, lo, hi))

    def test_the_pointer_ruler_is_not_blind(self):
        """Positive control: ⛔**everything goes through `pointer_ok`**, so that if that judge ever gets broken,
        this case goes red first.

        🔴Each of the first two versions had **a statement that is always true** here:
          `assertTrue(b_lo <= b_lo <= b_hi)`, and after swapping it out, what got written,
          `assertTrue(b_lo <= b_hi <= b_hi)`, **is still always true** (`x <= x` always holds).
        ⚠️And the reasoning at the time had **the direction backwards**: I wrote "the span degenerating to a
          single point ⇒ the gate would **let through** almost every pointer" — the exact opposite is true:
          degenerating to a single point would make it **reject almost everything** (which is loud, and visible).
          What actually needs guarding against is the span being **too wide** (say, degenerating to the whole
          module) ⇒ that is what "lets everything through" really looks like."""
        a_lo, _a_hi = self._span("spath")
        b_lo, b_hi = self._span("_fail")
        self.assertFalse(pointer_ok(a_lo, (b_lo, b_hi)), "the ruler cannot tell spath and _fail apart ⇒ the gate above is a statement that is always true")
        self.assertTrue(pointer_ok(b_lo, (b_lo, b_hi)))
        self.assertFalse(pointer_ok(b_hi + 1, (b_lo, b_hi)))
        # a span ⛔ must never be wide enough to swallow the whole module — that failure mode would let the gate **let everything through** (⛔ not reject everything)
        self.assertLess(b_hi - b_lo, len(SRC.splitlines()) // 4)

    def test_a_pointer_that_names_the_wrong_symbol_is_caught(self):
        """Negative control: take a settled-idiom line that **deliberately points at the wrong place**, and feed
        it to **the exact judge the gate itself uses** (⛔ never copy it again here).

        🔴The previous version did exactly that copy (`lo <= int(...) <= hi` written inside the case) ⇒ when
          `pointer_ok` got broken, the gate went red, yet this control stayed green regardless — it could not
          protect the very gate it claimed to be protecting.
          This same file's `_no_backslash` exists for exactly this reason, and I made the same mistake here
          again."""
        span = self._span("_one_line")
        bad = "# ⭐ just an example settled idiom (scv.py:%d _one_line)" % (span[1] + 5)
        m = self.PTR.search(bad)
        self.assertIsNotNone(m)
        self.assertFalse(pointer_ok(int(m.group(1)), span), "it points outside the function, yet the judge says it is inside")


class Notes(unittest.TestCase):
    """`scv.py` only ever keeps **an on-the-spot warning**; **the archaeology** moves to `NOTES.md`, and wherever
    it moved from keeps one line, `📎 NOTES.md::<anchor>`.

    ⭐This gate and the Retired block make the same point a second time: **a pointer to something that does not
      exist is worse than no pointer at all — it lets someone believe it has already been checked**. The pointer
      writes **the anchor's name**, ⛔ never a line number (a line number rots, that is the settled-idioms gate's
      own lesson).
    ⚠️Boundary (13c review M11): both gates only recognize a line starting with `## ` — a `###` subsection is
      **invisible** to them, so archaeology sitting inside a subsection with nobody pointing at it never turns
      red. The one occurrence today (the "### Archaeology" block under `codex-user-home`) hangs beneath a live
      anchor and tells the history of the decision that anchor records, ⛔ that does not count as going around the
      gate; ⛔ never use a `###` to house archaeology unrelated to the anchor above it (that would be going around
      the gate)."""

    ANCHOR = re.compile(r"NOTES[.]md::([a-z0-9-]+)")

    def _notes(self):
        with io.open(os.path.join(ROOT, "NOTES.md"), encoding="utf-8") as f:
            return f.read()

    def test_every_pointer_lands_on_an_anchor_that_exists(self):
        heads = {x[3:].strip() for x in self._notes().splitlines() if x.startswith("## ")}
        used = set(self.ANCHOR.findall(SRC))
        self.assertGreaterEqual(len(used), 20, "scv.py has almost no pointers left ⇒ this gate is idling")
        self.assertEqual(sorted(used - heads), [], "scv.py points at an anchor that does not exist in NOTES.md")

    def test_no_anchor_is_left_orphaned(self):
        """Pinned the other way around too: a section in `NOTES.md` that **nobody points at** means that piece of
        archaeology has already come loose from the code — it will rot right there, and nobody will ever notice."""
        heads = {x[3:].strip() for x in self._notes().splitlines() if x.startswith("## ")}
        self.assertEqual(sorted(heads - set(self.ANCHOR.findall(SRC))), [])

    def test_the_anchor_ruler_is_not_blind(self):
        """Positive control: the judge has to really be able to tell "there" from "not there", ⛔ never a
        statement that is always true."""
        self.assertEqual(self.ANCHOR.findall("see the section at 📎 NOTES.md::b4-rotation"), ["b4-rotation"])
        self.assertEqual(self.ANCHOR.findall("NOTES.md line 12"), [])      # a line number does not count as an anchor
        heads = {x[3:].strip() for x in self._notes().splitlines() if x.startswith("## ")}
        self.assertIn("b4-rotation", heads)
        self.assertNotIn("no-such-anchor", heads)


class SpawnEnvGate(unittest.TestCase):
    """15c review I1: `child_env()` stripping session variables only counts if **every single** CLI spawn actually
    gets it — there used to be no gate pinning this down at all (review's mutation K1: all 7 call sites went
    around it, and 300 cases stayed green). This gate covers "what gets passed" (⛔ not whether `child_env` itself
    is correct, ⛔ not what a child process really sees — each has its own separate test, see
    `spawn_env_violations`'s boundary)."""

    def test_every_cli_spawn_is_handed_child_env(self):
        doors = [n for n in ast.walk(TREE) if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) in SPAWN_DOORS]
        self.assertGreaterEqual(len(doors), 7, "the ruler has not gone blind: today there are 5 run_cli sites and 2 _Pipe sites")
        self.assertEqual(spawn_env_violations(TREE), [])

    def test_the_spawn_env_judge_is_not_blind(self):
        """Positive control (whatever should be named gets named) + false-positive side (a compliant writing
        style ⛔ must never be named)."""
        bad = ast.parse(
            "def a():\n    run_cli(x, env=dict(os.environ))\n"          # mutation K1's _Pipe-style writing
            "def b():\n    run_cli(x, env=None)\n"                      # mutation K1's run_cli-style writing (today the default catches it, and it still gets named)
            "def c(env):\n    run_cli(x, env=env)\n"                    # passed in as a parameter
            "def d():\n    e = child_env()\n    e2 = dict(e)\n    run_cli(x, env=e2)\n"
            "def f():\n    e = child_env()\n    e = {}\n    run_cli(x, env=e)\n"       # bound once, then re-bound
            "def g():\n    _Pipe(a, w, dict(os.environ), 'x')\n"
            "def h():\n    _Pipe(a, w)\n"                               # _Pipe has no default
            "def i():\n    run_cli(*args)\n"
            "def j():\n    e = child_env()\n    def k():\n        run_cli(x, env=e)\n")   # an inner def is counted on its own
        self.assertEqual(spawn_env_violations(bad), ["a@2", "b@4", "c@6", "d@10", "f@14", "g@16", "h@18", "i@20", "k@24"])
        good = ast.parse(
            "def a():\n    run_cli(x, timeout=1)\n"
            "def b():\n    run_cli(x, env=child_env())\n"
            "def c():\n    e = child_env()\n    run_cli(x, env=e)\n    _Pipe(a, w, e, 'x')\n"
            "def d():\n    run_cli(x, None, None, 1, child_env())\n"
            "class D:\n    def m(self):\n        self.pipe = _Pipe(a, w, child_env(), 'x')\n")
        self.assertEqual(spawn_env_violations(good), [])


class _FreshHome(unittest.TestCase):
    """A clean SCV_HOME per test case: minting a token / writing to disk are the kind of cases that bleed into
    each other."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="case-", dir=_HOME)
        os.environ["SCV_HOME"] = self.home
        scv._log_warned = False

    def tearDown(self):
        os.environ["SCV_HOME"] = _HOME
        shutil.rmtree(self.home, ignore_errors=True)


class SpathContract(_FreshHome):
    def test_legit_name_lands_where_it_should(self):
        self.assertEqual(scv.spath("work/ok.json"), scv.state_dir() / "work" / "ok.json")
        self.assertTrue((scv.state_dir() / "work").is_dir())

    def test_rejects_unknown_head(self):
        with self.assertRaises(ValueError):
            scv.spath("../evil.txt")

    def test_rejects_parent_escape(self):
        """`work/../../oops.txt`'s first segment is also work ⇒ checking only the first segment would let it
        pass without raising, while it actually lands outside state_dir()."""
        with self.assertRaises(ValueError):
            scv.spath("work/../../oops.txt")

    def test_rejects_empty_segment(self):
        with self.assertRaises(ValueError):
            scv.spath("work//oops.txt")

    def test_escape_creates_nothing_outside(self):
        """The other half: spath() also does mkdir(parents=True) — an escaping name ⛔ must never build a
        directory outside state_dir()."""
        outside = os.path.dirname(self.home)
        before = sorted(os.listdir(outside))
        with self.assertRaises(ValueError):
            scv.spath("work/../../escaped/x.json")
        self.assertEqual(sorted(os.listdir(outside)), before)


class ConfigContract(_FreshHome):
    def _disk_token(self):
        return json.loads((scv.state_dir() / "config.json").read_text(encoding="utf-8"))["local_token"]

    def test_first_load_mints_and_persists(self):
        cfg = scv.load_config()
        self.assertTrue(cfg["local_token"].startswith("scv-"))
        self.assertEqual(self._disk_token(), cfg["local_token"])

    def test_second_load_reuses_the_same_token(self):
        self.assertEqual(scv.load_config()["local_token"], scv.load_config()["local_token"])

    def test_loser_takes_the_winners_token(self):
        """Another process already minted one first ⇒ it must **use that one**, ⛔ it must never overwrite it (the
        cost of overwriting is that it is silent: one restart and it just works, and nobody ever finds out)."""
        p = scv.spath("config.json")
        p.write_text(json.dumps({"local_token": "scv-winner"}), encoding="utf-8")
        cfg = scv._mint_token(dict(scv.DEFAULT_CONFIG), p)
        self.assertEqual(cfg["local_token"], "scv-winner")
        self.assertEqual(self._disk_token(), "scv-winner")

    def test_concurrent_loads_agree_on_one_token(self):
        n, out, lk = 8, [], threading.Lock()
        bar = threading.Barrier(n)

        def worker():
            bar.wait()
            tok = scv.load_config()["local_token"]
            with lk:
                out.append(tok)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(set(out), {self._disk_token()})   # what everyone got = the one on disk (assert the correct value)

    def test_config_is_never_visible_half_written(self):
        """At the exact moment it is being serialized, config.json is either not there yet, or already complete.

        ⛔`open(p, "x")` first creates the file at 0 bytes and only writes the content afterward — in that window,
        another process's `p.exists()` is true, yet reading it gets back an empty string ⇒ a bare
        JSONDecodeError. This does not start a second process to race against it (there is no sync point = a
        false-negative gate); instead it peeks directly at the copy on disk at the exact moment content is about
        to be written."""
        p = scv.state_dir() / "config.json"
        real, seen = scv.json, []

        class _Spy:
            loads = staticmethod(real.loads)

            @staticmethod
            def dumps(*a, **kw):
                seen.append(p.read_bytes() if p.exists() else None)
                return real.dumps(*a, **kw)

        scv.json = _Spy
        try:
            cfg = scv.load_config()
        finally:
            scv.json = real
        final = p.read_bytes()
        self.assertTrue(cfg["local_token"])
        self.assertTrue(seen)                        # the ruler has not gone blind: it really did peek at that moment
        for snap in seen:
            self.assertIn(snap, (None, final))       # assert the correct value: either it is not there yet, or it is the final copy

    def test_mint_shouts_and_raises_when_hardlink_is_unavailable(self):
        """os.link raises some other OSError (the filesystem does not support hard links) ⇒ complain one line +
        re-raise it as-is, ⛔ it must never silently fall back to the old path."""
        p = scv.spath("config.json")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch("os.link", side_effect=OSError("no hardlink here")):
            with self.assertRaises(OSError):
                scv._mint_token(dict(scv.DEFAULT_CONFIG), p)
        self.assertFalse(p.exists())                 # ⛔must never leave a half-finished file behind
        logged = scv.state_dir() / "bridge.log"
        text = logged.read_text(encoding="utf-8") if logged.exists() else ""   # not there = it said nothing at all, the red is in the next line
        self.assertIn("no hardlink here", text)      # the OS's own words, unchanged

    def test_save_config_leaves_no_tmp_behind(self):
        cfg = scv.load_config()
        scv.save_config(cfg)
        self.assertEqual(sorted(os.listdir(scv.state_dir() / "tmp")), [])


class LoggingContract(_FreshHome):
    def test_log_shouts_once_when_it_cannot_write(self):
        """Being wrong has to be loud: the log silently disappearing = all three tables stay green. Complaining
        once is enough, ⛔ never flood the screen."""
        (scv.state_dir() / "bridge.log").mkdir(parents=True)   # occupy that name ⇒ open(..., "a") is guaranteed to blow up
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            scv.log("一")
            scv.log("二")
        shouts = [ln for ln in err.getvalue().splitlines() if "could not write" in ln]
        self.assertEqual(len(shouts), 1)
        self.assertIn("bridge.log", shouts[0])


class MainContract(unittest.TestCase):
    def test_version_prints_only_the_version_on_stdout(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = scv.main(["version"])
        self.assertEqual((rc, out.getvalue().strip(), err.getvalue()), (0, scv.VERSION, ""))

    def test_too_old_python_complains_on_stderr(self):
        """`scv version`'s contract is "stdout has only the version number" ⇒ every error always goes through
        stderr."""
        old, scv.MIN_PY = scv.MIN_PY, (99, 0)
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = scv.main(["version"])
        finally:
            scv.MIN_PY = old
        self.assertEqual((rc, out.getvalue()), (2, ""))
        self.assertIn("Python", err.getvalue())


if __name__ == "__main__":
    unittest.main()
