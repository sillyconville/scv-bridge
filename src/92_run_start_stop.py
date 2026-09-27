def _read_pid_file():
    """`None` = there is no such file; unreadable / wrong shape ⇒ `{"bad": reason}` (never let a broken file blow up `scv stop` into a traceback)."""
    p = spath("bridge.pid")
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"bad": "could not read it: %s" % _one_line(exc)}
    pid = rec.get("pid") if isinstance(rec, dict) else None
    return rec if isinstance(pid, int) and not isinstance(pid, bool) else {"bad": "wrong shape"}


def bridge_owner() -> tuple:
    """What state the bridge recorded in bridge.pid is in right now ⇒ `(state, rec)`, state ∈ none / alive / gone /
    unsure (could not tell, retried) / foreign (unrecognizable: corrupt, or a birth-id format written by a
    different version).
    🔴Always judge by `== rec["born"]`, never by the truthiness of the birth id (it is three-valued: `None` would
      be read as "already stopped" ⇒ failing to kill it while claiming it has stopped, A1); ⭐the format goes
      through `birth_known()` first: unrecognizable never means "not that bridge" (A3)."""
    rec = _read_pid_file()
    if rec is None:
        return "none", {}
    born = rec.get("born")
    if "bad" in rec or not isinstance(born, str) or not birth_known(born):
        return "foreign", rec
    now = proc_start_id(rec["pid"])
    return ("unsure" if now is None else "alive" if now == born else "gone"), rec


def started(port: int, ticket: str):
    """The condition `scv start` waits for: the ticket in bridge.pid is the one issued this time, and the bridge
    is answering on the port.
    🔴Never wait for `bridge.pid.exists()`: a stale file left behind by a previous crashed bridge would satisfy
      that on the spot (the f17c9e5 shape, A4).
    ⭐Recognized by ticket, never by pid: under a venv's python launcher the pid can belong to a different process
      (measured the same on this machine, ⏳ venv not measured)."""
    rec = _read_pid_file() or {}
    return rec if rec.get("ticket") == ticket and our_health(port) else None


def spawn_detached(argv: list) -> tuple:
    """B17: the bridge must never be an agent session's child process. On Windows the harness often locks child
    processes inside a Job ⇒ first try to break free of it, and say so plainly if it cannot.
    🔴win32 uses `CREATE_NO_WINDOW`, never `DETACHED_PROCESS`: the latter leaves the bridge with no console at
      all ⇒ every console child process it starts afterwards gets a new, visible window from the system (measured
      on a user's desktop, flashing non-stop). The former gives the bridge a hidden console, never attached to
      the terminal that started it (the two are mutually exclusive; giving both means one gets ignored). The
      child-process layer has its own `new_session_kw()` backing it up too, see that for why.
    ⭐stdout / stderr always go to DEVNULL: hooking them up to bridge.log would make every one of `log()`'s lines
      get written twice, and would bypass the one door that append writes to disk go through (the brief's
      original approach). Every exception at the subcommand layer goes through the top-level handler and lands
      on disk; the ones that cannot land there (an interpreter-level crash) get seen by `scv start` as "it started
      and quit right away", which sends the user to `scv run` in the foreground. 📎 NOTES.md::detach-0b"""
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name != "nt":
        return subprocess.Popen(argv, start_new_session=True, **kw).pid, ""
    flags = CREATE_NO_WINDOW | 0x00000200 | 0x01000000        # NEW_PROCESS_GROUP | BREAKAWAY_FROM_JOB
    try:
        return subprocess.Popen(argv, creationflags=flags, **kw).pid, ""
    except OSError as exc:
        log("⚠️ could not break free of the parent process's Job (%s) ⇒ falling back to starting it without breaking free" % exc)
        return subprocess.Popen(argv, creationflags=flags & ~0x01000000, **kw).pid, (
            "⚠️ could not break free of the parent process's Job: closing this terminal / session may take the bridge down with it. "
            "Please run this again from an ordinary terminal:" + NL + self_cmd("start"))


