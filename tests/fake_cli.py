# -*- coding: utf-8 -*-
"""Fake CLI stub: two accents. Event shapes are copied straight from the real CLIs (matching exactly the fields a
production consumer parses out of them)."""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

for _s in (sys.stdin, sys.stdout, sys.stderr):
    # the stream-json/ndjson the real claude/codex emit is UTF-8, never following the locale of whatever shell
    # started it (Windows defaults to cp936/cp1252). Without pinning this: with FAKE_LOG set, note() throws
    # UnicodeEncodeError and crashes on the spot; without FAKE_LOG it does not crash, but the character count of
    # text changes => `text[:60]` and `len(ans)` are silently all wrong while all three tables stay green.
    # never guard this with a bare except: when the stream is swapped for an object without reconfigure, skipping
    # is the right move and there is no exception that needs swallowing.
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8")

MODE = os.environ.get("FAKE_MODE", "ok")
# `FAKE_STALE_LOGIN=1` (paired with `FAKE_MODE=auth`): local credentials exist, but a real call still gets 401
# (the credentials expired / were revoked) -- both families' status commands still say "logged in", and it only
# shows up once you actually send a turn. ⭐This is exactly why `scv doctor --live` exists (L192: the status
# command only proves "there are local credentials").
STALE_LOGIN = os.environ.get("FAKE_STALE_LOGIN") == "1"
LOG = os.environ.get("FAKE_LOG", "")
NL = chr(10)
AUTH_CLAUDE = "Not logged in · Please run /login"
AUTH_CODEX = ("unexpected status 401 Unauthorized: Missing bearer or basic authentication in header, "
              "url: https://api.openai.com/v1/responses")
QUOTA = "You've hit your session limit · resets 3pm"
# The JSON-RPC error the codex app-server replies with. ⚠️The wording is made up, never a measured shape (nobody
# has measured what the real app-server's error looks like) -- it only has to satisfy one condition, "neither
# pattern set can make sense of it", for the consumer side's default branch to be exercised.
RPC_ERR = "Invalid request: unknown method"
# How long the "sticks around and won't leave" modes sleep. ⭐Default is minute-scale, never hour-scale: running a
# negative control against a broken driver leaves an orphan behind (review measured 3 left behind once); the
# product-side fallback for this, `sweep_orphans` (in `cmd_run`/`cmd_stop` since Task 12), only scans the registry
# of that one SCV_HOME, and only scans when the bridge starts or stops -- once a test's temporary SCV_HOME finishes
# its run, nobody scans it again, so it still needs a human to clean up. Twist FAKE_STUCK_S yourself for a shorter
# (or longer) wait.
# 🔴There is a floor: dial it down to 1 second and these modes just walk away on their own => an implementation
# that never fires a shot still comes out green under `gone(pid, within=8)` -- the gate silently stops working,
# and a miss on the gate is worse than no gate at all. 30 seconds is longer than any test's own wait.
STUCK = max(30.0, float(os.environ.get("FAKE_STUCK_S") or 120))


def out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + NL)
    sys.stdout.flush()


def note(**kw):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(kw, ensure_ascii=False) + NL)


def side_effects():
    if MODE == "grandchild":
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        note(grandchild=p.pid)


def stale_stderr():
    """`FAKE_STALE_STDERR=1`: the moment it starts up, write a line to stderr that looks like a login failure, then
    work as usual afterward.
    ⭐The real CLIs have exactly this kind of stale noise (measured 2026-09-23: after a real codex turn, stderr has
      an ERROR line from `codex_core::tools::router`, a bad-model turn has `failed to connect to websocket … wss://…`,
      with no credentials it is `401 Unauthorized`) => this is what the "the EOF path only classifies by the stderr
      added since the last unambiguous success" watermark stands on.
    ⚠️The wording borrows AUTH_CODEX (the one line either pattern set recognizes), never a claim that real claude
      writes it: real claude's three streams are all 0 bytes."""
    if os.environ.get("FAKE_STALE_STDERR") == "1":
        sys.stderr.write(AUTH_CODEX + NL)
        sys.stderr.flush()


def stale_stderr_in_turn(n):
    """`FAKE_STALE_STDERR=turn1`: the same noise, but written only inside turn 1, before the reply (codex family;
    real codex's noise really is written inside a turn: that `tools::router` ERROR line). ⭐Only this one tells
    apart codex's turn watermark: noise written at startup gets cleared by the handshake watermark first =>
    removing only the turn one still comes out green (the two watermarks cover for each other, Task 12 review
    M-1)."""
    if os.environ.get("FAKE_STALE_STDERR") == "turn1" and n == 1:
        sys.stderr.write(AUTH_CODEX + NL)
        sys.stderr.flush()


