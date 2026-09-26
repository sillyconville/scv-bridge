RATE_WINDOW_S = 3600   # the remote leg's local rate-limit window; ⭐one number, allowed to live in exactly one place: the 429's `Retry-After` reads this same one
# The measured hit count of the two consumers (recounted after Task 11 wired up the remote leg, never file:line):
#   ①`allow_remote()`'s window — every single remote job goes through it (`RemoteLeg._job` is the production
#     caller) ⇒ a real consumer.
#   ②`Handler._answer_error`'s `Retry-After` — production hits are still 0: the only thing in the whole file that
#     can produce `local_rate_limit` is `RemoteLeg` (at two spots: the rate limit and the in-flight cap), and that
#     leg does not send HTTP headers. ⛔Never read this as "already in use". It is not dead code (the day the
#     local leg gets its own rate limit, it will need this immediately), but today only the tests feed it.


class JobLog:
    """One line of metadata per job. A code audit governs "what it can do"; this file governs "what it actually
    did".
    ⭐`write()`'s signature carries no parameter at all that could hold a prompt or an answer ⇒ "never log the
      content" is guaranteed by the signature, never left to the caller's good conscience
      (`tests/test_60_joblog.py::Log::test_signature_cannot_carry_text` asserts exactly that, the signature
      itself).
    🔴🔴"Never log the content" is not an absolute guarantee — the exceptions below are bounded, and the public
      description (README's "Where prompts and answers end up") is written to match this section exactly:
      ① On `bridge.log`'s failure paths, up to `LINE_CAP_BYTES` bytes of model content can land on disk (`_fail()`'s
         `raw` is the entire answer on the claude side) — we do not control the content, but it is model output,
         never something the other end stuffed in; when a request is refused, that line also copies a short
         fragment of the rejected value (the `repr(…)[:N]` spots, N is in the code);
      ② Every line of `jobs.log` has up to `LINE_CAP_BYTES // 8` bytes of caller-supplied free text (the remote
         leg's `job_id`) — this one the other end can stuff on purpose, because job_id comes from the request.
         `leg`/`klass` are closed sets; `model` is too, via `shown_model()` (a model name that has been reported,
         or a sentence carrying no original text saying "an unreported model" — Task 14 review I1: this used to
         copy whatever raw value the other end gave, verbatim).
      ⇒ What the signature guarantees is "the content has no path in", never "not a single byte, ever".
      A product promise that keeps one unwritten exception is more dangerous than having no such promise at all."""

    def __init__(self):
        self._lock = threading.Lock()
        self._remote: collections.deque = collections.deque()

    def allow_remote(self, per_hour: int) -> bool:
        """The remote leg's local rate limit, with a one-hour window (never a minute —
        `test_the_window_is_an_hour_not_a_minute` pins it from the "one still inside the window must still count"
        side; pinning only "an old one expires" would leave a 60-second window just as green).
        ⚠️Three things the wiring layer needs to know:
        ①The slot is spent before the call starts, and a failure never gives it back — giving it back would let a
          caller that always fails drive this machine's CLI an unlimited number of times.
        ②`per_hour` is an argument on every single call, while the window is state shared by this one instance ⇒
          when wiring it up, settle on "read cfg in exactly one place".
        ③The window lives only in memory, and a process restart wipes it clean. This is a premise, never a
          conclusion: the quota is only genuinely "hourly" as long as (a) nobody adds an automatic restart for it,
          and (b) the other side cannot crash it. ⛔The sentence "this is not an exploitable hole" holds today only
          because nobody has written the restart logic yet, never because of a protected property.
          ⚠️Once autostart is installed, it becomes "the hour since the last start" instead — crash once and the
          quota resets to zero; and since `jobs.log` is persistent while the window is volatile, seeing more than
          `per_hour` entries within the same calendar hour is not a bug.
          ⭐If you add a supervisor/autostart into this file, come back and reread this section — the conclusion
          has to be judged again."""
        now = time.time()
        with self._lock:
            while self._remote and self._remote[0] < now - RATE_WINDOW_S:
                self._remote.popleft()
            if len(self._remote) >= per_hour:
                return False
            self._remote.append(now)
            return True

    def write(self, *, leg, model, klass, usage, latency_s, ttfc, queued_ms, rebuilt, job_id="", cli_version="") -> None:
        """🔴`model` is a name, not an identity ⇒ tallying BYOK spend from this log would silently undercount it;
        `cli_version` is the cheapest patch for that spot (its shape is locked down by `_cli_version()` ⇒ "never
        log the content" was not loosened for it). ⚠️Log lines written before this one do not have this key; a
        reader must treat "this key is missing" as "unknown". 📎 NOTES.md::model-name-not-identity"""
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "leg": leg, "job_id": job_id, "model": model,
               "cli_version": cli_version, "klass": klass,
               "usage": usage, "latency_s": latency_s, "ttfc": ttfc, "queued_ms": queued_ms, "rebuilt": rebuilt}
        # 🔴Per-field truncation cannot rely on `_append_capped`'s fallback to cover it: truncating the whole line
        #   would cut the JSON into half a line, and `tail()` reading half a line just throws ⇒ a 41 KB `job_id`
        #   (it too comes from the network) can kill the read side of the entire audit log.
        # ⭐Cuts every str field down one at a time, never picking "which fields are safe": that kind of list goes
        #   stale the moment the next field is added.
        row = {k: _clip(v, LINE_CAP_BYTES // 8) if isinstance(v, str) else v for k, v in row.items()}
        line = self._line(row)
        over = len(line.encode("utf-8")) - (LINE_CAP_BYTES - 2)
        if over > 0:
            # 🔴The cut above cannot reach `usage` (it is a dict, and its values are numbers) ⇒ one big number
            #   alone can push the whole line past the cap, and then `_append_capped` cuts it in half. ⭐So this
            #   measures once after `json.dumps`, and if it is over, falls back to one minimal but valid line ⇒
            #   "every line is complete JSON" holds by construction, not by relying on the unwritten premise that
            #   "every other field is a closed set".
            # ⭐The skeleton is cut by characters, never by `_clip`: `json.dumps`'s escaping expands by at most 6x
            #   ⇒ (16+16+32)×6 plus ts plus this sentence stays well clear of the cap, with no dependence on any
            #   field's content. 📎 NOTES.md::row-is-always-valid-json
            line = self._line({"ts": row["ts"], "leg": str(row["leg"])[:16], "klass": str(row["klass"])[:16],
                               "job_id": str(row["job_id"])[:32],
                               "scv_note": "the whole line went %d bytes past the single-line cap ⇒ only the skeleton was kept" % over})
        with self._lock:
            _append_capped("jobs.log", line)

    # 🔴`json.dumps(ensure_ascii=False)` only escapes C0 ⇒ DEL/C1/U+2028/U+2029 inside a remote `job_id` land on
    #   disk as-is: a terminal chokes on C1 when a human `type`s this file, and `str.splitlines()` treats
    #   U+0085/U+2028/U+2029 as line breaks (re-review 3, independent check 6) ⇒ swap them for backslash, the
    #   letter u, then four hex digits (a legal JSON escape; `json.loads` restores it character for character;
    #   Chinese text stays just as readable). ⭐After `json.dumps`, before measuring the line length: what gets
    #   measured is exactly the line that lands on disk, and the skeleton line goes through this too.
    JSON_RAW = re.compile("[" + chr(127) + "-" + chr(159) + chr(0x2028) + chr(0x2029) + "]")

    @classmethod
    def _line(cls, row: dict) -> str:
        return cls.JSON_RAW.sub(lambda m: chr(92) + "u%04x" % ord(m.group(0)), json.dumps(row, ensure_ascii=False))

    def tail(self, n: int = 20) -> list:
        """⭐Reads across a rotation: reading only the current file would give a few lines short right after a
        rotation just happened, and "only 3 lines came back" looks identical to "there are only 3 lines in total"
        to whoever is reading the log — that is silently handing back a wrong number."""
        rows = self._read("jobs.log", n)
        if len(rows) < n:
            rows = self._read("jobs.log.1", n - len(rows)) + rows   # make up however many lines are short (0 short never reaches here)
        return rows[-n:]

    @staticmethod
    def _read(name: str, n: int) -> list:
        p = spath(name)
        if not p.exists():
            return []
        # Filter first, then slice: the other way around, one blank line or marker line mixed into the tail would
        # hand back fewer than asked for.
        # ⭐Skips the rotation marker (the line `_append_capped` writes, which is not JSON) — it is metadata, never
        #   a job, and letting it through would hand a consumer a dict with no `leg`/`model` in it, silently.
        #   ⚠️The test blocks only this one kind of line: half a line of JSON still throws (that is bad data, never
        #   silently skip it).
        # 🔴Splits only on NL, never `splitlines()`: the latter also treats U+0085/U+2028 as line breaks, and lines
        #   written before the upgrade still have those as-is (re-review 3, independent check 6)
        good = [y for y in p.read_text(encoding="utf-8").split(NL) if y.startswith("{")]
        return [json.loads(x) for x in good[-n:]]