def serve_until(bridge, stop: threading.Event) -> None:
    """`scv run`'s main loop: reclaims idle sessions on the clock (A6). ⭐Wakes every 0.5s, never `wait(60)`: on
    win32, when the main thread is stuck in a long wait, Ctrl+C only gets handled once it wakes up (⏳this is
    written to CPython's known behaviour, not measured on this machine)."""
    last = poked = time.time()
    while not stop.wait(0.5):
        if time.time() - poked >= AWAKE_EVERY_S:
            poked = time.time()
            bridge.awake.tick()
        if time.time() - last >= GC_EVERY_S:
            last = time.time()
            try:
                n = bridge.sessions.gc_idle()
            except Exception as e:      # a clock-driven reclaim blowing up must never take the whole bridge down with it (it is not any one request)
                log("❌ error reclaiming sessions on the clock (the bridge keeps running): %s: %s" % (type(e).__name__, e))
                continue
            if n:
                log("reclaimed %d persistent session(s) idle for more than %ds" % (n, SESSION_IDLE_S))


SESSION_WORKDIR_RE = re.compile(r"[0-9a-f]{16}-[0-9a-f]{8}")


def sweep_work() -> int:
    """I-1: remove `work/` entries a session left behind with no chance to clean itself up (on win32, `cmd_stop`'s
    hard kill means `cmd_run`'s `finally: bridge.stop()` never runs, so `_drop`'s `shutil.rmtree` never does either
    — every `stop` used to leave one directory per still-alive session, `system.txt` and all).
    ⚠️Only ever call this next to `sweep_orphans()` (the two spots where no other bridge can be using this
      `SCV_HOME`): removing one out from under a bridge that is still running would corrupt a live session.
    🔴Matched only by the exact shape `_workdir` builds, `[0-9a-f]{16}-[0-9a-f]{8}` — never "anything under work/
      that is not a known name": `work/probe` and a `doctor --live` run's `doctor-<hex>` / `canary-<hex>.txt` are
      shaped differently on purpose, so this never touches one running at the same moment.
    Logs one line with the count; a failure removing one entry is loud, never fatal (bookkeeping, not the action a
    stop/start exists to do)."""
    d = spath("work")
    gone = []
    for p in (d.iterdir() if d.is_dir() else ()):
        if p.is_dir() and SESSION_WORKDIR_RE.fullmatch(p.name):
            try:
                shutil.rmtree(p)
                gone.append(p.name)
            except OSError as exc:
                log("⚠️ could not remove a leftover session work directory %s: %s" % (p, exc))
    if gone:
        log("cleaned up %d leftover session work director%s under work/ (left behind by a hard stop): %s" % (
            len(gone), "y" if len(gone) == 1 else "ies", ", ".join(gone[:8])))
    return len(gone)


