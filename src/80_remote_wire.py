# ━━ The remote leg (B6/B7/B9/B10/B12/B20/B24/B28/§5.2): take jobs from the dispatcher, stream results back one piece at a time
# 🔴this section is the only place in the whole file that actually dials out. It never hard-codes where to: the
#   hostname only ever comes from `remote_url`, which `scv pair` saves into config.json (the `# ━━ ①` section is
#   the one place allowed to hold an address literal). Not paired ⇒ `start_remote()` returns False, and not one
#   byte goes out to the network. The contract facing outward lives in `PROTOCOL.md` (its reader is whoever
#   implements the dispatcher, never ourselves).
STREAM_MAX_AGE_S = 25        # once an SSE pipe has lived this long, proactively switch to a new one (`0` = never switch proactively)
# 🔴a conservative default, not a measured one (it can only really be measured once it is live) ⇒ never read it as a conclusion. 📎 NOTES.md::stream-max-age-guess
STREAM_READ_TIMEOUT_S = 40   # a keepalive comes every 15s ⇒ 40s with not one byte = this pipe is dead
RESULT_FLUSH_S = 0.3         # batch incremental text for this long before sending one batch back
# ⚠️this number's real cost: `urllib` does not reuse connections ⇒ every chunk is a new TCP connection. Against a
#   real https dispatcher that means one full TCP+TLS handshake every 0.3 seconds × every job in flight (measured
#   TIME_WAIT locally: 8 jobs = +40, 4 trickling streams running 14s = +141). Making it smaller means more
#   responsive but also more expensive — never treat it as just a latency knob.
SEEN_JOBS = 512              # idempotency memory: how many job_ids it remembers (in memory only, cleared on every bridge restart)
POST_TIMEOUT_S = 30          # timeout for one hello/result-post attempt; `_post` tries up to 3 times (waiting 0.5/1.0 seconds in between) ⇒ one delivery's worst case is about 91 seconds
MIN_REDIAL_S = 1.0           # the minimum interval between two dials; a bad pipe goes into exponential backoff up to MAX_BACKOFF_S starting from the first one, and complains once it has hit STUCK_REDIALS in a row
STUCK_REDIALS, MAX_BACKOFF_S = 3, 60.0
STREAM_EVENT_MAX = 256 * 1024
SSE_ID_DIGITS = 20   # the most digits an `id:` can have (PROTOCOL's "an increasing integer"): 20 digits fit any 64-bit unsigned integer (2**64-1 is 20 digits), any longer and it stops looking like a counter
# 🔴a single event's byte count must have an upper bound (this is the one path the outside network can come in
#   through); judged in one place with one budget, never split into "one for readline, one running total" — that
#   would be two guards against the same symptom. 📎 NOTES.md::sse-decode-replace


def _loopback_http(url: str) -> bool:
    """The one exception to plaintext `http://`: a loopback address (the reference dispatcher runs on loopback,
    and the test harness relies on this).
    🔴judge by the parsed-out hostname, never by a string prefix: under a prefix check,
      `http://127.0.0.1.a-remote-host.example:9` would also pass, while urllib actually dials that remote name
      (review M-F measured `start_remote()` returning True).
    ⭐a spelling like `127.0.0.1@evil` gets `evil` as the hostname `urlsplit` picks out ⇒ also rejected.
    🔴the port has to be read too: `urlsplit` cuts the hostname at the first colon, `http.client` cuts at the last
      colon ⇒ `http://127.0.0.1:evil.example:443` reads as 127.0.0.1 on this side, while the other side dials
      `127.0.0.1:evil.example` (second follow-up review M-6, measured). The port is all-digits ⇔ there is only
      one colon ⇔ both sides cut at the same spot; reading `.port` raises `ValueError` when it is not all-digits
      (a spelling with a newline in it gets the newline stripped first by `urlsplit`, and what is left is still
      not all-digits)."""
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port          # never delete this: this line exists exactly for its ValueError
        return parts.scheme == "http" and ipaddress.ip_address(parts.hostname or "").is_loopback
    except ValueError:
        return False


def remote_url_refused(url: str) -> str:
    """The one and only judgment for "can this remote address be dialed": returns `""` = can dial, otherwise a
    reason meant for a person to read.
    ⭐two doors share it: the spot that actually dials (`Bridge.start_remote`, this also blocks a hand-written
      config.json) and `scv pair` (the pairing request itself carries the pairing code, and sending it in
      plaintext leaks the code ⇒ waiting until start to block it would already be too late). Never write a
      second copy at the second spot: a prefix check would let `http://127.0.0.1.<remote-domain>` through
      (review M-F). Gate: tests/test_95_setup_pair_update.py::Doors"""
    if url.startswith(HTTPS) or _loopback_http(url):
        return ""
    return ("the remote address must start with %s (plaintext sends the token, the pairing code, and every "
            "segment of every answer); plaintext http is only allowed on a loopback IP (127.x.x.x/[::1]), and "
            "never a hostname like localhost (it can be rewritten by hosts/DNS)" % HTTPS)


