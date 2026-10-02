# ━━ Local API (B3/B5/B23): a few OpenAI-compatible routes + four guards
GET_ROUTES = ("/healthz", "/v1/models", "/wake")
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
# 0.3.0 (spec B39): what `GET /wake` answers — a small page for a browser window (the lobby opens it in a popup of its
#   own: a top-level page is the one way a web page may reach loopback without a permission prompt), never JSON.
#   `Bridge.wake`'s outcome ⇒ (status, the one line it says, whether the page closes its own window).
WAKE_PAGES = {"woke": (200, "The bridge is awake.", True), "awake": (200, "The bridge is awake.", True),
              "not_paired": (409, "This bridge is not paired, so there is nothing to wake.", False),
              "cannot_dial": (409, "This bridge cannot dial out: bridge.log on this machine says why.", False)}
WAKE_CLOSE_MS = 600          # the page closes its own window this long after it shows (the lobby closes it too)
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