def cmd_run(args) -> int:
    cfg = load_config()     # ⭐L450: every path that goes on to sweep touches config first (it complains loudly if the disk is not writable, never let the bookkeeping step get there first and fail silently)
    state, rec = bridge_owner()
    if state == "alive":
        # 🔴this check has to run before the sweep: that bridge's CLIs are all in the registry with matching birth ids ⇒ a sweep would kill every one of them
        return _cmd_failed("another bridge (pid %s, port %s) is already using this SCV_HOME" % (rec["pid"], rec.get("port")),
                           "not starting a second one; to restart, stop it first:" + NL + self_cmd("stop"))
    sweep_tmp()
    if state in ("foreign", "unsure"):
        log("⚠️ bridge.pid cannot say whether the previous bridge is still around (%s) ⇒ not sweeping the previous "
            "leftovers this time: a sweep would kill the CLIs of a bridge that is still alive" % (
                "this file is not recognized" if state == "foreign" else "could not tell pid %s's birth id" % rec["pid"]))
    else:
        sweep_orphans()
        sweep_work()
    me = os.getpid()        # ⭐this bridge's own identity (written into bridge.pid), never the name used for the write buffer (that belongs only to _tmp_path)
    born = proc_start_id(me)
    if not born:            # 🔴A2: writing `None` in becomes `null` ⇒ the next `scv stop` would say "that is no longer the bridge" and delete the file, while the bridge is still running
        return _cmd_failed("could not tell this process's own birth id (%r) ⇒ not starting: it would go unrecognized when the bridge is stopped later" % (born,),
                           "try again; if it keeps happening, paste the last few lines of bridge.log")
    if unused_codex_home(cfg):
        log("⚠️ " + unused_codex_home(cfg))
    bridge = Bridge(cfg)
    try:
        port = bridge.start_local()
    except BridgeError:
        # ⭐`start_local` has already landed on disk (the port number plus the OS's original words). M-7: its class
        #   is `crashed` (∈ RETRYABLE) — nothing on the bridge-starting path reads `retryable` ⇒ never open a new
        #   category just for this (that would drag HTTP_STATUS / RETRYABLE / fix_hint along with it), but the
        #   wording still has to be right
        print(NL.join(["  ⇒ if something else is holding it, change the port in %s to a different number (retrying in a bit will not help);" % spath("config.json"),
                       "if a previous bridge is still open, look at it:", self_cmd("status"), "stop it:", self_cmd("stop")]), file=sys.stderr)
        return 1
    _atomic_write("bridge.pid", json.dumps({"pid": me, "born": born, "port": port,
                                            "ticket": getattr(args, "ticket", "") or "", "proxy_env": proxy_env(),
                                            "env": env_facts()}))   # ⭐names only (15c review I2: what doctor reports is the bridge's own copy)
    stop = threading.Event()
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, lambda *_: stop.set())
    try:
        paired = bridge.start_remote()
        log("scv %s is up (pid %d): the local API is on %s:%d; models %s; remote leg %s" % (
            VERSION, me, LOCAL_HOST, port, bridge.cat, bridge.remote.base if paired else "off (not paired)"))
        serve_until(bridge, stop)
    finally:
        bridge.stop()
        if (_read_pid_file() or {}).get("born") == born:    # only ever delete its own entry
            with contextlib.suppress(OSError):
                spath("bridge.pid").unlink()
        log("the bridge stopped (pid %d)" % me)
    return 0


def _log_tail(n: int = 5) -> str:
    try:
        with open(spath("bridge.log"), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8192))
            return NL.join(f.read().decode("utf-8", "replace").splitlines()[-n:])
    except OSError as exc:
        return "(could not read bridge.log: %s)" % exc


def cmd_start(args) -> int:
    cfg = load_config()
    port = int(cfg.get("port") or 8765)
    if our_health(port):
        print("already running (port %d)" % port)
        return 0
    state, rec = bridge_owner()
    if state == "alive":
        return _cmd_failed("the bridge at pid %s is still alive, but port %d is not answering (it is on %s)" % (rec["pid"], port, rec.get("port")),
                           NL.join(["it may be halfway through starting up (wait a few seconds and check again):", self_cmd("status"),
                                    "or config.json's port has changed ⇒ stop it, then start again:", self_cmd("stop"), self_cmd("start")]))
    ticket = secrets.token_hex(8)
    pid, note = spawn_detached([sys.executable, os.path.abspath(__file__), "run", "--ticket", ticket])
    end = time.time() + START_WAIT_S
    while time.time() < end:
        rec = started(port, ticket)
        if rec is None and proc_start_id(pid) == "":
            rec = started(port, ticket)                    # exiting and writing the pid file have no guaranteed order ⇒ check once more
            if rec is None:
                return _cmd_failed("the bridge process started and quit right away (pid %d); the last few lines of bridge.log are below" % pid,
                                   "see the lines above; running it in the foreground shows exactly where it died:" + NL + self_cmd("run"), _log_tail())
        if rec is not None:
            print("up: pid %d, port %d, log %s" % (rec["pid"], port, spath("bridge.log")))
            if note:
                print(note)
            return 0
        time.sleep(0.5)
    return _cmd_failed("did not come up within %d seconds (pid %d)" % (START_WAIT_S, pid),
                       "see %s; running it in the foreground shows exactly where it is stuck:" % spath("bridge.log") + NL + self_cmd("run"))