# The whole family that dialing (`OpenerDirector.open` inside `_open`) can raise: `OSError` (including
#   `URLError`/`HTTPError`/timeouts), `ValueError` (a bad URL, a reply that cannot be parsed),
#   `http.client.HTTPException` (`BadStatusLine`/`IncompleteRead`: an unhealthy proxy replying with one bad status
#   line is exactly this, review C-A).
NET_ERRORS = (OSError, ValueError, http.client.HTTPException)


def redirect_refused(req, newurl: str) -> str:
    """The one and only judgment for "does this hop get followed": returns `""` = follow it, otherwise the reason
    (review M-8).
    🔴urllib follows a redirect carrying `Authorization` over as-is (it only strips content-length/content-type,
      and carries it across hosts too), and by default it also follows an https→http downgrade ⇒ ① ones carrying
      a token (the remote leg's hello/stream/result) never follow any redirect at all: blocking only the
      downgrade is not enough, hopping to another https host would hand the token over just the same; the
      dispatcher must never reply with a 3xx (PROTOCOL.md) ② ones without a token (`scv update` fetching a file
      from the public repo, backed by a sha256 on its content; `scv pair`'s code is in the request body, and
      urllib drops the request body when it follows a 301/302/303, and does not follow a 307/308 POST at all)
      only block an https downgrade.
    🔴③ a request that started from loopback never follows a redirect out beyond loopback (13d, item 5):
      `local_get` should only ever dial loopback, but whatever answers on that port could be some other program
      (on this dev machine, 8765 is exactly that), and it can reply with a 302, sending
      `scv status`/`stop`/`doctor` off to the outside network with a GET. "Is it loopback" uses the same
      plaintext judgment (`_loopback_http`)."""
    if req.has_header("Authorization"):
        return "requests carrying a token never follow redirects (following would hand the token to the new address as-is)"
    if _loopback_http(req.full_url) and not _loopback_http(newurl):
        return "a request that started from loopback never follows a redirect out beyond loopback"
    if urllib.parse.urlsplit(req.full_url).scheme == "https" and urllib.parse.urlsplit(newurl).scheme != "https":
        return "never follows a redirect that downgrades from https"
    return ""


