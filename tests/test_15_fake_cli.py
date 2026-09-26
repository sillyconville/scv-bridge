# -*- coding: utf-8 -*-
"""The stub itself has to be right first: if it spits out the wrong shape, every case built on top of it is going
green against a mirage."""
import ast
import collections
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tests import helpers

import scv  # helpers already put ROOT on sys.path when it was imported

NL = chr(10)
# The real claude --output-format stream-json's first line. ⭐Its **correct value** is pinned with a literal by the
# ClaudeOpeningFrames case; this constant only exists so other places can reuse it, ⛔ it is not "whoever consumes it
# gets to drift along with it".
INIT = {"type": "system", "subtype": "init", "session_id": "fake", "model": "fake"}
# Two notifications the real app-server (codex-cli 0.155.0-alpha.9.2, measured twice on this machine on 2026-09-22)
# inserts on its own, **that nobody ever asked for**, and the two land in **different positions**:
# `remoteControl/status/changed` is sandwiched **between** two replies, while `thread/started` lands **after** the
# reply to thread/start. ⭐They are ⛔ not decoration: if the stub answered one question with exactly one reply and
# nothing more, "identify a reply by its id" and "treat the first event as the reply" would look identical across
# this entire file.
STRAY = "remoteControl/status/changed"
STRAYS = (STRAY, "thread/started")


def _tail_name(node):
    """`subprocess.run(...)` / `run(...)` both return "run"."""
    return node.attr if isinstance(node, ast.Attribute) else (node.id if isinstance(node, ast.Name) else "")


FakeRun = collections.namedtuple("FakeRun", "rc out err pid")


def kill_stub_tree(stub_pid, orphans=()):
    """Kill the stub's entire group.

    ⛔Never call `scv.kill_pid_tree` on the grandchild: the grandchild's pgid = the stub's pid ⇒ it is **not** the
      leader of its own group, so that path would go down the fallback branch, print a ⚠️ saying "new_session_kw()
      was missed when this was started" (a **false alarm**: the stub really was put in its own group), and it would
      only kill the grandchild itself.
    POSIX: one killpg takes out the whole group — even though the stub has already exited, the group is still
      there and nobody else could have taken over the leader slot.
    Windows: there is no such group semantics (`new_session_kw()` there only carries `CREATE_NO_WINDOW`, ⛔ it does
      not group anything). While the stub is still alive, `taskkill /T` can follow the parent-child relationship
      down to the grandchild; once the stub has already exited, `/T` can no longer reach that orphan ⇒ it has to be
      killed by name instead."""
    if os.name == "nt":
        scv.kill_pid_tree(stub_pid)
        for pid in orphans:
            scv.kill_pid_tree(pid)
    else:
        with contextlib.suppress(OSError):
            os.killpg(stub_pid, signal.SIGKILL)


def setUpModule():
    # ⭐The home directory is created here, ⛔ never at import time: discover imports every module first, and
    # changing SCV_HOME at import time would mean "whichever module is imported last wins"
    helpers.fresh_home("fake-cli", unittest.addModuleCleanup)


def talk(family, lines, mode="ok", env_extra=None):
    env = dict(os.environ, FAKE_MODE=mode, **(env_extra or {}))
    p = subprocess.run([sys.executable, helpers.FAKE, family], input=(NL.join(lines) + NL).encode("utf-8"),
                       capture_output=True, env=env, timeout=30, **scv.new_session_kw())
    return [json.loads(x) for x in p.stdout.decode("utf-8").splitlines() if x.strip()], p.returncode


def user(text):
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False)


# ── The few helpers below are ways of driving it that this file added on its own: talk() is only good enough for
#    "feed stdin, read back JSON lines", while the stub also has a quick subcommand (output is not JSON), death
#    modes that need extra environment variables, and bookkeeping that needs a specific cwd.
def run_fake(rest, env_extra=None, stdin=b"", timeout=30, cwd=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, helpers.FAKE] + rest, input=stdin, capture_output=True,
                          env=env, timeout=timeout, cwd=cwd, **scv.new_session_kw())


def stamped(rest, env_extra, stdin, timeout=30):
    """Every line of the stub's stdout carries **the moment it arrived** (`time.perf_counter()`) ⇒
    `[(seconds, event)]`. Measuring "how long the silence between two frames lasted" is **only ever allowed to use
    this** (`silence`).
    🔴⛔Never subtract two subprocess wall clocks (this file used to write it that way in two places): 13c fix1b hit
      a real case where the "no backoff" arm's **whole wall clock ran 19.617s**, and the difference came out
      negative — where those 19 seconds went **was never found** (interpreter startup, teardown, and pipes closing
      slowly all get counted into the wall clock; 13d pulled 180 runs on their own and could not reproduce it). The
      original judge's margin was only **0.1 second** (a 0.8s sleep judged as ≥0.7, a 0.6s one judged as ≥0.5) ⇒ the
      zero-input control arm running even a little slower makes it red on its own, and the docstring's line
      "⛔ never falsely red just because the machine is slow" only held for the arm under test (13d item 9).
      Measuring the arrival gap between two frames on the reading side ⇒ startup, teardown, and closing pipes never
      enter the judge at all.
    ⚠️The false-red paths still left (⏳ none of them measured under real load): ① the reading thread getting
      starved right at the moment "the previous frame" arrives, while the next frame is not starved; ② whatever the
      stub itself does between the two frames — between `api_retry`'s two frames there is only that one sleep;
      `slow_first`'s init and its first frame have one more `FAKE_LOG` append in between (`note(turn=…)`, which a
      slow disk or antivirus intervening can push past the 0.35 on the control arm, 13d fix1 review M5) ⇒ here it
      is always `FAKE_LOG=""` (the stub's `note()` then does not write at all), and a caller who wants the log has
      to give it explicitly."""
    env = dict(os.environ, FAKE_LOG="")
    env.update(env_extra or {})
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen([sys.executable, helpers.FAKE] + rest, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=err, env=env, **scv.new_session_kw())
        watchdog = threading.Timer(timeout, p.kill)      # ⛔ if EOF never comes it would hang forever: pull the trigger at the deadline, wind up either way
        watchdog.start()
        try:
            p.stdin.write(stdin)
            p.stdin.close()
            got = [(time.perf_counter(), line) for line in iter(p.stdout.readline, b"")]
        finally:
            watchdog.cancel()
            p.stdout.close()
            p.wait(timeout)
    return [(t, json.loads(x)) for t, x in got if x.strip()]


def silence(stamps, first, then=lambda e: True):
    """How many seconds separate the frame `first` from the first frame after it matching `then` (both are
    `event -> bool`; `stamps` is `stamped`'s output)."""
    i = next(k for k, (_t, e) in enumerate(stamps) if first(e))
    j = next(k for k in range(i + 1, len(stamps)) if then(stamps[k][1]))
    return stamps[j][0] - stamps[i][0]


def spawn_fake(rest, env_extra=None, stdin=b"", timeout=30, cwd=None):
    """stdout/stderr land in a file ⛔ never a pipe — only the two `grandchild` cases need this one.

    🔴The grandchild inherits the stub's pipe handles: once the stub itself has exited, the pipe still never sees
      EOF. The final cleanup `communicate()` that `subprocess.run` does after a timeout **has no cap of its own**
      ⇒ the test harness would hang all the way until the grandchild wakes up on its own (measured on 2026-09-21:
      120s, not a guess).
    ⭐The production path (`scv.run_cli`) is immune precisely because it does "kill_tree the whole tree first, then
      cap the cleanup at 15s" ⇒ ⛔ never use a bare `subprocess.run` as a stand-in for it.
    ⭐Carries `**scv.new_session_kw()`: the stub has to be **the leader of its own group**, the same shape as
      production (the Popen inside `scv.py`). Without it, on POSIX the stub and the grandchild would both land in
      **the test process's own group** ⇒ a single killpg during cleanup could take out the entire CI runner's
      group; what is currently blocking that is only the group-leader gate inside `kill_pid_tree` — pinning one
      test file's correctness on **another module's guard rail**, and since this machine is Windows and goes
      through taskkill instead, **this dependency can never surface on a dev machine**.
    ⭐The timeout has to pull the trigger: this helper exists specifically to observe orphans, and if it timed out
      and leaked the stub and the grandchild together, that would leak exactly the thing it exists to observe."""
    env = dict(os.environ)
    env.update(env_extra or {})
    d = tempfile.mkdtemp(prefix=".scv-test-spawn-")
    unittest.addModuleCleanup(helpers.remove_tree, d)     # D25: ⛔ never delete it right here — the grandchild may still be holding out/err open, wait for the module to finish (by then it has been reaped)
    o, e = os.path.join(d, "out"), os.path.join(d, "err")
    with open(o, "wb") as fo, open(e, "wb") as fe:
        p = subprocess.Popen([sys.executable, helpers.FAKE] + rest, stdin=subprocess.PIPE,
                             stdout=fo, stderr=fe, env=env, cwd=cwd, **scv.new_session_kw())
        p.stdin.write(stdin)
        p.stdin.close()
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_stub_tree(p.pid)
            with contextlib.suppress(subprocess.TimeoutExpired):
                p.wait(timeout=15)
            raise
    with open(o, "rb") as f:
        out = f.read()
    with open(e, "rb") as f:                 # ⭐read stderr back too: when this goes red, the reason cannot be left sitting in an mkdtemp nobody will ever go look at
        err = f.read()
    return FakeRun(rc, out, err, p.pid)


def jlines(raw):
    return [json.loads(x) for x in raw.decode("utf-8").splitlines() if x.strip()]


def jrpc(obj):   # ⛔ never call it rpc: in FakeCodex, `rpc` is a local list name, and having the same name reads as if it were the same thing
    return json.dumps(obj, ensure_ascii=False)


def codex_lines(system="SYS", text="甲", effort=None):
    params = {"threadId": "t-fake", "input": [{"type": "text", "text": text}]}
    if effort is not None:
        params["effort"] = effort
    return [jrpc({"id": 1, "method": "initialize", "params": {}}),
            jrpc({"method": "initialized", "params": {}}),
            jrpc({"id": 2, "method": "thread/start", "params": {"baseInstructions": system}}),
            jrpc({"id": 3, "method": "turn/start", "params": params})]


