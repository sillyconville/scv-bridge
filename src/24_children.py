_children_lock = threading.Lock()
PROBE_MAX_SECONDS = 10   # a run_cli that has not registered past this long is not a probe, it is a real live process ⇒ complain


def _row_ok(c) -> bool:
    """The shape of one row in the registry. ⚠️Two traps: `isinstance(True, int)` is true ⇒ `pid: true` would slip
    through; `family` is required too — a consumer reaches straight for `c["family"]`, and leaving it out just
    pushes a KeyError downstream."""
    return (isinstance(c, dict) and isinstance(c.get("pid"), int) and not isinstance(c.get("pid"), bool)
            and isinstance(c.get("born"), str) and isinstance(c.get("family"), str))


_children_warned: set = set()


def children() -> list:
    """⭐Valid JSON is not the same as the right shape: a hand-edited table, or one an old version wrote, can blow
    up the whole startup over one row missing a key.
    Bad rows are skipped and complained about, good rows still get used — on the sweep path, managing fewer is
    better than managing none.
    🔴The same cause complains only once: this function gets called once per `/healthz` hit, and that is the one
      endpoint that needs no token ⇒ once the table is broken, one unauthenticated poll can rotate real
      diagnostics out at the 4 MiB pace (measured: 5 polls = 5 lines). This is the whole bridge's one and only
      path where unauthenticated input drives a write to local disk. Never turn this into a cache: that would let
      the reading go stale."""
    good, why, msg = _children_parse()
    if why:     # wrong needs to be loud: a broken table means the last batch of orphans is never reclaimed, never swallow it silently
        _children_once(why, msg)
    return good


def _children_parse() -> tuple:
    """Registry → `(good rows, how it broke, the original words)`, how-it-broke ∈ ""/read/shape/rows. ⭐`children()`
    and the check before writing the table share this one ruler."""
    p = spath("children.json")
    try:
        rows = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except (OSError, ValueError) as exc:
        return [], "read", "children.json could not be read, the last batch of orphans can only be let go: " + str(exc)
    if not isinstance(rows, list):
        return [], "shape", "children.json is not a list (got %s) ⇒ discarding the whole thing" % type(rows).__name__
    good = [c for c in rows if _row_ok(c)]
    if len(good) != len(rows):
        return good, "rows", "children.json has %d row(s) shaped wrong, skipped (the %d good row(s) are still used)" % (len(rows) - len(good), len(good))
    return good, "", ""


def _children_once(why: str, msg: str) -> None:
    """The same kind of breakage complains only once (`why` is the category, never the whole sentence — the whole
    sentence's length would change with the row count, which would make deduping pointless)."""
    if why not in _children_warned:
        _children_warned.add(why)
        log(msg)


def _children_save(rows: list) -> None:
    """⭐Atomic write: the entire reason this file exists is to survive an abnormal death ⇒ getting killed mid-write
    is exactly the scenario it has to handle.
    A bare write_text truncates the previous copy first; getting killed in that instant means the last batch of
    orphans is never reclaimed again, and not a sound is made about it.
    🔴When the copy on disk is not a fully-good table (unreadable / not a list / has bad rows) ⇒ move it aside to
      `children.json.bad` as-is before writing: the old code only complained once when it could not be read, and
      the next time the table got written (`sweep_orphans` has two production call sites: the next `scv start` /
      `scv stop`) the bad copy got overwritten and the evidence was gone (an earlier review, M1).
      ⭐The move happens on the write side, never the read side: whoever writes already holds `_children_lock`,
      while the readers include the token-free `/healthz` and the facts-only doctor — neither should touch disk,
      and neither should race the writer to rename the same file."""
    _, why, msg = _children_parse()
    if why:
        _children_keep_bad(msg)
    _atomic_write("children.json", json.dumps(rows))


def _children_keep_bad(msg: str) -> None:
    """⭐Keeps only the most recent copy (overwrites the previous .bad): bounded on disk; every move logs one line
    to bridge.log naming the path, so the history of past moves lives there.
    ⚠️When it cannot be moved (another process has it open) ⇒ complain, and write anyway: stopping the registry
    altogether over one bad copy (every CLI started after that can no longer register) would be worse."""
    bad = spath("children.json.bad")     # ⭐the path is written at the front of the message: `log()` truncates from the tail when it is too long
    try:
        os.replace(spath("children.json"), bad)
    except OSError as exc:
        log("⚠️ the registry is broken, tried to move it to %s first to keep as evidence, could not move it (%s) ⇒ it will be overwritten by the new table any moment, its content will not survive. How it broke: %s" % (bad, exc, msg))
        return
    log("⚠️ the registry is broken ⇒ moved the old copy to %s as-is before writing the new table (kept as evidence; breaking again will replace this one). How it broke: %s" % (bad, msg))


