CLAUDE_STALL_S = 30     # claude has a thinking_delta heartbeat at most every ≤2s while it thinks ⇒ 30s of nothing means it is truly stuck
CODEX_STALL_S = 90      # codex has zero events while it thinks; too tight a leash misjudges a normal slow answer as stuck
FIRST_TOKEN_S = 45      # the longest wait for "there is a heartbeat but no content yet"; the dispatcher can override it via opts.first_token_timeout
CLOSE_GRACE_S = 10
# The handshake (initialize/thread/start) codex does on its own has its own cap. ⚠️This number is not one of the
# plan's four windows: it is a pure fallback Task 7 set on its own — on a real CLI those two calls are
# millisecond-scale, and 60s is only there so a stuck one has some ceiling, never a threshold that was measured.
CODEX_HANDSHAKE_S = 60


def _rpc_error(err) -> str:
    """JSON-RPC's `error` → the one sentence of original words handed to a human. There is only one implementation
    allowed anywhere both families share this same reasoning.
    ⚠️`err` is not guaranteed to be an object: `.get` called directly on a bare string raises an `AttributeError`,
      which is not a `BridgeError`, logs no line at all, and picks exactly the "something has already gone wrong"
      path to blow up in — wiping out the CLI's original words entirely.
    ⭐Takes `message`, never `str(the whole object)`: the latter shows the user a Python dict's repr
      (`{'code': -32600, 'message': '…'}`), which shortchanges B21's "error = the original text" right there.
      ⚠️It does not affect classification (`classify` matches by substring, and the original words are still
      inside the repr) ⇒ all three tables stay green, and only a human reading it would notice.
    ⭐`code` must never get dropped: it is the machine-readable half of this error, folded into the same line.
    ⚠️When even `message` is missing, the whole object is kept as-is (B21) — never make up a sentence to fill it
      in."""
    if not isinstance(err, dict):
        return str(err)
    msg = err.get("message")
    if not msg:
        return str(err)
    code = err.get("code")
    return "%s (code=%s)" % (msg, code) if code is not None else str(msg)


USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
              "cached_input_tokens", "reasoning_output_tokens")
CODEX_USAGE_KEYS = {"inputTokens": "input_tokens", "cachedInputTokens": "cached_input_tokens",
                    "outputTokens": "output_tokens", "reasoningOutputTokens": "reasoning_output_tokens"}
USAGE_MAX = 10 ** 12          # one trillion tokens: if this ever shows up it is the CLI or our own arithmetic gone wrong, never a real conversation turn
_usage_warned: set = set()


def _usage_once(family: str, key: str) -> bool:
    """Only complain once per family, per field. ⚠️Complaining one line per turn would let a new field that is
    "present on every turn" flood `bridge.log`, which we just capped — the very thing that cap exists to prevent."""
    if (family, key) in _usage_warned:
        return False
    _usage_warned.add((family, key))
    return True


