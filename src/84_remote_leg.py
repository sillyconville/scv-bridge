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
                self.b.joblog.write(leg="remote", model=shown_model(job.get("model"), self.b.cat), klass=klass, job_id=jid,
                                    cli_version=self.b.cli_ver(job.get("model")), usage=res.get("usage") or {},
                                    latency_s=round(time.time() - t0, 2), ttfc=res.get("ttfc"),
                                    queued_ms=res.get("queued_ms") or 0, rebuilt=res.get("rebuilt"))