def child_add(pid: int, family: str) -> bool:
    """⭐Returns False = this process never made it into the registry, the bridge cannot manage it ⇒ the caller must
    clean up after it itself (kill it and complain), never treat it as nothing happened.
    On POSIX this only accepts a pid that is "the leader of its own process group": sweep kills the whole group
    with killpg, and taking in an ungrouped pid means the next sweep takes the bridge's own group down with it too
    — and the suppress(OSError) inside kill_pid_tree would swallow that without a sound.
    Windows does not check this: taskkill /T finds the tree by parent-child relationship there, this shape does
    not exist on that side.
    Never turn this into raising an exception: the call site sits right after a successful spawn, and raising
    would drop the caller into a state of "the process is up but never registered".
    ⚠️But that is not a promise that it never raises: the `_children_save` call at the very end does not swallow a
      single exception ⇒ callers have to handle both shapes (returning `False` and raising `OSError` both mean "did
      not register"). Never try/except it away here: swallowing that original message would leave the caller
      holding nothing but an unexplained `False`. 📎 NOTES.md::child-add-contract"""
    if os.name != "nt":
        try:
            leader = os.getpgid(pid) == pid
        except OSError as exc:
            log("refusing to register pid %d (%s): could not find its process group: %s" % (pid, family, exc))
            return False
        if not leader:
            log("refusing to register pid %d (%s): it is not the leader of its own group ⇒ new_session_kw() was skipped when it was started. "
                "Registering it would let the next sweep killpg the bridge's own group down with it" % (pid, family))
            return False
    born = proc_start_id(pid)   # ⚠️computed outside the lock (on POSIX it starts a `ps`)
    if not born:
        # ⭐the two kinds of "nothing" are said apart: it is already gone (died right after starting) / could not
        #   tell (retried once). Used to be the same sentence.
        log("refusing to register pid %d (%s): %s ⇒ registering it would never be swept anyway (sweep's test is whether the birth id still matches)"
            % (pid, family, "it is already gone (exited right after starting)" if born == "" else "could not tell its birth id (retried once)"))
        return False
    with _children_lock:
        _children_save(children() + [{"pid": pid, "born": born, "family": family}])
    return True


def child_remove(pid: int) -> None:
    with _children_lock:
        rows = children()
        kept = [c for c in rows if c.get("pid") != pid]
        if len(kept) == len(rows):   # settling an account that was never in the table ⇒ either a double removal, or the registration was refused and nobody noticed
            log("⚠️ child_remove did not find pid %d: it was not in the registry to begin with" % pid)
        _children_save(kept)


SWEEP_STRIKES = 3   # how many sweeps a row whose birth id could not be told gets to stick around (see `sweep_orphans`)


def sweep_orphans() -> list:
    """Sweeps last run's leftovers at startup. Only kills when the birth id still matches. Returns the pids
    actually killed.
    ⚠️Only call this after confirming no other bridge is using this SCV_HOME (`cmd_run` / `cmd_stop` both go
      through `bridge_owner()` first): a live bridge's own CLIs are all in this same table too, birth ids and all
      ⇒ one sweep would kill every one of them.
    🧑‍⚖️A row that could not be told (`None`) is never killed and never forgotten: it used to be silently dropped
      when the table was cleaned. Now it stays in the table to be re-checked next time, complaining once each
      time, and only lets go after `SWEEP_STRIKES` consecutive sweeps (starting the bridge and `scv stop` each
      count as one; the count lives in that row's `strikes`) still could not tell.
      ⚠️The complaint never says "please end it by hand": on a non-admin machine, `None` is mostly the pid having
      been recycled by the system to a process we have no permission to inspect (the re-review addendum's survey:
      all 143 `None`s were access denied) — going after that pid would kill the wrong process.
    ⭐The `_children_save` at the end is bookkeeping, never the action itself (whatever needed killing is already
      killed above) ⇒ if it cannot be written, complain and move on, never blow up the startup (the truly fatal
      "state directory is not writable" gets hit by `load_config` on that same startup path anyway — never let
      bookkeeping beat it to the punch).
    ⚠️Inside the lock is N × (a birth id, plus maybe one `kill_pid_tree`): the former is microsecond-scale on
      win32, the latter is up to 15s worst case ⇒ only call this right before starting the bridge / right after
      stopping it."""
    killed, old, unsure, keep = [], [], [], []
    with _children_lock:
        for c in children():
            born = c.get("born") or ""
            if born and not birth_known(born):
                old.append(c)            # a format an old version wrote (win32's .NET Ticks): never killed, never compared
                continue
            now = proc_start_id(int(c["pid"])) if born else ""
            if born and now == born:
                kill_pid_tree(int(c["pid"]))
                killed.append(int(c["pid"]))
            elif now is None:
                n = c.get("strikes")
                c = dict(c, strikes=(n if isinstance(n, int) and not isinstance(n, bool) else 0) + 1)
                unsure.append(c)
                if c["strikes"] < SWEEP_STRIKES:
                    keep.append(c)
        try:
            _children_save(keep)
        except OSError as exc:
            log("⚠️ swept, but could not write the registry back (%s) ⇒ the next sweep (starting or stopping the bridge) will scan the same batch again (a birth id mismatch is never a false kill)"
                % exc)
    if unsure:
        log("⚠️ %d row(s) in the registry could not have their birth id told (retried) ⇒ not killed: %s. The CLIs the bridge starts run as the same user as the bridge, so normally this can always be told "
            "⇒ most likely this pid has been recycled by the system to a process we have no permission to inspect (never end it by pid); "
            "it stays in the table to be re-checked next sweep (starting or stopping the bridge), and stops being tracked after %d misses in a row" % (len(unsure), "; ".join(
                "pid %s (%s, attempt %d%s)" % (c["pid"], c["family"], c["strikes"],
                                            ", no longer tracked" if c["strikes"] >= SWEEP_STRIKES else "")
                for c in unsure), SWEEP_STRIKES))
    if old:
        # 🔴the table written back above does not have them any more ⇒ this is the last time anyone will remember
        #   them. If the message did not carry the pid, an orphan that might genuinely be left over from the
        #   previous version would be silently forgotten, with nobody left able to go clean it up (re-review
        #   addendum two, M-5).
        # ⚠️"check the process name first" is not a formality: the system recycles pids to other processes,
        #   killing by pid alone can kill the wrong one.
        log("⚠️ children.json has %d row(s) with an old-format birth id (written by the previous version, this one does not recognize it) ⇒ not swept, "
            "already removed from the table: %s. If those pids are still running that same CLI (check the process name first: pids get recycled by the system to other processes), "
            "please end them by hand" % (len(old), "; ".join("pid %s (%s)" % (c["pid"], c["family"]) for c in old)))
    if killed:
        log("⚠️ cleaned up CLI child processes left over from last time: %s" % killed)
    return killed


