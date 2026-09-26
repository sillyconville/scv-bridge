def _one_line(text) -> str:
    """Text from outside (the CLI's stdout/stderr, an exception's `str(e)`) gets folded to one line before it
    crosses into our side.
    ⭐Folding is never truncation: `bridge.log` wants "one line, one entry", and B21 wants "the original words,
      not a character changed" — `splitlines()[0]` trades the second for the first, and the half that gets traded
      away is exactly the reason: node's crash puts the top of the stack in its first two lines, and the line that
      actually tells you how to fix it (`Error: Cannot find module …`) is the third one. Folding to one line lets
      both constraints hold at once.
    ⭐Fold at the border where it comes in: fixing it at the border where it goes out just leaves the next call
      site short again.
    ⚠️Never touch text that was already one line — otherwise "folding" and "mangling the original words" stop
      being distinguishable."""
    s = str(text)
    if NL not in s and chr(13) not in s:
        return s
    return " ⏎ ".join(x.rstrip() for x in s.splitlines() if x.strip())


# C0 / DEL / C1 + U+2028 / U+2029 (line/paragraph separators: Cc class, `str.splitlines()` still breaks a line at
# them ⇒ `_log_tail()` splits one line into two, D24)
CTRL_CHARS = re.compile("[" + chr(0) + "-" + chr(31) + chr(127) + "-" + chr(159) + chr(0x2028) + chr(0x2029) + "]")


def _no_ctrl(text: str) -> str:
    """Control characters become "backslash x two hex digits" (U+2028/U+2029 get "backslash u four hex digits",
    the same notation `JobLog._line` uses). ⭐`log()` — this one door — calls it: the other side can stuff ESC/BEL
    into an HTTP reason phrase, and writing that to disk as-is would let it manipulate the front-end terminal and
    `_log_tail()`'s echo (measured, Task 14 re-review 2). Newlines are already folded away by `_one_line` first
    (that part is never touched). ⭐The set = every line break `splitlines()` recognizes (other than the two
    `_one_line` already folds away): gate
    tests/test_97_docs.py::ControlCharsInBridgeLog::test_every_line_break_splitlines_knows_is_escaped"""
    return CTRL_CHARS.sub(lambda m: chr(92) + ("x%02x" if ord(m.group(0)) < 256 else "u%04x") % ord(m.group(0)), text)


LOG_CAP_BYTES = 4 * 1024 * 1024
LINE_CAP_BYTES = 2048