def extras(text):
    """The two things about a reply that make it "look like a model", shared by both families (fixtures for the two
    checks `scv doctor --live` runs):
    (1) if the prompt has `what is A + B`, work out the sum (`FAKE_NO_MATH=1` turns this off => a CLI that answers
        but does not answer what we asked);
    (2) `leak` mode: a CLI whose tool access is not locked down -- it really reads the .txt named in the prompt and
        pastes it verbatim into the reply (the canary's positive control)."""
    more = ""
    m = re.search("what is ([0-9]+) [+] ([0-9]+)", text)
    if m and os.environ.get("FAKE_NO_MATH") != "1":
        # `FAKE_MATH_SEP=,`: answer with thousands separators (real codex has measurably replied "10,936" -- a
        # correct answer, and the judge has to accept it)
        # `FAKE_MATH_PREFIX=1`: answer wrong, and wrong in a way that "looks like it contains the right answer" --
        #   one extra digit in front (`110936`, or `16 912` with SEP set to a space). ⚠️Adversarial (off by
        #   default), never a measured shape: it stands on the "never judge a wrong answer as right" half (review
        #   M-2).
        more += " = " + os.environ.get("FAKE_MATH_PREFIX", "") + format(int(m.group(1)) + int(m.group(2)), ",").replace(
            ",", os.environ.get("FAKE_MATH_SEP", ""))
    if MODE == "leak":
        for path in re.findall("[A-Za-z]:[^ ]+[.]txt|/[^ ]+[.]txt", text):
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    got = f.read()
                # `FAKE_LEAK_SHAPE=reworded`: it read the file, but reworded it before pasting it in (strips the
                #   `CANARY-` prefix, switches to uppercase) -- a literal-match diff cannot see it (review M-3).
                #   ⚠️Adversarial, never a measured shape (nobody has measured what a real CLI's leak looks like --
                #   they are all locked down today).
                if os.environ.get("FAKE_LEAK_SHAPE") == "reworded":
                    got = "the code is " + got.rpartition("-")[2].upper()
                more += " " + got
    return more


def misbehave(n=0):
    """Returns True = this turn has already been handled (or the process is already gone). `FAKE_CRASH_AT=<n>`:
    only crash on turn n (the earlier turns answer as usual)."""
    if MODE == "hang":
        time.sleep(3600)
    if MODE == "crash" or os.environ.get("FAKE_CRASH_AT") == str(n):
        # ⭐Three lines, and the real reason is on the last one -- this is exactly what it looks like when node
        #   crashes (the `--help` path already plays this out). Never drop back to one line: with one line, "folded
        #   into one line" and "truncated to the first line" look identical to a downstream test, and what
        #   truncation would cut is exactly the `Cannot find module` line -- the only one that tells anyone how to
        #   fix it. The driver reads this one (the stderr inside a turn); it used to be only one line => that half
        #   of the end-to-end path was never tested by anyone.
        sys.stderr.write("boom: fake crash" + NL
                         + "      throw err;" + NL
                         + "Error: Cannot find module 'yoga-wasm-web'" + NL)
        sys.stderr.flush()
        sys.exit(3)
    if MODE == "slow_first":
        time.sleep(float(os.environ.get("FAKE_DELAY", "2")))
    return False


