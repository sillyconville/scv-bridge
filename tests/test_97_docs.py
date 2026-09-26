# -*- coding: utf-8 -*-
"""The gate over the three public-facing documents (`README.md` / `setup.md` / `skill/SKILL.md`) (Task 14: B13 /
B14 / B15 / B16, "what it catches" measured by B34's yardstick).

⭐There is only ever one set of criteria: every fact in the documents that can be derived from the code has its
  expected value computed here straight from scv.py (the two argv lines, `WRITES`, `REMOTE_PATHS`, both sides of
  the compatibility table, the subcommands and their options, the hello keys, the probe commands, the handshake
  method names, the log fields and their caps, the error-class table ...), never copied by hand and then asserted
  by hand — a hand copy drifts. The code changes and the document does not keep up ⇒ red; the document changes and
  says something different from the code ⇒ also red.
⭐Only reports facts that can be checked (B14): not one word that hands the reader a verdict (safe / rest assured /
  officially allowed ...) is allowed.
⚠️Boundary (what this cannot see): the sentences a human has to check — Task 13c's real-call measurements
  (R1-R11), Task 0b/B17's detach measurements, the `--safe-mode` help text verbatim, "measured only on win32" —
  this can only pin down "the names/numbers written in the documents agree with the code", never "that measurement
  itself was real"; each one is marked with its source, sentence by sentence, in an earlier measurement (not part
  of this repository). For the meaning of the prose ("check rc first", "why it
  was blocked and told to read doctor"), only the few most load-bearing sentences are pinned verbatim, and
  rewording them will turn those cells red — that is deliberate: whoever changes them has to come back and reread
  what they are pinning.
"""
import ast
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from tests import helpers
from tests import test_00_budget as budget          # the list of the ten load-bearing gates, `call_sites`, `gate_missing` (never keep a second copy)
from tests.test_70_local_api import accepted, dispatched
from tests.test_90_cli import BARE_SELF, spawn_sites
import scv

NL = chr(10)
README, SETUP, SKILL = "README.md", "setup.md", "skill/SKILL.md"
DOCS = (README, SETUP, SKILL)
with io.open(scv.__file__, encoding="utf-8") as _f:
    SRC = _f.read()
TREE = ast.parse(SRC)


def setUpModule():
    helpers.fresh_home("docs", unittest.addModuleCleanup)   # `Bridge.health` reads the registry ⇒ needs its own SCV_HOME (never built at import time)


def read(name):
    with io.open(os.path.join(helpers.ROOT, *name.split("/")), encoding="utf-8") as f:
        return f.read()


# ━━ A few small rulers for reading Markdown (they only recognize the conventions these three documents use; anything they cannot recognize goes red on the spot, never guessed at)
HEADING = re.compile(r"^(#{1,6}) (.+?)\s*$")
FENCE = re.compile(r"^```([a-z]*)\n(.*?)^```$", re.M | re.S)


def section(text, title):
    """The section whose heading is exactly `title` (up to the next heading of the same or a higher level). Not
    found, or found in more than one place: red on the spot either way."""
    lines = text.splitlines()
    hits = [i for i, x in enumerate(lines) if HEADING.match(x) and HEADING.match(x).group(2) == title]
    assert len(hits) == 1, "heading \"%s\" appears %d times" % (title, len(hits))
    level = len(HEADING.match(lines[hits[0]]).group(1))
    end = next((j for j in range(hits[0] + 1, len(lines))
                if HEADING.match(lines[j]) and len(HEADING.match(lines[j]).group(1)) <= level), len(lines))
    return NL.join(lines[hits[0] + 1:end])


def sections(text, level):
    """The section under each level-`level` heading ⇒ [(heading, body)]."""
    out, cur, body = [], None, []
    for x in text.splitlines():
        m = HEADING.match(x)
        if m and len(m.group(1)) <= level:
            if cur is not None:
                out.append((cur, NL.join(body)))
            cur, body = (m.group(2) if len(m.group(1)) == level else None), []
        elif cur is not None:
            body.append(x)
    if cur is not None:
        out.append((cur, NL.join(body)))
    return out


def ticks(text):
    """Everything wrapped in backticks (inline code) in a piece of text, in the order it appears."""
    return re.findall(r"`([^`" + NL + r"]+)`", text)


def span(text, anchor, stop):
    """The stretch from `anchor` (exactly one place in the whole text) to the first `stop` after it."""
    assert text.count(anchor) == 1, "anchor \"%s\" appears %d times" % (anchor, text.count(anchor))
    rest = text[text.index(anchor) + len(anchor):]
    assert stop in rest, "the anchor \"%s\" has no \"%s\" anywhere after it" % (anchor, stop)
    return rest[:rest.index(stop)]


def table(text, header):
    """The table whose header row is exactly `header` (`| a | b |`) ⇒ each row's cells (the header and separator
    rows dropped)."""
    lines = text.splitlines()
    assert lines.count(header) == 1, "header row \"%s\" appears %d times" % (header, lines.count(header))
    i = lines.index(header) + 2
    rows = []
    while i < len(lines) and lines[i].startswith("|"):
        rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
        i += 1
    return rows


def code_lines(text):
    """All the "code" in a document: every line inside a fenced block, plus every span of inline code outside one."""
    out = [ln for _lang, body in FENCE.findall(text) for ln in body.splitlines() if ln.strip()]
    return out + ticks(FENCE.sub("", text))


# ━━ Expected values computed straight from the code (each one computed in exactly one place)
def fn_node(qualname):
    """`A.b` or `b` ⇒ that FunctionDef (in scv.py's AST)."""
    parts, scope = qualname.split("."), TREE
    for p in parts:
        scope = next(n for n in ast.walk(scope) if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == p)
    return scope


def claude_line():
    return ["<model>" if x == "MODEL" else x
            for x in scv.claude_argv(["<claude>"], "MODEL", "<work-dir>/system.txt", "<work-dir>/isolation.json")]


def codex_line():
    return [x.replace("MODEL", "<model>").replace('effort="high"', 'effort="<effort>"')
            for x in scv.codex_argv(["<codex>"], "MODEL", "high")]


def probe_commands():
    """The string of literals in every `run_cli(list(head) + [literal…])` call ⇒ in source order."""
    out = []
    for n in ast.walk(TREE):
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "run_cli" and n.args \
                and isinstance(n.args[0], ast.BinOp) and isinstance(n.args[0].right, ast.List):
            out.append(" ".join(e.value for e in n.args[0].right.elts))
    return out


def rpc_calls():
    """The method names in `CodexDriver.__init__`'s `self._call("method", …)` calls, in order."""
    init = fn_node("CodexDriver.__init__")
    calls = [n for n in ast.walk(init) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "_call" and n.args and isinstance(n.args[0], ast.Constant)]
    return [n.args[0].value for n in sorted(calls, key=lambda n: (n.lineno, n.col_offset))]   # ⚠️ast.walk visits level by level, never in source order


def thread_start_params():
    init = fn_node("CodexDriver.__init__")
    call = next(n for n in ast.walk(init) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "_call"
                and n.args and getattr(n.args[0], "value", "") == "thread/start")
    return {k.value: v.value for k, v in zip(call.args[1].keys, call.args[1].values) if isinstance(v, ast.Constant)}


def dict_keys_in(qualname, pick=lambda d: True):
    """The keys (in order) of the first dict literal in that function that satisfies `pick`."""
    for n in ast.walk(fn_node(qualname)):
        if isinstance(n, ast.Dict) and all(isinstance(k, ast.Constant) for k in n.keys) and pick(n):
            return [k.value for k in n.keys]
    raise AssertionError("no such dict literal found in %s" % qualname)


def minted_keys():
    """The keys `_mint_token` adds into cfg (`cfg["…"] = …`)."""
    return [t.slice.value for n in ast.walk(fn_node("_mint_token")) if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)]


def request_defaults():
    return scv.normalize_request({"model": "x", "messages": [{"role": "user", "content": "a"}]})


def names_referring_to(name):
    """The functions in the source that read `name` (`main`'s dispatch table counts as reading it too)."""
    out = set()

    def visit(node, fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node.name
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load):
            out.add(fn)
        for c in ast.iter_child_nodes(node):
            visit(c, fn)

    visit(TREE, "<module>")
    return out


def spawned_programs():
    """Among the places that spawn a subprocess, the programs whose argv[0] is a literal ⇒
    `{program name: the rest of the literals across its argv calls}` (`taskkill`, `ps`, ...)."""
    out = {}
    for n in ast.walk(TREE):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") in ("run", "Popen") \
                and getattr(n.func.value, "id", "") == "subprocess" and n.args and isinstance(n.args[0], ast.List) \
                and n.args[0].elts and isinstance(n.args[0].elts[0], ast.Constant):
            out.setdefault(n.args[0].elts[0].value, set()).update(
                e.value for e in n.args[0].elts[1:] if isinstance(e, ast.Constant))
    return out


def arg_of_call_in(qualname, callee, index):
    """The `index`-th argument of the call to `callee(…)` inside that function, evaluated against scv's globals
    (⭐this reads the value the code actually uses, never a hand-derived copy)."""
    calls = [n for n in ast.walk(fn_node(qualname)) if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == callee and len(n.args) > index]
    assert len(calls) == 1, "%s calls %s in %d place(s)" % (qualname, callee, len(calls))
    return eval(compile(ast.Expression(calls[0].args[index]), "<scv>", "eval"), vars(scv))


QUOTE_DOORS = {"log", "_bad", "_fail", "BridgeError", "_refuse", "_cmd_failed"}
# Assigning to these attributes gets the same treatment as going through a door: `RemoteLeg.refused` is both
#   written to disk (`log("❌ " + self.refused)`) and surfaced on `/healthz` (no token needed) and `status`'s stdout
#   (Fix 4, re-review 3 I-b: the dispatcher's `min_supported` used to go out verbatim from here)
QUOTE_ATTRS = {"refused"}