class _Redirects(urllib.request.HTTPRedirectHandler):
    """The redirect handler installed on every request (via `_open`). When refusing, close the 3xx response
    already in hand before raising (never leave it unclosed: that is a `ResourceWarning`).
    🔴the hop that does get followed strips the proxy credential (13d, measured): `ProxyHandler` adds it to the
      request header, and urllib copies it into the new request as-is when following a redirect ⇒ if the new
      address is in NO_PROXY it connects to the origin directly, credential and all (measured locally on
      loopback: once through `_fetch`, once end to end through `scv update`). `ProxyHandler` adds it back fresh
      whenever the new request genuinely needs to go through the proxy."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        why = redirect_refused(req, newurl)
        if why:
            fp.close()
            raise ValueError("%s: %d → %s" % (why, code, _clip(str(newurl), 200)))
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        new.remove_header("Proxy-authorization")       # this is exactly the casing `add_header` stores (`capitalize()`)
        return new


def _open(req, timeout: float, *handlers):
    """The one and only door for dialing (`_fetch` reading a reply — the `limit=0` branch never reads a single
    byte — and `_stream_once` reading the stream both go through it; the dialing map is pinned by
    test_95::Doors): every request gets `_Redirects` installed on it (`urlopen`'s default opener would follow any
    redirect at all); `handlers` is how `local_get` passes in the one that routes around the proxy.
    ⭐still honors the proxy environment variables as usual (B32: `build_opener` carries a `ProxyHandler` by
      default). A non-2xx raises `HTTPError`, which is itself a response object — call `close()` on it before
      discarding it (I-4); what to do about failure (retry / return None / give a human message) is up to the
      caller."""
    try:
        return urllib.request.build_opener(*handlers, _Redirects()).open(req, timeout=timeout)
    except NET_ERRORS as e:
        if hasattr(e, "close"):
            e.close()
        raise


def _fetch(req, timeout: float, *handlers, limit: int | None = None) -> bytes:
    """Dial once, read the response body back (the `limit=0` branch never reads a single byte, see below) (shared
    by the remote leg's result posts, a subcommand checking on itself, `scv pair`, `scv update`), through `_open`.
    ⭐the response body has an upper bound (this is data from the far side of the network): defaults to
      `MAX_BODY` (the hello reply, PROTOCOL.md's hard-limits table); `scv update` gives it its own
      `SCV_PY_MAX_BYTES` (review M-7: the two bounds mean different things, never let tightening `MAX_BODY` on the
      local leg reject updates as collateral damage).
    ⭐`limit=0` = never read a single byte, and return as soon as it is 2xx (`/bridge/result`: the bridge never
      looks at the body; reading it would give it an upper bound, and treating a delivery that did reach the
      other side as a failure once it goes over that bound, re-review 2, N-M2)."""
    limit = MAX_BODY if limit is None else limit
    with _open(req, timeout, *handlers) as r:
        raw = r.read(limit + 1) if limit else b""
    if len(raw) > limit:
        raise ValueError("the response body exceeds %d bytes" % limit)
    return raw


def _ver(s: str) -> tuple:
    return tuple(int(x) for x in re.findall("[0-9]+", str(s))[:3])


# hello reply's `min_supported` (PROTOCOL.md); ⭐always matched with fullmatch. Each segment caps at 9 digits:
#   without the cap, a segment over 4300 digits makes `_ver`'s `int()` raise ⇒ backs off forever, never `too_old`
#   (second follow-up review 4, Out-of-Scope 2)
MIN_SUPPORTED_RE = re.compile("[0-9]{1,9}([.][0-9]{1,9}){0,2}")


PATH_WORD = re.compile("[^" + chr(92) + "s'" + '"' + "`()" + chr(92) + "[" + chr(92) + "]{}<>,;（）「」【】，。；：、]+")
PATH_SEP, HOME_MARK = "[/" + chr(92) * 2 + "]+", chr(0xE000)
DRIVE_HEAD = re.compile("[A-Za-z]:" + PATH_SEP)
API_PATHS = frozenset(ROUTES + tuple(REMOTE_PATHS.values()))     # this bridge's own API paths: an exact set (never a prefix — `/v1/../x` would still be accepted)
# Where a word might start, scanned in one pass (fix2, second follow-up review N-M2: it used to slice character by
#   character, quadratic): the start of a word, right after `=`/`:`/a CJK character (not the private-use area —
#   `HOME_MARK` lives there, outside the scope of the follow-up review, item 4), or a drive letter not preceded by
#   a letter or digit
PATH_START = re.compile("^|(?<=[=:" + chr(0x2E80) + "-" + chr(0xD7FF) + chr(0xF900) + "-" + chr(0xFFEF) + "])|(?<![A-Za-z0-9])(?=[A-Za-z]:[/"
                        + chr(92) * 2 + "])")


def _home_re():
    """The home directory is spliced together segment by segment: between segments it accepts any number of `/`
    and backslashes (mixed spellings, doubled as in a repr all count), and the drive-letter head also accepts
    Git Bash's `/c` and WSL's `/mnt/c`; both ends are bounded at a segment edge (`/home/al` never swallows
    `/home/alice`, `/root` never mangles `…/rootkit`). Failing to get the home directory — any exception at all
    (15c review M4) — ⇒ None: this one pass simply does not run."""
    try:
        segs = [s for s in re.split(PATH_SEP, str(user_home())) if s]
    except Exception:
        return None
    if not segs or len(segs) == 1 and segs[0].endswith(":"):
        return None
    drive = re.fullmatch("([A-Za-z]):", segs[0])
    head = ("(?:%s|%s%s|%smnt%s%s)" % (re.escape(segs.pop(0)), PATH_SEP, drive.group(1), PATH_SEP, PATH_SEP, drive.group(1))
            if drive else "")
    edge = "[A-Za-z0-9_.~-]"
    return re.compile("(?<!%s)%s%s(?!%s)" % (edge, head, "".join(PATH_SEP + re.escape(s) for s in segs), edge), re.I)


def _path_at(tok: str, i: int, end: int, slashes: list) -> bool:
    """Is `tok[i:end]` the start of a local path — in constant time: only looks at the first few characters, and
    the count of `/` uses a prefix sum (`slashes`). Path-shaped = home directory / backslash / drive letter +
    slash / `~/` / `file:` / starts with `/` and has at least two segments (except this bridge's own exact
    interface paths); the two slashes of `scheme://` never count (except for `file:`)."""
    if tok.startswith("//", i) and i >= 2 and tok[i - 1] == ":" and tok[i - 2].isalnum() and tok[max(0, i - 5):i - 1].lower() != "file":
        return False
    return bool(tok[i:i + 1] in (HOME_MARK, chr(92)) or DRIVE_HEAD.match(tok, i) or tok[i:i + 2] in ("~/", "~" + chr(92))
                or tok[i:i + 5].lower() == "file:"
                or tok[i:i + 1] == "/" and slashes[end] - slashes[i] >= 2 and (end - i > 32 or tok[i:end] not in API_PATHS))


def _no_local_paths(text: str) -> str:
    """The one and only pass before the remote leg's error text leaves the machine (15c, lead's word swap ⑦3):
    the home directory is recognized segment by segment (`_home_re`), then whatever segment looks like a local
    path gets replaced with `<local path>` (the starting points `PATH_START` scans out are each run through
    `_path_at`; a backslash anywhere else in the word ⇒ mask starting from the word's own start — fix2's fallback
    for follow-up review N-M1; a later segment split off by a space within the same path gets merged into the
    previous one), and a line at the end says how many were replaced (a home directory swapped for `~` counts
    too). The whole pass is linear (follow-up review N-M2).
    ⭐collected by shape, never deleted one at a time at each `raise` site (a blacklist: the next new way to leak
      one will not come tell us about it — the same reasoning as `_cli_version`).
    ⚠️what this cannot catch: a relative path with no backslash, a bare user name appearing outside a path
      (`~al/x` counts too), a machine name, a single-segment `/tmp`, the part after a space in a path that
      carries no slash, a path glued onto a character other than `=`/`:`/a CJK character that also does not start
      with a drive letter or a backslash (`|/srv/x`, `@/home/bob/x`);
      what it over-masks: the segment one space after a path that carries a slash (the `5/5` in `C:/a 5/5`; two
      paths separated by only one space, where the second does not start with a drive letter or a slash, count as
      one), and a word with a backslash gets masked in its entirety from its own start. The local leg and
      bridge.log never go through this (this machine's own owner needs the full text). 📎 NOTES.md::no-local-paths"""
    hre, n, out, last, pos = _home_re(), 0, [], None, 0
    text = hre.sub(HOME_MARK, text) if hre else text
    for m in PATH_WORD.finditer(text):
        tok, gap, pos = m.group(0), text[pos:m.start()], m.end()
        end, slashes = len(tok.rstrip(":.")), [0]
        for ch in tok:
            slashes.append(slashes[-1] + (ch == "/"))
        at = next((s.start() for s in PATH_START.finditer(tok) if s.start() < end and _path_at(tok, s.start(), end, slashes)), None)
        tail = tok[end:]
        if at is None and last == m.start() - 1 and gap == " " and ("/" in tok or chr(92) in tok):
            out.append(tail)            # the segment after a space within the same path (`C:/Program Files/x`) ⇒ merge into the previous one, never count it separately
            last = m.end()
            continue
        out.append(gap)
        if chr(92) in (tok if at is None else tok[:at]):
            at = 0                      # fallback: a backslash in the word (before the starting point) ⇒ mask from the word's own start to its end (a relative backslash path, a UNC path glued onto another character)
        if at is None:
            n += tok.count(HOME_MARK)
            out.append(tok.replace(HOME_MARK, "~"))
            continue
        n, last = n + 1 + tok[:at].count(HOME_MARK), m.end()
        out.append(tok[:at] + "<local path>" + tail)
    text = ("".join(out) + text[pos:]).replace(HOME_MARK, "~")
    return text + ("(%d local paths in the original words were not sent; the original is in bridge.log on this "
                   "machine, which keeps only the first %d bytes of each line)" % (n, LINE_CAP_BYTES) if n else "")


def hello_payload(bridge: Bridge) -> dict:
    """B24: the whitelist of fields reported. Never add one more key here without first changing PROTOCOL.md and
    test_hello_is_a_whitelist.
    ⚠️a whitelist controls the key names, not the values ⇒ the `cli_version` slot has a separate shape check of
      its own, see `_cli_version()`."""
    return {"protocol": PROTOCOL, "bridge_version": VERSION, "os": os.name + "/" + sys.platform,
            "python": "%d.%d" % sys.version_info[:2],
            "families": [{"family": f, "cli_version": _cli_version(i["version"])}
                         for f, i in bridge.found.items() if not i["blocked"]],
            "models": bridge.cat, "max_concurrent": int(bridge.cfg.get("max_concurrent") or 4)}


def remote_request(d: dict) -> dict:
    """A job from the dispatcher → laid out into the shape the local leg's mouth expects, going through the exact
    same `normalize_request()`.
    🔴never write a second copy of intake validation here: the length check (`_name`), the range check
      (`_seconds`), the content-shape check (`_text_of`) — not one of them can be skipped; "one rule, two
      submission paths" with one side missing its copy means the bug only shows up on the other side.
      What each one missing would do, and which compatibility-table behaviors this path inherits as a result,
      📎 NOTES.md::two-legs-one-entry-gate"""
    opts = d.get("opts") if isinstance(d.get("opts"), dict) else {}
    msgs = d.get("messages")
    head = [{"role": "system", "content": d.get("system") or ""}]
    return normalize_request({"model": d.get("model"), "session": d.get("session"),
                              "messages": (head + msgs) if isinstance(msgs, list) else msgs,
                              "effort": opts.get("effort"), "timeout": opts.get("timeout"),
                              "first_token_timeout": opts.get("first_token_timeout")})


