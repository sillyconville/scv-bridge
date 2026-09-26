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