def quote_sites():
    """Every `x[:N]` truncation, inside the arguments of the doors that write to disk / report back (`QUOTE_DOORS`)
    and inside the values assigned to the `QUOTE_ATTRS` attributes ⇒ `{(the function it is in, x's source, N)}`.
    ⭐What is visible when this is compared as a set (`test_every_peer_quote…`, which compares `(function, source)`):
      what gets truncated changes, a truncation is removed (that entry vanishes from the set), a new truncation is
      added; N only feeds into the sentence "the maximum of the peer's cells = README's number" ⇒ only becomes
      visible if it is cut longer and that exceeds the current maximum.
    ⚠️What this cannot see (a boundary; re-review m3, re-review 2 m-b): ① cut longer but still ≤ the maximum
      (re-review 2's cut `[:32]`→`[:64]` stays green); ② a newly added reference that is never truncated at all
      (`log("…%s" % original_value)`); ③ one truncated through a local variable before being passed to the door —
      today there are three of these: two inside `_check_extra_models` (config.json's `extra_models` names, via
      `_extra_once`) and one inside `usage_numbers` (a usage key name from the CLI's output), none of which is peer
      text, and neither table lists them; ④ anything that writes to disk without going through these doors."""
    out = set()

    def visit(node, fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node.name
        door = isinstance(node, ast.Call) and (getattr(node.func, "id", "") in QUOTE_DOORS
                                               or getattr(node.func, "attr", "") in QUOTE_DOORS)
        if door or isinstance(node, ast.Assign) and any(getattr(t, "attr", "") in QUOTE_ATTRS
                                                        for tg in node.targets for t in ast.walk(tg)):
            for s in ast.walk(node):
                if isinstance(s, ast.Subscript) and isinstance(s.slice, ast.Slice) and s.slice.lower is None \
                        and isinstance(s.slice.upper, ast.Constant) and isinstance(s.slice.upper.value, int):
                    out.add((fn, ast.unparse(s.value), s.slice.upper.value))
        for c in ast.iter_child_nodes(node):
            visit(c, fn)

    visit(TREE, "<module>")
    return out


# ⭐The places that quote a value given by the peer (README's "bridge.log" entry lists them one by one) ⇒
#   `{(function, what gets truncated): the words README uses to name it}`. Always "repr first, then cut" (review
#   re-review m4: cutting first and then `%r` would let the escaping inflate the length; a newline in the original
#   value would also be inflated by `log()` folding it into " ⏎ ").
QUOTED_PEER_TEXT = {("_closed", "repr(value)"): "`effort`", ("_text_of", "repr(kind)"): "content-block type",
                    ("normalize_request", "repr(role)"): "`role`", ("_guard", "repr(host)"): "`Host` header",
                    ("_guard", "repr(origin)"): "`Origin` header", ("_preflight", "repr(self.path)"): "path",
                    ("_get", "repr(self.path)"): "path", ("_post", "repr(self.path)"): "path",
                    ("_dispatch", "repr(event)"): "the name of an event from the paired service",
                    ("_route", "repr(event)"): "the name of an event from the paired service",
                    ("_route", "repr(raw)"): "the event itself when it was not valid JSON",
                    ("cmd_update", "repr(commit)"): "the commit and sha256", ("cmd_update", "repr(want)"): "the commit and sha256",
                    # Fix 3 (added by the maintainer): the request path, and the peer's own error text (HTTP reason phrase etc.), also always get repr'd before being cut
                    ("_json", "repr(self.path)"): "a second response", ("_serve", "repr(self.path)"): "hit an internal error",
                    ("_post", "repr(str(e))"): "of a failed call to the paired service", ("run", "repr(str(e))"): "of the remote leg dropping",
                    ("_dispatch", "repr(str(e))"): "of an error while handling one of its events",
                    # Task 15b (M-6): after the ack step moved onto the send channel (`_Outbox._loop`), the line for it blowing up also landed there
                    ("_loop", "repr(str(e))"): "of an error while handling one of its events",
                    ("cmd_pair", "repr(str(e))"): "of a failed `pair`", ("cmd_update", "repr(str(e))"): "of a failed download in `update`",
                    # Fix 4 (re-review 3 I-b): the minimum version the dispatcher asked for — the entry point passes it through the shape gate first (a bad shape reports only the length), and one with a good shape is still repr'd before being cut
                    ("run", "repr(need)"): "the minimum version the paired service asked for"}
# What gets cut here is our own material (never peer text): a filename under tmp or under work/, a codex reply's key names (protocol vocabulary), the session id's hash
QUOTED_OWN_TEXT = {("sweep_tmp", "gone"), ("__init__", "sorted((str(k) for k in res))"),     # `__init__` = CodexDriver's
                   ("_run_session", "hashlib.sha256(sid.encode('utf-8')).hexdigest()"),
                   # I-1: sweep_work's `gone` is a list of work/ directory names (the hash-and-random shape
                   #   _workdir builds), the same kind of material as sweep_tmp's own `gone`, never peer text
                   ("sweep_work", "gone")}


# ━━ Places that read from disk, places that spawn a subprocess: pinned into tables (in the style of test_00's `NET_CALLERS` / `DISK_WRITERS`).
# ⭐README's "Files it reads outside the state directory" and "What it runs" sections are the two a human checked
#   against these two tables: one more place reading from disk or spawning a subprocess ⇒ this goes red ⇒ whoever
#   changes it has to go back and reread those two sections to confirm they are still true.
# ⚠️The `_open` cell is urllib's `OpenerDirector.open` (the name collision, not a disk read); among the places that
#   read from disk, only `cmd_update` reads something outside the state directory (`scv.py` itself), and everything
#   else goes through `spath()` (the two spath gates in test_00 govern how a path gets built).
READ_SITES = {"_append_capped", "_children_parse", "_log_tail", "_mint_token", "_open", "_read", "_read_pid_file",
              "cmd_update", "load_config"}
SPAWN_SITES = {"__init__", "_birth_once", "kill_pid_tree", "proc_rss_kb", "run_cli", "spawn_detached"}


def read_sites():
    return {fn for fn, _ln, n in budget.call_sites(TREE, lambda n: n.split(".")[-1] in ("read_text", "read_bytes")
                                                    or n == "open")}


# ━━ B14: never hand the reader a verdict word
BANNED = [re.compile(p, re.I) for p in (
    r"\bsafe(ly|ty)?\b", r"\bsecur(e|ely|ity)\b", r"\bguarantee", r"100 ?%", r"\bofficial(ly)?\b", r"\bharmless\b",
    r"\brisk[- ]free\b", r"\bno risk\b", r"rest assured", r"don'?t worry", r"nothing to worry", r"no need to worry",
    r"\btrustworthy\b", "安全", "放心", "无风险", "官方允许",
    # Task 14 review I3: a rephrased hint at a guarantee ("nothing can leak in", "fully isolated") used to slip
    #   past the word list. `isolation.json` is a filename, and \b stops it from being caught.
    r"\bisolated\b", r"nothing[^.]{0,30}leak", r"(cannot|can't|never)[^.]{0,10}leak")]
FLAG_NAMES = ("--safe-mode",)       # the flag's own name is never a verdict (it is one item on claude's command line)


def verdict_words(text):
    for f in FLAG_NAMES:
        text = text.replace(f, "")
    return sorted({m.group(0).lower() for p in BANNED for m in p.finditer(text)})


class NoVerdictWords(unittest.TestCase):
    def test_no_doc_hands_the_reader_a_verdict(self):
        """B14: setup.md has the agent report facts; the document itself writing a sentence like "this is safe"
        would void the whole audit mechanism on the spot (spec §7)."""
        for name in DOCS:
            with self.subTest(doc=name):
                self.assertEqual(verdict_words(read(name)), [])

    def test_the_ruler_is_not_blind(self):
        """positive control: every entry the brief's original word list had must still be caught; the flag name
        `--safe-mode` never counts; changing the case still gets caught."""
        for w in ("是安全的", "请放心", "可以放心", "无风险", "官方允许", "officially allowed", "is safe", "are safe",
                  "100%", "guarantee", "It is Secure.", "rest assured", "Don't worry"):
            with self.subTest(word=w):
                self.assertNotEqual(verdict_words("x " + w + " y"), [])
        self.assertEqual(verdict_words("`--safe-mode` is on"), [])
        self.assertEqual(verdict_words("safe --safe-mode"), ["safe"])       # stripping the flag name must never also let the word right next to it through
        for w in ("so calls are fully isolated", "nothing on this machine leaks into calls", "nothing can leak",
                  "it cannot leak", "prompts never leak"):
            with self.subTest(word=w):
                self.assertNotEqual(verdict_words(w), [])
        self.assertEqual(verdict_words("the settings file `isolation.json`; it can show that the secret leaked"), [])


# ━━ README: facts derived from the code
class ReadmeArgv(unittest.TestCase):
    """The two command lines: the two JSON arrays in README must equal, item for item, what `claude_argv` /
    `codex_argv` build (after the placeholders are substituted).
    ⭐Uses a JSON array, never one shell line: argv never goes through a shell to begin with (`Popen(list)`), and
    the array is the only way to write it with no quoting ambiguity."""

    def blocks(self):
        return [json.loads(body) for lang, body in FENCE.findall(section(read(README), "What it runs")) if lang == "json"]

    def test_the_two_command_lines_are_the_ones_the_code_builds(self):
        self.assertEqual(self.blocks(), [claude_line(), codex_line()])

    def test_the_optional_effort_tail_and_the_codex_default(self):
        with_effort = scv.claude_argv(["<claude>"], "MODEL", "a", "b", "high")
        self.assertEqual(with_effort[len(claude_line()):], ["--effort", "high"])
        self.assertIn('`"--effort", "<effort>"`', read(README))
        dflt = next(x for x in scv.codex_argv(["x"], "m", None) if x.startswith("model_reasoning_effort="))
        self.assertIn("`<effort>` is `%s` when the request sets none" % json.loads(dflt.split("=", 1)[1]), read(README))

    def test_the_claude_session_files_are_the_ones_the_driver_writes(self):
        """`system.txt` / `isolation.json` and their content: computed from the literals in
        `ClaudeDriver.__init__`."""
        init = fn_node("ClaudeDriver.__init__")
        consts = {n.value for n in ast.walk(init) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        iso = next(ast.literal_eval(n.args[0]) for n in ast.walk(init) if isinstance(n, ast.Call)
                   and getattr(n.func, "attr", "") == "dumps")
        text = read(README)
        for name in ("system.txt", "isolation.json"):
            self.assertIn(name, consts)
            self.assertIn("`%s`" % name, text)
        self.assertIn("`%s`" % json.dumps(iso), text)

    def test_the_ruler_is_not_blind(self):
        """positive control: one item short, one item extra, or a reordering are each told apart (never a
        criterion that is true no matter what, along the lines of "as long as it is a list")."""
        good = claude_line()
        self.assertNotEqual(good[:-1], claude_line())
        self.assertNotEqual(good + ["--x"], claude_line())
        self.assertNotEqual([good[1], good[0]] + good[2:], claude_line())
        self.assertEqual(len(self.blocks()), 2)


class ReadmeFactsFromCode(unittest.TestCase):
    """Every fact in README that can be derived from the code (names, paths, numbers) ⇒ expected values computed
    here."""

    @classmethod
    def setUpClass(cls):
        cls.text = read(README)

    def test_what_it_connects_to(self):
        t = section(self.text, "What it connects to")
        self.assertIn("on `%s` only, on port %d" % (scv.LOCAL_HOST, scv.DEFAULT_CONFIG["port"]), t)
        self.assertEqual(ticks(span(t, "unless its `Host` header is", ";")), list(scv.LOOPBACK_HOSTS))
        routes = ["GET " + r for r in scv.GET_ROUTES] + ["POST " + r for r in scv.POST_ROUTES]
        self.assertEqual(ticks(span(t, "**The local API** answers", ". ")), routes)
        self.assertIn("`<url>%s`" % scv.REMOTE_PATHS["pair"], t)
        self.assertEqual(ticks(span(t, "talks to that one host, on", ". ")),
                         [p for k, p in scv.REMOTE_PATHS.items() if k != "pair"])
        self.assertIn("must start with `%s`" % scv.HTTPS, t)
        self.assertIn("`%s/<commit>/scv.py`" % scv.UPDATE_BASE, t)

    def test_hello_is_exactly_the_whitelist(self):
        stub = types.SimpleNamespace(found={}, cat=[], cfg={})
        said = ticks(span(section(self.text, "What it connects to"), "`hello` sends exactly these keys:", "—"))
        self.assertEqual(sorted(said), sorted(scv.hello_payload(stub)))
        self.assertEqual(len(said), len(set(said)))

    def test_what_it_writes_is_the_whole_writes_list(self):
        """the table's first column = `WRITES`, ⭐pinned both ways (`assertEqual`): the code writing one more name,
        or the table listing one more, either way goes red."""
        t = section(self.text, "What it writes")
        names = [ticks(r[0])[0] for r in table(t, "| Name | What it is |")]
        self.assertEqual(sorted(names), sorted(scv.WRITES))
        self.assertEqual(len(names), len(set(names)))

    def test_rotation_numbers_come_from_the_code(self):
        t = section(self.text, "What it writes")
        cap, line = scv.LOG_CAP_BYTES, scv.LINE_CAP_BYTES
        self.assertIn("when one reaches %d bytes" % cap, t)
        self.assertIn("no line is longer than %d bytes" % line, t)
        self.assertIn("2 × (%d + %d) bytes" % (cap, line), t)

    def test_the_one_write_outside_the_state_dir(self):
        """`update`'s buffer sitting next to scv.py: the suffix is computed from the literal in `cmd_update`."""
        suffix = [n.value for n in ast.walk(fn_node("cmd_update")) if isinstance(n, ast.Constant) and n.value == ".new"]
        self.assertEqual(len(suffix), 1)
        self.assertIn("`scv.py%s`" % suffix[0], section(self.text, "What it writes"))

    def test_config_keys(self):
        said = ticks(span(section(self.text, "What it writes"), "`config.json` keys:", NL))
        self.assertEqual(sorted(said), sorted(list(scv.DEFAULT_CONFIG) + minted_keys()))

    def test_what_it_runs(self):
        t = section(self.text, "What it runs")
        self.assertIn("`%%LOCALAPPDATA%%/%s`" % scv.CODEX_GLOB, t)
        wrap = [[e.value for e in n.value.elts if isinstance(e, ast.Constant)] for n in ast.walk(fn_node("cli_head"))
                if isinstance(n, ast.Return) and isinstance(n.value, ast.List)]
        wrap = [w for w in wrap if w]            # the `return [exe]` cell has no literal; what we want is the wrapper around a `.cmd`
        self.assertEqual(len(wrap), 1)
        self.assertIn("`%s`" % " ".join(wrap[0]), t)
        self.assertIn("an effort from " + ", ".join("`%s`" % e for e in scv.EFFORTS), t)
        self.assertEqual(ticks(span(t, "Then the bridge calls", ". ")), rpc_calls())

    def test_thread_start_literals_both_ways(self):
        """`sandbox` / `ephemeral` checked both ways (Task 14 review I4(b)/A12: this used to check only "what is in
        the code" ⇒ dropping `sandbox` from thread/start entirely would still leave README saying read-only, and
        the suite still green). First asserts both keys are in the code, then pulls the two values out of README
        and compares them against the code."""
        code = thread_start_params()
        self.assertEqual(sorted({"sandbox", "ephemeral"} - set(code)), [])
        para = span(section(self.text, "What it runs"), "`thread/start` carries", ". ")
        said = dict(re.findall(r'`"(sandbox|ephemeral)": ([^`]+)`', para))
        self.assertEqual({k: json.loads(v) for k, v in said.items()}, {k: code[k] for k in ("sandbox", "ephemeral")})

    def test_quick_probes_are_exactly_the_run_cli_calls(self):
        """the probe table's first column = every `run_cli(list(head) + [literal…])` call in the code, pinned both
        ways (review M7/A13: this used to only check "everything in the code is written in the document", so the
        document listing one more probe that does not even exist would still be green)."""
        found = probe_commands()
        self.assertIn("--version", found)          # the ruler is not blind: the scanner really does recognize that pattern
        t = section(self.text, "What it runs")
        rows = table(t, "| Probe | CLI | When |")
        self.assertEqual(sorted(ticks(r[0])[0] for r in rows), sorted(set(found)))
        probe_dir = [n.value for n in ast.walk(fn_node("_probe_dir")) if isinstance(n, ast.Constant)
                     and isinstance(n.value, str) and n.value.startswith("work/")]
        self.assertEqual(len(probe_dir), 1)
        self.assertIn("`%s`" % probe_dir[0], span(t, "**Quick probes.**", NL + NL))

    def test_every_program_it_spawns_is_named(self):
        """the places that spawn a subprocess are pinned into a table; the programs whose argv starts with a
        literal = the README table's first column (pinned both ways), and every program's fixed arguments are
        written down."""
        self.assertEqual({fn for fn, _kind in spawn_sites(TREE)}, SPAWN_SITES,
                         "the places that spawn a subprocess changed ⇒ go reread README's \"What it runs\", then update this table")
        progs = spawned_programs()
        self.assertEqual(sorted(progs), ["ps", "taskkill"])        # the ruler is not blind
        rows = {ticks(r[0])[0]: r[1] for r in table(section(self.text, "What it runs"), "| Program | Fixed arguments | When |")}
        self.assertEqual(sorted(rows), sorted(progs))
        for prog, fixed in progs.items():
            with self.subTest(program=prog):
                self.assertEqual(sorted(set(ticks(rows[prog]))), sorted(fixed))

    def test_the_read_sites_are_the_ones_the_readme_was_checked_against(self):
        self.assertEqual(read_sites(), READ_SITES,
                         "the places that read from disk changed ⇒ go reread README's \"Files it reads outside the state directory\", then update this table")

    def test_update_runs_only_when_asked(self):
        """B13: never a silent background self-update. `cmd_update` is only ever read by `main`'s dispatch table
        (never called from a background path)."""
        self.assertEqual(names_referring_to("cmd_update"), {"main"})
        self.assertIn("It runs only when you run it.", section(self.text, "Commands"))

    def test_where_prompts_end_up(self):
        t = section(self.text, "Where prompts and answers end up")
        fields = dict_keys_in("JobLog.write", lambda d: any(k.value == "ts" for k in d.keys))
        self.assertEqual(ticks(span(t, "`jobs.log` holds one line of metadata per job —", " — ")), fields)
        # ⭐job_id's ceiling is read from the exact expression `JobLog.write`'s `_clip(v, …)` actually uses (review
        #   I4(a)/A6: this used to be hand-copied as `// 8`, and changing the code to `// 4` still kept the suite green)
        self.assertIn("up to %d bytes of its text" % arg_of_call_in("JobLog.write", "_clip", 1), t)
        self.assertIn("up to %d bytes of the CLI's raw output" % scv.LINE_CAP_BYTES, t)
        self.assertIs(thread_start_params()["ephemeral"], True)
        # review I1: `model` is a closed set — README describes `shown_model`'s behavior, which `ModelNamesOnDisk` pins by behavior
        self.assertIn("`model` is always a name the bridge listed in `/v1/models`", t)

    def test_every_peer_quote_in_bridge_log_is_listed_and_escaped_first(self):
        """README's "bridge.log" entry lists "which values it quotes from the peer" = the truncation points inside
        the code's doors that write to disk (review re-review N1: this used to be a hand-written list that missed
        the name of a dropped event).
        ⭐The load-bearing part is the first sentence (set equality, comparing `(function, source)`): the truncation
          points written directly inside the disk-writing doors are pinned into two tables (the peer's / our own),
          and one more, one fewer (X5: a truncation removed), or a change to what gets truncated (reverting to cut
          first and `%r` second: what got cut would then not be `repr(…)` any more) all go red. ⚠️The "whole set"
          only reaches as far as `quote_sites` can see: the three places truncated through a local variable before
          being passed to the door (two in `_check_extra_models`, one in `usage_numbers`, config/CLI text) are in
          neither table.
        ⭐The second sentence, "the peer's cells all start with `repr(`", guards against the table itself being
          changed (re-review 2 m-b ③); the third sentence has README name every one of them, with the ceiling =
          their maximum (cut longer but still under the maximum is invisible here — README's number still holds)."""
        t = section(self.text, "Where prompts and answers end up")
        sites = quote_sites()
        self.assertEqual({(fn, src) for fn, src, _n in sites}, set(QUOTED_PEER_TEXT) | QUOTED_OWN_TEXT,
                         "the truncation points inside the doors that write to disk changed ⇒ go reread README's \"bridge.log\" entry, then update these two tables")
        peer = [(fn, src, n) for fn, src, n in sites if (fn, src) in QUOTED_PEER_TEXT]
        self.assertEqual([s for s in peer if not s[1].startswith("repr(")], [])
        line = span(t, "These lines quote a short piece of something that came from outside", "A model name the bridge did not list")
        self.assertIn("escaped first and then cut, so at most %d characters as written" % max(n for _f, _s, n in peer), line)
        for (fn, src), words in QUOTED_PEER_TEXT.items():
            with self.subTest(site=fn + ":" + src):
                self.assertIn(words, line)

    def test_a_malformed_min_supported_is_logged_as_its_length_only(self):
        """re-review 3 I-b: "one to three dot-separated groups of digits, each up to nine digits" is read verbatim
        from the entry point's own ruler (pinned: the ruler changes ⇒ this goes red ⇒ come back and reread this
        sentence); "records only the length" is pinned by behavior in
        tests/test_80_remote.py::MinSupportedShape (bridge.log / `/healthz` / `status` all carry only the length).
        Fix 5: each group is capped at 9 digits (the maintainer's ruling; `[0-9]+` used to have no cap, and a group
        over 4300 digits made `_ver` raise and back off forever)."""
        self.assertEqual(scv.MIN_SUPPORTED_RE.pattern, "[0-9]{1,9}([.][0-9]{1,9}){0,2}")
        self.assertIn("A minimum version from the paired service that is not one to three numbers of up to nine digits joined "
                      "by dots is logged as its length only.", section(self.text, "Where prompts and answers end up"))

    def test_where_the_error_message_comes_from(self):
        """README's error-body paragraph (re-review N3): the N in "the last N characters of stderr" is read from
        `_Pipe.stderr_tail`'s slice; " (code=N)" is read from `_rpc_error`'s format string."""
        tail = [s.slice.lower for s in ast.walk(fn_node("_Pipe.stderr_tail")) if isinstance(s, ast.Subscript)
                and isinstance(s.slice, ast.Slice) and s.slice.lower is not None]
        self.assertEqual(len(tail), 1)
        n = -ast.literal_eval(tail[0])
        t = section(self.text, "Local API")
        self.assertIn("the last %d characters of the CLI's stderr" % n, t)
        fmt = [c.value for c in ast.walk(fn_node("_rpc_error")) if isinstance(c, ast.Constant) and isinstance(c.value, str)
               and "code=" in c.value]
        self.assertEqual(fmt, ["%s (code=%s)"])
        # re-review 2 I-a: `(code=…)` is only added when the error object carries a code; with no message it is the whole object (pinned by behavior, three cells)
        self.assertEqual(scv._rpc_error({"message": "m"}), "m")
        self.assertNotIn("(code=", scv._rpc_error({"message": "m"}))
        self.assertEqual(scv._rpc_error({"message": "m", "code": 5}), "m (code=5)")
        self.assertEqual(scv._rpc_error({"code": 5}), str({"code": 5}))
        self.assertIn("followed by ` (code=…)` when Codex gave a code (the whole error object when it has no `message`)", t)
        # the sentence for when a failed turn has no error object: read from the literal in `CodexDriver.turn`
        turn = [c.value for c in ast.walk(fn_node("CodexDriver.turn")) if isinstance(c, ast.Constant) and c.value == "turn failed"]
        self.assertEqual(len(turn), 1)
        self.assertIn("A failed Codex turn with no error object gives `%s`" % turn[0], t)
        # the cell for "wrapped in a sentence the bridge writes itself" for Codex: the handshake's `_call`s carrying `why` (the 4th argument) are exactly those steps
        steps = [n.args[0].value for n in sorted(
            (n for n in ast.walk(fn_node("CodexDriver.__init__")) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "_call" and len(n.args) >= 4), key=lambda n: (n.lineno, n.col_offset))]
        self.assertEqual(steps, ["config/read", "skills/list"])
        self.assertIn("Codex's %s failing while a session starts" % " or ".join("`%s`" % s for s in steps), t)
        # the class for the "session closed while in flight" cell: read from the BridgeError `_closed_midflight` builds
        klass = [n.args[0].value for n in ast.walk(fn_node("_closed_midflight")) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", "") == "BridgeError"]
        self.assertEqual(klass, ["cancelled"])
        self.assertIn("a session closed while the call was in flight (class `%s`)" % klass[0], t)

    def test_facts_the_rereview_changed_without_a_red(self):
        """re-review I2's four cuts (X1-X4) and two more: code facts that README states, that no suite used to
        compare, are now all read straight from the code."""
        t = self.text
        # X1: the session working-directory name = the first N characters of the sha256 of the session id
        cut = [s.slice.upper.value for s in ast.walk(fn_node("SessionManager._workdir")) if isinstance(s, ast.Subscript)
               and isinstance(s.slice, ast.Slice) and isinstance(s.slice.upper, ast.Constant)]
        self.assertEqual(len(cut), 1)
        self.assertIn("the first %d hex characters of the sha256 of the session id" % cut[0], t)
        # X2: config.json's file mode (the two chmod calls in `save_config` / `_mint_token` must be the same number)
        modes = {n.args[1].value for f in ("save_config", "_mint_token") for n in ast.walk(fn_node(f))
                 if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "chmod" and len(n.args) == 2}
        self.assertEqual(len(modes), 1)
        self.assertIn("File mode %s where the OS supports it" % ("0" + oct(modes.pop())[2:]), t)
        # X3: `/v1/sessions/close`'s 202 reply body
        bodies = [ast.literal_eval(n.args[1]) for n in ast.walk(fn_node("_make_handler._close")) if isinstance(n, ast.Call)
                  and getattr(n.func, "attr", "") == "_json" and n.args and getattr(n.args[0], "value", None) == 202]
        self.assertTrue(bodies)
        for b in bodies:
            self.assertIn("`202 %s`" % json.dumps(b), t)
        # X4: the cell in thread/start's skills table
        skills = [n for n in ast.walk(fn_node("_codex_user_off")) if isinstance(n, ast.Dict)
                  and any(getattr(k, "value", None) == "include_instructions" for k in n.keys)]
        self.assertEqual(len(skills), 1)
        val = dict(zip([k.value for k in skills[0].keys], skills[0].values))["include_instructions"]
        self.assertIn("sets `include_instructions` to %s" % json.dumps(ast.literal_eval(val)), section(t, "What it runs"))
        # `start`'s three flags: evaluate the `flags` expression in `spawn_detached`, then break it into names by the Win32 constants
        flags = [n.value for n in ast.walk(fn_node("spawn_detached")) if isinstance(n, ast.Assign)
                 and getattr(n.targets[0], "id", "") == "flags"]
        self.assertEqual(len(flags), 1)
        value = eval(compile(ast.Expression(flags[0]), "<scv>", "eval"), vars(scv))
        win32 = {"CREATE_NEW_PROCESS_GROUP": 0x00000200, "CREATE_BREAKAWAY_FROM_JOB": 0x01000000, "CREATE_NO_WINDOW": 0x08000000}
        names = sorted(k for k, v in win32.items() if value & v)
        self.assertEqual(sum(win32[k] for k in names), value)          # no unrecognized bits
        said = sorted(ticks(span(section(t, "Known limits"), "`start` launches the bridge with", ".")))
        self.assertEqual(said, names)
        # the state directory: the environment variable name and the default subdirectory name are read from `state_dir`
        consts = [n.value for n in ast.walk(fn_node("state_dir")) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        self.assertEqual(sorted(consts), [".scv", "SCV_HOME"])
        self.assertIn("`$SCV_HOME` if it is set, otherwise `.scv` in your home directory", section(t, "What it writes"))

    def test_universal_sentences_the_code_can_back(self):
        """A few checks Fix 2 added to back universal statements: README's "only" / "always" / "never" sentences
        are pinned by the code or by behavior (the code changes and the document does not keep up ⇒ red)."""
        # "listens on 127.0.0.1 only": the local API is started on `(LOCAL_HOST, port)`
        srv = [n for n in ast.walk(fn_node("Bridge.start_local")) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_Server"]
        self.assertEqual(len(srv), 1)
        self.assertEqual(ast.unparse(srv[0].args[0].elts[0]), "LOCAL_HOST")
        # "A running bridge that is not paired sends nothing to any outside address": not paired ⇒ the remote leg never starts (the cell where it does start is in test_80)
        for cfg in ({"remote_url": "", "remote_token": ""}, {"remote_url": "https://x.invalid", "remote_token": ""},
                    {"remote_url": "", "remote_token": "t"}):
            with self.subTest(cfg=cfg):
                b = types.SimpleNamespace(cfg=cfg, remote=None)
                self.assertIs(scv.Bridge.start_remote(b), False)
                self.assertIsNone(b.remote)
        # "The only finish_reason the bridge sends is stop": every non-None finish_reason literal in the local API
        reasons = set()
        for n in ast.walk(fn_node("_make_handler")):
            if isinstance(n, ast.Dict):
                reasons |= {v.value for k, v in zip(n.keys, n.values) if getattr(k, "value", None) == "finish_reason"
                            and isinstance(v, ast.Constant) and v.value is not None}
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "chunk" and len(n.args) > 1:
                reasons.add(ast.literal_eval(n.args[1]))
        self.assertEqual(reasons, {"stop"})
        self.assertIn("The only `finish_reason` the bridge sends is `stop`", self.text)
        # "a message with any other role is rejected" and "the last message must be a `user` message": pinned by behavior
        ok = [{"role": "user", "content": "a"}]
        with self.assertRaises(scv.BridgeError):
            scv.normalize_request({"model": "x", "messages": [{"role": "tool", "content": "a"}]})
        mgr = scv.SessionManager({"max_concurrent": 1}, lambda: ["claude/haiku"])
        with self.assertRaises(scv.BridgeError) as cm:
            mgr.run(session_id=None, model_id="claude/haiku", effort=None, system="", on_started=lambda ms: None,
                    messages=ok + [{"role": "assistant", "content": "b"}], on_delta=lambda s: None, cancel=None)
        self.assertEqual(cm.exception.klass, "bad_request")
        # "Neither setup.md nor scv.py puts an `scv` command on PATH": not one of setup.md's commands touches PATH;
        #   the places scv.py writes to disk are pinned in test_00's `DISK_WRITES` (one more goes red), and it never
        #   writes to `os.environ` anywhere
        self.assertEqual([c for c in code_lines(read(SETUP)) if re.search(r"\bPATH\b", c)], [])
        stores = [n for n in ast.walk(TREE) if isinstance(n, (ast.Assign, ast.AugAssign)) and any(
            "os.environ" in ast.unparse(t) for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
        self.assertEqual(stores, [])

    def test_status_names_a_live_pid_and_only_then(self):
        """README / SKILL say "the sentence names the process recorded in bridge.pid (same pid and its birth id
        matching) only when it is confirmed still running; in every other case not a word about the pid"
        (re-review N2, re-review 2 m-a: this used to be written as "say it whenever the pid is alive", while the
        `gone` cell has a pid that is very much alive — its own suite already refuted it).
        Pins five cells by behavior: confirmed to be that process ⇒ says it; pid alive but the birth id does not
        match (gone), the birth id is unrecognizable (foreign), no bridge.pid at all (none), or its current birth id
        cannot even be asked (unsure: `proc_start_id` returns None, the branch inside `bridge_owner`) ⇒ stdout is
        exactly the "not running" sentence, not a word about the pid."""
        import contextlib
        me = os.getpid()
        port = int(scv.load_config().get("port") or 8765)
        mine = {"pid": me, "born": scv.proc_start_id(me), "port": 1}
        out = {}
        for label, rec in (("alive", mine), ("gone", {"pid": me, "born": "ft:1", "port": 1}),
                           ("foreign", {"pid": me, "born": "not-a-birth-id", "port": 1}), ("none", None), ("unsure", mine)):
            with contextlib.suppress(FileNotFoundError):
                os.remove(scv.spath("bridge.pid"))
            if rec is not None:
                with open(scv.spath("bridge.pid"), "w", encoding="utf-8") as f:
                    json.dump(rec, f)
            asking = mock.patch.object(scv, "proc_start_id", return_value=None) if label == "unsure" else contextlib.nullcontext()
            buf = io.StringIO()
            with asking, contextlib.redirect_stdout(buf):
                owner = scv.bridge_owner()[0]
                rc = scv.cmd_status(types.SimpleNamespace())
            out[label] = (owner, rc, buf.getvalue())
        with contextlib.suppress(FileNotFoundError):
            os.remove(scv.spath("bridge.pid"))
        self.assertEqual(out["alive"][1], 1)
        self.assertIn(str(me), out["alive"][2])
        self.assertIn("still alive", out["alive"][2])
        for label in ("gone", "foreign", "none", "unsure"):
            with self.subTest(state=label):
                if label in ("none", "unsure"):
                    self.assertEqual(out[label][0], label)          # precondition: this cell was really produced (never silently testing a different one)
                self.assertNotIn("pid", out[label][2])
                self.assertNotIn("still alive", out[label][2])
                self.assertEqual(out[label][1:], (1, "not running (no answer on port %d)" % port + NL))   # the correct value: exactly that one sentence
        # keeping the structural check: the conditional expression that says the pid, with the condition being exactly alive. ⚠️A comment here used to say the none/unsure cells "cannot be produced by the suite" — that was wrong
        #   (re-review 3 m-c): none = not writing bridge.pid, unsure = `proc_start_id` returning None; the two cells
        #   above are produced exactly that way (commit `1d47eab`'s title said so too; history is not rewritten, and
        #   this is recorded in task-14-report.md's Fix 4 section)
        said_pid = [n for n in ast.walk(fn_node("cmd_status")) if isinstance(n, ast.IfExp)
                    and "still alive" in ast.unparse(n.body)]
        self.assertEqual([ast.unparse(n.test) for n in said_pid], ["state == 'alive'"])
        said = ("if the process recorded in `bridge.pid` (same pid and start time) is confirmed to be still running, "
                "the sentence says so, and otherwise it says nothing about the pid")
        self.assertIn(said, self.text)
        self.assertIn(said, read(SKILL))

    def test_the_canary_window_comes_from_the_code(self):
        """The 8 in "any 8 consecutive hex characters" is read from the slice inside `_canary_seen`
        (review M7/A10)."""
        cut = [s.slice.upper for s in ast.walk(fn_node("_canary_seen")) if isinstance(s, ast.Subscript)
               and isinstance(s.slice, ast.Slice) and isinstance(s.slice.upper, ast.BinOp)]
        self.assertEqual(len(cut), 1)
        self.assertIsInstance(cut[0].right, ast.Constant)
        self.assertIn("any %d consecutive hex characters" % cut[0].right.value,
                      section(self.text, "Checking it yourself"))

    def test_known_limits_numbers(self):
        t = section(self.text, "Known limits")
        self.assertIn("checks every %g seconds" % scv.PEER_PROBE_S, t)
        self.assertIn("a keep-alive comment every %g seconds" % scv.KEEPALIVE_S, t)

    def test_min_python(self):
        floor = "Python %d.%d or newer" % scv.MIN_PY
        for name in DOCS:
            with self.subTest(doc=name):
                self.assertIn(floor, read(name))

    def test_commands_table_is_the_dispatch_table(self):
        """the subcommand table: the first column's names = the subcommands `main` really dispatches; the options
        each cell names = the options argparse really accepts; the positional-argument counts match."""
        rows = table(section(self.text, "Commands"), "| Subcommand | What it does |")
        opts, npos = accepted(TREE)
        seen = []
        for cell, what in rows:
            words = ticks(cell)[0].replace("[", " ").replace("]", " ").split()
            sub, rest = words[0], words[1:]
            seen.append(sub)
            flags = [w for w in rest if w.startswith("-")]
            with self.subTest(sub=sub):
                self.assertEqual(sorted(flags), sorted(opts.get(sub, {})))
                pos = [w for i, w in enumerate(rest) if not w.startswith("-")
                       and not (i and rest[i - 1].startswith("-") and opts.get(sub, {}).get(rest[i - 1]))]
                self.assertEqual(len(pos), npos.get(sub, 0))
        self.assertEqual(sorted(seen), sorted(dispatched(TREE)))
        start = next(what for cell, what in rows if ticks(cell)[0] == "start")
        self.assertIn("up to %d seconds" % scv.START_WAIT_S, start)

    def test_local_api_facts(self):
        t = section(self.text, "Local API")
        for fam, models in (("claude", scv.CLAUDE_MODELS), ("codex", scv.CODEX_MODELS)):
            for m in models:
                self.assertIn("`%s/%s`" % (fam, m), t)
        entry = dict_keys_in("_make_handler", lambda d: any(k.value == "owned_by" for k in d.keys))
        self.assertEqual(sorted(ticks(span(t, "Each entry has only", "."))), sorted(entry))
        self.assertEqual(ticks(span(t, "`usage` has only", ", with")), list(scv._usage_openai({})))
        meta = dict_keys_in("_make_handler.meta")
        self.assertEqual(ticks(span(t, "carry an `scv` object:", ".")), meta)
        body = next(c for c in ticks(t) if c.startswith('{"error": {'))
        self.assertEqual(re.findall('"([a-z_]+)"', body)[1:],
                         list(scv.error_body(scv.BridgeError("unknown", "x"))["error"]))
        d = request_defaults()
        self.assertIn("at most %g (0 or no value means the default: %g and %g)"
                      % (scv.MAX_TIMEOUT_S, d["first_token"], d["timeout"]), t)
        zero = scv.normalize_request({"model": "x", "messages": [{"role": "user", "content": "a"}],
                                      "first_token_timeout": 0, "timeout": 0})
        self.assertEqual((zero["first_token"], zero["timeout"]), (d["first_token"], d["timeout"]))   # "0 means the default" is really true
        # "the first eight are rejected only when the value is not empty": an empty value is accepted and listed in ignored (review I5)
        empty = scv.normalize_request({"model": "x", "messages": [{"role": "user", "content": "a"}],
                                       **{k: [] for k in scv.REJECTED_PARAMS}})
        self.assertEqual(empty["ignored"], sorted(scv.REJECTED_PARAMS))
        self.assertIn("The first %d are rejected when their value is not empty" % len(scv.REJECTED_PARAMS), t)
        # M13: the borrowed pieces are all really in the code (their source is the plan's "borrowed from CLIProxyAPI" section)
        for piece in ('": keep-alive"', 'data("[DONE]")', '"choices": []'):
            self.assertIn(piece, SRC)
        self.assertTrue({"message", "type", "code", "retryable"} <= set(scv.error_body(scv.BridgeError("unknown", "x"))["error"]))
        self.assertIn("idle for %d seconds" % scv.SESSION_IDLE_S, t)

    def test_error_class_table(self):
        rows = table(section(self.text, "Local API"), "| Class | HTTP status | Retryable |")
        got = sorted((ticks(c)[0], int(s), r) for c, s, r in rows)
        want = sorted((k, v, "yes" if k in scv.RETRYABLE else "no") for k, v in scv.HTTP_STATUS.items())
        self.assertEqual(got, want)

    def test_the_ten_gates_it_names_are_the_ten_in_the_budget_docstring(self):
        """There is only one list of "which ten gates auditability rests on" (`Budget::test_line_budget`'s
        docstring); README's list must be exactly that one."""
        named = set(budget.GATE_RE.findall(section(self.text, "Reading the source")))
        self.assertEqual(named, set(budget.GATE_RE.findall(budget.Budget.test_line_budget.__doc__ or "")))
        self.assertEqual(len(named), 10)

    def test_every_test_it_points_to_exists(self):
        """A pointer to a suite that does not exist is worse than no pointer at all (the same reasoning as test_00's
        own check; the criterion shares `gate_missing`)."""
        refs = re.findall(r"`(tests/[a-z0-9_]+[.]py)::([A-Za-z_][A-Za-z0-9_]*)(?:::([A-Za-z_][A-Za-z0-9_]*))?`",
                          self.text)
        self.assertGreaterEqual(len(refs), 12)
        for path, klass, test in refs:
            with self.subTest(ref=path + "::" + klass + ("::" + test if test else "")):
                if test:
                    self.assertEqual(budget.gate_missing(path, klass, test), "")
                else:
                    with io.open(os.path.join(helpers.ROOT, *path.split("/")), encoding="utf-8") as f:
                        tree = ast.parse(f.read())
                    self.assertIn(klass, {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)})

    def test_the_borrowed_pattern_is_the_one_the_code_says_it_borrowed(self):
        """"borrowed from CLIProxyAPI" may only be written when the code itself says the same thing (never claim a
        "comparison" that was never done: the brief's word "cross-checked" turned out to have no evidence behind
        it)."""
        if "CLIProxyAPI" in self.text:
            self.assertIn("CLIProxyAPI", SRC)
        self.assertNotIn("cross-checked", self.text)

    def test_nothing_is_left_to_fill_in(self):
        for name in DOCS:
            with self.subTest(doc=name):
                self.assertNotIn("<filled in", read(name))


class CompatTable(unittest.TestCase):
    """The compatibility table is a promise to callers: as of Task 14 there is only one copy, in README.md's
    "Local API" section (it used to be in scv.py's comments, with a copy carried into PROTOCOL.md that had already
    drifted — missing `modalities` and two timeout parameters).
    ⭐A promise needs a gate: the easiest way for it to drift is someone adding a parameter to `REJECTED_PARAMS`
      and forgetting to write it into the table. Pinned both ways, plus a third: every entry in `KNOWN_PARAMS`
      must have its own row in the table."""

    def rows(self):
        return table(section(read(README), "Local API"), "| Parameter | What the bridge does |")

    def _refused_cell(self):
        """The "flat 400" cell. ⭐The criterion lands on this cell specifically: never use "shows up somewhere in
        the whole table" as the criterion — `tools` gets mentioned in passing elsewhere, and that would slip
        through."""
        cells = [r[0] for r in self.rows() if "**Rejected with HTTP 400**" in r[1]]
        self.assertEqual(len(cells), 1, "the \"flat 400\" row is missing from the compatibility table (or there is more than one)")
        return cells[0]

    def test_every_refused_param_is_named_there(self):
        missing = [p for p in scv.REJECTED_PARAMS if "`" + p + "`" not in self._refused_cell()]
        self.assertEqual(missing, [], "these parameters, which get a 400, are not written into the compatibility table ⇒ the promise and the code have drifted apart")

    def test_it_does_not_promise_to_reject_params_that_do_not_exist(self):
        """the reverse half: the parameters this cell names must really be parameters this bridge recognizes (a
        false claim here is more expensive: the reader will write their code to match it)."""
        known = set(scv.KNOWN_PARAMS) | set(scv.REJECTED_PARAMS)
        named = set(re.findall(r"`([a-z][a-z_]*)`", self._refused_cell()))
        self.assertTrue(named)
        self.assertEqual(sorted(n for n in named if n not in known), [])

    def test_every_known_param_has_its_row(self):
        """Every one of `KNOWN_PARAMS` (the ones accepted and handled by their own semantics) must be named in the
        first column: add a new parameter, forget to say how it is handled ⇒ red."""
        first = set(t for r in self.rows() for t in re.findall(r"`([a-z][a-z_]*)`", r[0]))     # `n` is a single letter
        self.assertEqual(sorted(set(scv.KNOWN_PARAMS) - first), [])

    def test_the_ruler_is_not_blind(self):
        """positive control: the criterion must really be able to tell "in that cell" apart from "not in it"."""
        cell = self._refused_cell()
        self.assertIn("`tools`", cell)
        self.assertNotIn("`stream`", cell)          # a supported parameter must never show up in the "flat 400" cell
        self.assertNotIn("`no_such_param_ever`", cell)


# ━━ Rules shared by all three documents
LOGIN_TAILS = [tuple(v) for v in scv.LOGIN_ARGS.values()]


def login_steps(text):
    """Every stretch of code (fenced block / inline code) that runs a CLI login: its trailing words are exactly one
    of `LOGIN_ARGS`'s entries.
    ⭐Only looks for "ends with a login subcommand": `login status` (a zero-quota status probe) never counts."""
    out = []
    for c in code_lines(text):
        words = [w.strip("'" + chr(34)) for w in c.split()]
        if any(len(words) > len(t) and tuple(words[-len(t):]) == t for t in LOGIN_TAILS):
            out.append(c)
    return out


def bare_self_commands(text):
    """13b's rule, carried over onto the documents: a self-referring command that looks pasteable, like
    `scv <subcommand>` / `scv.py <subcommand>` (the criterion is test_90's `BARE_SELF`, never a second copy of it).
    There is exactly one exception: a line that also has the `<python>` placeholder — that line is "a rule for the
    agent plus a placeholder" (13b's "for Task 14" section), never a command meant to be pasted verbatim.
    ⚠️The cost: prose is also never allowed to write a bare lower-case "scv" word (`scv is…`, `scv local…` are
    just as red), and must write `scv.py` (backticked) or "the bridge" instead."""
    hits = []
    for ln in text.splitlines():
        if "<python>" in ln:
            continue
        hits += [m.group(0) for m in BARE_SELF.finditer(ln)]
    return hits


class AllThreeDocs(unittest.TestCase):
    def test_no_login_step_for_the_bridge(self):
        """The maintainer's hard constraint: "no user login — asking for one raises suspicion" ⇒ not one of the
        three documents may carry a "go log in" command; the CLI's own login command is only ever handed over by
        doctor, computed for this machine (the full executable path included), and the user runs it themselves.
        The dedicated-home setup (`CODEX_HOME=`, `codex-login`) must never appear again either."""
        for name in DOCS:
            text = read(name)
            with self.subTest(doc=name):
                self.assertEqual(login_steps(text), [])
                for word in ("codex-login", "CODEX_HOME=", "codex-home"):
                    self.assertNotIn(word, text)

    def test_the_login_ruler_is_not_blind(self):
        self.assertEqual(login_steps("run `codex login` now"), ["codex login"])
        self.assertEqual(login_steps("```bash" + NL + "claude auth login" + NL + "```"), ["claude auth login"])
        self.assertEqual(login_steps("`login status`, `auth status --json`"), [])

    def test_no_bare_self_command(self):
        for name in DOCS:
            with self.subTest(doc=name):
                self.assertEqual(bare_self_commands(read(name)), [])

    def test_the_bare_ruler_is_not_blind(self):
        self.assertEqual(bare_self_commands("run scv start"), ["scv start"])
        self.assertEqual(bare_self_commands('python "~/.scv/scv.py" setup'), ['scv.py" setup'])
        self.assertEqual(bare_self_commands('<python> "$HOME/.scv/scv.py" setup'), [])
        self.assertEqual(bare_self_commands("`scv.py` prints; scv, the bridge"), [])


# ━━ setup.md
def setup_steps(text=None):
    """Every `## ` section of setup.md (or the full text given, e.g. one the release script has already stamped)
    ⇒ (heading, [(language, body)…])."""
    return [(title, FENCE.findall(body)) for title, body in sections(read(SETUP) if text is None else text, 2)]


def step_with(word, text=None):
    hits = [blocks for _t, blocks in setup_steps(text) if blocks and all(word in b for _l, b in blocks)]
    assert len(hits) == 1, "the step in setup.md carrying \"%s\" appears %d time(s)" % (word, len(hits))
    return dict(hits[0])


def download_url():
    """The download step's address in setup.md: the same one `scv update` builds (`UPDATE_BASE/<commit>/scv.py`),
    with the commit either the placeholder `<COMMIT>` or one the release script has stamped in (its shape = the
    `COMMIT_RE` that `scv update` accepts). ⭐Used in two places: `test_the_download_is_the_file_update_would_fetch`
    checks the shape, and `SetupMdInRealShells.paste` swaps it for a loopback server — `paste` used to only
    recognize the placeholder, so a cell that had already been stamped with a real commit id would really go dial
    GitHub (found in Task 15).
    ⚠️Computed here, never a module-level constant: test_95 temporarily swaps `scv.UPDATE_BASE` inside its own
    suite."""
    return re.compile(re.escape(scv.UPDATE_BASE) + "/(<COMMIT>|" + scv.COMMIT_RE.pattern + ")/scv[.]py")


INSTALLED = '"$HOME/.scv/scv.py"'


class SetupMd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(SETUP)

    def test_it_asks_whether_to_audit_after_installing(self):
        """spec §7/B14: proactively asks the user whether they want it audited once installed (the brief used to
        assert the Chinese "want it audited"; the document is now English, and the question's meaning is
        unchanged).
        ⭐"once installed" is pinned too: the question comes after the step that runs setup."""
        ask = "Ask the user whether they want you to audit `scv.py`"
        self.assertEqual(self.text.count(ask), 1)
        self.assertGreater(self.text.index(ask), self.text.index('scv.py" setup'))
        self.assertIn("including anything that concerns you", self.text)

    def test_it_pins_a_hash(self):
        self.assertRegex(self.text, "sha256[^0-9a-f]{0,40}([0-9a-f]{64}|<SHA256>)")

    def test_the_download_is_the_file_update_would_fetch(self):
        """the download address is the same one `scv update` builds (`UPDATE_BASE/<commit>/scv.py`); `<COMMIT>` is
        filled in by Task 15's release script."""
        url = download_url()
        dl = step_with("--create-dirs")
        self.assertEqual(sorted(dl), ["bash", "powershell"])
        for lang, body in dl.items():
            with self.subTest(shell=lang):
                self.assertRegex(body, url)
                self.assertIn("-o " + INSTALLED, body)
        self.assertTrue(dl["powershell"].startswith("curl.exe "))   # in PS 5.1 a bare curl is an alias for Invoke-WebRequest
        self.assertTrue(dl["bash"].startswith("curl "))

    def test_every_step_gives_one_block_per_shell(self):
        for title, blocks in setup_steps():
            with self.subTest(step=title):
                self.assertIn([lang for lang, _b in blocks], ([], ["powershell", "bash"]))

    def test_every_command_runs_the_bridge_from_where_step_2_put_it(self):
        """every later step starts from the path the download step wrote the file to (never a `~/…` anywhere:
        PowerShell 5.1 does not expand `~` in a native program's arguments)."""
        for word in ("hashlib", '" setup', " pair "):
            for lang, body in step_with(word).items():
                with self.subTest(step=word, shell=lang):
                    self.assertIn(INSTALLED, body)
                    self.assertTrue(body.startswith("<python> "))
        self.assertNotIn("~/", self.text)


class _Serve(BaseHTTPRequestHandler):
    body = b""

    def do_GET(self):
        if self.path != "/scv.py":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        return


@unittest.skipUnless(os.name == "nt", "PowerShell 5.1 / Git Bash are win32-only (the POSIX side ⏳ has no machine to test on; Task 15's CI covers it separately)")
class SetupMdInRealShells(unittest.TestCase):
    """Every command block in setup.md, run for real on this machine's PowerShell 5.1 and Git Bash (interactive)
    (`helpers.run_in_shell`: parsed the way pasting it in would be).
    ⭐Every pitfall measured on this machine and steered around has its own tooth here: PS 5.1's `curl` is an
      alias, `~` is not expanded, `&&` is a parse error.
    ⚠️Three substitutions (not one other word changed): the download address is swapped for a loopback server the
      suite starts itself (it hands out this very repository's scv.py; never touches the outside network);
      `<python>` is swapped for the python on PATH; the step that runs setup has `setup` swapped for `version`, and
      then swapped again to run `status` once more (setup would go probe the real CLI — the suite must never touch
      the real CLI; `status` has zero side effects, and it dials the suite's own "not-the-bridge" port) — this
      second pass pins review I6: what reaches the reader through the shell is UTF-8 (only the ASCII half of it —
      `PipedOutputIsUtf8` is where the non-ASCII half is proved, since S2 made scv's own output English).
    ⚠️HOME/USERPROFILE point at a temporary directory (never touching the real `~/.scv`). PowerShell's `$HOME`
      follows USERPROFILE; Git Bash's follows HOME.
    ⚠️`PYTHONIOENCODING` / `PYTHONUTF8` are stripped from the subprocess's environment (the command that runs the
      full suite carries the former; the user's shell has neither).
    ⚠️Git Bash is found by walking up from the `git` on PATH to `bin/bash.exe` (`helpers.git_bash`, never
      `which bash`: that could be WSL's launcher); whichever shell is missing just has its cell skipped, with the
      reason given, and the end checks "the shells that ran = the shells that were not missing"."""

    @classmethod
    def setUpClass(cls):
        with open(scv.__file__, "rb") as f:
            _Serve.body = f.read()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Serve)
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.srv.server_close)
        cls.addClassCleanup(cls.srv.shutdown)
        cls.url = "http://127.0.0.1:%d/scv.py" % cls.srv.server_address[1]
        cls.python = "python" if shutil.which("python") else ("python3" if shutil.which("python3") else "")

    def paste(self, shell, body, home, sub="version", raw=False):
        line = download_url().sub(lambda m: self.url, body.rstrip(NL))
        line = line.replace("<python>", self.python).replace('" setup', '" ' + sub)
        self.assertNotIn("https://", line)                    # never touches the outside network: the download address has always been swapped for the loopback server
        env = {"USERPROFILE": home, "HOME": home, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        with mock.patch.dict(os.environ, env):
            for k in ("PYTHONIOENCODING", "PYTHONUTF8"):   # ⭐whoever runs the suite most likely has this set (the full-run command carries it); the user's shell has neither
                os.environ.pop(k, None)
            helpers.refuse_default_port()                 # A12: the `status` pass's subprocess reads the current SCV_HOME's config
            p = helpers.run_in_shell(shell, line, home)
        if raw:
            return p.stdout, p.stderr.decode("utf-8", "replace")
        return p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")

    def test_each_block_does_what_the_step_says(self):
        if not self.python:
            self.skipTest("no python/python3 on PATH")
        want_sha = hashlib.sha256(_Serve.body).hexdigest()
        ran = []
        for shell, lang in (("PowerShell", "powershell"), ("Git Bash", "bash")):
            with self.subTest(shell=shell):
                why = helpers.shell_missing(shell)
                if why:
                    self.skipTest(why)
                home = tempfile.mkdtemp(prefix="scv-docs-home-")
                self.addCleanup(shutil.rmtree, home, True)
                target = os.path.join(home, ".scv", "scv.py")
                out, err = self.paste(shell, step_with("--version")[lang], home)
                self.assertRegex(out, r"Python 3[.]([9]|[1-9][0-9])", "%s did not find Python: %s" % (shell, err[-300:]))
                self.assertFalse(os.path.exists(os.path.dirname(target)))      # the next step must really create the directory for it
                out, err = self.paste(shell, step_with("--create-dirs")[lang], home)
                self.assertTrue(os.path.isfile(target), "%s: the download step did not write the file: %s" % (shell, err[-300:]))
                with open(target, "rb") as f:
                    self.assertEqual(f.read(), _Serve.body)          # byte for byte
                out, err = self.paste(shell, step_with("hashlib")[lang], home)
                self.assertIn(want_sha, out.split(), "%s: the hash step: %s" % (shell, err[-300:]))
                out, err = self.paste(shell, step_with('" setup')[lang], home)
                self.assertEqual(out.strip(), scv.VERSION, "%s: the step that starts scv.py: %s" % (shell, err[-300:]))
                # review I6: step 4's line (`setup` swapped for the zero-side-effect, Chinese-carrying `status`;
                #   the port is the suite's own "not-the-bridge" one), once it reaches the reader through this
                #   shell, must be UTF-8 (never the locale encoding: on Chinese Windows it used to be GBK bytes)
                got, err = self.paste(shell, step_with('" setup')[lang], home, sub="status", raw=True)
                self.assertIn("not running", got.decode("utf-8", "replace"), "%s: %r %s" % (shell, got[:80], err[-300:]))
                ran.append(shell)
        self.assertEqual(ran, [s for s in ("PowerShell", "Git Bash") if not helpers.shell_missing(s)])


# ━━ The two behaviors behind README's "Where prompts and answers end up" (Task 14 review I1, I6; the maintainer's ruling: change the code)
class ModelNamesOnDisk(unittest.TestCase):
    """review I1: `jobs.log`'s `model` used to be copied verbatim from whatever value the peer gave (the remote leg
    writes `str(job.get("model"))` in a `finally`, even when validation failed), and `resolve_model`'s own error
    text also copied the model name `%r` into bridge.log ⇒ README's "only two bounded exceptions" was a false
    claim.
    ⭐Changed to: a model name written to disk may only be one /v1/models has reported (`shown_model`); the error
      body handed back to the caller still carries the original name (that one goes back to the caller itself).
    ⭐The criterion is written as "did the text land on disk": neither jobs.log's nor bridge.log's full text may
      ever contain those two pieces of original text; positive control: a name that was reported is recorded
      verbatim (the ruler is not blind: "write everything as a placeholder" would also pass); the caller's error
      body still has the original name in it (never "erase it even from the reply to the caller themselves").
    ⚠️Zero real CLI: the two calls with a mismatched model name fail before any process is even started; the
      positive-control call goes through a fake CLI (`scv.cli_head` swapped for a stub)."""

    LONG = "MY-PROMPT-TEXT-" + "x" * 485                 # 500 characters: fails `_name`'s length check
    SHORT = "claude/SECRET-in-model-name"               # ≤128 characters: passes the length check, fails `resolve_model`

    @classmethod
    def setUpClass(cls):
        real = scv.cli_head
        scv.cli_head = helpers.fake_head
        cls.addClassCleanup(setattr, scv, "cli_head", real)
        helpers.fresh_home("docs-model", cls.addClassCleanup)
        cls.b, cls.port, cls.token = helpers.start_bridge()
        cls.addClassCleanup(cls.b.stop)

    def disk(self, name):
        p = scv.spath(name)
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def test_a_model_name_nobody_listed_never_lands_on_disk(self):
        msgs = [{"role": "user", "content": "hi"}]
        status, _h, body = helpers.http("POST", self.port, "/v1/chat/completions", token=self.token,
                                        body={"model": self.SHORT, "messages": msgs})
        self.assertEqual(status, 400)
        self.assertIn(self.SHORT, json.loads(body)["error"]["message"])      # this goes back to the caller itself: the original name is still carried
        leg, sent = scv.RemoteLeg(self.b), []
        leg._report = lambda jid, seq, event, **kw: sent.append((jid, event))
        for jid, model in (("j-long", self.LONG), ("j-short", self.SHORT), ("j-ok", "claude/haiku")):
            leg._cancels[jid] = threading.Event()
            leg._inflight.acquire()
            leg._job(jid, {"model": model, "messages": msgs}, (time.time(), 0))
        self.assertIn(("j-short", "error"), sent)
        self.assertIn(("j-ok", "done"), sent)
        jobs, blog = self.disk("jobs.log"), self.disk("bridge.log")
        for secret in ("MY-PROMPT-TEXT", "SECRET-in-model-name"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, jobs)
                self.assertNotIn(secret, blog)
        rows = {r["job_id"]: r["model"] for r in (json.loads(x) for x in jobs.splitlines() if x.startswith("{"))}
        self.assertEqual(rows["j-ok"], "claude/haiku")                       # positive control: a reported name is recorded verbatim
        self.assertIn("claude/haiku", self.b.cat)
        for jid in ("j-long", "j-short"):
            self.assertNotIn(rows[jid], self.b.cat)
        self.assertIn("has never reported this model", blog)                 # that line still gets written (a failure must be loud), it just never copies the original name


class ControlCharsInBridgeLog(unittest.TestCase):
    """Fix 3 (added by the maintainer): the `log()` door escapes control characters (C0/DEL/C1). The cause
    (re-review 2, measured for real): a dispatcher stuffing ESC/BEL into an HTTP reason phrase went straight into
    bridge.log and the foreground's stderr, and `_log_tail()` would even echo it back to the terminal when `start`
    failed.
    ⭐The criterion follows the shape of the bug: ① the door itself — no line may ever carry a raw control
      character when it goes through `log()` to disk or to stderr (a newline is still folded by `_one_line`, as
      before); ② `log()` is bridge.log's only door to disk (the full set of callers of
      `_append_capped("bridge.log", …)` = {log}) ⇒ the door covers every line of bridge.log.
    ⚠️③The end-to-end cell (a real `RemoteLeg._post` hitting a loopback server that replies
      `HTTP/1.0 500 <ESC…BEL>`) is caught by two guardrails at once: the door's escaping, and that call site's own
      `repr(str(e))[:128]` (another part of Fix 3) — they mask each other ⇒ each is pinned separately: ① pins the
      door, and `quote_sites`'s set equality pins that site's repr; ③ only proves the two together block it."""

    RAW = chr(27) + "[31mRED" + chr(27) + "[0m" + chr(7) + chr(0x9b) + "X"
    BAD = (chr(27), chr(7), chr(0x9b))

    def setUp(self):
        helpers.fresh_home("docs-ctrl", self.addCleanup)

    def disk(self):
        p = scv.spath("bridge.log")
        return p.read_bytes().decode("utf-8") if p.exists() else ""

    def test_the_door_escapes_control_characters(self):
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            scv.log("probe " + self.RAW + NL + "next")
        disk = self.disk()
        for c in self.BAD:
            with self.subTest(char=hex(ord(c))):
                self.assertNotIn(c, disk)
                self.assertNotIn(c, err.getvalue())
        self.assertIn("probe " + chr(92) + "x1b[31mRED", disk)          # positive control: that piece really did reach disk, just escaped
        self.assertIn(chr(92) + "x07" + chr(92) + "x9bX", err.getvalue())
        self.assertIn(" ⏎ next", disk)                                   # a newline is still folded into one line, as before (that behavior is never touched)
        where = {ln: fn for fn, ln, _n in budget.call_sites(TREE, lambda n: n == "_append_capped")}
        targets = sorted((where[n.lineno], ast.unparse(n.args[0])) for n in ast.walk(TREE) if isinstance(n, ast.Call)
                         and getattr(n.func, "id", "") == "_append_capped" and n.args)
        self.assertEqual(targets, [("log", "'bridge.log'"), ("write", "'jobs.log'")])   # `write` = the place JobLog writes jobs.log
        self.assertIn("every line has its control characters escaped", read(README))

    def test_line_and_paragraph_separators_are_escaped_too(self):
        """D24 (recorded by both re-review 3 and 4): U+2028/U+2029 are never in the Cc category, so they used to
        slip past the door ⇒ they land on disk verbatim, `_log_tail()` (`splitlines()`) splits one line into two,
        and the last few lines echoed back when `start` fails end up misaligned. ⭐The criterion: neither disk nor
        stderr has the raw bytes of these two characters; the correct value is "backslash, u, four digits";
        `_log_tail(1)` gets back the whole line intact."""
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            scv.log("probe-2028 peer<" + chr(0x2028) + "mid" + chr(0x2029) + ">end")
        raw = scv.spath("bridge.log").read_bytes()
        for c in (chr(0x2028), chr(0x2029)):
            with self.subTest(char=hex(ord(c))):
                self.assertNotIn(c.encode("utf-8"), raw)
                self.assertNotIn(c, err.getvalue())
        self.assertIn(("peer<" + chr(92) + "u2028mid" + chr(92) + "u2029>end").encode("utf-8"), raw)
        last = scv._log_tail(1)
        self.assertTrue("probe-2028" in last and last.endswith(">end"), repr(last))

    def test_every_line_break_splitlines_knows_is_escaped(self):
        """the gate is shaped after the bug: `_log_tail()` splits lines with `str.splitlines()` ⇒ every line break
        it recognizes must be escaped by the door (newline / carriage return excepted: those two are folded by
        `_one_line`). The set is computed here, never hand-copied into a table (the day Python recognizes one more,
        this must go red too); the ruler is not blind: U+2028 is in it."""
        breaks = [c for c in map(chr, range(0x110000)) if len(("a" + c + "b").splitlines()) > 1]
        self.assertIn(chr(0x2028), breaks)
        self.assertEqual([hex(ord(c)) for c in breaks if c not in (NL, chr(13)) and not scv.CTRL_CHARS.fullmatch(c)], [])

    def test_a_reason_phrase_full_of_escape_codes_lands_escaped(self):
        import contextlib
        raw = self.RAW

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(500, raw)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                return

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        leg = scv.RemoteLeg(types.SimpleNamespace(cfg={"remote_url": "http://127.0.0.1:%d" % srv.server_address[1],
                                                       "remote_token": "t", "max_concurrent": 4}))
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}), contextlib.redirect_stderr(err):
            with self.assertRaises(Exception):
                leg._post("result", {"job_id": "j", "seq": 0, "event": "ack"}, tries=1)
        disk = self.disk()
        self.assertIn("RED", disk)                                       # positive control: the reason phrase really did reach that line
        for c in self.BAD:
            with self.subTest(char=hex(ord(c))):
                self.assertNotIn(c, disk)
                self.assertNotIn(c, err.getvalue())


class PipedOutputIsUtf8(unittest.TestCase):
    """review I6: when it is not a console (piped / redirected), stdout/stderr used to write with the locale
    encoding — on Chinese Windows an agent reading through a pipe got GBK bytes, and on an English locale Chinese
    text all turned into `?`. ⭐Changed so both are always UTF-8 (at the top of `main`, and only when `isatty()` is
    false).
    ⭐Two cells: ① never carrying `PYTHONIOENCODING` (the default on the user's machine; the command that runs the
      full suite carries it, and it is stripped here); ② forcing a non-UTF-8 pipe encoding (`cp1252`) — ① would be
      a no-op on a machine whose locale is already UTF-8, ② has teeth on any machine.
    ⭐stdout uses `status` (zero side effects, dialing the suite's own "not-the-bridge" port) for the ASCII half,
      plus `doctor` (also zero cost without `--live`) for a stdout line that really carries a non-ASCII character
      (`✅`); stderr uses `update` with no arguments (no latest.json ⇒ refused without touching anything), whose
      refusal really carries `❌` and `⇒`. Decoded as UTF-8, with undecodable bytes swapped for the replacement
      character. ⭐S2 (the English pass) took the teeth out of this half: before it, the fix's proof rode on a
      Chinese sentence going missing when a stream's UTF-8 switch was removed; today's scv.py is English
      throughout, so the proof now rides on these non-ASCII marks instead (measured, M-5: with only the ASCII
      "not running"/"not updated" checks, removing stderr's UTF-8 `reconfigure` left this whole class green).
    ⭐`doctor` is here too (C21: what an agent reads through a pipe is exactly status/doctor): this class has its
      own SCV_HOME, config.json's `*_bin` points at a fake CLI (never letting the subprocess go probe the real CLI
      on this machine), and the port is the suite's own "not-the-bridge" one.
    ⚠️CI has a separate step that runs only this class, with neither variable in its environment
      (.github/workflows/ci.yml; pinned by `CiWorkflow`); this class also strips them itself first, so the
      full-run step carrying `PYTHONIOENCODING` does not turn it into a no-op either. On a machine whose locale is
      already UTF-8 (a Linux/macOS runner), ① is a no-op and ② still has teeth."""

    @classmethod
    def setUpClass(cls):
        from tests.test_90_cli import fake_bins
        cls.home = helpers.fresh_home("docs-pipe", cls.addClassCleanup)
        cfg = scv.load_config()
        cfg.update(fake_bins(cls.home))
        scv.save_config(cfg)

    def run_scv(self, args, extra):
        env = dict(os.environ)
        for k in ("PYTHONIOENCODING", "PYTHONUTF8"):
            env.pop(k, None)
        env.update(extra)
        helpers.refuse_default_port(env["SCV_HOME"])        # A12: the subprocess cannot see the in-process dial gate
        return subprocess.run([sys.executable, scv.__file__] + args, capture_output=True, env=env, timeout=90,
                              **scv.new_session_kw())

    def test_status_and_a_refusal_come_out_as_utf8(self):
        """M-5: `not running`/`not updated` are ASCII, so a stream that silently fell back to the locale encoding
        (or a non-UTF-8 one forced by PYTHONIOENCODING) would decode them identically either way ⇒ this used to
        pass even with stderr's UTF-8 switch removed entirely (measured: the full suite, 908 OK). Give each stream
        its own non-ASCII fact to really carry: `doctor`'s stdout has `✅`, `update`'s refusal on stderr has `❌`
        and `⇒`."""
        for label, extra in (("the locale encoding", {}), ("forced cp1252", {"PYTHONIOENCODING": "cp1252"})):
            with self.subTest(pipe=label):
                p = self.run_scv(["status"], extra)
                self.assertEqual(p.returncode, 1)
                self.assertIn("not running", p.stdout.decode("utf-8", "replace"), repr(p.stdout[:60]))
                p = self.run_scv(["doctor"], extra)
                out = p.stdout.decode("utf-8", "replace")
                self.assertEqual(p.returncode, 0, p.stderr[-600:])
                self.assertIn("✅", out, repr(p.stdout[:200]))                 # stdout really carries a non-ASCII character
                p = self.run_scv(["update"], extra)
                self.assertEqual(p.returncode, 1)
                err = p.stderr.decode("utf-8", "replace")
                self.assertIn("not updated", err, repr(p.stderr[:60]))
                self.assertIn("❌", err, repr(p.stderr[:200]))                 # stderr really carries a non-ASCII character
                self.assertIn("⇒", err, repr(p.stderr[:200]))

    def test_doctor_comes_out_as_utf8(self):
        for label, extra in (("the locale encoding", {}), ("forced cp1252", {"PYTHONIOENCODING": "cp1252"})):
            with self.subTest(pipe=label):
                p = self.run_scv(["doctor"], extra)
                text = p.stdout.decode("utf-8", "replace")
                self.assertEqual(p.returncode, 0, p.stderr[-600:])
                self.assertEqual(text.count(chr(0xFFFD)), 0, repr(p.stdout[:80]))
                self.assertIn("local credentials found", text, repr(p.stdout[-200:]))
                json.JSONDecoder().raw_decode(text)                       # the facts that came before are still a whole, valid JSON document


# ━━ SKILL.md
B16 = ("start", "stop", "status", "update", "doctor")      # spec B16: the skill is a shell, and its actions are these subcommands of the bridge


class SkillMd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(SKILL)
        cls.rows = table(cls.text, "| The user wants | Subcommand | Reading the output |")

    def test_it_only_maps_words_to_subcommands(self):
        """B16: every action is a subcommand of the bridge (whatever is named must really be in the dispatch
        table, and its options must really be ones argparse accepts), and not one of B16's five is missing."""
        opts, _npos = accepted(TREE)
        named = []
        for row in self.rows:
            words = ticks(row[1])[0].split()
            named.append(words[0])
            with self.subTest(sub=words[0]):
                self.assertIn(words[0], dispatched(TREE))
                for w in words[1:]:
                    self.assertIn(w, opts.get(words[0], {}))
        self.assertEqual(sorted(set(B16) - set(named)), [])

    def test_no_code_in_the_shell(self):
        self.assertEqual(re.findall("import |def |class ", self.text), [])     # never any code in the shell (B16/B29)

    def test_it_runs_the_bridge_from_where_setup_md_puts_it(self):
        self.assertIn("<python> " + INSTALLED + " <subcommand>", self.text)

    def test_its_python_rule_is_setup_md_step_1(self):
        """there is only one "find Python" rule: SKILL's per-shell order = the commands in setup.md step 1's two
        blocks (re-review m2: SKILL used to split by operating system while setup.md split by shell, so the two
        gave different answers for Git Bash on Windows)."""
        blocks = step_with("--version")
        for shell, lang in (("in PowerShell try", "powershell"), ("(Git Bash on Windows included) try", "bash")):
            with self.subTest(shell=lang):
                want = [ln.rsplit(" --version", 1)[0] for ln in blocks[lang].splitlines() if ln.strip()]
                self.assertEqual(ticks(span(self.text, shell, ";" if lang == "powershell" else ".")), want)

    def test_status_is_read_by_its_exit_code_first(self):
        """13b review: `status` at rc=0 has a whole JSON document on stdout, and when it is not running rc=1 and
        stdout is one plain sentence ⇒ never `json.loads` first."""
        row = next(r for r in self.rows if ticks(r[1])[0] == "status")
        self.assertTrue(row[2].startswith("Check the exit code first."))

    def test_the_why_question_goes_to_doctor_not_healthz(self):
        """13b fix1: `/healthz`'s families now carry only "blocked or not" ⇒ only doctor can answer "why is it
        blocked"."""
        row = next(r for r in self.rows if ticks(r[1])[0] == "doctor")
        self.assertIn("`doctor` says why", row[2])
        health = scv.Bridge.health(types.SimpleNamespace(
            sessions=types.SimpleNamespace(counts=lambda: {}), started_at=0, cat=[], remote=None,
            found={"codex": {"version": "codex 1.2.3 C:/x/codex.exe", "blocked": "请跑：C:/x/codex.exe login"}}))
        self.assertEqual(health["families"], {"codex": {"version": "1.2.3", "blocked": True}})


if __name__ == "__main__":
    unittest.main()