def usage_numbers(raw, family: str) -> dict:
    """The usage a CLI gives us ⇒ keep only the numeric values on the whitelist. ⭐The cleaning point sits at the
    driver layer, never at the point that writes to disk.
    ⭐Why: the same `usage` also goes into the wiring layer's response, and into the remote leg ⇒ cleaning it only
      at the point that writes to disk still leaves those two paths sending whatever the CLI stuffed in straight
      out onto the network, as-is. Clean untrusted data at the layer where it comes in the door, and every
      downstream consumer benefits.
    ⭐The whitelist is allowed to exist in exactly one place: both families share this one function, never write it
      twice, once per family — fixing one copy would leave the other one missed.
    ⭐What gets pinned down is the value, not just the key: even on the codex side, where we build the key
      ourselves by name, the value is still copied over as-is.
    ⚠️A dropped field must be loud, never silent (the day the CLI adds a useful field, dropping it silently means
      nobody ever finds out), but only complain once per field — complaining one line per turn would let a new
      field that is "present on every turn" flood `bridge.log`, which we just capped.
    ⚠️When complaining, report only the field name, never the value, and truncate the name too: a hostile call can
      turn the key name itself into a prompt.
    🔴Has to swallow any type at all: `(raw or {})` only blocks falsy values ⇒ a truthy non-dict raises an
      `AttributeError` on the spot, and the call site sits outside `_fail` with no try around it ⇒ a conversation
      turn that would otherwise have succeeded turns into an exception with no klass that never gets logged.
      ⚠️The structural gate only scans for `raise`, and is blind by design to an exception nobody wrapped.
    🔴A numeric type is not the same thing as a number that can be written to disk: `10**4000` is an int, and one
      field alone can push the whole line past the single-line cap ⇒ magnitude has to be blocked too (`USAGE_MAX`;
      it blocks `inf`/`nan` along with it). 📎 NOTES.md::usage-whitelist"""
    if not isinstance(raw, dict):
        if _usage_once(family, "<" + type(raw).__name__ + ">"):
            log("%s's usage is not a dict (it is %s) ⇒ the whole thing was thrown away" % (family, type(raw).__name__))
        return {}
    out, dropped = {}, []
    for k, v in raw.items():
        if k in USAGE_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) < USAGE_MAX:
            out[k] = v
        else:
            dropped.append(str(k)[:32])
    fresh = sorted(x for x in dropped if _usage_once(family, x))
    if fresh:
        log("%s's usage had field(s) that did not make it into the log (not on the whitelist, or the value is not "
            "a number that can be written to disk): %s" % (family, ", ".join(fresh)))
    return out


def _fail(klass: str, raw: str, family: str = "") -> BridgeError:
    """The one exit for the driver layer's failure paths: write one line to bridge.log, then hand the exception
    back to the caller (the global rule that a failure must be loud).
    ⭐The write sits here, never in `error_body()`: that one runs on every single request, and it would flood the
      screen even for a healthy bridge.
    ⭐The line must carry both `klass` and the original words together: `classify()` returning `unknown` is the
      only signal that the pattern list has gone stale.
    ⚠️Folded, never truncated: the CLI's original words are often several lines, and this folds them explicitly
      once — the rule is "outside text gets folded at the border where it comes in". `raw` itself must reach the
      caller without a single character changed (B21).
    🔴But the copy that lands on disk does have a cap (`_clip`), and that does not conflict with the rule above:
      on the claude side `raw` can be the entire model answer ⇒ one failed turn could write the whole thing into
      `bridge.log`, exactly the thing B30 ("never log the content") exists to prevent. B21 is satisfied by the copy
      handed back to the caller; keeping just the head of the copy on disk is enough for diagnosis.
      📎 NOTES.md::b21-vs-b30"""
    log("%s: this turn failed (%s): %s" % (family or "?", klass, _one_line(raw)))
    err = BridgeError(klass, raw, family)
    err.logged = True   # ⭐this one's account has already been recorded: the layer above (HTTP/remote leg) must never write a second line
    return err