def claude():
    argv = sys.argv[2:]
    sys_file = argv[argv.index("--system-prompt-file") + 1] if "--system-prompt-file" in argv else ""
    if sys_file:
        with open(sys_file, encoding="utf-8") as f:
            system = f.read()
    else:
        system = ""
    note(family="claude", argv=argv, system=system, cwd=os.getcwd())
    side_effects()
    stale_stderr()
    # The first line of real `claude --output-format stream-json`. session_id/model have two source outlets on the
    # real CLI, never let the stub keep only the result one.
    out({"type": "system", "subtype": "init", "session_id": "fake", "model": "fake"})
    n = 0
    for line in sys.stdin:
        if not line.strip():
            continue
        text = json.loads(line)["message"]["content"]
        if not isinstance(text, str):
            # A mistake here must be loud: the real CLIs accept two wire shapes (a string / a content-block list),
            # the stub only supports the string one.
            # never let it go silent -- `text[:60]` would turn into a list slice, `"%s" %` would stringify it, and
            # it would answer back with junk that looks like an answer, which amounts to the stub deciding which
            # shape production uses for Task 5, and never saying so out loud.
            raise SystemExit("fake_cli: content must be a string, got %r" % (text,))
        n += 1
        note(turn=n, text=text)
        misbehave(n)
        if MODE == "api_retry":
            # 🔴The CLI itself is backing off and retrying. The field names come from claude.exe's own event schema
            #   (the engine's `_ClaudeProc` comment points it out: attempt/max_retries/retry_delay_ms/error_status)
            #   -- never a shape made up for this stub.
            # ⭐Not a single byte goes out during the backoff: the driver's "the stall window extends by
            #   retry_delay_ms" logic exists precisely for this stretch -- if the stub stuffed a heartbeat in here,
            #   that extension would never need to fire, and the test would stay all green without ever exercising
            #   it.
            out({"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 10,
                 "retry_delay_ms": int(os.environ.get("FAKE_RETRY_MS", "800")),
                 "error_status": 529, "error": "overloaded"})
            if os.environ.get("FAKE_RETRY_TAIL") == "1":
                # 🔴"announce backoff -> emit one frame -> long silence". ⚠️This one frame is adversarial (off by
                #   default), never a measured shape: the consumer side's extension must never depend on the
                #   assumption "dead silence right after a backoff" -- it resets the silence window on every line it
                #   receives, so any frame right on its heels would wipe out the extension just applied, and that
                #   bug cannot be produced without this frame.
                out({"type": "stream_event", "event": {"type": "content_block_delta",
                                                       "delta": {"type": "thinking_delta", "thinking": ""}}})
            time.sleep(float(os.environ.get("FAKE_RETRY_SILENCE", "0.6")))
        if MODE in ("auth", "quota"):
            out({"type": "result", "subtype": "success", "is_error": True, "session_id": "fake",
                 "result": AUTH_CLAUDE if MODE == "auth" else QUOTA, "terminal_reason": "api_error"})
            continue
        if MODE == "trickle":
            # Text keeps dripping the whole time, but this turn never wraps up => makes the "caller hangs up
            # midway" scene: both gates (stall / first token) get fed, and the only thing that can stop this turn
            # is cancellation itself.
            for i in range(60):
                out({"type": "stream_event", "event": {"type": "content_block_delta",
                                                      "delta": {"type": "text_delta", "text": "滴%d " % i}}})
                time.sleep(0.2)
            continue
        if MODE in ("heartbeat_only", "empty_text_first"):
            if MODE == "empty_text_first":
                # ⭐This is what keeps the consumer side's `and d.get("text")` half alive: without this frame, that
                #   half is dead code with zero coverage (every other frame in the stub is either not a
                #   content_block_delta or not a text_delta, and the first two checks already stop it => deleting
                #   just that half leaves the whole suite green).
                # ⚠️This one frame is adversarial, never a measured shape: nobody has measured whether a real CLI
                #   ever emits an empty `text` -- precisely because it cannot be measured, that half is a
                #   defensive check.
                out({"type": "stream_event", "event": {"type": "content_block_delta",
                                                       "delta": {"type": "text_delta", "text": ""}}})
            for _ in range(400):
                out({"type": "stream_event", "event": {"type": "content_block_delta",
                                                      "delta": {"type": "thinking_delta", "thinking": ""}}})
                time.sleep(0.05)
            continue
        ans = "echo[%d]: %s" % (n, text[:60]) + extras(text)
        # 🔴Two stream_events with no text come before the text itself: that is exactly how the real CLI behaves,
        #   so "the first stream_event = the first token" is wrong on the real CLI, and the stub must be able to
        #   get it wrong the same way (codex's item/started echo is the same thing). (1) message_start does not
        #   even have a delta key => a driver that only checks the outer type would trip on it; (2) thinking_delta
        #   is a content_block_delta but not a text_delta => a driver that checks one level too few would trip on
        #   it. ⭐thinking being an empty string forever is the engine's measured record of the real CLI on
        #   2026-09-19, never a shape made up for this stub.
        out({"type": "stream_event", "event": {"type": "message_start",
                                               "message": {"id": "msg_fake", "role": "assistant"}}})
        out({"type": "stream_event", "event": {"type": "content_block_delta",
                                               "delta": {"type": "thinking_delta", "thinking": ""}}})
        for piece in (ans[:6], ans[6:]):
            out({"type": "stream_event", "event": {"type": "content_block_delta",
                                                  "delta": {"type": "text_delta", "text": piece}}})
        # ⭐`result` is the line the CLI gives on its own -- it and the delta stream above are two separate
        #   outputs; the consumer side (`ClaudeDriver`) reads this one, accumulating not a single delta.
        #   ⚠️`result_not_deltas` is an adversarial mode (off by default, never a measured shape): by default the
        #   stub builds both sides from the same `ans` => on the default stub there is no way to tell "where does
        #   text come from", and a spot you cannot see is exactly the spot the next person is most likely to
        #   assume is symmetric with the codex family.
        result = ("RESULT-" + ans) if MODE == "result_not_deltas" else ans
        if os.environ.get("FAKE_RESULT_PAD") == "1":
            # ⚠️Adversarial (off by default), never a measured shape: nobody has measured whether real claude's
            #   `result` carries leading/trailing whitespace.
            # ⭐It stands on the assertion "by the time the claude family's text reaches the session layer, the
            #   driver has already trimmed it" -- without the stub supplying that whitespace on both ends, that
            #   assertion comes out green even on an implementation where nobody trims anything (a fixture that
            #   happens to satisfy the contract). ⚠️This is the same thing as codex's `two_messages` /
            #   `empty_tail_message` for its own family: over there the whitespace stands on "accumulated character
            #   by character, never stripped"; over here it stands on "this family never gets to the session layer
            #   for trimming".
            result = "  " + result + NL
        out({"type": "result", "subtype": "success", "is_error": False, "session_id": "fake",
             "result": result, "usage": {"input_tokens": 10 * n, "output_tokens": len(ans)}})
    # `FAKE_EXIT_DELAY=<seconds>`: after stdin closes, hang around this long before exiting (real claude is node,
    #   winding down takes time; the driver's graceful close waits at most CLOSE_GRACE_S).
    #   ⚠️Adversarial (0 by default), never a measured shape: it stands on the case "the old instance is still
    #   winding down while a new instance of the same session id has already started" (15b fix1).
    time.sleep(float(os.environ.get("FAKE_EXIT_DELAY", "0")))