class _OwnLog(unittest.TestCase):
    """Cases that need to read FAKE_LOG: each gets its own log file, ⛔ never share the module-level one (two
    appending to the same one would bleed into each other)."""

    def setUp(self):
        d = tempfile.mkdtemp(prefix=".scv-test-fakelog-")
        self.addCleanup(helpers.remove_tree, d)               # D25: delete when done (test_99 reconciles it)
        self.log = os.path.join(d, "fake.jsonl")
        old = os.environ.get("FAKE_LOG")
        os.environ["FAKE_LOG"] = self.log
        # ⛔Never write `environ.__setitem__(key, old)` directly: when old is None that is a TypeError, and if the
        # cleanup path blows up it would leave every case after this one pointed at my own temp file
        self.addCleanup(lambda: os.environ.__setitem__("FAKE_LOG", old) if old is not None
                        else os.environ.pop("FAKE_LOG", None))


class FakeClaude(unittest.TestCase):
    def test_two_turns_count_up_in_one_process(self):
        evs, rc = talk("claude", [user("甲"), user("乙")])
        results = [e for e in evs if e["type"] == "result"]
        self.assertEqual([r["result"] for r in results], ["echo[1]: 甲", "echo[2]: 乙"])
        # ⭐This line was originally the Brief's word-for-word `if e["type"] == "stream_event"` — it carried within
        #   it the very assumption I-2 exists to fix (that "stream_event = the body text"). Once the stub gained
        #   the real CLI's opening frames it went straight to a KeyError ⇒ changed to the engine's (`_ClaudeProc`)
        #   real two-level judgment: content_block_delta first, then text_delta.
        deltas = [e["event"]["delta"]["text"] for e in evs
                  if e["type"] == "stream_event" and e["event"]["type"] == "content_block_delta"
                  and e["event"]["delta"]["type"] == "text_delta"]
        self.assertEqual("".join(deltas), "echo[1]: 甲echo[2]: 乙")
        self.assertEqual(rc, 0)

    def test_auth_mode_matches_the_real_wording(self):
        evs, _ = talk("claude", [user("甲")], mode="auth")
        self.assertEqual(evs[-1]["is_error"], True)
        self.assertEqual(evs[-1]["result"], "Not logged in · Please run /login")


class FakeCodex(unittest.TestCase):
    def test_handshake_thread_turn(self):
        rpc = [json.dumps({"id": 1, "method": "initialize", "params": {}}),
               json.dumps({"method": "initialized", "params": {}}),
               json.dumps({"id": 2, "method": "thread/start", "params": {"baseInstructions": "SYS"}}),
               json.dumps({"id": 3, "method": "turn/start",
                           "params": {"threadId": "t-fake", "input": [{"type": "text", "text": "甲"}]}})]
        evs, _ = talk("codex", rpc)
        self.assertEqual(evs[0], {"id": 1, "result": {"userAgent": "fake"}})
        # 🔴**Position is part of the shape too**, and the two notifications' positions are **not the same** (both
        #   measurements against the real CLI agree): one is sandwiched between two replies, the other lands
        #   **after** the reply to thread/start.
        self.assertEqual(evs[1]["method"], STRAY)
        self.assertNotIn("id", evs[1])
        self.assertEqual(evs[2]["result"]["thread"]["id"], "t-fake")
        self.assertEqual(evs[3]["method"], "thread/started")
        self.assertNotIn("id", evs[3])
        methods = [e.get("method") for e in evs[4:]]
        self.assertEqual(methods, ["item/started", "item/agentMessage/delta", "item/agentMessage/delta",
                                   "item/completed", "thread/tokenUsage/updated", "turn/completed"])


