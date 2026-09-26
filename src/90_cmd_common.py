# ━━ Subcommands (run / start / stop / status / token / doctor / setup / pair / update)
# ⭐This block only ever touches the blocks above it through an explicit function call, and never adds a new
#   mutable global shared across blocks (the split has already happened: the pieces under src/ are joined into
#   the one scv.py players get).
# ⭐Every failure path goes through `_cmd_failed`: one line lands in bridge.log (visible on stderr too) plus one
#   sentence of "what to do next" (Minor 6 / C15).
# ⚠️Process / network behaviour has only been measured on win32; the few POSIX branches (SIGTERM,
#   `start_new_session`) are written to the code, ⏳ not measured.
GC_EVERY_S = 60          # the clock-driven cadence for reclaiming idle persistent sessions: `gc_idle` used to only get called when a new session was built, so with nobody arriving it never shrank (an earlier measurement)
START_WAIT_S = 60        # how long `scv start` waits at most (starting the bridge runs detect first: a real CLI answers each probe in 0.05-0.43s)
STOP_WAIT_S = CLOSE_GRACE_S + KILL_BUDGET_S   # POSIX: how long to wait after SIGTERM for it to wind down on its own (closing one session = the grace period plus, worst case, one tree-kill)
STOPPED_WITHIN_S = 5.0   # how long to wait, after a hard kill, for the process to actually vanish from the system
PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")


def self_cmd(*words: str) -> str:
    """The one and only door for a self-referential command: every human-facing "go run one of scv's own
    subcommands" goes through it (a bare `scv <subcommand>` reappearing in a human-facing string turns red on the
    spot: tests/test_90_cli.py::SelfCommands). After install there is no `scv` on PATH (setup does not install a
    launcher, a product decision) ⇒ pasting the bare form would not run.
    ⭐What it computes is what this particular install actually looks like: interpreter = `sys.executable`, script
      = this file's absolute path (never `python3` / `python` / `~`); turning that into something pasteable is
      `paste_cmd`'s job. ⚠️May come out as several lines (one label line, one command line) ⇒ the caller gives it
      its own line to itself, never uses it as a `%` / `.format` template (a `%` / `{` in the path would blow up).
    🔴The output carries this machine's absolute path (a path with the user's name in it) ⇒ never goes into the
      API's response body, never out over the network."""
    return paste_cmd([sys.executable, os.path.abspath(__file__)] + list(words))


def _cmd_failed(what: str, next_step: str, detail: str = "") -> int:
    log("❌ " + what)
    if detail:      # ⭐printed as-is to stderr, never folded into the line above: `log()` truncates its tail by LINE_CAP_BYTES, and the last few lines (why it died) get cut off first
        print(detail, file=sys.stderr)          # ⚠️this one never goes through the `log()` door ⇒ text from outside has to be run through `_no_ctrl` by the caller first (the two spots in pair / update)
    print("  ⇒ " + next_step, file=sys.stderr)
    return 1


def local_get(port: int, path: str, timeout: float = 3.0):
    """Ask itself. Must bypass the environment proxy: when the user has set HTTP_PROXY without configuring
    no_proxy, going through the proxy cannot reach 127.0.0.1 on this machine (B32).
    ⭐It is one of the named outbound points in `NET_CALLERS`, but it only ever dials loopback: the address comes
      only from section ①'s `LOCAL_URL`; never follow a 302 outside loopback (`redirect_refused` ③)."""
    try:
        return json.loads(_fetch(LOCAL_URL % port + path, timeout, urllib.request.ProxyHandler({})).decode("utf-8"))
    except NET_ERRORS:         # closes over `HTTPError` / the whole exception family, at the `_open` door
        return None


def our_health(port: int):
    """Is it this bridge answering on that port. "Something answered" is not "the bridge is running": on this
    dev machine, port 8765 was measured being held by an unrelated program."""
    h = local_get(port, "/healthz")
    return h if isinstance(h, dict) and h.get("protocol") == PROTOCOL and "version" in h else None


def mask_proxy(url: str) -> str:
    """A password inside a proxy address never goes into any output. Blocked whether or not a scheme is present
    (`user:pw@host:port` is recognized by many proxy tools).
    🔴Masks up to the last `@`, never stops at a `/` (Task 12 review I-2): when the password itself contains `@`,
      Node / Python / Rust all split on the last `@` (and that kind of config actually works); stopping at `/`
      would mean the config itself never works at all, and it is exactly when the proxy is broken that someone
      runs doctor and pastes the output for someone else to read.
    ⚠️Would rather over-mask: a proxy address with `@` in its path (rare) gets its hostname masked away too along
      with the password — a leaked password costs more than an invisible hostname."""
    return re.sub("^([A-Za-z][A-Za-z0-9+.-]*://)?.*@", lambda m: (m.group(1) or "") + "***@", url, flags=re.S)


def proxy_env() -> dict:
    return {k: mask_proxy(v) for k, v in os.environ.items() if k.lower() in PROXY_VARS}


def _remask(env):
    """The proxy variables stored in bridge.pid get masked again on the way back out: an older version may not
    have masked them completely (the `mask_proxy` from before I-2)."""
    if not isinstance(env, dict):
        return "(bridge.pid did not record this item, or its shape is wrong: %s)" % type(env).__name__
    return {str(k): mask_proxy(v) if isinstance(v, str) else "(not a string, not printed)" for k, v in env.items()}