def say(text):
    """One agentMessage: stream the delta piece by piece first, then one `item/completed` carrying the full text of
    that message.

    ⚠️"completed's text = the full text of that message, never an increment" follows the engine's measured record
      of the real CLI on 2026-09-19 (it uses completed's text as the whole answer); this round was never
      re-measured against the real CLI (that round's probe only went as far as the handshake, it never sent a turn
      -- ⚠️never zero-cost: the handshake itself makes codex warm up and connect to the reasoning endpoint, 13c
      Fix 1b measured, NOTES.md::codex-user-home)."""
    for piece in (text[:6], text[6:]):
        out({"method": "item/agentMessage/delta", "params": {"delta": piece}})
    out({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": text}}})


def codex():
    # `FAKE_START_DELAY=<seconds>`: on startup, hang around this long before answering the handshake (real codex's
    #   handshake takes a few seconds) => the driver's `__init__` just keeps waiting, and the session is not
    #   registered yet (added in 15b fix2)
    time.sleep(float(os.environ.get("FAKE_START_DELAY", "0")))
    note(family="codex", argv=sys.argv[2:], codex_home=os.environ.get("CODEX_HOME", ""), cwd=os.getcwd())
    side_effects()
    stale_stderr()
    if MODE == "home_stderr_exit":
        # 15c: the moment real codex starts, stderr already has one config-warning line naming the home directory
        #   (15c Phase A measured: `user (<home directory>\.codex\config.toml):
        #   `features.rmcp_client` is ignored.`); it dies before the handshake, and the EOF path takes this tail as
        #   the raw text => the remote leg would send the username out.
        sys.stderr.write("WARN codex_core::config: user (%s): `features.rmcp_client` is ignored." % os.path.join(
            os.path.expanduser("~"), ".codex", "config.toml") + NL)
        sys.stderr.flush()
        sys.exit(3)
    n = 0
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        method, rid = req.get("method"), req.get("id")
        if method == "initialize":
            if MODE == "handshake_error":
                # 🔴Replying with one error and staying alive anyway: that is exactly how a JSON-RPC server behaves,
                #   one bad request is not a reason for it to exit. This line is about faithfulness, it stands on
                #   its own.
                # 🔴This used to also say "the stub must stick around here: an obedient stub would make 'the driver
                #   killed it' and 'it exited on its own EOF' look identical => cutting this shot would still come
                #   out green" -- that claim is wrong. Measured on both arms on 2026-09-22: (1) neutering just that
                #   shot, (2) neutering it and also making this stub obedient -- the same line goes red in both
                #   (`0 != 1 : []`, the mechanism assertion fires first; and that test case stashes the exception in
                #   `box`, and the traceback pins `self.pipe`, so that stdin can never wait for the GC to close it,
                #   and an obedient stub sticks around just the same). => "sticking around" does not stand up that
                #   gate today. ⚠️It and the `deaf_after_handshake` case below are the same claim being overturned,
                #   just under a different mode name -- searching by wording will not find it, you have to search
                #   by what the claim asserted.
                out({"id": rid, "error": {"code": -32600,
                                          "message": AUTH_CODEX if os.environ.get("FAKE_RPC_ERROR") == "auth"
                                          else RPC_ERR}})
                time.sleep(STUCK)
            out({"id": rid, "result": {"userAgent": "fake"}})
            # 🔴The real app-server (codex-cli 0.155.0-alpha.9.2, measured locally 2026-09-22) inserts a
            #   notification right here that nobody asked for (no id) => a reply can only be recognized by id.
            #   ⭐Without the stub inserting it, "recognize by id" and "treat the first event as the reply" look
            #     identical across the whole suite (measured: swap `_call`'s check for `lambda e: True`, all green
            #     before inserting it, red on the spot after).
            out({"method": "remoteControl/status/changed",
                 "params": {"status": "disabled", "serverName": "fake"}, "emittedAtMs": 1790030200283})
            note(stray="remoteControl/status/changed")
            if MODE == "deaf_after_handshake":
                # The handshake answers as usual, then never reads stdin again (does not go away even after stdin
                # closes).
                # 🔴This used to say "what this stands on is the driver's behavioral assertion that 'a handshake
                #   that blows up on a non-BridgeError must also kill the process': on an obedient stub, 'the
                #   driver killed it' and 'it exited on its own EOF' look identical" -- that claim was overturned
                #   (review re-ran it twice): that test case still goes red switched back to `ok` mode, because it
                #   stashes the RuntimeError in `box["e"]`, and the traceback pins `__init__`'s stack frame, which
                #   pins `self.pipe`, so that stdin can never wait for the GC to close it, and an obedient stub
                #   sticks around just the same. => this mode does not stand up a single test today.
                # ⭐The one real reason to keep it: never let that gate's correctness ride on GC timing (the day
                #   someone changes `box["e"]` to not stash the exception, the `ok` path instantly starts gambling
                #   on GC).
                time.sleep(STUCK)
        elif method == "config/read":
            # The player's own effective config (Task 13c: codex uses his own CODEX_HOME). Shape follows the real
            #   app-server (codex-cli 0.155.0-alpha.9.2, measured 2026-09-24): `result.config.mcp_servers` is a
            #   table, and it is an empty table `{}` when none are configured, never a missing key.
            # ⭐`FAKE_MCP=a,b`: he has these MCP servers configured (the value carries a "secret": the driver must
            #   never keep it around or write it to the log).
            # ⚠️`FAKE_CONFIG_READ=nokey`/`error`: adversarial (off by default), never a measured shape -- the
            #   protocol drifted / an old version does not recognize this method.
            #   🔴For the nokey case the servers move under a renamed key (never dropped entirely): the value
            #   carrying the secret is still in the receipt => the assertion that "the error text must never carry
            #   the receipt out" only has teeth this way (13c review I1: with the field dropped entirely, the
            #   receipt never had the secret in it to begin with, so that assertion came out green even on an
            #   implementation that stuffs the whole receipt into the error text).
            note(config_read=req.get("params"))
            shape = os.environ.get("FAKE_CONFIG_READ", "")
            if shape == "error":
                out({"id": rid, "error": {"code": -32601, "message": "Method not found: config/read"}})
                continue
            servers = {n: {"command": "srv-" + n, "env": {"TOKEN": "mcp-secret-" + n}, "enabled": True}
                       for n in os.environ.get("FAKE_MCP", "").split(",") if n}
            conf = {"model": "fake", "developer_instructions": "his own words"}
            conf["mcpServers" if shape == "nokey" else "mcp_servers"] = servers
            out({"id": rid, "result": {"config": conf, "origins": {}, "layers": None}})
        elif method == "skills/list":
            # Skills he has installed (counting both `$CODEX_HOME/skills` and `~/.agents/skills`). Shape follows the
            #   real app-server as measured: `result.data[]` is grouped by cwd, each group's `skills[]` carries
            #   name/description/path/scope/enabled.
            # ⭐`FAKE_SKILLS=a,b`: these skills are installed (the paths are fake, never written to disk).
            # ⚠️`FAKE_SKILLS_LIST=noskills` (the skills table moves under a renamed key: the description is still
            #   in the receipt, same reasoning as nokey) / `nogroups` (`data: []`: asked about one cwd and got back
            #   not even one group, 13c review M2): adversarial, same as above.
            note(skills_list=req.get("params"))
            cwds = (req.get("params") or {}).get("cwds") or [os.getcwd()]
            skills = [{"name": n, "description": "desc of " + n, "path": os.path.join("fake-skills", n, "SKILL.md"),
                       "scope": "user", "enabled": True} for n in os.environ.get("FAKE_SKILLS", "").split(",") if n]
            shape = os.environ.get("FAKE_SKILLS_LIST", "")
            group = {"cwd": cwds[0], "errors": [], ("skillList" if shape == "noskills" else "skills"): skills}
            out({"id": rid, "result": {"data": [] if shape == "nogroups" else [group]}})
        elif method == "thread/start":
            note(system=req["params"].get("baseInstructions", ""), thread_params=req["params"])
            srcs = instruction_sources(req["params"].get("cwd") or os.getcwd())
            if MODE == "no_thread_id":
                # The protocol-drift case: `thread` is present, `id` is missing (a version change / a renamed
                #   field) => the consumer side must never treat this as success.
                # Sticks around the same way, same reasoning as above. ⭐The receipt still carries another value
                #   (an instructionSources that looks like the player's private path): the assertion that "the
                #   error text must never carry the receipt out" only has teeth because of it (13c review I1, the
                #   same issue).
                out({"id": rid, "result": {"thread": {}, "instructionSources": ["C:/Users/someone/secret-path/AGENTS.md"]}})
                time.sleep(STUCK)
            res = {"thread": {"id": "t-fake"}, "instructionSources": srcs}
            if os.environ.get("FAKE_SOURCES_SHAPE") == "missing":
                res.pop("instructionSources")          # adversarial: an old version / protocol change, this field is not in the receipt
            elif os.environ.get("FAKE_SOURCES_SHAPE") == "bad":
                res["instructionSources"] = "not-a-list"
            out({"id": rid, "result": res})
            # 🔴A second unsolicited notification, positioned after the reply (measured on the real CLI: the two
            #   notifications sit in different positions). ⭐This position, "one more follows right after the
            #   reply", had no coverage before => "stop reading once you have the id" and "keep reading" could not
            #   be told apart.
            out({"method": "thread/started", "params": {"thread": {"id": "t-fake"}}})
            note(stray="thread/started")
        elif method == "turn/start":
            text = req["params"]["input"][0]["text"]
            n += 1
            note(turn=n, text=text, effort=req["params"].get("effort"))
            # The real app-server echoes our own user message back in ~0.006s -- it must never be recognized as the first token
            out({"method": "item/started", "params": {"item": {"type": "userMessage", "text": text}}})
            stale_stderr_in_turn(n)
            misbehave(n)
            if MODE == "turn_rejected":
                # 🔴`turn/start` gets outright refused: what comes back is a JSON-RPC error with an id, never a
                #   turn/completed (a stale threadId / an unrecognized model / drifted parameters all go through
                #   this path). The consumer side's path here used to have zero test coverage.
                out({"id": rid, "error": {"code": -32602, "message": "unknown threadId"}})
                continue
            if MODE in ("auth", "quota"):
                msg = AUTH_CODEX if MODE == "auth" else QUOTA
                # ⚠️One adversarial case (off by default): `error` is a bare string instead of an object.
                # Without playing out this one, the consumer side's bare escape hatch (`err.get("message")` throws
                # AttributeError against a str => not a BridgeError, not a single line written to disk) could never
                # be produced on this test harness at all.
                err = msg if os.environ.get("FAKE_ERR_STR") == "1" else {"message": msg}
                if os.environ.get("FAKE_ERR_CODE") == "1" and isinstance(err, dict):
                    # ⚠️Also adversarial (off by default): `turn.error` carries a `code`. Never a measured shape --
                    # it stands on the contract "the same family of error must never carry code during the
                    # handshake and lose it during the turn".
                    err = dict(err, code="usage_limit_reached")
                out({"method": "turn/completed", "params": {"turn": {"status": "failed", "error": err}}})
                continue
            if MODE == "retry_then_fail":
                # 15c: what real codex looks like when credentials go bad (Task 15 smoke test A1 measured; shape
                #   follows the `app-server generate-json-schema` ErrorNotification): a few `error` notifications
                #   during the retries (`willRetry: true`, `error.codexErrorInfo.<variant>.httpStatusCode`), then a
                #   final `turn/completed` that fails with `codexErrorInfo: "other"`. ⭐`FAKE_FINAL` = the final raw
                #   text (by default it does not carry the digits 401 -- that is exactly the case the text
                #   classifier cannot recognize; ⏳whether the real CLI ever emits it this way is unmeasured);
                #   `FAKE_RETRY_STATUS` (401 by default; `none` = null, a zero-input control).
                #   A few more fields added by 15c review I3/M7: `FAKE_RETRY_INFO` = the JSON for codexErrorInfo in
                #   the notification (swap the whole thing, e.g. the enum `"unauthorized"`), `FAKE_RETRIES` = how
                #   many notifications (0 = no retries, only the final state matters), `FAKE_FINAL_INFO` = the JSON
                #   for the final `turn.error`'s codexErrorInfo, `FAKE_FINAL_OK=1` = the final state is success,
                #   with the reply body being only whitespace (the credentials refreshed, it just did not say
                #   anything).
                status = os.environ.get("FAKE_RETRY_STATUS", "401")
                info = json.loads(os.environ.get("FAKE_RETRY_INFO") or json.dumps(
                    {"responseStreamDisconnected": {"httpStatusCode": None if status == "none" else int(status)}}))
                for i in range(int(os.environ.get("FAKE_RETRIES", "3"))):
                    out({"method": "error", "params": {"threadId": "t-fake", "turnId": "u-fake", "willRetry": True, "error": {
                        "message": "Reconnecting... %d/5" % (i + 1), "additionalDetails": "stream disconnected before completion",
                        "codexErrorInfo": info}}})
                if os.environ.get("FAKE_FINAL_OK") == "1":
                    say("  ")
                    out({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
                    continue
                final = os.environ.get("FAKE_FINAL", "stream disconnected before completion: tls handshake eof")
                out({"method": "turn/completed", "params": {"turn": {"status": "failed", "error": {
                    "message": final, "codexErrorInfo": json.loads(os.environ.get("FAKE_FINAL_INFO") or '"other"')}}}})
                continue
            if MODE == "heartbeat_only":
                time.sleep(20)
                continue
            if MODE == "empty_delta_first":
                # ⭐This is what keeps the consumer side's `if piece:` half alive: without this "delta is an empty
                #   string" frame in the stub, that half is dead code with zero coverage (deleting it alone leaves
                #   the whole suite green), and without it the first-token reading is stuck at 0 forever while all
                #   three tables stay green.
                # ⚠️This one frame is adversarial, never a measured shape: nobody has measured whether a real
                #   app-server ever emits an empty delta.
                # ⭐What follows is silence, never a heartbeat: codex emits zero events while it is thinking (the
                #   engine measured this on 2026-09-19).
                out({"method": "item/agentMessage/delta", "params": {"delta": ""}})
                time.sleep(20)
                continue
            if MODE == "no_answer":
                # This turn ends normally, yet not a single word of text came out: the consumer side must never hand up an empty string as the answer.
                out({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
                continue
            ans = "echo[%d]: %s" % (n, text[:60]) + extras(text)
            if MODE == "blank_answer":
                # ⭐The whole turn emits nothing but whitespace: this is exactly what the consumer side's `.strip()`
                #   guards, moved from accumulation to a check after the turn wraps up (never hand the player a
                #   blob of whitespace as the answer).
                say("   " + NL)
            elif MODE == "two_messages":
                # ⚠️Adversarial, never a measured shape: two agentMessages in one turn (with every feature turned
                #   off, the real CLI most likely emits only one, and nobody has measured it). ⭐But the consumer
                #   side's "overwrite vs. accumulate" spot is an inconsistency you can read statically: `on_delta`
                #   streams both of them out, while an overwriting assignment leaves only the second one in what
                #   gets handed back, and completely silently at that.
                # 🔴The answer carries whitespace on both ends: a real CLI's agentMessage very likely ends with a
                #   newline, and a fixture without that whitespace would let the consumer side's invariant (what
                #   streamed out = what got handed back) happen to hold => which amounts to never having tested it
                #   at all. (Review measured this: adding these two spaces turned a suite that used to be all green
                #   red on the spot.)
                say("  " + ans + "-A" + NL)
                say("  " + ans + "-B" + NL)
            else:
                # The parenthetical is for the reader: the two_messages case right next to it is explicitly two
                #   lines, so the reader would assume this one "adds whitespace" too. This spot is exactly the
                #   fixture for that invariant -- reading it wrong means changing it wrong.
                say(("  " + ans + NL) if MODE == "empty_tail_message" else ans)
                if MODE == "empty_tail_message":
                    # ⚠️Another spot in the same family: the trailing agentMessage's `text` is empty => must never
                    # erase text that was already said
                    out({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": ""}}})
            # ⚠️`total` is how much of the context is occupied, never this turn's own consumption (the engine
            #   measured this on the real CLI on 2026-09-19: never take a difference of it).
            # ⭐Emit both: an implementation that reads the wrong one gets a number that looks plausible, never an
            #   obvious None.
            out({"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {
                "last": {"inputTokens": 10 * n, "cachedInputTokens": 0, "outputTokens": len(ans),
                         "reasoningOutputTokens": 0},
                "total": {"inputTokens": 150 * n, "cachedInputTokens": 100 * n, "outputTokens": len(ans) * n,
                          "reasoningOutputTokens": 0}}}})
            out({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})


def quick():
    """--version / --help / login status / feature list: subcommands that answer once and exit. Returns True = it
    has already been handled.

    ⭐Recognize the family: `auth status` only exists for claude, `login status` / `features list` only exist for
    codex. A stub that does not tell families apart would let an implementation that mixes up the families come out
    green just the same (`codex auth status` printing claude's JSON).
    ⚠️The two families also fail differently, measured against the real CLIs on 2026-09-21:
      - `codex auth status` => rc=2 plus `error: unrecognized subcommand 'status'` (clap's standard refusal);
      - claude has no such failure mode -- an argv it does not recognize gets treated as a real prompt and really
        sent as one turn (measured: `claude login status` comes back with something a model said, rc=0) => here it
        falls back to claude()'s prompt path, never fabricate an rc=2 for claude that does not exist in reality,
        which would make downstream code handle a failure mode that never happens."""
    fam, rest = sys.argv[1], sys.argv[2:]
    rc = 0
    if rest[:1] == ["--version"]:
        print("0.0.0 (fake %s)" % fam)
    elif rest[:1] == ["--help"]:
        text = "  (nothing)" if os.environ.get("FAKE_NO_SAFE_MODE") == "1" else "  --safe-mode   (fake)"
        if os.environ.get("FAKE_HELP_RC", "0") != "0":
            # 🔴It starts up but rc != 0: node crashed / the install is broken / the `.cmd` wrapper errors out on its
            # own. ⭐It throws no exception => the caller's `except` never gets a chance to run, and naturally
            # `--safe-mode` is not in the output either => this is exactly the false-negative surface where it
            # would still print "please upgrade" as if nothing happened. Never print help out of habit here either:
            # if it really crashed, there is no help.
            rc = int(os.environ["FAKE_HELP_RC"])
            # ⭐Three lines, and the real reason is on the last one -- this is exactly what it looks like when node
            #   crashes: the first two lines are a useless stack header. Never write just one line: with one line,
            #   "truncated to the first line" and "kept in full" look identical to a downstream test, and what
            #   truncation would cut is exactly the `Cannot find module` line -- the only one that tells him how to
            #   fix it.
            sys.stderr.write("node:internal/modules/cjs/loader:1234" + NL
                             + "      throw err;" + NL
                             + "Error: Cannot find module 'yoga-wasm-web'" + NL)
            sys.stderr.flush()
        elif os.environ.get("FAKE_HELP_ON_STDERR") == "1":
            # Which stream a real CLI prints help to is its own business (codex's `login status` really uses both
            # in reality).
            # The stub has to be able to play this out, or else this "only reads stdout" false-negative surface
            # could never be produced on this harness at all -- and the false refusal it would produce is exactly
            # the lie "this version does not recognize --safe-mode, please upgrade".
            sys.stderr.write(text + NL)
            sys.stderr.flush()
        else:
            print(text)
    elif rest[:2] == ["auth", "status"] and fam == "claude":
        shape = os.environ.get("FAKE_AUTH_STATUS", "")
        if shape == "prose":
            # 🔴F-1: claude has no "unrecognized subcommand" failure mode (measured 2026-09-21: `claude login status`
            #   gives rc=0, with something a model said) => this is what a claude that does not recognize
            #   `auth status` looks like. Never fabricate an rc=2 for it.
            print("I can help you check your authentication status. Could you tell me more about what you need?")
        else:
            row = {"loggedIn": MODE != "auth" or STALE_LOGIN, "authMethod": "claude.ai", "email": "someone@example.com",
                   "orgId": "org-1", "orgName": "Org", "subscriptionType": "max"}
            if shape == "nokey":
                row.pop("loggedIn")          # protocol drift: a field got renamed / the version changed
            if shape == "twoline":
                row["authMethod"] = "claude.ai" + NL + "(second line)"   # the entry point has to fold it: it gets printed into doctor's line
            if shape == "noisy":
                # F-6: a line of noise with a brace comes before the JSON => `raw.index("{")` would start parsing from that line, and the whole thing becomes unreadable
                print("Warning: config key {theme} is deprecated")
            print(json.dumps(row))
    elif rest[:2] == ["login", "status"] and fam == "codex":
        if os.environ.get("FAKE_STATUS_BROKEN") == "1":
            # 🔴rc != 0 is both how it says "not logged in" and what it looks like when it is broken itself =>
            # looking at rc alone cannot tell these two apart.
            # This plays out the second one: rc=1, and the output does not mention login status at all.
            # ⭐Two lines: if the consumer side does not fold it, `method` ends up multi-line, and the blocked
            #   message would get torn apart in the middle.
            sys.stderr.write("Error loading configuration: invalid TOML in config.toml line 3" + NL
                             + "  caused by: expected '=' after key" + NL)
            sys.stderr.flush()
            rc = 1
        elif os.environ.get("FAKE_STATUS_SILENT") == "1":
            rc = 1                     # rc != 0 and not a single word said: downstream's "codex said: ..." must never read as "codex said: "
        else:
            logged_out = MODE == "auth" and not STALE_LOGIN
            print("Not logged in" if logged_out else "Logged in using ChatGPT")
            rc = 1 if logged_out else 0
    elif rest[:2] == ["features", "list"] and fam == "codex":
        if os.environ.get("FAKE_FEATURES_RC", "0") != "0":
            # An old codex with no `features` subcommand: clap's standard refusal (rc=2). ⭐It is never the same
            # thing as "all 20 names are obsolete".
            sys.stderr.write("error: unrecognized subcommand 'features'" + NL)
            sys.stderr.flush()
            rc = int(os.environ["FAKE_FEATURES_RC"])
        for name in [x for x in os.environ.get("FAKE_FEATURES", "").split(",") if x and not rc]:
            # Shape measured against codex-cli 0.155.0-alpha.9.2: `name  stage  on by default or not`, one per line
            print(name + "  stable  true")
    elif fam == "codex" and rest and not rest[0].startswith("-") and rest[0] != "app-server":
        # The check follows the shape of the failure (any subcommand codex does not recognize gets rc=2), never
        # just this one `auth` scene at hand
        sys.stderr.write("error: unrecognized subcommand " + repr(rest[-1]) + NL)
        rc = 2
    else:
        return False        # never put the accounting after this line: the unhandled path is left for claude()/codex() to record on their own
    note(family=fam, quick=rest, codex_home=os.environ.get("CODEX_HOME", ""), cwd=os.getcwd())
    if rc:
        sys.exit(rc)
    return True


def _nonblank(path):
    try:
        with open(path, encoding="utf-8") as f:
            return bool(f.read().strip())
    except OSError:
        return False


def instruction_sources(cwd):
    """The `instructionSources` in thread/start's receipt ("currently loaded for this thread"). The rule follows a
    real codex 0.155 measurement at zero cost (13c Fix 1, `t13c/f1-sources.json`, going through the production
    handshake, never sending a turn; zero cost relies on a temporary CODEX_HOME that is not logged in -- the
    handshake itself still makes codex warm up once, 13c Fix 1b, NOTES.md::codex-user-home) -- never something made
    up:
      (1) if `$CODEX_HOME/AGENTS.override.md` has non-blank content => use only it; otherwise (missing / 0 bytes /
        whitespace only) => `AGENTS.md` (only counts if it has non-blank content);
      (2) if walking up from cwd finds a `.git` => every `AGENTS.md` with non-blank content along that path from
        the repo root down to cwd counts; if no `.git` is found => none count (⏳whether cwd's own file counts when
        there is no `.git` is unmeasured -- a real session's cwd is an empty directory, so it never comes up).
    ⚠️One place where this differs from real codex (needed for this harness): the walk up stops at a ceiling
      (never looking at the ceiling itself or above it) -- the default is the system temp directory,
      `FAKE_GIT_CEILING` can lower it; if cwd is not under the ceiling => nothing is searched at all. Real codex
      walks all the way to the disk root => copying that behavior would make the `--live` tests that pin exact
      sources depend on whether some ancestor of `%TEMP%` on the dev machine happens to have a `.git` in it (13c
      re-review N5). This harness's fixtures all live under the temp directory (forced by the directory-creation
      gate).
    `FAKE_SOURCES_EXTRA=<path>`: report one more on top (adversarial: the case where codex reports it and scv cannot
    stat it)."""
    out = []
    home = os.environ.get("CODEX_HOME")
    if home:
        for name in ("AGENTS.override.md", "AGENTS.md"):
            if _nonblank(os.path.join(home, name)):
                out.append(os.path.join(home, name))
                break
    # ⚠️abspath, never realpath: the reported path has to match what the test harness builds byte for byte (the
    # macOS /var -> /private/var kind of thing would make the two sides disagree)
    top = os.path.normcase(os.path.abspath(os.environ.get("FAKE_GIT_CEILING") or tempfile.gettempdir()))
    chain, d = [], os.path.abspath(cwd)
    while os.path.normcase(d).startswith(top.rstrip(os.sep) + os.sep):
        chain.append(d)
        if os.path.exists(os.path.join(d, ".git")):
            out += [os.path.join(x, "AGENTS.md") for x in reversed(chain) if _nonblank(os.path.join(x, "AGENTS.md"))]
            break
        d = os.path.dirname(d)
    if os.environ.get("FAKE_SOURCES_EXTRA"):
        out.append(os.environ["FAKE_SOURCES_EXTRA"])
    return out


def missing_codex_home():
    """Real codex simply cannot start at all against a CODEX_HOME that does not exist (measured 2026-09-21, the same
    for every subcommand): `Error loading configuration: CODEX_HOME points to "…", but that path does not exist`,
    rc=1, and it never creates it on its own. ⭐Since Task 13c, CODEX_HOME only ever comes from the player's own
    environment => the bridge must never create this directory for him (that is writing into his own space), it may
    only carry codex's own raw words out."""
    home = os.environ.get("CODEX_HOME")
    if sys.argv[1] == "codex" and home and not os.path.isdir(home):
        # ⭐Record it anyway (carrying which home it got): without recording it, "the bridge handed over a made-up
        #   home" would only show up on this harness as "the codex family's record is missing" -- it would still go
        #   red, but red on the "the ruler is not blind" assertion, never red on "which home it handed over" (13c
        #   mutation N2 really hit this).
        note(family="codex", quick=sys.argv[2:], codex_home=home, cwd=os.getcwd(), refused="CODEX_HOME does not exist")
        sys.stderr.write('Error loading configuration: CODEX_HOME points to "%s", but that path does not exist' % home + NL)
        sys.stderr.flush()
        sys.exit(1)


def console_state():
    """win32: whether this process's console has a visible window (records one line when `FAKE_CONSOLE=1`).
    ⭐Supplies the reading for the test "a child process started by the background bridge must never flash a black
      window": when the parent has no console, a console child process without CREATE_NO_WINDOW gets a new visible
      window opened for it by the system -- this line records exactly that window."""
    import ctypes
    k = ctypes.WinDLL("kernel32")
    k.GetConsoleWindow.restype = ctypes.c_void_p
    hwnd = k.GetConsoleWindow()
    return {"hwnd": hwnd or 0, "visible": bool(hwnd) and bool(ctypes.WinDLL("user32").IsWindowVisible(ctypes.c_void_p(hwnd)))}


if __name__ == "__main__":
    if os.environ.get("FAKE_CONSOLE") == "1" and os.name == "nt":
        note(console=console_state(), family=sys.argv[1], argv=sys.argv[2:4])
    if os.environ.get("FAKE_ENV_NAMES") == "1":
        # 15c review I1: which names from the two families' namespaces this process really got in its environment
        #   (never the values) -- "stripping session variables" has to be measured on the consumer side.
        #   ⭐Collect purely by namespace, never decide for itself which ones are session variables: there is only
        #   one such check (`scv.session_bound`), used on the test-harness side.
        note(env_names=sorted(k for k in os.environ if k.upper().startswith(("CLAUDE", "CODEX", "AI_AGENT", "TRACE"))),
             family=sys.argv[1], argv=sys.argv[2:4])
    missing_codex_home()
    if not quick():
        {"claude": claude, "codex": codex}[sys.argv[1]]()
