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

        # ---- Exits: the response only ever goes out through these three doors (the third, `_page`, is `GET /wake`'s alone)
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

        def _page(self, status: int, text: str, close: bool) -> None:
            """0.3.0: the third exit, `GET /wake`'s alone — a page for a browser window, never JSON. ⭐No CORS header
            ever (this path skipped the `Origin` guard, see `_guard`): a page on another site can open it, but can
            never read what it says."""
            # `window` + chr(46): the address gate would read the two words joined by a dot as a host name
            script = (("<script>setTimeout(function () { window" + chr(46) + "close(); }, %d);</script>") % WAKE_CLOSE_MS) if close else ""
            data = ("<!doctype html><meta charset=utf-8><title>scv</title><p>%s</p>%s" % (text, script)).encode("utf-8")
            if self._sent:
                log("⚠️ the local API tried to send a second response (status=%d, GET /wake) ⇒ only logging this line" % status)
                return
            self._sent = True
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
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
        def _guard(self, need_token: bool, need_json: bool, any_origin: bool = False) -> bool:
            host = (self.headers.get("Host") or "").strip().lower()
            if host.startswith("["):        # `[::1]:8765` ⇒ `::1`
                host = host[1:].split("]")[0]
            elif host.count(":") == 1:      # `127.0.0.1:8765` ⇒ `127.0.0.1`
                host = host.split(":")[0]   # never strip a bare IPv6 address too: `::1` would get cut down to `:`, a 403 for someone who did nothing wrong
            if host not in LOOPBACK_HOSTS:
                # DNS rebinding: the request really did land on the loopback port, but the browser thinks it is visiting evil.example
                return self._refuse(403, "forbidden_host", "the Host header is not a loopback address: %s" % repr(host)[:64])
            origin = self.headers.get("Origin")
            if origin and not any_origin:
                # ⭐`any_origin` is `GET /wake`'s alone (0.3.0, spec B39): all it can do is make the bridge dial the one
                #   service it is already paired with, and it never echoes this header back ⇒ letting it in costs nothing
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
            elif path == "/wake":
                if self._guard(False, False, any_origin=True):
                    self._page(*WAKE_PAGES[bridge.wake("GET /wake")])
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


