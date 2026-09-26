# 🔴The three single words are matched by "no letter glued on either side", never a bare substring and never
#   regex's own word boundary — both extremes have already bitten once: a bare substring judged `presets`/
#   `quotation` as quota (429 plus fix_hint lying); the `chr(92)+"b"` kind of word boundary counts `_` and CJK as
#   part of the word too ⇒ a real quota code like `insufficient_quota` falls through to unknown instead.
#   ⭐Both sides block `[A-Za-z]`, never `[a-z]`: the latter's correctness rests entirely on the `.lower()` line
#     below — move that lower away and `overloadedFn`'s capital `F` stops being blocked, the false positive comes
#     right back, and every test is still green at that moment.
#   ⚠️Still cannot fix the `(reading 'resets')` case (text alone cannot tell the two apart) ⇒
#     tests/test_40_errors.py keeps one expectedFailure pinned on it. 📎 NOTES.md::quota-word-boundary
QUOTA_PAT = ("usage limit", "session limit", "hit your", "rate limit", "exceeded your")
QUOTA_WORD = re.compile("(?<![A-Za-z])(?:overloaded|resets|quota)(?![A-Za-z])")
AUTH_PAT = ("not logged in", "please run /login", "401 unauthorized", "missing bearer", "login required",
            "token expired", "token has expired", "invalid api key", "authentication_error", "codex login")
HTTP_STATUS = {"auth_required": 401, "quota": 429, "local_rate_limit": 429, "bad_request": 400, "cancelled": 499,
               "timeout": 504, "crashed": 502, "unknown": 502}
# 🔴`local_rate_limit` = refused by the bridge's own rate limit, and its window is by the hour ⇒ giving only
#   "retryable" and never "how long to wait" means the client just retries immediately, gets 429 again, and keeps
#   retrying until it has burned a whole hour ⇒ this one class must have a fix_hint. 📎 NOTES.md::local-rate-limit
RETRYABLE = ("timeout", "crashed", "quota", "local_rate_limit")


def classify(raw: str, default: str = "unknown") -> str:
    """The class is an added field; the original text is never touched. Quota is checked before login: quota
    wording sometimes carries the word "login" in it, never the other way round.
    ⚠️`default` comes from the caller and is returned as-is ⇒ the caller is responsible for keeping it inside
      `HTTP_STATUS` (otherwise the downstream `HTTP_STATUS[klass]` is a purely self-inflicted KeyError). The wiring
      layer either blocks it at the door (fail loud) or goes uniformly through `.get(klass, 502)`.
    ⚠️Returning `unknown` means "we could not read what this CLI said", and it is the only signal that the pattern
      list has gone stale ⇒ the log line has to carry both `klass` and the original words, or it can go stale
      forever with nobody ever finding out."""
    low = (raw or "").lower()
    if any(p in low for p in QUOTA_PAT) or QUOTA_WORD.search(low):
        return "quota"
    if any(p in low for p in AUTH_PAT):
        return "auth_required"
    return default


def fix_hint(klass: str, family: str) -> str:
    if klass == "auth_required":
        # ⭐codex uses the player's own CODEX_HOME (Task 13c) ⇒ a bare `codex login` logs into exactly the home the
        #   bridge uses, so this sentence is true. (At Task 13 the home was scv's own; this line used to point at
        #   scv's own codex-login subcommand, which went away with 13c.)
        #   ⚠️It does not know where the executable is (mostly not on PATH): only doctor can work that one out, the
        #   one with a path (`login_cmd`).
        return " ".join([family] + LOGIN_ARGS[family]) if family in LOGIN_ARGS else ""
    if klass == "quota":
        return "wait for the quota to reset (see the original words for how long), or switch to a model from the other family on this bridge"
    if klass == "local_rate_limit":
        # ⭐This sentence is the companion to `retryable=True`, never decoration: without spelling out whose rate
        #   limit it is and how long the window is, the client will just retry immediately and hit it again. Never
        #   copy the number (`remote_jobs_per_hour`'s default) into this sentence — copy it and it starts lying the
        #   moment the user changes the config, and nobody can tell that it is lying.
        # ⚠️There are two different sources, and how long to wait differs between them ⇒ this sentence must never
        #   name only one (it used to mention only the hourly one, while "in-flight is at the cap" is something to
        #   wait a few seconds for). ⭐Whoever reads this is the dispatcher's implementer or operator (production
        #   hits of this class on the local leg are 0) ⇒ give the knob's name directly, and say that retrying needs
        #   a new id (the same id only ever gets one sequence starting from 0).
        return ("this is the bridge's own rate limit, not an upstream quota; which one you hit is in the original "
                "words — the hourly job count is config.json's remote_jobs_per_hour (window by the hour), the "
                "in-flight count is max_concurrent (wait for a few to finish) ⇒ do not retry immediately, retry "
                "with a new job_id")
    return ""


def error_body(e: BridgeError) -> dict:
    return {"error": {"message": e.raw, "type": e.klass, "code": e.klass, "retryable": e.klass in RETRYABLE,
                      "fix_hint": fix_hint(e.klass, e.family), "family": e.family}}


def error_payload(e: BridgeError, leg: str) -> dict:
    """The one place that computes the error response body, and along the way fills in "the copy nobody logged
    yet" — ⭐both legs share this one function.
    🔴Why the accounting sits at this layer: the `bad_request`/`cancelled` that `SessionManager` raises is normal
      control flow, and it deliberately does not go through `_fail()` (writing a line for every bad request that
      comes in would turn bridge.log into background noise) ⇒ that layer and this one each assume the other is
      logging it — the textbook way to build a silent failure. This line is that missing entry.
    ⭐The test is the explicit flag `e.logged`, never guessing from `klass` who already logged it.
    ⚠️🔴Never let either leg call `error_body()` on its own: on the leg that skips this, every `bad_request` the
      other side gets is zero lines on disk — while all three tables are green. Gate:
      tests/test_70_local_api.py::OneDoor::test_error_body_is_computed_in_exactly_one_place
    📎 NOTES.md::api-is-the-last-catcher"""
    if not e.logged:
        e.logged = True
        log("%s: this request failed (%s): %s" % (leg, e.klass, e.on_disk))
    return error_body(e)