def cmd_stop(args) -> int:
    load_config()           # ⭐L450: see the same line in cmd_run
    state, rec = bridge_owner()
    p = spath("bridge.pid")
    if state == "none":
        # never sweep in this case: with no pid file there is no way to tell whether another bridge is using this SCV_HOME (the next bridge start will sweep)
        print("no bridge.pid: the bridge is not running, or it was not started with the start / run subcommand")
        return 0
    if state == "foreign":
        return _cmd_failed("bridge.pid is not recognized (%s) ⇒ this version cannot confirm whether pid %s is that bridge, so it was left untouched and the file was left in place"
                           % (rec.get("bad") or "the birth-id format was written by a different version", rec.get("pid")),
                           "stop it with the bridge version that wrote it; once you have confirmed that bridge is really gone (the line below says not running), delete %s:" % p
                           + NL + self_cmd("status"))
    if state == "unsure":
        return _cmd_failed("could not tell whether pid %s is still that bridge (retried) ⇒ left untouched, bridge.pid was left in place" % rec["pid"],
                           NL.join(["stop it again in a bit:", self_cmd("stop"), "check whether it still answers:", self_cmd("status")]))
    pid, born = rec["pid"], rec["born"]
    if state == "gone":
        print("pid %d is no longer that bridge (it disappeared without winding down) ⇒ only clearing bridge.pid" % pid)
    else:
        if os.name != "nt":
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)               # let it wind down on its own first (closing its CLI sessions)
            _wait_not(pid, born, STOP_WAIT_S)
        if proc_start_id(pid) == born:
            kill_pid_tree(pid)                             # win32 has no SIGTERM to send: taskkill /T takes the CLIs down with it too
            _wait_not(pid, born, STOPPED_WITHIN_S)
        now = proc_start_id(pid)                           # `""` or someone else's birth id (the pid was reused) = that bridge is gone
        if now is None or now == born:
            return _cmd_failed("pid %d did not stop (%s) ⇒ bridge.pid was left in place" % (
                pid, "could not tell its birth id" if now is None else "still alive"), "run it again:" + NL + self_cmd("stop"))
    if (_read_pid_file() or {}).get("born") == born:
        with contextlib.suppress(OSError):
            p.unlink()
    sweep_orphans()
    sweep_work()
    if state == "alive":
        print("stopped: pid %d" % pid)
        log("stop: stopped pid %d (a hard kill on win32: that bridge had no time to write its own 'stopped' line)" % pid)
    return 0


def _wait_not(pid: int, born: str, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end and proc_start_id(pid) == born:
        time.sleep(0.2)


def cmd_status(args) -> int:
    port = int(load_config().get("port") or 8765)
    health = our_health(port)
    if health is None:
        state, rec = bridge_owner()
        print("not running (no answer on port %d)%s" % (port, "; but the pid %s recorded in bridge.pid is still alive (it is on %s)"
                                               % (rec["pid"], rec.get("port")) if state == "alive" else ""))
        return 1
    print(json.dumps(health, ensure_ascii=False, indent=2))
    # ⭐/healthz must never carry a path or original words ⇒ the pasteable lines get printed here instead, on stderr (stdout keeps only that one JSON blob, 13b review M2)
    blocked = [f for f, i in (health.get("families") or {}).items() if isinstance(i, dict) and i.get("blocked")]
    if blocked:
        print("⇒ these families are blocked and not being reported: %s; see doctor for why and what to do:" % ", ".join(blocked) + NL + self_cmd("doctor"), file=sys.stderr)
    if (health.get("remote") or {}).get("state") == "too_old":
        print("⇒ the remote leg refused this version; update (no arguments = use the pair the dispatcher gave in its last hello):" + NL + self_cmd("update"), file=sys.stderr)
    return 0


def cmd_token(args) -> int:
    print(load_config()["local_token"])
    return 0