class _Pipe:
    """One long-lived CLI child process plus a read loop. The two gates are two ways to die: stall = not a single
    byte comes out; first_token = there is activity but no content."""

    def __init__(self, argv: list, cwd, env: dict, family: str):
        self.family = family
        self._out_thread = self._err_thread = None   # ⭐set before Popen: the failure path below also has to go through `_close_pipes()`
        # 🔴"Did we close this ourselves" must be an explicit flag, never guessed from the exception type: the
        #   whole `OSError` family comes free (an fd closed by someone else, EMFILE, an I/O error the driver layer
        #   raises are all it) ⇒ guessing by type would let a read failure that is the bridge's own get swallowed
        #   into a quiet sentinel, walk down the EOF path, kill a perfectly healthy child, and then build a
        #   confident, specific, wrong conclusion out of a stale stderr tail — with no real reason recorded
        #   anywhere.
        # ⭐Uses `threading.Event`, never a plain bool: `set()`/`is_set()` go through a lock internally ⇒ that
        #   gives a real happens-before edge (a plain bool not blowing up today is because the implementation
        #   happens not to reorder it, never because the semantics forbid it).
        # 📎 NOTES.md::pipe-closing-flag
        self._closing = threading.Event()
        self._read_error = None
        try:
            self.popen = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          cwd=str(cwd), env=env, **new_session_kw())
        except (OSError, ValueError) as e:
            # ⚠️`ValueError` is not redundant: `Popen` raises it when argv/cwd/env carries a NUL, and catching only
            #   OSError would let those cases escape bare. The outside input that used to reach it (config.json's
            #   codex_home) went away with Task 13c ⇒ today this is defense in depth (⏳ not audited for other
            #   paths).
            # ⛔`TypeError` (a non-string slipped into argv) is deliberately not caught: that is our own bug, and
            #   wrapping it into a retryable crashed would be passing off a bug as a retryable failure.
            #   📎 NOTES.md::popen-raises
            raise _fail("crashed", "could not start the %s CLI: %s" % (family, e), family)
        self.pid = self.popen.pid
        self._listed = False
        # 🔴From this line until `born = True`, the process is already running while this `_Pipe` has not been
        #   handed to anyone yet ⇒ any exception escaping this stretch means a long-lived CLI nobody recognizes and
        #   nobody can kill. ⭐The cleanup goes through `finally` plus a flag, never `except <a family we
        #   recognize>` (the shape is "any exception leaks it").
        #   Gate: tests/test_30_drivers.py::SpawnWindow::test_any_exception_after_spawn_buries_the_child
        #   📎 NOTES.md::spawn-window
        born = False
        try:
            try:
                listed, why = child_add(self.pid, family), "could not get into the registry"
            except OSError as e:
                # 🔴Letting this escape bare means the process is already running, was never registered, and
                #   nobody can kill it — the caller does not even get the pid.
                listed, why = False, "writing the registry table blew up (%s)" % e
            if not listed:
                # ⛔Do not copy `run_cli`'s "loud but keep going": that one takes a second-scale probe, this is a
                #   long-lived process that can live for a whole session.
                # ⚠️There is nothing to unregister on this path (`_listed` is still False ⇒ the `_unlist()` call
                #   below is a no-op) — never write a second cleanup path just for this case.
                raise _fail("crashed", "started the %s CLI (pid %d) but %s, and it has already been killed: "
                                       "leaving it running would make it an untraceable orphan the moment the "
                                       "bridge dies" % (family, self.pid, why), family)
            self._listed = True
            self._q: queue.Queue = queue.Queue()
            self._err: collections.deque = collections.deque(maxlen=40)
            # ⭐The read side (`stderr_tail`) and the write side (the pump thread) touch the same deque ⇒ the same
            #   lock. The reason for this lock is not "it would blow up today" (today's `"".join` runs entirely in
            #   C): it is ① under a no-GIL build it is no longer atomic; ② the next person to write
            #   `[x for x in self._err]` turns the race real immediately — and what it throws is an unclassified
            #   exception, one that picks exactly the "something has already gone wrong" error path to blow up in,
            #   wiping out the CLI's original words. 📎 NOTES.md::err-deque-race
            self._err_lock = threading.Lock()
            self._out_thread = threading.Thread(target=self._pump_out, daemon=True)
            self._out_thread.start()
            self._err_thread = threading.Thread(target=self._pump_err, daemon=True)
            self._err_thread.start()
            born = True
        finally:
            if not born:
                self.kill()   # ⭐one exit: kill the tree, unregister (only what was really registered), and the three parent-side fds

    def _read_failed(self, which: str, exc: Exception) -> None:
        """A pump thread's read blew up. This only counts as normal when we are the ones closing the pipe right
        now, otherwise it must be loud.
        ⭐The test is the explicit flag `self._closing`, never the exception type (the reason is in the block in
        `__init__`).
        ⚠️What gets written to disk is this one's own sentence, never `self._read_error`: the latter only records
          the first one ⇒ if both pumps blow up one after another, the two lines on disk would read identically,
          both saying stdout — a lying error message.
        ⚠️Carries the moment it happened: `_read_error` never gets cleared ⇒ without a timestamp it would be read
          as "just happened"."""
        if self._closing.is_set():
            return
        said = "reading %s's %s blew up (at %s): %r" % (self.family, which, time.strftime("%H:%M:%S"), exc)
        if self._read_error is None:   # ⭐keep only the first one: the rest are mostly its knock-on effects
            self._read_error = said
        log("⚠️ " + said + " (not the CLI exiting)")

    def _pump_out(self) -> None:
        try:
            for raw in iter(self.popen.stdout.readline, b""):
                self._q.put(raw)
        except (OSError, ValueError) as exc:
            # ⭐What this branch catches can only be a real read failure ⇒ hand it all to `_read_failed` to
            #   complain about; the `_closing` flag is left in only as a fallback for the POSIX path that has
            #   never been measured. 📎 NOTES.md::pipe-closing-flag
            self._read_failed("stdout", exc)
        finally:
            self._q.put(None)   # ⭐unconditional: this is the exact sentinel `read_until` recognizes

    def _pump_err(self) -> None:          # ⛔not draining stderr fills the pipe and stalls the CLI
        try:
            for raw in iter(self.popen.stderr.readline, b""):
                self._err_add(decode(raw))
        except (OSError, ValueError) as exc:
            # ⚠️This one is more silent than the stdout branch: a blown-up stderr pump just leaves the tail a bit
            #   shorter, and that tail is `classify()`'s only input ⇒ the sentence "stderr is empty" can itself be
            #   a lie.
            # ⭐Once this throws, this pump is gone and the tail is frozen forever (nothing anywhere restarts it),
            #   and when it dies between two turns the session is not dropped for it — that is a trade-off, not an
            #   oversight. 📎 NOTES.md::stderr-pump-death
            self._read_failed("stderr", exc)

    def _err_add(self, text: str) -> None:
        """Push one line into the stderr tail. ⭐The one and only write door: tests go through it too, never
        append bare anywhere else (a bare append skips the lock, and that race only ever shows up on the
        "something has already gone wrong" path, the hardest place to debug)."""
        with self._err_lock:
            self._err.append(text)

    def alive(self) -> bool:
        return self.popen.poll() is None

    def stderr_tail(self) -> str:
        with self._err_lock:
            return "".join(self._err)[-500:].strip()

    def mark_ok(self) -> None:
        """The waterline (OUT-1, the fix has already been ruled on): every "unambiguous success" clears the
        stderr tail to zero ⇒ whatever `classify` gets fed on the EOF path afterward is only what was added after
        this moment. 🔴Without clearing it, a piece of stale noise (measured on a real codex: a
        `codex_core::tools::router` ERROR line, `failed to connect to websocket … wss://…`; with no credentials it
        is `401 Unauthorized`) would get an unrelated crash afterward judged as `auth_required`, sending the user
        off to log in again. ⭐Changed for both families the same way (call sites: before both families' `turn`
        returns successfully, and when codex's handshake succeeds).
        ⚠️Bounded, not eliminated: a line written before success but only read by the pump after this moment is
        still left sitting there (the pump is asynchronous). 📎 NOTES.md::real-cli-stderr"""
        with self._err_lock:
            self._err.clear()

    def send(self, obj: dict) -> None:
        try:
            self.popen.stdin.write((json.dumps(obj, ensure_ascii=False) + NL).encode("utf-8"))
            self.popen.stdin.flush()
        except (OSError, ValueError) as e:
            raise _fail("crashed", "%s CLI's stdin is already closed: %s | %s" % (self.family, e, self.stderr_tail()),
                        self.family)

    def read_until(self, pred, timeout: float, stall, first_token, has_content, cancel) -> dict:
        deadline = time.time() + timeout
        quiet_until = time.time() + stall if stall else None
        content_at = time.time() + first_token if first_token else None
        while True:
            now = time.time()
            if cancel is not None and cancel.is_set():
                self.kill()
                raise _fail("cancelled", "the caller cancelled this turn", self.family)
            content_until = None if (content_at is None or has_content()) else content_at
            # 🔴Both gates are each held up by two mechanisms, never drop either one: ① checking the gate at the
            #   top of the loop holds up "the CLI arrives faster than the gate" (the queue always has something in
            #   it ⇒ `queue.Empty` never fires); ② computing `caps` from the gates holds up "the CLI goes
            #   completely silent" (when not one byte comes out, the top of the loop never gets a second turn).
            #   📎 NOTES.md::two-gates-two-mechanisms
            due = [(t, msg) for t, msg in ((deadline, "the whole turn timed out after %gs" % timeout),
                                           (quiet_until, "not a single byte out for %gs (stuck)" % (stall or 0)),
                                           (content_until, "no first byte of content out for %gs" % (first_token or 0)))
                   if t is not None and now >= t - 0.01]
            if due:
                self.kill()
                raise _fail("timeout", self.family + ": " + min(due)[1], self.family)
            caps = [t for t in (deadline, quiet_until, content_until) if t is not None]
            if cancel is not None:
                caps.append(now + 0.25)
            try:
                raw = self._q.get(timeout=max(min(caps) - now, 0))
            except queue.Empty:
                continue
            if raw is None:
                self._err_thread.join(timeout=1)     # let the last few stderr lines land in the tail, or the error report cannot show the cause of death
                # 🔴Whoever Popens it reaps it: unregistering without reaping leaves it a zombie on POSIX (this
                #   case is invisible on win32). Also covers another case at the same time: stdout broke but it
                #   is still alive ⇒ kill the tree. 📎 NOTES.md::reap-your-own
                self.kill()
                if self._read_error:
                    # 🔴A read blowing up is not the CLI finishing talking ⇒ this case must never go through
                    #   `classify(tail)`: that would take a tail that was never read in full and make up a
                    #   confident upstream class for what is the bridge's own I/O failure. The class is pinned to
                    #   `crashed`, never classified; the original words carry both our read error and the stderr
                    #   tail together (B21).
                    #   ⚠️This has a cost (a real quota that coincides with a read failure loses its 429/401
                    #   upstream). 📎 NOTES.md::read-error-not-eof
                    raise _fail("crashed", "%s | %s" % (self._read_error, self.stderr_tail()), self.family)
                tail = self.stderr_tail() or "the CLI process exited (exit=%s), stderr is empty" % self.popen.poll()
                raise _fail(classify(tail, "crashed"), tail, self.family)   # ⭐unregistering has already been done by kill() and only once
            if stall:
                # ⭐`max`, never a direct assignment: a direct assignment would let the `api_retry` case below be
                #   wiped out by whatever frame immediately follows it (the shape "announce a backoff → emit one
                #   frame → go quiet for a long time" would still be misjudged; a 529 backoff of dozens of seconds
                #   plus the 30s window is a real window).
                quiet_until = max(quiet_until, time.time() + stall)
            s = decode(raw).strip()
            if not s.startswith("{"):
                continue
            try:
                ev = json.loads(s)
            except ValueError:
                continue
            if stall and ev.get("type") == "system" and ev.get("subtype") == "api_retry":
                quiet_until = time.time() + stall + (ev.get("retry_delay_ms") or 0) / 1000   # the CLI is backing off on its own, never kill this as stuck
            if pred(ev):
                return ev

    def _unlist(self) -> None:
        """Unregister, and only once.
        🔴`child_remove` complains "this pid was never in the registry" for a pid not in the table, and that line
          is the only signal that a registration attempt was rejected and nobody noticed ⇒ unregistering a second
          time would turn it into routine noise. ⛔Never rewrite this as "leave `_listed` set and unregister again
          next time": that would make that signal fire every time.
        🔴`child_remove`'s own `_children_save` call swallows no exception at all ⇒ this has to catch it here:
          what is usually flying on this path is some other exception (all three failure paths call `kill()` first
          and then `raise _fail(...)`) ⇒ letting it escape would wipe out the CLI's original words and class
          entirely (B21/B26 voided on the spot). 📎 NOTES.md::unlist-once"""
        if self._listed:
            self._listed = False
            try:
                # ⭐Catches only `OSError`, and that is not a missed case, it has been checked (that is the only
                #   family this path can raise).
                # ⚠️Never harden this by copying `ClaudeDriver.__init__`'s case (which specifically catches
                #   `ValueError`): that path's input comes from outside, this one is something we built ourselves.
                #   Which exceptions to catch is decided by where that path's input comes from, never by copying
                #   the neighbor. 📎 NOTES.md::unlist-once
                child_remove(self.pid)
            except OSError as exc:
                log("⚠️ unregistering pid %d failed, a dead pid will be left behind in the registry: %s" % (self.pid, exc))

    def _close_pipes(self) -> None:
        """The one and only release point for stdout/stderr (the failure path in `__init__`, `close()` and
        `kill()` all go through it).
        ⚠️stdin has a separate one: the `stdin.close()` call inside `close()` — that is the polite-close signal
          (letting the CLI read EOF and exit on its own), not a release, which is why it comes before this. Do not
          let a later reader get tripped up looking for "the one implementation" by the letter.
        ⭐Relies on GC, never on closing: that reference chain breaks the moment any exception's traceback or one
          debugging reference holds onto it. And each session holds 3 parent-side fds ⇒ on Linux the first thing
          hit is `RLIMIT_NOFILE` (default 1024), never memory — it is the hardest ceiling among the resources
          counted per session_id.
        ⚠️Waits for the pump threads to exit first (`join`), and if they never do, it simply does not close.
        📎 NOTES.md::close-pipes-blocks"""
        # 🔴The flag is raised after the join, never before: raising it before would swallow a real read failure
        #   that happens after the flag is raised but during the join. The test is simple: during the join we have
        #   not closed a single fd yet ⇒ whatever blows up in that stretch must be a real error, and must be loud.
        for t in (self._out_thread, self._err_thread):
            # ⚠️`t is not threading.current_thread()` guards against "calling `close()`/`kill()` from inside the
            #   pump thread itself". No path does that today ⇒ no test goes red while it is absent; never read it
            #   as "some path does call it this way".
            if t is not None and t is not threading.current_thread():
                t.join(timeout=1)
        self._closing.set()   # ⭐from this moment on, and only from this moment on, a pump read blowing up counts as "we closed it ourselves"
        # 🔴🔴Never close a pipe that still has a pump stuck on it: when the read end is stuck in `readline()` and
        #   `close()` is called, closing is exactly the call that gets blocked ⇒ that would stuff an unbounded
        #   block into what is supposed to be `kill()`'s bounded path. A skipped fd can only wait for GC ⇒ this
        #   must be loud.
        #   📎 NOTES.md::close-pipes-blocks
        for name, pipe, pump in (("stdin", self.popen.stdin, None),
                                 ("stdout", self.popen.stdout, self._out_thread),
                                 ("stderr", self.popen.stderr, self._err_thread)):
            if pipe is None:
                continue
            if pump is not None and pump.is_alive():
                # ⚠️Names which one: when both pumps are stuck at once this writes two lines, and without naming
                #   the pipe the two lines would read identically — leaving no way to tell from disk which fd was
                #   skipped (the same problem as the `_read_failed` case).
                log("⚠️ %s's %s pipe did not get closed: the pump is still stuck in readline (most likely a "
                    "grandchild process is holding the write end), closing it now would block us ⇒ this fd can "
                    "only be left for GC" % (self.family, name))
                continue
            with contextlib.suppress(OSError, ValueError):
                pipe.close()

    def close(self, grace: float = CLOSE_GRACE_S) -> str:
        with contextlib.suppress(OSError, ValueError):
            self.popen.stdin.close()
        try:
            self.popen.wait(timeout=grace)
            self._unlist()
            self._close_pipes()
            return "exited"
        except subprocess.TimeoutExpired:
            self.kill()
            return "killed"

    def kill(self) -> None:
        """⚠️The worst-case blocking time differs between the two platforms ⇒ the time the cancel/timeout gate
        takes to get back to the caller has to account for it:
          · win32: ≈32s = the `subprocess.run(timeout=15)` inside `taskkill /T /F`
            plus `proc.wait(timeout=15)` (two stretches, in series, both inside `kill_tree`), plus two
            `join(timeout=1)` calls;
          · POSIX: ≈17s = `os.killpg` is instant ⇒ only `proc.wait(timeout=15)` plus the two `join` calls are
            left.
        🔴The 0.26s on the happy path is not the ceiling (the previous version wrote it as "17s" using exactly
          that number); 32/17 is pieced together from "the parts that were measured plus the two 15s pulled out
          of reading the code", never read this as the whole thing having been measured. Underreporting it is
          just setting a trap for whoever picks this up next. 📎 NOTES.md::kill-time-budget"""
        kill_tree(self.popen)
        self._unlist()
        self._close_pipes()

