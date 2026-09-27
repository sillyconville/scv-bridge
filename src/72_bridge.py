ES_SYSTEM_REQUIRED = 0x00000001     # SetThreadExecutionState: reset the system idle timer once (⛔ never ES_CONTINUOUS: that one sticks to the calling thread)
AWAKE_EVERY_S = 30                  # how often the main loop asks; far below any sleep timeout Windows offers (1 minute is the shortest)


class KeepAwake:
    """While the remote leg has a job running, or had one end less than `window_s` ago, keep resetting Windows'
    system idle timer so the machine does not fall asleep in the middle of someone's game (maintainer 2026-09-27:
    the bridge runs on a desktop at home while the player plays on a phone — a desktop that sleeps pauses the game,
    and nobody can wake it from the phone).
    ⭐Only the system idle timer, once per tick (ES_SYSTEM_REQUIRED alone): the display may still turn off, and
      nothing is left behind when the bridge stops. ⛔never ES_CONTINUOUS: it sticks to the calling thread.
    ⭐The tail (`window_s` after the last job) covers the gaps inside one game: the player's own turn, a pause
      waiting for a login. A machine that is already asleep when a game starts cannot be helped from here.
    POSIX: a no-op (not measured; left to the POSIX pass)."""

    def __init__(self, window_s: float, poke=None, clock=time.time):
        self.window_s = float(window_s)
        self._poke = poke if poke is not None else _poke_idle_timer
        self._clock = clock
        self._lock = threading.Lock()
        self._busy = 0
        self._last = None                 # when the last job ended; None = none yet
        self.pokes = 0

    def begin(self) -> None:
        with self._lock:
            self._busy += 1

    def end(self) -> None:
        with self._lock:
            self._busy = max(0, self._busy - 1)
            self._last = self._clock()

    def wanted(self) -> bool:
        if self.window_s <= 0:
            return False
        with self._lock:
            return self._busy > 0 or (self._last is not None and self._clock() - self._last < self.window_s)

    def tick(self) -> bool:
        """Called by the main loop every `AWAKE_EVERY_S`. Returns whether it asked Windows to stay awake."""
        if not self.wanted():
            return False
        try:
            self._poke()
        except Exception as e:           # never let this take the bridge down: it is a convenience, not the job
            log("⚠️ could not ask the system to stay awake: %s: %s" % (type(e).__name__, _one_line(e)))
            return False
        self.pokes += 1
        return True


def _poke_idle_timer() -> None:
    if sys.platform == "win32":
        _k32().SetThreadExecutionState(ES_SYSTEM_REQUIRED)


def _awake_window(v) -> float:
    """config.json's `keep_awake_s`: missing ⇒ 600; a number ≥ 0 ⇒ that; anything else ⇒ 600, said once."""
    if v is None:
        return 600.0
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
        return float(v)
    log("⚠️ config.json's keep_awake_s is not a number ≥ 0 (it is %r) ⇒ using 600" % (v,))
    return 600.0


