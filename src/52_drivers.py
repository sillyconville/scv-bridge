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