class FakeCodexPlayersOwnHome(unittest.TestCase):
    """Task 13c: codex runs inside the player's own CODEX_HOME ⇒ the driver has to ask `config/read`/`skills/list`
    first to learn which MCP servers he configured and which skills he installed (then turn each one off inside
    thread/start). The stub has to be able to play both questions — the shape is taken from the real app-server
    (codex-cli 0.155.0-alpha.9.2, measured on 2026-09-24)."""

    HEAD = [jrpc({"id": 1, "method": "initialize", "params": {}}), jrpc({"method": "initialized", "params": {}})]

    def answer(self, rid, lines, **env):
        evs, _rc = talk("codex", self.HEAD + lines, env_extra=env)
        got = [e for e in evs if e.get("id") == rid]
        self.assertEqual(len(got), 1, evs)
        return got[0]

    def test_config_read_lists_his_servers_and_an_empty_table_when_he_has_none(self):
        ask = [jrpc({"id": 2, "method": "config/read", "params": {"cwd": "x"}})]
        conf = self.answer(2, ask, FAKE_MCP="a,b")["result"]["config"]
        self.assertEqual(sorted(conf["mcp_servers"]), ["a", "b"])
        self.assertIn("mcp-secret-a", json.dumps(conf))      # a "secret" rides along in the value: the driver side has to prove it never got carried off
        # ⭐When none are configured, the real codex answers with an **empty table**, ⛔ not the key missing entirely (measured) — the driver relies on exactly this to tell "nothing configured" from "the protocol changed"
        self.assertEqual(self.answer(2, ask)["result"]["config"]["mcp_servers"], {})

    def test_skills_list_groups_by_cwd(self):
        ask = [jrpc({"id": 2, "method": "skills/list", "params": {"cwds": ["w"]}})]
        data = self.answer(2, ask, FAKE_SKILLS="s1")["result"]["data"]
        self.assertEqual([(g["cwd"], [s["path"] for s in g["skills"]]) for g in data],
                         [("w", [os.path.join("fake-skills", "s1", "SKILL.md")])])
        self.assertEqual(self.answer(2, ask)["result"]["data"][0]["skills"], [])

    def test_the_adversarial_shapes_are_there_when_asked_for(self):
        """⚠️All three cases here are **adversarial** (off by default, ⛔ not a measured shape): the protocol has
        drifted, or an old version does not recognize this method."""
        conf_ask = [jrpc({"id": 2, "method": "config/read", "params": {}})]
        self.assertNotIn("mcp_servers", self.answer(2, conf_ask, FAKE_CONFIG_READ="nokey")["result"]["config"])
        self.assertIn("Method not found", self.answer(2, conf_ask, FAKE_CONFIG_READ="error")["error"]["message"])
        sk_ask = [jrpc({"id": 2, "method": "skills/list", "params": {}})]
        self.assertNotIn("skills", self.answer(2, sk_ask, FAKE_SKILLS_LIST="noskills")["result"]["data"][0])
        self.assertEqual(self.answer(2, sk_ask, FAKE_SKILLS_LIST="nogroups")["result"]["data"], [])
        # ⭐Moved ⛔ not dropped: the value still carries the secret/description in the reply ("the original wording of an error must never carry a reply" only has teeth because of this, 13c review I1)
        nokey = self.answer(2, conf_ask, FAKE_CONFIG_READ="nokey", FAKE_MCP="a")["result"]["config"]
        self.assertIn("mcp-secret-a", json.dumps(nokey))
        moved = self.answer(2, sk_ask, FAKE_SKILLS_LIST="noskills", FAKE_SKILLS="s")["result"]["data"][0]
        self.assertIn("desc of s", json.dumps(moved))

    def test_instruction_sources_follow_what_real_codex_does(self):
        """The `instructionSources` in thread/start's reply: the stub's rules follow a zero-quota measurement
        against the real codex 0.155 (13c Fix 1, `f1-sources.json`; zero quota comes from a temporary CODEX_HOME
        with no login, ⛔ never from the handshake — the handshake itself pre-warms one call,
        NOTES.md::codex-user-home) — an override with real non-blank content beats AGENTS.md; a blank one does not
        count; only when there is a `.git` above cwd does the run of AGENTS.md files from the repo root down to
        cwd get counted in."""
        import shutil
        root = tempfile.mkdtemp(prefix=".scv-test-src-")
        self.addCleanup(shutil.rmtree, root, True)
        home, repo = os.path.join(root, "home"), os.path.join(root, "repo")
        wd = os.path.join(repo, "sub", "wd")
        os.makedirs(home)
        os.makedirs(wd)

        def put(path, text):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)

        def sources():
            ask = [jrpc({"id": 2, "method": "thread/start", "params": {"baseInstructions": "S", "cwd": wd}})]
            return self.answer(2, ask, CODEX_HOME=home)["result"]["instructionSources"]

        A, O = os.path.join(home, "AGENTS.md"), os.path.join(home, "AGENTS.override.md")
        put(A, "agt")
        self.assertEqual(sources(), [A])
        put(O, "ovr")
        self.assertEqual(sources(), [O])
        for blank in ("", " " + NL + chr(9)):
            put(O, blank)
            self.assertEqual(sources(), [A], repr(blank))
        put(os.path.join(repo, "AGENTS.md"), "prj")
        put(os.path.join(repo, "sub", "AGENTS.md"), "sub")
        self.assertEqual(sources(), [A])                                   # no .git: not one of them counts
        os.makedirs(os.path.join(repo, ".git"))
        self.assertEqual(sources(), [A, os.path.join(repo, "AGENTS.md"), os.path.join(repo, "sub", "AGENTS.md")])

    def test_the_search_for_git_stops_at_the_benchs_own_temp_root(self):
        """13c re-review N5: the stub used to walk up from cwd looking for `.git` all the way to the disk root ⇒
        cases asserting **exact** sources for `--live` would depend on the dev machine: if some ancestor of
        `%TEMP%` (say, the home directory is itself a dotfiles repo) has a `.git` and a non-blank AGENTS.md, they
        would go red ("a fixture that happens to be satisfied").
        ⇒ The stub's upward search now **stops at a ceiling** (⛔ it does not look at the ceiling itself or above
          it): the default is the system temp directory — every fixture this file has lives under it (the
          directory-creation gate forces that); `FAKE_GIT_CEILING` can lower it (that is exactly how this case
          proves it).
        ⭐Two cases welded together: with the ceiling pinned at `outer` ⇒ `outer/.git` is invisible; with the
          ceiling above it ⇒ the very same fixture becomes visible (⛔ otherwise a stub that always returns empty
          would be green here too)."""
        import shutil
        root = tempfile.mkdtemp(prefix=".scv-test-ceiling-")
        self.addCleanup(shutil.rmtree, root, True)
        outer = os.path.join(root, "outer")
        wd = os.path.join(outer, "inner", "wd")
        os.makedirs(wd)
        os.makedirs(os.path.join(outer, ".git"))
        with open(os.path.join(outer, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("above the ceiling")
        home = os.path.join(root, "home")
        os.makedirs(home)

        def sources(ceiling):
            ask = [jrpc({"id": 2, "method": "thread/start", "params": {"baseInstructions": "S", "cwd": wd}})]
            return self.answer(2, ask, CODEX_HOME=home, FAKE_GIT_CEILING=ceiling)["result"]["instructionSources"]

        self.assertEqual(sources(outer), [])
        self.assertEqual(sources(root), [os.path.join(outer, "AGENTS.md")])
        self.assertEqual(sources(os.path.join(root, "elsewhere")), [])        # cwd is not under the ceiling

    def test_a_codex_home_that_is_not_there_stops_codex_but_not_claude(self):
        """The real codex, against a CODEX_HOME that does not exist, **cannot start any subcommand at all**
        (original words, measured 09-21), and ⛔ never creates it on its own."""
        gone = os.path.join(tempfile.gettempdir(), "scv-no-such-codex-home-5c1a")
        self.assertFalse(os.path.exists(gone))
        for rest in (["codex", "--version"], ["codex", "login", "status"], ["codex", "app-server"]):
            with self.subTest(rest=rest):
                p = run_fake(rest, {"CODEX_HOME": gone})
                self.assertEqual(p.returncode, 1)
                self.assertIn('CODEX_HOME points to "%s", but that path does not exist' % gone, p.stderr.decode("utf-8"))
        self.assertFalse(os.path.exists(gone))
        self.assertEqual(run_fake(["claude", "--version"], {"CODEX_HOME": gone}).returncode, 0)   # only affects the codex family


class ClaudeShape(unittest.TestCase):
    """`n` is "which turn number this process has answered" — every case after this about "does it resume the old
    process or start a new one" uses it as the ruler."""

    def test_a_second_process_starts_counting_from_one_again(self):
        first, _ = talk("claude", [user("甲"), user("乙")])
        second, _ = talk("claude", [user("丙")])
        self.assertEqual([e["result"] for e in first if e["type"] == "result"], ["echo[1]: 甲", "echo[2]: 乙"])
        self.assertEqual([e["result"] for e in second if e["type"] == "result"], ["echo[1]: 丙"])

    def test_answer_keeps_only_the_first_60_chars_of_the_question(self):
        evs, _ = talk("claude", [user("甲" * 70)])
        self.assertEqual([e for e in evs if e["type"] == "result"][-1]["result"], "echo[1]: " + "甲" * 60)

    def test_usage_rides_along_on_the_result(self):
        evs, _ = talk("claude", [user("甲"), user("乙")])
        results = [e for e in evs if e["type"] == "result"]
        self.assertEqual([r["usage"]["input_tokens"] for r in results], [10, 20])
        self.assertEqual([r["usage"]["output_tokens"] for r in results], [len("echo[1]: 甲"), len("echo[2]: 乙")])
        self.assertEqual([r["session_id"] for r in results], ["fake", "fake"])

    def test_blank_lines_are_not_a_turn(self):
        env = dict(os.environ, FAKE_MODE="ok")
        raw = (NL + "   " + NL + user("甲") + NL).encode("utf-8")
        p = subprocess.run([sys.executable, helpers.FAKE, "claude"], input=raw,
                           capture_output=True, env=env, timeout=30, **scv.new_session_kw())
        results = [e for e in jlines(p.stdout) if e["type"] == "result"]
        self.assertEqual([r["result"] for r in results], ["echo[1]: 甲"])


class ClaudeDeaths(unittest.TestCase):
    def test_quota_mode_carries_the_real_wording(self):
        evs, _ = talk("claude", [user("甲")], mode="quota")
        self.assertEqual(evs[-1]["is_error"], True)
        self.assertEqual(evs[-1]["result"], "You've hit your session limit · resets 3pm")
        self.assertEqual(evs[-1]["terminal_reason"], "api_error")

    def test_auth_and_quota_keep_answering_the_next_turn_too(self):
        # ⭐Both turns have to report the error: the driver side's "can it still answer the next turn after one turn failed" case relies on this
        evs, _ = talk("claude", [user("甲"), user("乙")], mode="auth")
        self.assertEqual([e["result"] for e in evs if e["type"] == "result"],
                         ["Not logged in · Please run /login"] * 2)

    def test_crash_mode_exits_3_and_shouts_on_stderr(self):
        p = run_fake(["claude"], {"FAKE_MODE": "crash"}, stdin=(user("甲") + NL).encode("utf-8"))
        self.assertEqual(p.returncode, 3)
        # the opening line was already spat out before it even read stdin ⇒ asserting "only this, ⛔ no body, no result either" pins it down harder than `== b""`
        self.assertEqual(jlines(p.stdout), [INIT])
        err = [x for x in p.stderr.decode("utf-8").splitlines() if x.strip()]
        self.assertIn("boom: fake crash", err[0])
        # ⭐**The crash's original words must be multiple lines, and the real cause has to be on the very last
        #   line** — that is exactly what node looks like when it crashes. ⛔If it only ever spat out one line, "it
        #   got folded to one line" and "it got truncated to the first line" would look **exactly the same**
        #   downstream, and truncation would cut off precisely the one sentence that tells anyone how to fix it.
        #   The `--help` path already plays this out; the driver goes down **this** path (a turn's stderr), and it
        #   used to only be one line ⇒ that whole end-to-end half was untested.
        self.assertGreaterEqual(len(err), 3)
        self.assertIn("Cannot find module", err[-1])

    def test_hang_mode_never_answers(self):
        with self.assertRaises(subprocess.TimeoutExpired) as cm:
            run_fake(["claude"], {"FAKE_MODE": "hang"}, stdin=(user("甲") + NL).encode("utf-8"), timeout=2)
        self.assertEqual(jlines(cm.exception.stdout or b""), [INIT])

    def test_slow_first_waits_before_the_first_byte_then_answers_normally(self):
        """⭐What is being measured is **the silence as seen from the reading side**: how long the gap is between
        the moment it comes up (init) and this turn's first frame (`stamped`/`silence`).
        ⛔Never the whole subprocess's wall clock — the wall clock includes interpreter startup: on a machine with
        a cold cache or antivirus slowing things down, a broken stub with **no sleep at all** can still run past
        0.6s (a false negative); taking "wall clock with the delay minus wall clock without it" stuffs the coming
        and going of two whole child-process wall clocks (startup, teardown, closing pipes, all included) into a
        margin of only 0.1 second (a false red, 13d item 9).
        The zero-input control arm (`FAKE_DELAY=0`: the sleep under test removed) is measured with the exact same
        ruler, and the two numbers are welded into the same line of output."""
        def gap(delay):
            stamps = stamped(["claude"], {"FAKE_MODE": "slow_first", "FAKE_DELAY": delay}, (user("甲") + NL).encode("utf-8"))
            return stamps, silence(stamps, lambda e: e.get("subtype") == "init")

        stamps, slow = gap("0.6")
        _s0, zero = gap("0")
        said = "silence after init %.3fs / zero-input control (no sleep) %.3fs" % (slow, zero)
        self.assertGreaterEqual(slow, 0.5, said)
        self.assertLess(zero, 0.35, said)
        self.assertEqual([e for _t, e in stamps if e["type"] == "result"][-1]["result"], "echo[1]: 甲")

    def test_heartbeat_only_keeps_thinking_and_never_says_a_word(self):
        with self.assertRaises(subprocess.TimeoutExpired) as cm:
            run_fake(["claude"], {"FAKE_MODE": "heartbeat_only"},
                     stdin=(user("甲") + NL).encode("utf-8"), timeout=2)
        evs = jlines(cm.exception.stdout or b"")
        self.assertEqual(evs[0], INIT)
        beats = evs[1:]
        self.assertGreaterEqual(len(beats), 5)       # one every 0.05s, over 2s that is at least 5 no matter what
        self.assertEqual({e["type"] for e in beats}, {"stream_event"})
        self.assertEqual({e["event"]["delta"]["type"] for e in beats}, {"thinking_delta"})
        self.assertEqual({e["event"]["delta"]["thinking"] for e in beats}, {""})


class ClaudeResultSource(unittest.TestCase):
    """⭐The claude family's `text` comes from **the `result` field**, ⛔ never the delta stream (the two are the
    CLI's two independent outputs).

    ⚠️**Adversarial** mode (off by default): makes the two **deliberately disagree**. ⛔This is not a measured
    shape — nobody has ever measured whether the real CLI's `result` keeps up with the delta stream. It exists
    because, on the default stub, both sides are built from the same `ans` ⇒ "where does `text` come from" is
    **entirely invisible** on the default stub, and the invisible spot is exactly where the next person is most
    likely to assume the two are symmetric."""

    def test_the_result_can_differ_from_what_the_deltas_spelled(self):
        evs, rc = talk("claude", [user("甲")], mode="result_not_deltas")
        self.assertEqual(rc, 0)
        deltas = [e["event"]["delta"]["text"] for e in evs
                  if e["type"] == "stream_event" and e["event"]["type"] == "content_block_delta"
                  and e["event"]["delta"]["type"] == "text_delta"]
        self.assertEqual("".join(deltas), "echo[1]: 甲")
        self.assertEqual(evs[-1]["result"], "RESULT-echo[1]: 甲")

    def test_by_default_they_agree(self):
        """Zero-input control: without touching this knob the two sides must still be **the exact same sentence**
        — ⛔ otherwise the default stub would already be lying on its own."""
        evs, _ = talk("claude", [user("甲")])
        self.assertEqual(evs[-1]["result"], "echo[1]: 甲")


class ClaudeOpeningFrames(unittest.TestCase):
    """⭐"The first stream_event = the first character" holds true on the stub always, and on the real CLI **it
    does not hold at all** ⇒ the stub has to be able to make it fail exactly the same way.

    The real `claude --output-format stream-json`'s first line is `system/init`; before a turn's body there are
    also `stream_event`s **with no body text at all**. The engine (`_ClaudeProc`'s `llm.py`) added two levels of
    judgment for exactly this: first `inner["type"] == "content_block_delta"`, then
    `d["type"] == "text_delta" and d.get("text")` (only counts as the first character when it is **non-empty**);
    its own comment records "measured against the real CLI on 2026-09-19: thinking is always an empty string" — so
    the empty-thinking frame below is ⛔ not a shape I made up.
    ⭐The contrast is the evidence: the codex side's stub deliberately built a trap ("the echo is not the first
      character", `fake_cli.py`'s item/started); the claude side used to have not one fake first-character in it ⇒
      a driver bug that treats the opening frame as the first character would go all-green three tables in a row."""

    def test_the_very_first_line_is_the_session_init(self):
        evs, _ = talk("claude", [user("甲")])
        self.assertEqual(evs[0], {"type": "system", "subtype": "init", "session_id": "fake", "model": "fake"})

    def test_the_first_stream_events_carry_no_text_at_all(self):
        evs, _ = talk("claude", [user("甲")])
        streams = [e for e in evs if e["type"] == "stream_event"]
        # ①A driver that only checks the outer type would be fooled by this (it does not even have a delta key)
        self.assertEqual(streams[0]["event"]["type"], "message_start")
        # ②A driver that stops as soon as it sees content_block_delta would be fooled by this (a real shape the engine has measured)
        self.assertEqual(streams[1]["event"], {"type": "content_block_delta",
                                               "delta": {"type": "thinking_delta", "thinking": ""}})
        # ③The body comes **after** these — this is the real bar that judging the first character has to clear
        self.assertEqual([s["event"]["delta"]["text"] for s in streams[2:]], ["echo[1", "]: 甲"])

    def test_every_turn_gets_its_own_opening_frames(self):
        evs, _ = talk("claude", [user("甲"), user("乙")])
        kinds = [e["event"]["type"] if e["type"] == "stream_event" else e["type"] for e in evs]
        one = ["message_start", "content_block_delta", "content_block_delta", "content_block_delta", "result"]
        self.assertEqual(kinds, ["system"] + one + one)


class ClaudeApiRetry(unittest.TestCase):
    """The real shape of the CLI backing off and retrying on its own: it first emits one `system/api_retry` (with
    `retry_delay_ms`), then **not a single byte for the whole backoff window**, and answers normally once it is
    done backing off.

    ⭐The field names are taken from claude.exe's own event schema (the engine's `_ClaudeProc` comment points at
      it: attempt/max_retries/retry_delay_ms/error_status) — ⛔ this is not a made-up shape.
    ⭐This family of cases **must not** be killed as stuck ⇒ the driver has to extend the stall window by
      `retry_delay_ms`. If the stub sent heartbeats during the backoff, that extension would never need to fire,
      and the test would go **entirely green while measuring nothing**."""

    def test_it_announces_the_backoff_then_answers_normally(self):
        evs, rc = talk("claude", [user("甲")], mode="api_retry")
        kinds = [e.get("subtype") if e["type"] == "system" else e["type"] for e in evs]
        self.assertEqual(kinds[:2], ["init", "api_retry"])
        self.assertEqual(evs[1]["retry_delay_ms"], 800)          # the default value: gives the driver a number for "how long to extend by"
        self.assertEqual((evs[1]["attempt"], evs[1]["max_retries"], evs[1]["error_status"]), (1, 10, 529))
        self.assertEqual(evs[-1]["result"], "echo[1]: 甲")       # still manages to answer once it is done backing off
        self.assertEqual(rc, 0)

    def test_a_tail_frame_can_land_right_after_the_announcement(self):
        """🔴The shape "announce a backoff → **emit one frame** → go silent for a long time" is one the stub used
        to be unable to produce ⇒ the consumer-side bug where "the extension gets wiped out by whatever frame
        immediately follows it" **did not exist on the test bench**.
        ⚠️This frame is **adversarial** (`FAKE_RETRY_TAIL` is off by default), ⛔ not a measured shape — it plays
        out "the CLI happened to say something else during the backoff", and the correct behavior on the consumer
        side should never depend on the assumption that "after a backoff it is absolutely silent"."""
        evs, rc = talk("claude", [user("甲")], mode="api_retry",
                       env_extra={"FAKE_RETRY_TAIL": "1", "FAKE_RETRY_SILENCE": "0.2"})
        at = [i for i, e in enumerate(evs) if e.get("subtype") == "api_retry"][0]
        tail = evs[at + 1]
        self.assertEqual(tail["event"]["delta"], {"type": "thinking_delta", "thinking": ""})
        self.assertEqual(evs[-1]["result"], "echo[1]: 甲")
        self.assertEqual(rc, 0)

    def test_an_empty_text_delta_mode_exists_and_keeps_beating(self):
        """⭐`empty_text_first`: emits one frame that is a **`text_delta` with an empty `text`** first, then only
        heartbeats after that, as usual.
        What it backs up is the consumer side's `and d.get("text")` half of the condition — without this frame in
        the stub, that half of the condition is dead code with zero coverage.
        ⚠️This frame is **adversarial**, ⛔ not a measured shape: nobody has measured whether the real CLI ever
        emits an empty `text`."""
        with self.assertRaises(subprocess.TimeoutExpired) as cm:
            run_fake(["claude"], {"FAKE_MODE": "empty_text_first"},
                     stdin=(user("甲") + NL).encode("utf-8"), timeout=2)
        evs = jlines(cm.exception.stdout or b"")
        self.assertEqual(evs[0], INIT)
        self.assertEqual(evs[1]["event"]["delta"], {"type": "text_delta", "text": ""})
        beats = evs[2:]
        self.assertGreaterEqual(len(beats), 5)
        self.assertEqual({e["event"]["delta"]["type"] for e in beats}, {"thinking_delta"})
        # ⛔No real body text is allowed to follow after this: otherwise the first-character gate would get closed by the real body text, and this mode would no longer test that half of the condition at all
        self.assertEqual([e for e in beats if e["event"]["delta"].get("text")], [])

    def test_the_backoff_is_silent_and_really_takes_that_long(self):
        """Both halves are needed: ① **not one event** during the backoff; ② it **really did sleep** that long —
        ⛔ never trust only the first half, since a broken stub with the sleep written as 0 looks structurally
        identical to a good one.
        ⭐Half ② measures **the silence as seen from the reading side**: the arrival gap between the backoff event
          and the next frame (`stamped`/`silence`). It used to be "wall clock of the backing-off run minus wall
          clock of the non-backing-off run ≥ 0.7" (with a 0.8 sleep): both runs' wall clocks each carry startup,
          teardown, and closing pipes, leaving a margin of only 0.1 second ⇒ the non-backing-off arm running even
          a little slower makes it red on its own (13c fix1b hit this for real: `retry=0.876s base=19.617s` — the
          arm under test was fine (0.876s comfortably covers the 0.8s sleep), the one that went red was the control
          arm — its whole wall clock was 19.6s, and where that went **was never found**; 13d item 9 pulled 180 runs
          on their own, 0 red, could not reproduce it. ⛔It is not that the stub failed to sleep — this half is
          held up by that 0.876s).
        The zero-input control arm (`FAKE_RETRY_SILENCE=0`: the sleep under test removed) is measured with the
        exact same ruler, and the two numbers are welded into the same line of output."""
        def gap(s):
            stamps = stamped(["claude"], {"FAKE_MODE": "api_retry", "FAKE_RETRY_SILENCE": s}, (user("甲") + NL).encode("utf-8"))
            return stamps, silence(stamps, lambda e: e.get("subtype") == "api_retry")

        stamps, slow = gap("0.8")
        _s0, zero = gap("0")
        evs = [e for _t, e in stamps]
        hits = [e for e in evs if e["type"] == "system" and e.get("subtype") == "api_retry"]
        self.assertEqual(len(hits), 1)
        at = evs.index(hits[0])
        # the very next frame right after the backoff event is the body's opening frame ⇒ not one heartbeat in between
        self.assertEqual(evs[at + 1]["event"]["type"], "message_start")
        said = "silence after the backoff %.3fs / zero-input control (no sleep) %.3fs" % (slow, zero)
        self.assertGreaterEqual(slow, 0.7, said)
        self.assertLess(zero, 0.35, said)


class ClaudeInputShape(unittest.TestCase):
    def test_a_content_block_list_is_refused_out_loud(self):
        """The real CLI's stream-json input accepts both shapes (a bare string, and `[{"type":"text","text":"…"}]`),
        the stub only supports the string.

        ⛔This must never be silent: without handling it, `text[:60]` becomes a list slice, `"%s" %` stringifies
        it ⇒ it answers `echo[1]: [{'type': 'text', …}]` and still returns rc=0 — **the stub would be deciding on
        Task 5's behalf which wire shape production uses, without ever saying so out loud**, and if that day ever
        comes, the symptom would be a string of assertion failures nobody can make sense of."""
        line = json.dumps({"type": "user", "message": {"role": "user",
                                                       "content": [{"type": "text", "text": "甲"}]}},
                          ensure_ascii=False)
        p = run_fake(["claude"], stdin=(line + NL).encode("utf-8"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("content must be a string", p.stderr.decode("utf-8"))   # fake_cli.py's own text (not one of this batch's files) is still Chinese
        self.assertEqual([e for e in jlines(p.stdout) if e["type"] == "result"], [])


class CodexShape(unittest.TestCase):
    def test_the_user_echo_comes_back_before_any_answer(self):
        # the real app-server echoes back the user message we just sent within ~0.006s ⇒ the first character must never be recognized from this. The stub has to echo it back the same way.
        evs, _ = talk("codex", codex_lines(text="甲"))
        started = [e for e in evs if "id" not in e and e.get("method") not in STRAYS][0]
        self.assertEqual(started["method"], "item/started")
        self.assertEqual(started["params"]["item"], {"type": "userMessage", "text": "甲"})
        answer = [e for e in evs if e.get("method") == "item/completed"][0]
        self.assertEqual(answer["params"]["item"], {"type": "agentMessage", "text": "echo[1]: 甲"})

    def test_deltas_spell_out_the_same_answer(self):
        evs, _ = talk("codex", codex_lines(text="甲"))
        deltas = [e["params"]["delta"] for e in evs if e.get("method") == "item/agentMessage/delta"]
        self.assertEqual("".join(deltas), "echo[1]: 甲")

    def test_token_usage_rides_along(self):
        evs, _ = talk("codex", codex_lines(text="甲"))
        last = [e for e in evs if e.get("method") == "thread/tokenUsage/updated"][0]["params"]["tokenUsage"]["last"]
        self.assertEqual(last, {"inputTokens": 10, "cachedInputTokens": 0,
                                "outputTokens": len("echo[1]: 甲"), "reasoningOutputTokens": 0})

    def test_the_context_total_rides_along_too_and_is_not_the_same_number(self):
        """⚠️`total` is **context in use**, ⛔ never this turn's consumption (measured against the real CLI by the
        engine on 2026-09-19, `⛔ never take a difference from it`).
        ⭐The stub emits both, and that is exactly what makes the comment **checkable**: an implementation that
          reads this field wrong gets back a number that **looks correct** (⛔ not `None`), and the `None` kind of
          bug is one any assertion at all would catch — the one that is actually dangerous is the former."""
        usage = [e for e in talk("codex", codex_lines(text="甲"))[0]
                 if e.get("method") == "thread/tokenUsage/updated"][0]["params"]["tokenUsage"]
        self.assertEqual(usage["total"]["inputTokens"], 150)
        self.assertNotEqual(usage["total"]["inputTokens"], usage["last"]["inputTokens"])

    def test_two_turns_on_one_thread_count_up(self):
        lines = codex_lines(text="甲") + [jrpc({"id": 4, "method": "turn/start",
                                               "params": {"threadId": "t-fake",
                                                          "input": [{"type": "text", "text": "乙"}]}})]
        evs, _ = talk("codex", lines)
        answers = [e["params"]["item"]["text"] for e in evs if e.get("method") == "item/completed"]
        self.assertEqual(answers, ["echo[1]: 甲", "echo[2]: 乙"])


class CodexDeaths(unittest.TestCase):
    def test_auth_mode_matches_the_real_401_wording(self):
        evs, _ = talk("codex", codex_lines(), mode="auth")
        turn = [e for e in evs if e.get("method") == "turn/completed"][0]["params"]["turn"]
        self.assertEqual(turn["status"], "failed")
        self.assertEqual(turn["error"]["message"],
                         "unexpected status 401 Unauthorized: Missing bearer or basic authentication in header, "
                         "url: https://api.openai.com/v1/responses")

    def test_the_turn_error_can_also_be_a_bare_string(self):
        """⚠️This case is **adversarial**, ⛔ not a measured shape: `turn.error` is a **bare string** instead of an
        object (the protocol drifted / a different implementation). The stub does not play this shape by default,
        so the consumer side's **bare-flight** path (`err.get("message")` raising an AttributeError against a str
        — not a BridgeError, not one line logged) could not be produced on the test bench at all.
        Zero-input control is the case right above this one: without touching this knob it must still be
        `{"message": ...}`."""
        evs, _ = talk("codex", codex_lines(), mode="auth", env_extra={"FAKE_ERR_STR": "1"})
        err = evs[-1]["params"]["turn"]["error"]
        self.assertIsInstance(err, str)
        self.assertIn("401 Unauthorized", err)

    def test_quota_mode_matches_the_real_wording(self):
        evs, _ = talk("codex", codex_lines(), mode="quota")
        turn = [e for e in evs if e.get("method") == "turn/completed"][0]["params"]["turn"]
        self.assertEqual(turn["error"]["message"], "You've hit your session limit · resets 3pm")

    def test_crash_mode_exits_3_after_the_user_echo(self):
        p = run_fake(["codex", "app-server"], {"FAKE_MODE": "crash"},
                     stdin=(NL.join(codex_lines()) + NL).encode("utf-8"))
        self.assertEqual(p.returncode, 3)
        self.assertEqual([e.get("method") for e in jlines(p.stdout)][-1], "item/started")
        self.assertIn("boom: fake crash", p.stderr.decode("utf-8"))

    def test_heartbeat_only_stalls_after_the_user_echo(self):
        with self.assertRaises(subprocess.TimeoutExpired) as cm:
            run_fake(["codex", "app-server"], {"FAKE_MODE": "heartbeat_only"},
                     stdin=(NL.join(codex_lines()) + NL).encode("utf-8"), timeout=2)
        methods = [e.get("method") for e in jlines(cm.exception.stdout or b"")]
        self.assertEqual(methods[-1], "item/started")
        self.assertNotIn("turn/completed", methods)

    def _stuck(self, env_extra, lines=None):
        """The few modes where the stub **just hangs around**: it only comes back once it times out, and hands
        back whatever events it managed to emit up to that point."""
        with self.assertRaises(subprocess.TimeoutExpired) as cm:
            run_fake(["codex", "app-server"], env_extra,
                     stdin=(NL.join(lines or codex_lines()) + NL).encode("utf-8"), timeout=2)
        return jlines(cm.exception.stdout or b"")

    def test_a_handshake_error_comes_back_as_a_jsonrpc_error_and_it_keeps_running(self):
        """🔴A real JSON-RPC server that sends back an `error` **stays alive afterward** ⇒ the stub has to keep
        hanging around, ⛔ it must never EOF and exit on its own.

        ⭐This is not decoration: the driver's "the handshake blew up, kill it" case, against an obedient stub,
          looks **exactly the same** as "it left on its own" (Task 6 actually hit this exact shape: deleting that
          kill call left the behavioral assertions all-green).
        ⚠️The error object's wording is **synthetic**, ⛔ not a measured shape (nobody has measured what the real
        app-server's error looks like) — it only has to satisfy "neither set of pattern strings can make sense of
        it", the one condition needed to reach the consumer side's default case."""
        evs = self._stuck({"FAKE_MODE": "handshake_error"})
        self.assertEqual(len(evs), 1)                       # only this one reply came back, thread/start's was never answered
        self.assertEqual(evs[0]["id"], 1)
        self.assertIn("Invalid request", evs[0]["error"]["message"])
        self.assertNotIn("result", evs[0])

    def test_the_handshake_error_wording_is_swappable(self):
        """One knob, two sets of original words: by default neither set of pattern strings can make sense of it,
        switch it to a real 401 and it should be recognized.
        ⛔Without this case, "the consumer side really did read the text" and "it always falls through to default"
        cannot be told apart."""
        evs = self._stuck({"FAKE_MODE": "handshake_error", "FAKE_RPC_ERROR": "auth"})
        self.assertIn("401 Unauthorized", evs[0]["error"]["message"])

    def test_thread_start_can_come_back_without_an_id(self):
        """The case where the protocol has drifted: `thread` is there, `id` is missing ⇒ the consumer side must
        ⛔ never treat it as success. The stub still hangs around here too.
        ⭐The reply still carries a value that looks like the player's own private path (13c Fix 1): the consumer
          side must ⛔ never carry it into the original words when it reports the error
          (tests/test_30_drivers.py::CodexHandshake)."""
        evs = self._stuck({"FAKE_MODE": "no_thread_id"})
        self.assertEqual(evs[-1], {"id": 2, "result": {"thread": {},
                                                       "instructionSources": ["C:/Users/someone/secret-path/AGENTS.md"]}})

    def test_an_empty_first_delta_is_followed_by_silence(self):
        """⭐`empty_delta_first`: emits one frame with an **empty delta** first, then **zero events** afterward
        (codex's thinking period is exactly silence).
        What it backs up is the consumer side's `if piece:` half of the condition — without this frame in the
        stub, that half of the condition is dead code with zero coverage.
        ⚠️This frame is **adversarial**, ⛔ not a measured shape."""
        evs = self._stuck({"FAKE_MODE": "empty_delta_first"})
        self.assertEqual(evs[-2:], [{"method": "item/started",
                                     "params": {"item": {"type": "userMessage", "text": "甲"}}},
                                    {"method": "item/agentMessage/delta", "params": {"delta": ""}}])

    def test_it_can_go_deaf_right_after_the_handshake(self):
        """⭐`deaf_after_handshake`: answers the handshake, then **stops reading stdin** ⇒ closing stdin or not, it
        never leaves.

        🔴**⛔Never say it "backs up" the driver gate — that claim has already been overturned, and this is the
          third copy of it here (the earlier two have already been retracted)**.
          The facts: the driver's case for "the handshake blew up on something that is **not** a BridgeError, and
          it still has to kill the process" stayed red **even after switching back to `ok` mode** (review reran it
          twice) — because that case stored the exception in `box["e"]`, its traceback pinned `__init__`'s stack
          frame, that stack frame pinned `self.pipe` ⇒ that stdin **can never be closed until GC gets around to
          it**, and even the `ok`-mode stub ends up hanging around the same way. ⇒ **This mode backs up zero test
          cases today.**
        ⭐The one and only reason to keep it, and it has to be stated honestly: **to not pin that gate's
          correctness on GC timing** (the day someone changes `box["e"]` to stop holding the exception, the `ok`
          path would instantly start gambling on GC timing)."""
        evs = self._stuck({"FAKE_MODE": "deaf_after_handshake"})
        # the two handshake replies come back as usual (⛔ it does not play dead right from the start) — only after that does it stop reading stdin ⇒ thread/start's never gets answered
        self.assertEqual([e.get("id") for e in evs], [1, None])
        self.assertEqual(evs[1]["method"], STRAY)

    def test_a_turn_can_be_rejected_outright_with_a_jsonrpc_error(self):
        """`turn/start` gets refused outright (a stale threadId / an unrecognized model / a drifted parameter) ⇒
        the reply is a JSON-RPC error carrying an id, ⛔ never `turn/completed`. The stub did not play this shape
        before, so this path on the consumer side (which used to have zero test cases) could not be produced at
        all."""
        evs, _ = talk("codex", codex_lines(), mode="turn_rejected")
        self.assertEqual(evs[-1], {"id": 3, "error": {"code": -32602, "message": "unknown threadId"}})
        self.assertEqual([e.get("method") for e in evs if "id" not in e and e.get("method") not in STRAYS],
                         ["item/started"])        # ⛔a rejection means there is no body frame either, nor a turn/completed

    def test_the_failed_turn_error_can_carry_a_code(self):
        """⚠️This case is **adversarial** (off by default): `turn.error` carries a `code`.
        ⛔This is not a measured shape — but what it backs up is the contract "the same family of error must
        never keep its code during the handshake and lose it by the time it reaches a turn"."""
        evs, _ = talk("codex", codex_lines(), mode="quota", env_extra={"FAKE_ERR_CODE": "1"})
        self.assertEqual(evs[-1]["params"]["turn"]["error"],
                         {"message": "You've hit your session limit · resets 3pm",
                          "code": "usage_limit_reached"})

    def test_an_answer_that_is_nothing_but_whitespace(self):
        """⭐`blank_answer`: the whole turn emits nothing but whitespace. The consumer side's `.strip()` call was
        moved to after the final judgment, and this is exactly the case guarding that move."""
        evs, _ = talk("codex", codex_lines(), mode="blank_answer")
        said = [e["params"]["item"]["text"] for e in evs if e.get("method") == "item/completed"]
        self.assertEqual(said, ["   " + NL])
        self.assertEqual("".join(e["params"]["delta"] for e in evs
                                if e.get("method") == "item/agentMessage/delta"), "   " + NL)

    def test_two_agent_messages_in_one_turn(self):
        """⚠️**Adversarial**, ⛔ not a measured shape: two agentMessages in one turn (whether the real app-server
        ever does this has never been measured; with every feature turned off it is very likely only ever one).
        The stub does not play this shape by default, so the consumer side's "overwrite vs. accumulate" case
        could not be produced at all.
        ⭐Each one **streams its deltas first, then a `item/completed` carrying that one message's full text** —
          "completed's text = that one message's full text, ⛔ never an increment" follows the engine's real
          record against the CLI from 2026-09-19 (it takes completed's text as the whole answer)."""
        evs, rc = talk("codex", codex_lines(), mode="two_messages")
        self.assertEqual(rc, 0)
        said = [e["params"]["item"]["text"] for e in evs if e.get("method") == "item/completed"]
        # 🔴**Whitespace on both ends**: a real CLI's one agentMessage very likely ends with a newline nine times
        #   out of ten. A fixture without whitespace would let the consumer side's invariant (what streamed out =
        #   what came back) hold **by accident** ⇒ effectively never tested at all.
        self.assertEqual(said, ["  echo[1]: 甲-A" + NL, "  echo[1]: 甲-B" + NL])
        deltas = [e["params"]["delta"] for e in evs if e.get("method") == "item/agentMessage/delta"]
        # ⭐What streamed out = the two full texts joined together: the consumer side's invariant (what streamed out = what came back) is measured against exactly this
        self.assertEqual("".join(deltas), "".join(said))

    def test_the_last_agent_message_can_come_back_empty(self):
        """⚠️This case is **adversarial**: the final agentMessage's `text` is empty.
        ⛔The real body text before it must still be there — otherwise this mode would no longer be testing
        "does an empty text wipe out a body that already existed"."""
        evs, _ = talk("codex", codex_lines(), mode="empty_tail_message")
        said = [e["params"]["item"]["text"] for e in evs if e.get("method") == "item/completed"]
        self.assertEqual(said, ["  echo[1]: 甲" + NL, ""])
        # ⭐The final one **has no delta at all** (it is exactly "a message that only ever shows up in completed") ⇒ only the first message ever streamed out
        self.assertEqual("".join(e["params"]["delta"] for e in evs
                                if e.get("method") == "item/agentMessage/delta"), said[0])

    def test_a_turn_can_complete_without_any_agent_message(self):
        """`turn/completed` says success, with not one word of body text. ⛔No body frame is allowed in between
        either, or the consumer side's "empty body" case would get its test closed off by real body text."""
        evs, rc = talk("codex", codex_lines(), mode="no_answer")
        self.assertEqual(rc, 0)
        self.assertEqual([e.get("method") for e in evs if "id" not in e and e.get("method") not in STRAYS],
                         ["item/started", "turn/completed"])
        self.assertEqual(evs[-1]["params"]["turn"], {"status": "completed"})


class FakeLog(_OwnLog):
    def test_claude_records_argv_system_and_every_turn(self):
        d = tempfile.mkdtemp(prefix=".scv-test-sys-")
        self.addCleanup(helpers.remove_tree, d)
        sysfile = os.path.join(d, "system.txt")
        with open(sysfile, "w", encoding="utf-8") as f:
            f.write("你是甲")
        argv = ["--system-prompt-file", sysfile, "--output-format", "stream-json"]
        run_fake(["claude"] + argv, stdin=(user("甲") + NL + user("乙") + NL).encode("utf-8"), cwd=d)
        rows = helpers.read_fake_log(self.log)
        self.assertEqual(rows[0]["family"], "claude")
        self.assertEqual(rows[0]["argv"], argv)
        self.assertEqual(rows[0]["system"], "你是甲")
        self.assertEqual(os.path.normcase(os.path.realpath(rows[0]["cwd"])), os.path.normcase(os.path.realpath(d)))
        self.assertEqual([(r["turn"], r["text"]) for r in rows[1:]], [(1, "甲"), (2, "乙")])

    def test_claude_without_a_system_file_records_an_empty_system(self):
        run_fake(["claude", "-p"], stdin=(user("甲") + NL).encode("utf-8"))
        rows = helpers.read_fake_log(self.log)
        self.assertEqual(rows[0]["system"], "")
        self.assertEqual(rows[0]["argv"], ["-p"])

    def test_codex_records_codex_home_base_instructions_and_effort(self):
        d = tempfile.mkdtemp(prefix=".scv-test-codexhome-")
        self.addCleanup(helpers.remove_tree, d)
        run_fake(["codex", "app-server"], {"CODEX_HOME": d},
                 stdin=(NL.join(codex_lines(system="SYS", effort="low")) + NL).encode("utf-8"))
        # ⭐Pick by key, ⛔ never by index: this table has other rows in it too (the uninvited notification gets logged as well) — an index shifting one over would make the red a KeyError nobody can read, not "what got missed"
        rows = helpers.read_fake_log(self.log)
        boot = [r for r in rows if "argv" in r][0]
        start = [r for r in rows if "thread_params" in r][0]
        turn = [r for r in rows if "turn" in r][0]
        self.assertEqual(rows.index(boot), 0)          # the boot entry is still the very first one
        self.assertEqual(boot["family"], "codex")
        self.assertEqual(boot["argv"], ["app-server"])
        self.assertEqual(boot["codex_home"], d)
        self.assertEqual(start["system"], "SYS")
        self.assertEqual(start["thread_params"]["baseInstructions"], "SYS")
        self.assertEqual((turn["turn"], turn["text"], turn["effort"]), (1, "甲", "low"))

    def test_no_log_env_means_no_file_and_no_crash(self):
        env = dict(os.environ)
        env.pop("FAKE_LOG", None)
        p = subprocess.run([sys.executable, helpers.FAKE, "claude"], input=(user("甲") + NL).encode("utf-8"),
                           capture_output=True, env=env, timeout=30, **scv.new_session_kw())
        self.assertEqual(p.returncode, 0)
        self.assertFalse(os.path.exists(self.log))
        self.assertEqual([e for e in jlines(p.stdout) if e["type"] == "result"][-1]["result"], "echo[1]: 甲")


class Quick(unittest.TestCase):
    """A subcommand that answers in one shot and exits: the probe (Task 4) reads exactly these."""

    def test_version_names_the_family(self):
        for family in ("claude", "codex"):
            p = run_fake([family, "--version"])
            self.assertEqual(p.stdout.decode("utf-8").strip(), "0.0.0 (fake %s)" % family)
            self.assertEqual(p.returncode, 0)

    def test_help_advertises_safe_mode_unless_switched_off(self):
        self.assertIn("--safe-mode", run_fake(["claude", "--help"]).stdout.decode("utf-8"))
        off = run_fake(["claude", "--help"], {"FAKE_NO_SAFE_MODE": "1"}).stdout.decode("utf-8")
        self.assertEqual(off.strip(), "(nothing)")

    def test_help_can_be_printed_on_stderr_instead(self):
        """Which stream the real CLI prints help to is its own business (codex today does both).
        The stub has to be able to play this one too, or else this whole miss-it-entirely surface (reading only
        stdout) could not be produced on the test bench."""
        p = run_fake(["claude", "--help"], {"FAKE_HELP_ON_STDERR": "1"})
        self.assertEqual(p.stdout, b"")
        self.assertIn("--safe-mode", p.stderr.decode("utf-8"))
        self.assertEqual(p.returncode, 0)

    def test_help_can_exit_nonzero_while_still_starting(self):
        """🔴**The rc axis**: the CLI **does start up**, but rc != 0 (node crashed / a broken install / a `.cmd`
        wrapper reporting its own error). It ⛔ **does not raise an exception**, and `--safe-mode` is naturally
        absent from the output either ⇒ this is exactly the "prints the please-upgrade message just the same" miss
        surface. The stub cannot play this one, so that surface could not be produced on the test bench at all."""
        p = run_fake(["claude", "--help"], {"FAKE_HELP_RC": "1"})
        self.assertEqual(p.returncode, 1)
        both = (p.stdout + p.stderr).decode("utf-8")
        self.assertNotIn("--safe-mode", both)
        self.assertIn("throw err", both)            # the crash's original words have to be kept (B21)
        # ⭐**Three lines, and the real cause is on the last one**: if the consumer side "keeps only the first line", that is exactly the sentence it would cut off.
        self.assertEqual(len([x for x in both.splitlines() if x.strip()]), 3)
        self.assertIn("Cannot find module 'yoga-wasm-web'", both.splitlines()[-1])

    def test_login_status_can_break_without_ever_saying_not_logged_in(self):
        """The codex case: rc != 0 is **both** the way it says "not logged in" **and** the way it looks when it
        crashed ⇒ rc alone cannot tell the two apart. The stub has to be able to play "rc != 0 and not a single
        word about login status"."""
        p = run_fake(["codex", "login", "status"], {"FAKE_STATUS_BROKEN": "1"})
        self.assertEqual(p.returncode, 1)
        both = (p.stdout + p.stderr).decode("utf-8")
        self.assertNotIn("logged in", both.lower())
        self.assertIn("config.toml", both)          # its own original words

    def test_auth_status_is_json_and_flips_with_the_mode(self):
        ok = json.loads(run_fake(["claude", "auth", "status"]).stdout.decode("utf-8"))
        self.assertEqual(ok["loggedIn"], True)
        self.assertEqual((ok["authMethod"], ok["subscriptionType"]), ("claude.ai", "max"))
        self.assertEqual(json.loads(run_fake(["claude", "auth", "status"],
                                             {"FAKE_MODE": "auth"}).stdout.decode("utf-8"))["loggedIn"], False)

    def test_login_status_answers_on_both_stdout_and_the_exit_code(self):
        ok = run_fake(["codex", "login", "status"])
        self.assertEqual(ok.stdout.decode("utf-8").strip(), "Logged in using ChatGPT")
        self.assertEqual(ok.returncode, 0)
        bad = run_fake(["codex", "login", "status"], {"FAKE_MODE": "auth"})
        self.assertEqual(bad.stdout.decode("utf-8").strip(), "Not logged in")
        self.assertEqual(bad.returncode, 1)

    def test_features_list_prints_one_line_per_feature(self):
        p = run_fake(["codex", "features", "list"], {"FAKE_FEATURES": "alpha,beta"})
        self.assertEqual(p.stdout.decode("utf-8").splitlines(), ["alpha  stable  true", "beta  stable  true"])
        self.assertEqual(run_fake(["codex", "features", "list"], {"FAKE_FEATURES": ""}).stdout, b"")


class QuickIsFamilyAware(_OwnLog):
    """⭐**The two families' subcommands are not one shared set**, and the stub must never be one either (it used
    to be that `codex auth status` would spit out claude's own JSON ⇒ an implementation that mixed up the two
    families would stay all-green on this stub).

    The two families **fail differently** too — measured against the real CLIs on 2026-09-21:
      · `codex auth status` ⇒ rc=2 + `error: unrecognized subcommand 'status'`;
      · claude has **no such failure mode** — an argv it does not recognize is treated as **a prompt** and really
        sent as a call (`claude login status` comes back with something the model said, rc=0) ⇒ the stub sends it
        down claude()'s own prompt path too, ⛔ never invent an rc=2 failure mode for claude that does not exist in
        reality (that would let Task 5 write its handling against an error mode that is not real)."""

    def test_codex_refuses_claudes_auth_subcommand(self):
        p = run_fake(["codex", "auth", "status"])
        self.assertEqual(p.returncode, 2)
        self.assertIn("unrecognized subcommand", p.stderr.decode("utf-8"))
        self.assertEqual(p.stdout, b"")                      # ⛔not even one character of claude's JSON is allowed to leak out

    def test_claude_treats_codexs_login_subcommand_as_a_prompt(self):
        """Matching the real CLI: claude takes it as a prompt ⇒ the stub goes down the claude() path (empty stdin
        ⇒ it just emits the opening frame and exits)."""
        p = run_fake(["claude", "login", "status"])
        self.assertEqual(p.returncode, 0)
        self.assertEqual([json.loads(x)["type"] for x in p.stdout.decode("utf-8").splitlines()], ["system"])

    def test_a_handled_subcommand_is_recorded_with_its_family_and_codex_home(self):
        """The probe side has to be able to assert "which string was sent, with which CODEX_HOME, running in which
        directory" ⇒ the stub has to record it (cwd: Task 12 review M-7, a zero-quota probe must ⛔ never run in
        the caller's cwd)."""
        d = tempfile.mkdtemp(prefix=".scv-test-quickhome-")
        where = tempfile.mkdtemp(prefix=".scv-test-quickcwd-")
        for x in (d, where):
            self.addCleanup(helpers.remove_tree, x)
        run_fake(["codex", "login", "status"], {"CODEX_HOME": d}, cwd=where)
        rows = helpers.read_fake_log(self.log)
        self.assertEqual(rows, [{"family": "codex", "quick": ["login", "status"], "codex_home": d, "cwd": where}])

    def test_the_prompt_path_is_not_polluted_by_a_quick_record(self):
        """Negative control: a path never handled by quick() must ⛔ never gain an extra row — `rows[0]` is the
        anchor every FakeLog case relies on."""
        run_fake(["claude"], stdin=(user("甲") + NL).encode("utf-8"))
        rows = helpers.read_fake_log(self.log)
        self.assertEqual([r for r in rows if "quick" in r], [])
        self.assertEqual(rows[0]["family"], "claude")
        self.assertIn("argv", rows[0])


class Grandchild(_OwnLog):
    """Orphan sweeping (Task 2's `sweep_orphans`) needs a real two-level tree: the stub dies, the grandchild is
    still alive."""

    def test_the_grandchild_outlives_the_stub_and_its_pid_is_recorded(self):
        r = spawn_fake(["claude"], {"FAKE_MODE": "grandchild"}, stdin=(user("甲") + NL).encode("utf-8"))
        self.assertEqual(r.rc, 0, r.err.decode("utf-8", "replace"))   # the stub itself already exited clean
        self.assertEqual([e for e in jlines(r.out) if e["type"] == "result"][-1]["result"], "echo[1]: 甲")
        rows = helpers.read_fake_log(self.log)
        pids = [x["grandchild"] for x in rows if "grandchild" in x]
        self.assertEqual(len(pids), 1)
        # ⭐Kill the **leader** (the stub), ⛔ never the leaf: the grandchild is not the leader of its own group, and calling kill_pid_tree on it would take the fallback branch
        self.addCleanup(kill_stub_tree, r.pid, pids)
        # a non-empty birth id = this pid really is still running (`None` = could not tell, ⛔ does not count, re-review two M-8). ⚠️**Each side carries its own kind of ambiguity, and each only blocks one half of it**:
        #  ① "no birth id" could mean either "dead" or "could not tell" ⇒ ⛔ never assert "definitely cleaned up" from it — that is a gate that cannot catch a missed kill;
        #  ② non-empty also **does not mean alive**: on POSIX, `ps -o lstart= -p PID` still returns rc=0 with output for **a zombie nobody reaped**
        #    (`scv.proc_rss_kb`'s own docstring already warns about this shape). It does not bite here only because the stub already exited clean
        #    ⇒ the grandchild got adopted by init and reaped right away, ⛔ but whoever copies this case has to know this other half still exists.
        self.assertTrue(helpers.born_alive(scv.proc_start_id(pids[0])))

    def test_plain_mode_spawns_no_grandchild(self):
        # zero-input control: take the MODE under test away and measure again (⭐ the exact same spawn_fake path, only this one knob turned)
        # otherwise the case above could not tell "the stub really recorded it" from "it records absolutely anything"
        spawn_fake(["claude"], {"FAKE_MODE": "ok"}, stdin=(user("甲") + NL).encode("utf-8"))
        rows = helpers.read_fake_log(self.log)
        # ⭐First pin down "it really ran": otherwise "no grandchild row" and "the stub never even ran" look identical
        #   (RED 2, measured: this stays green even without fake_cli.py at all ⇒ a vacuous assertion)
        self.assertEqual([(r["turn"], r["text"]) for r in rows if "turn" in r], [(1, "甲")])
        self.assertEqual([r for r in rows if "grandchild" in r], [])


class Utf8Stdio(_OwnLog):
    """The stub's stdio ⛔ must never follow whatever shell started it: the real claude/codex's stream-json/ndjson
    output is UTF-8 no matter what.

    ⭐Uses `PYTHONIOENCODING=latin-1` as a **deterministic stand-in** for "an unfriendly locale": on this machine
      (Windows/cp936), the stub catches the same disease when that variable is simply left unset, but Linux/macOS's
      locale is already UTF-8 by default ⇒ writing it as "take the variable away" would leave two of the three CI
      legs running an empty measurement. Setting it explicitly means all three legs are really measuring something.
    ⭐What this pins down is the "**silently drifting**" half: every red below is a **value or text**, ⛔ none of
      them rely on "did it raise an exception or not". The "hard crash" half (under cp936, stdin's `\\udcb2` makes
      `note()` raise a UnicodeEncodeError) is a variant of the Windows-locale story, fixed by the same line — the
      report keeps the original traceback."""

    def hostile(self, text, with_log=False):
        env = dict(os.environ, PYTHONIOENCODING="latin-1")
        env.pop("PYTHONUTF8", None)
        if with_log:
            env["FAKE_LOG"] = self.log
        else:
            env.pop("FAKE_LOG", None)
        return subprocess.run([sys.executable, helpers.FAKE, "claude"], input=(user(text) + NL).encode("utf-8"),
                              capture_output=True, env=env, timeout=30, **scv.new_session_kw())

    def test_a_short_turn_keeps_its_real_length(self):
        p = self.hostile("甲")
        self.assertEqual(p.returncode, 0)
        res = [e for e in jlines(p.stdout) if e["type"] == "result"][-1]
        self.assertEqual(res["result"], "echo[1]: 甲")
        # ⭐This is the "silent" case: the line above is **also correct on a broken stub** (the bytes round-trip through latin-1 unchanged)
        self.assertEqual(res["usage"]["output_tokens"], len("echo[1]: 甲"))

    def test_a_long_turn_is_cut_at_60_real_characters(self):
        p = self.hostile("甲" * 70)
        self.assertEqual(p.returncode, 0)
        res = [e for e in jlines(p.stdout) if e["type"] == "result"][-1]
        # ⭐**Complements** the case above: with a long body, output_tokens happens to land right (69 == 69), what is wrong is the body text
        #   ⇒ the two must be checked side by side, since either one alone could be fooled
        self.assertEqual(res["usage"]["output_tokens"], len("echo[1]: " + "甲" * 60))
        self.assertEqual(res["result"], "echo[1]: " + "甲" * 60)

    def test_what_lands_in_the_log_is_what_we_sent(self):
        p = self.hostile("甲", with_log=True)
        self.assertEqual(p.returncode, 0)
        self.assertEqual([(r["turn"], r["text"]) for r in helpers.read_fake_log(self.log) if "turn" in r],
                         [(1, "甲")])


class StuckModesDoNotOutliveTheDay(unittest.TestCase):
    """⭐Those "just hangs around" modes must have a **ceiling**: when review ran the zero-input control it left
    behind 3 orphans that lasted **hours**.

    In production this is backed by `sweep_orphans` — since Task 12 it has two production call sites (starting the
    bridge in `cmd_run`, and `scv stop`), but it only ever scans **that one SCV_HOME**'s registry, and only when
    the bridge starts or stops; the harness uses a temporary SCV_HOME (nobody sweeps it again once the run is
    done), and a stub started directly never even gets into the table ⇒ whoever breaks the driver while running a
    control still has to clean it up by hand. Defaults down to minute-scale, and an environment variable can tune
    it further."""

    @staticmethod
    def _stuck_value(**env_extra):
        """⛔Never `import fake_cli` inside this process: importing it would reconfigure **this test process's
        own** stdio, and `STUCK` is read at the moment of import ⇒ two cases would contaminate each other. Ask a
        separate process instead."""
        env = {k: v for k, v in os.environ.items() if k != "FAKE_STUCK_S"}
        env.update(env_extra)
        out = subprocess.run([sys.executable, "-c",
                              "import sys;sys.path.insert(0, %r);import fake_cli;print(fake_cli.STUCK)"
                              % os.path.dirname(helpers.FAKE)],
                             env=env, capture_output=True, timeout=30, **scv.new_session_kw())
        return out.stdout.decode("utf-8").strip()

    def test_the_default_is_minutes_not_hours(self):
        self.assertEqual(self._stuck_value(), "120.0")

    def test_it_can_be_turned_up_but_not_down_past_the_floor(self):
        """The ruler is not blind: this knob **really does** get read (⛔ it is not just "the default happens to
        be 120").

        🔴But it **must have a floor**: turned down to 1 second, those "just hangs around" modes would walk away on
        their own ⇒ **an implementation with no kill switch at all would stay green under `gone(pid, within=8)`**
        (the gate silently stops working). "A gate that misses is worse than no gate" ⇒ the floor is 30 seconds,
        longer than any test case's own wait."""
        self.assertEqual(self._stuck_value(FAKE_STUCK_S="600"), "600.0")
        self.assertEqual(self._stuck_value(FAKE_STUCK_S="1"), "30.0")


class SpawnHygiene(unittest.TestCase):
    """⭐Structural gate: every place in this file that starts the stub with `Popen`/`run` must carry
    `**scv.new_session_kw()`.

    Built to match **the shape of the bug**, ⛔ not the specific incident on hand: I-1's real danger is not "this
    one Grandchild case was written wrong", it is **the next four Tasks copying the way it starts a process from
    here**.
    A behavioral control cannot be built on this machine — on Windows, `new_session_kw()` ⛔ never groups anything
    (since Task 12 it only ever carries `CREATE_NO_WINDOW`), and the two ways of writing the grouping half are
    equivalent here ⇒ only a structural assertion is deterministic (the same reason as `test_10_proc.py`'s
    `ModuleHygiene`).
    The miss surface: a newly added call that starts the stub and forgets to carry it ⇒ this gate names it."""

    @staticmethod
    def _starts_the_stub(call):
        return any(isinstance(n, ast.Attribute) and n.attr == "FAKE" for n in ast.walk(call))

    @staticmethod
    def _makes_a_group_leader(call):
        return any(kw.arg is None and isinstance(kw.value, ast.Call)
                   and _tail_name(kw.value.func) == "new_session_kw" for kw in call.keywords)

    def _offenders(self, tree):
        return ["%s@%d" % (_tail_name(n.func), n.lineno) for n in ast.walk(tree)
                if isinstance(n, ast.Call) and _tail_name(n.func) in ("Popen", "run")
                and self._starts_the_stub(n) and not self._makes_a_group_leader(n)]

    def test_every_place_this_file_starts_the_stub_makes_it_a_group_leader(self):
        with io.open(__file__, encoding="utf-8") as f:
            self.assertEqual(self._offenders(ast.parse(f.read())), [])

    def test_the_scanner_is_not_blind(self):
        """Positive control: each miss surface gets its own **synthetic sample**. ⛔Never rely only on a shape the
        real file happens to already have — the real file is entirely green right now, and "a scanner that is
        entirely green" looks exactly like "a scanner that has gone blind"."""
        bad_run = ast.parse("subprocess.run([sys.executable, helpers.FAKE, 'claude'], timeout=30)")
        self.assertEqual(len(self._offenders(bad_run)), 1)
        bad_popen = ast.parse("subprocess.Popen([sys.executable, helpers.FAKE] + rest, stdout=fo)")
        self.assertEqual(len(self._offenders(bad_popen)), 1)
        good = ast.parse("subprocess.run([sys.executable, helpers.FAKE, 'c'], **scv.new_session_kw())")
        self.assertEqual(self._offenders(good), [])
        # false-positive side: a child-process call that does not start the stub must ⛔ never be named (this file may start other things in the future)
        other = ast.parse("subprocess.run(['git', 'status'], capture_output=True)")
        self.assertEqual(self._offenders(other), [])


class Helpers(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ("SCV_HOME", "FAKE_LOG", "FAKE_MODE")}
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_fresh_home_makes_a_new_dir_and_points_the_three_env_vars_at_it(self):
        one = helpers.fresh_home("alpha", self.addCleanup)
        self.assertTrue(os.path.isdir(one))
        self.assertIn("alpha", os.path.basename(one))
        self.assertEqual(os.environ["SCV_HOME"], one)
        self.assertEqual(os.environ["FAKE_LOG"], os.path.join(one, "fake.jsonl"))
        self.assertEqual(os.environ["FAKE_MODE"], "ok")
        two = helpers.fresh_home("alpha", self.addCleanup)
        self.assertNotEqual(one, two)                # one home per test ⇒ ⛔ the two must never cross-contaminate

    def test_fresh_home_is_removed_and_the_env_restored_when_its_owner_is_done(self):
        """D25: delete when done (it used to never delete at all, and one run left behind 100+ in %TEMP%); on
        cleanup the environment variables go back to what they were before it was created (⛔ never left pointing
        at a directory that no longer exists).
        ⭐Check against the shape of the cleanup registration: **take the registered call in hand and run it
        yourself** — ⛔ never wait for this test case to finish (by then nobody is watching anymore)."""
        before = {k: os.environ.get(k) for k in helpers.FRESH_KEYS}
        got = []
        d = helpers.fresh_home("gamma", lambda fn, *a: got.append((fn, a)))
        # review M4: the cleanup call is caught in hand and only run below ⇒ if anything goes red before that, the directory is left in %TEMP% forever (the mutation-kill ledger missed one, twice, in two separate rounds)
        #   ⇒ once it is in hand, cover it with one line right away (`remove_tree` is harmless against a directory that is already gone)
        self.addCleanup(helpers.remove_tree, d)
        self.assertEqual(len(got), 1)                               # it registered, exactly once
        self.assertEqual(os.environ["SCV_HOME"], d)
        self.assertIn(d, helpers.SCV_TEST_DIRS)                     # it made it onto test_99's reconciliation ledger
        fn, args = got[0]
        fn(*args)
        self.assertFalse(os.path.exists(d))
        self.assertEqual({k: os.environ.get(k) for k in helpers.FRESH_KEYS if before[k] is not None},
                         {k: v for k, v in before.items() if v is not None})
        self.assertEqual(helpers.scv_test_dirs_left().count(d), 0)

    def test_fake_head_points_at_a_file_that_is_really_there(self):
        self.assertEqual(helpers.fake_head("claude"), [sys.executable, helpers.FAKE, "claude"])
        self.assertEqual(helpers.fake_head("codex", {"any": "cfg"}), [sys.executable, helpers.FAKE, "codex"])
        self.assertTrue(os.path.isfile(helpers.FAKE))
        self.assertEqual(os.path.basename(helpers.FAKE), "fake_cli.py")

    def test_read_fake_log_reads_the_env_path_by_default_and_tolerates_a_missing_file(self):
        d = helpers.fresh_home("beta", self.addCleanup)
        self.assertEqual(helpers.read_fake_log(), [])            # never run yet ⇒ an empty table, ⛔ never a crash
        with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps({"turn": 1, "text": "甲"}, ensure_ascii=False) + NL)
            f.write(NL)                                          # a blank line has to be skipped
            f.write(json.dumps({"turn": 2, "text": "乙"}, ensure_ascii=False) + NL)
        self.assertEqual(helpers.read_fake_log(), [{"turn": 1, "text": "甲"}, {"turn": 2, "text": "乙"}])
        self.assertEqual(helpers.read_fake_log(os.path.join(d, "nope.jsonl")), [])


if __name__ == "__main__":
    unittest.main()