class Bridge:
    """A bridge = one config + one detection pass + a session manager + an audit log + a local HTTP leg.
    ⚠️`found`/`cat` are detected at the moment the bridge starts: if the user runs `codex login` after starting the
      bridge, it only becomes visible after a restart — never change this to probe on every request (`detect()`
      has to start two child processes each time, and its few lines of decision log about "not reporting this
      whole family" would get flooded into background noise)."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.found = detect(cfg)
        self.cat = catalog(cfg, self.found)
        self.sessions = SessionManager(cfg, lambda: self.cat)
        self.joblog = JobLog()
        # 0.2.0: `keep_awake_s` — how long after the last remote job the machine is kept awake (0 = off)
        self.awake = KeepAwake(_awake_window(cfg.get("keep_awake_s")))
        self.started_at = time.time()
        self.httpd = None
        self.remote = None                 # assigned only in Task 11
        self._closed_at: dict = {}         # session → the last time someone came to close it
        self._closed_lock = threading.Lock()

    def start_local(self, port: int | None = None) -> int:
        want = int(self.cfg.get("port") or 8765) if port is None else int(port)
        try:
            self.httpd = _Server((LOCAL_HOST, want), _make_handler(self))
        except OSError as exc:
            # This is the most common way starting the bridge fails (a previous one is still open / something
            # else is holding the port) ⇒ never let the user see a bare WinError.
            err = BridgeError("crashed", "the local API could not start (port %d): %s" % (want, _one_line(exc)))
            log(err.raw)
            err.logged = True
            raise err
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self.httpd.server_address[1]

    def start_remote(self) -> bool:
        """Not paired = not one byte goes out to the network. Returns False = this bridge only has a local leg
        today (two different reasons, kept apart in the log).
        🔴the plaintext slot: §① 's `HTTPS` line says "the remote address has to start with it", and before this
          spot nowhere in the whole file was actually enforcing it — another promise that was written down and
          then nobody enforced. Without this guard, an `http://` `remote_url` (which could also be hand-written
          into config.json) would send `Authorization: Bearer <remote_token>`, the hello payload's "which CLIs are
          installed on this machine", and every segment of every answer, all in plaintext.
        ⭐this one spot that actually dials must judge for itself (a hand-written config can go around `scv pair`);
          the judgment itself lives in exactly one place (`remote_url_refused`), and `pair` calls it too.
        ⚠️plaintext `http://` on loopback is an exception made for the test harness (the reference dispatcher runs
          on 127.0.0.1)."""
        url = str(self.cfg.get("remote_url") or "")
        if not url or not self.cfg.get("remote_token"):
            return False
        why = remote_url_refused(url)          # ⭐the same judgment as `scv pair` (only one is allowed to exist)
        if why:
            log("❌ the remote leg is not starting: config.json's remote_url is no good: " + why)
            return False
        self.remote = RemoteLeg(self)
        threading.Thread(target=self.remote.run, daemon=True).start()
        return True

    def cli_ver(self, model_id) -> str:
        """The version of the CLI family behind this model (for `jobs.log`'s bookkeeping; detection already ran
        when the bridge started, this call costs nothing)."""
        return _cli_version((self.found.get(str(model_id).partition("/")[0]) or {}).get("version"))

    def health(self) -> dict:
        """⚠️never call `snapshot()`: its cost grows with the registry's row count (before the switch to ctypes,
        win32 started one powershell per pid to ask its RSS; measured 2.843s even for a fresh manager with zero
        sessions; POSIX still runs one `ps` per pid), and this endpoint is exactly what people poll.
        ⭐`children` is just the registry's row count (one file read, measured to cost nothing), and it is a global
          number for the whole SCV_HOME, while `sessions`/`queued`/`running` belong to this one bridge — two
          different scopes, never read them as the same thing.
        🔴this endpoint takes no token ⇒ `families` only gives the version's shape + whether it is blocked (13b
          review M3): the original words in `blocked`/`version` can carry an absolute local path (a pastable login
          command, the OS's own words, a path with a user name in it); the reason and what to do about it are left
          for the local doctor. 📎 NOTES.md::snapshot-is-expensive"""
        out = self.sessions.counts()
        out.update(version=VERSION, protocol=PROTOCOL, uptime_s=int(time.time() - self.started_at),
                   models=self.cat, children=len(children()),
                   families={f: {"version": _cli_version(i["version"]), "blocked": bool(i["blocked"])}
                             for f, i in self.found.items()},
                   remote=(self.remote.describe() if self.remote else {"state": "off"}))
        return out

    def note_close(self, sid: str, order=None, detach=False) -> bool:
        """Record that someone has started closing this session. ⭐What gets recorded is the moment work begins,
        not the moment it finishes closing: the judgment is "did anyone come to close it while this turn was in
        flight", and the act of closing itself can drag on for over ten seconds. `order` = the event sequence
        number on the remote leg's receive-stream thread (see `closed_during`).
        Returns True = a new close (the caller should go close it); False = a previous close is still running ⇒
        only refresh the record to this one (15b fix2 M-b: that thread will pick up one more round once it
        finishes the one in its hand)."""
        now = time.time()
        with self._closed_lock:
            for k, row in list(self._closed_at.items()):
                if now - row["at"] > CLOSE_MEMORY_S:
                    self._closed_at.pop(k, None)   # ⭐sweep it while we're here: this table must never keep growing with every close
            row = self._closed_at.get(sid)
            # ⭐this remote-side call landing on a locally-initiated "still closing" (a row with no order) also
            #   counts as a new one: the local thread only closes the one it popped itself, and never picks up
            #   another round (15b fix3 N-M1)
            fresh = row is None or row["done"] or (order is not None and row.get("order") is None)
            self._closed_at[sid] = ({"at": now, "done": False, "error": "", "order": order} if fresh
                                    else dict(row, at=now, order=order))
            if detach:          # remote leg: detaching and recording share the same lock (both the check after `_run_session` registers and the check at the closing thread's finish rely on it)
                self.sessions.detach(sid)
            return fresh

    def note_closed(self, sid: str, error: str = "", order=None):
        """That close is done (record it whether it succeeded or not). 🔴"started" and "finished" must never look
        the same, and `close_session()` on its own cannot tell them apart: what it returns is "was it in the table
        just now" — the first call already popped it ⇒ the second call always gets `False`, byte for byte the
        same as "this bridge never had this session at all". This entry is what makes up for that gap.
        ⚠️record it even when it blows up: not recording it leaves "still closing" hanging forever, and that is an
        answer that will never change.
        ⭐if the order recorded no longer belongs to this call (another one arrived while this was closing, M-b; or
          while this local call was closing, the remote leg started its own, fix3 N-M1) ⇒ never record it as
          finished, return that call's order instead: the remote caller picks up one more round; the local caller
          does not need to worry about it (that call has its own thread, and it will record the finish itself)."""
        with self._closed_lock:
            row = self._closed_at.get(sid)
            if row is not None and row.get("order") != order:
                return row["order"]
            if row is not None:
                row["done"], row["error"] = True, error
        return None

    def closed_during(self, sid, since: float, order=None) -> bool:
        """Did anyone come to close this same session while this turn (which began at `since`) was in flight.
        🔴the remote leg (both sides carry the receive-stream thread's event sequence number `order`) judges by
          event order, never by the wall clock (15b fix2 N-I2): close and job are handled on the same thread in
          arrival order, but win32's `time.time()` ticks about once every 1 ms, and two back-to-back events often
          land on the same value ⇒ `>=` would judge "closed, then asked" as "closed while queued" — a false,
          non-retryable cancelled. ⭐the sequence number strictly increases ⇒ never a tie. The local leg has no
          single arrival order (one thread per request), so it still goes by the wall clock."""
        with self._closed_lock:
            row = self._closed_at.get(sid) if sid else None
            if row and order is not None and row.get("order") is not None:
                return row["order"] > order
            return bool(row) and row["at"] >= since

    def close_state(self, sid) -> str:
        """The outcome of the previous close: `""` = no memory of this, `"closing"` = still closing, `"closed"` =
        finished closing, `"!<original words>"` = that close blew up (⇒ every time it is asked, it gets the same
        line back; never let a failed close turn into a silent False)."""
        with self._closed_lock:
            row = self._closed_at.get(sid) if sid else None
            if not row:
                return ""
            if not row["done"]:
                return "closing"
            return ("!" + row["error"]) if row["error"] else "closed"

    def stop(self) -> None:
        if self.remote:
            self.remote.stop()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.sessions.close_all()