def run_cli(argv: list, input: bytes | None = None, cwd=None, timeout=None, env=None,
            family: str | None = None) -> subprocess.CompletedProcess:
    """One single-shot child process, with a timeout that actually takes effect: kill the whole tree first, then
    clean up, and the cleanup itself is time-limited (a process outside the tree that still holds the pipe must
    never be allowed to hang us too).
    ⭐Only goes into the registry when `family` is given (paired with the settling of accounts in `finally`).
      Leaving out family = this tree never enters the registry, and if the bridge dies it becomes an orphan nobody
      can find ⇒ only ever use this for a second-scale probe (the `--version` kind).
    🔴"Only for probes" relies on people remembering ⇒ the runtime gate below backs it up: the test is how long
      this path can run — a long timeout is a real live process, and a real live process must be registered.
      📎 NOTES.md::run-cli-probe-gate
    ⭐`env` defaults to `child_env()` (15c review I1: it used to default to `None` = inherit as-is, and leaving out
      `env=` silently went back to before session variables were stripped)."""
    env = child_env() if env is None else env
    if family is None and (timeout is None or timeout > PROBE_MAX_SECONDS):
        # 🔴`timeout is None` means no upper bound at all, and that happens to be this function's own default value
        #   ⇒ the easiest misuse to write is exactly the worst kind.
        # ⚠️the wording has to match what caused it: for `timeout=None`, the usual real fix is "give it a
        #   second-scale timeout" ⇒ saying only "please pass family" would shove a real probe into the registry and
        #   pay for a birth id for nothing (microsecond-scale on win32 now, one `ps` on POSIX)
        log("⚠️ run_cli was not given a family, yet is waiting %s: this tree is not in the registry, and if the bridge dies it becomes an orphan nobody can find. "
            "Anything over %s second(s) is not a probe ⇒ please pass family; if it really is a probe, give it a second-scale timeout instead"
            % ("an unbounded time" if timeout is None else "%s second(s)" % timeout, PROBE_MAX_SECONDS))
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=cwd, env=env, **new_session_kw())
    registered = False
    if family is not None:
        try:
            registered = child_add(proc.pid, family)   # it is necessarily the group leader: new_session_kw() is already applied above
        except OSError as exc:
            # 🔴this line sits after Popen and before try: `_children_save` does not swallow a single exception
            #   (disk full / permissions / SCV_HOME deleted), and letting it pass through means this tree has no
            #   one left to communicate with it or kill_tree it, and the caller cannot even get its pid ⇒ exactly
            #   the shape this file exists to prevent.
            log("⚠️ writing the table failed while registering pid %d (%s): %s" % (proc.pid, family, exc))
        if not registered:
            # never ignore that False (that is child_add's own contract). Chosen here: complain but keep running,
            # never kill it — killing a real live process over one bookkeeping failure is worse than letting it
            # run unregistered.
            log("⚠️ the tree run_cli started never made it into the registry (pid %d, %s): it keeps running, but the bridge cannot find it if it dies" % (proc.pid, family))
    try:
        try:
            out, err = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            kill_tree(proc)
            with contextlib.suppress(subprocess.TimeoutExpired):
                # error = the original text (B21): whatever the CLI had already printed must never be thrown away on the timeout path
                exc.stdout, exc.stderr = proc.communicate(timeout=15)
            raise exc
    finally:
        if registered:   # ⭐settling the account happens in finally: the timeout path is exactly where it is needed most. Only settle the one that actually got registered
            try:
                child_remove(proc.pid)
            except OSError as exc:
                # never let this override a TimeoutExpired already in flight: the error category the caller sees would change completely (B21/B26)
                log("⚠️ failed to settle the account for pid %d, a dead pid will be left in the registry: %s" % (proc.pid, exc))
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)