def _clip(text: str, limit: int = LINE_CAP_BYTES) -> str:
    """The truncation used at the border where things go to disk: over the limit, keep the first `limit` bytes and
    say plainly how much got cut.
    ⚠️This is not the same thing as `_one_line()`'s "folding is never truncation", and the two do not conflict
      either: that one covers the original words at the border where they come in (not a byte dropped, `raw` still
      goes back to the caller unchanged, B21); this one covers the copy that lands on disk — diagnosis wants the
      start of the original words (the error message is almost always at the front), not the model's entire reply.
    ⭐It is the precondition for the claim "every name on disk has an upper bound": the rotation gate measures the
      file that already exists, and puts zero constraint on the line currently being written ⇒ without a per-line
      cap, one line alone could punch straight through it.
    ⭐Cuts by bytes, but never cuts a character in half: it leaves 64 bytes for the marker, and
      `decode(..., "ignore")` drops the half-character left dangling at the tail ⇒ what goes out is always legal
      utf-8, and always <= limit. 📎 NOTES.md::clip-not-fold"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    head = raw[:limit - 64].decode("utf-8", "ignore")
    return head + "…(truncated %d bytes)" % (len(raw) - len(head.encode("utf-8")))


_append_locks_guard = threading.Lock()
_append_locks: dict = {}
_rotate_warned: set = set()   # 🔴keeps this by name, never a single global bool: otherwise once `bridge.log`
                              #   complains that one time, `jobs.log` gets zero complaints of its own (the lock
                              #   was already per-name, the flag just never caught up)


def _append_lock(name: str) -> threading.Lock:
    """One lock per file name — that is the invariant `_append_capped()` actually needs.
    🔴Never "serializing is on the caller": `log()` uses a module-level lock, `JobLog` uses one lock per instance
      ⇒ two `JobLog()`s are two locks hitting the same `jobs.log`, and each caller satisfies that sentence on its
      own. The result is one stale rotation silently overwriting a whole generation — no error, no missing field.
    ⭐Fixed order outer to inner (the caller's lock, then this one), the inner never reaches back for the outer ⇒
      no deadlock.
    ⭐This table has at most `len(WRITES)` rows ⇒ it never grows. 📎 NOTES.md::one-lock-per-name"""
    with _append_locks_guard:
        lock = _append_locks.get(name)
        if lock is None:
            lock = _append_locks[name] = threading.Lock()
        return lock


def _append_capped(name: str, line: str) -> None:
    """The one write door for an append-only log: past the cap, move the whole thing to `<name>.1`, then append
    this line.
    ⭐The cap is written in exactly one place, never copied again in a second spot (`tests/test_60_joblog.py::OnePlace`
      pins there being only this one function that opens in append mode). This family has only two members,
      `bridge.log` / `jobs.log`, and both are driven by network-side input — the rate the other side sends at is
      never ours to control.
    ⭐As long as rotation succeeds, every name on disk stays < 2×(LOG_CAP_BYTES + LINE_CAP_BYTES), through rotation
      (here) plus the per-line cap (`_clip`) holding at the same time. ⚠️"As long as rotation succeeds" is not a
      throwaway phrase: while another process has this file open it will keep growing, and that gets one loud
      complaint.
      2 bytes are left per line for the newline (`open(..., "a")` turns into CRLF on win32) ⇒ this cuts at
      `LINE_CAP_BYTES - 2`.
    🔴This cap protects the byte count, never the information: rotation guarantees disk stays bounded, but a
      hot loop of our own making (say, the remote leg redialing at 45 Hz with zero progress) can squeeze every
      other line out of rotation within seconds ⇒ "disk filled up" gets blocked, "the log got washed out by our
      own noise" does not. ⇒ a caller that will write to disk at high frequency has to throttle or dedupe itself,
      never expect this to do it.
    ⭐Whether rotation went fine or not has to be visible either way: success leaves one line at the very start of
      the new generation (never through `log()` — it is already holding `_log_lock` out there, which would mean
      recursion plus deadlock), failure gets one loud complaint. ⇒ this mechanism is never allowed to be mute.
      📎 NOTES.md::b4-rotation"""
    p, prev = spath(name), spath(name + ".1")   # ⭐the two names clear the gate together: forgetting to register
    with _append_lock(name):                    #   `.1` in WRITES blows up on the very first line, not on the day it hits 4 MiB (by then the scene is far from the cause)
        mark = ""
        try:
            if p.stat().st_size >= LOG_CAP_BYTES:
                os.replace(p, prev)
                mark = "⤶ the previous generation filled up (%d bytes), moved to %s.1, this one starts here" % (LOG_CAP_BYTES, name)
        except FileNotFoundError:
            pass                                # no such file yet ⇒ nothing to rotate, and that is not an error
        except OSError as exc:                  # could not move it: complain once per name, never flood the screen and never complain only about the first one
            if name not in _rotate_warned:
                _rotate_warned.add(name)
                print(name + " cannot be rotated, it will keep growing: " + str(exc), file=sys.stderr, flush=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write((_clip(mark, LINE_CAP_BYTES - 2) + NL if mark else "") + _clip(line, LINE_CAP_BYTES - 2) + NL)


_log_lock = threading.Lock()
_log_warned = False


def log(msg: str) -> None:
    global _log_warned
    # ⭐the fallback gate is built on the one exit: this is the only place in the whole file that writes to
    #   bridge.log ⇒ folding it here means whoever appends another multi-line one later cannot escape it (never
    #   count on every call site remembering to fold — the next call site is the next slot).
    # 🔴one cap has to cover every exit for this line: `_append_capped` only protects the copy on disk, and the
    #   `print(..., file=sys.stderr)` below takes the copy that never passed through `_clip` ⇒ the same cap was
    #   only half enforced (measured: one request came back with 200,000 characters of stderr).
    #   ⇒ cut once, right here, so both exits get the same line.
    #   ⚠️the cut inside `_append_capped` stays as it is: it is that file's one write door, and the cap must never
    #   be left to the caller's memory.
    line = _clip(time.strftime("%Y-%m-%d %H:%M:%S ") + _no_ctrl(_one_line(msg)), LINE_CAP_BYTES - 2)
    with _log_lock:
        try:
            _append_capped("bridge.log", line)
        except OSError as exc:   # wrong needs to be loud: a log silently vanishing is three tables all green. one complaint is enough, never flood the screen
            if not _log_warned:
                _log_warned = True
                print("could not write to bridge.log: " + str(exc), file=sys.stderr, flush=True)
    print(line, file=sys.stderr, flush=True)


def decode(raw: bytes) -> str:
    """On Windows, going through a cmd pipe occasionally spits out bytes in the console code page: try strict
    utf-8 first, fall back to gbk on failure."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", "replace")

