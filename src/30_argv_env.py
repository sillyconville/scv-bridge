CLAUDE_MODELS = ("haiku", "sonnet", "opus")
CODEX_MODELS = ("gpt-6-luna", "gpt-5.6-terra", "gpt-6-sol")    # 0.2.0: the service's own subscription seats (maintainer, 2026-09-27); next: find local models by themselves
EFFORTS = ("low", "medium", "high")
MODEL_RE = re.compile("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CODEX_GLOB = "OpenAI/Codex/bin/*/codex.exe"
# The two sentences codex's `login status` is recognized by. ⭐rc != 0 is both how it says "not logged in" and how
# it looks when it is simply broken ⇒ rc alone cannot tell the two apart, so this also checks whether it actually
# said one of these two sentences (`_codex_auth_from_text` and the admission gate share this one ruler).
CODEX_STATUS_MARKS = ("Logged in", "Not logged in")
# Every codex built-in feature unrelated to "answer a piece of text" gets turned off (the list comes from an
# earlier measurement of config.json's codex_disabled_features, plus shell_tool). ⚠️codex says nothing when it
# does not recognize a `-c` ⇒ a name going stale fails silently; `scv doctor` checks this list against `codex
# features list`.
# ⭐The last two are off by default already (Task 13c added them): the player's own config.toml can turn them back
#   on — `memories` injects his memory summary into every session, `multi_agent_v2` adds a whole family of tools
#   for spawning sub-agents; `-c` sits on top of his own layer ⇒ turned off here once more. Each one is
#   load-bearing on its own (measured at zero budget: dropping either one alone puts that same thing straight back
#   into the request sent to the model).
CODEX_OFF = ("shell_tool", "apps", "browser_use", "browser_use_external", "computer_use", "code_mode_host",
             "image_generation", "multi_agent", "plugins", "skill_search", "sleep_tool", "tool_suggest",
             "unified_exec", "view_image", "goals", "hooks", "workspace_dependencies", "in_app_browser",
             "skill_mcp_dependency_install", "tool_call_mcp_elicitation", "memories", "multi_agent_v2")


def _closed(value, allowed, what: str) -> str:
    """The gate for a closed set. ⚠️Truncate the echoed-back original value: some of these `value`s come from the
    network (`effort` is one), and this sentence goes straight into both the response body and stderr — neither of
    which has a cap like `_clip`'s ⇒ without truncating, the other side stuffs in 200,000 characters and gets
    200,000 characters echoed back (measured). ⭐Truncate to just enough for diagnosis: the reader wants to know
    what shape the value they sent had."""
    if value not in allowed:
        raise BridgeError("bad_request", "%s is not in the set this bridge recognizes: %s" % (what, repr(value)[:32]))
    return value


def _model_ok(model) -> str:
    """The closed-set gate for a model name. ⭐What B27 has to pin down is "the remote side cannot slip a single
    character into the command line" ⇒ this guarantee has to live at the one and only assembly point: today the
    only entry is `resolve_model` (which only lets through names `catalog` has already reported), but the moment
    Task 5 opens one more path — a retry, a default-value fallback, doctor self-check — that builds argv directly,
    the closure is gone, and no test at all would go red.
    ⚠️The test is `MODEL_RE`, never `CLAUDE_MODELS`: `extra_models` is a legitimate source.
    ⚠️`fullmatch`, never `match`: `match`'s `$` lets a trailing newline through (`"haiku" + NL` would still pass)."""
    if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
        raise BridgeError("bad_request", "model name is not a shape this bridge recognizes: %r" % (model,))
    return model


def env_missing(name: str) -> bool:
    """The one test for "this environment variable is missing" (shared by `cli_head` looking for the desktop
    codex, and doctor's `CRITICAL_ENV`; an empty string counts as missing, 15c review M6)."""
    return not os.environ.get(name)


def cli_head(family: str, cfg: dict) -> list | None:
    """The executable part of a CLI. Returns None if not found. A configured value is checked for existing before
    use (codex's self-update swaps out its hashed directory)."""
    conf = cfg.get(family + "_bin") or ""
    exe = conf if conf and Path(conf).exists() else shutil.which(family)
    if not exe and family == "codex" and not env_missing("LOCALAPPDATA"):
        hits = sorted(Path(os.environ["LOCALAPPDATA"]).glob(CODEX_GLOB), key=lambda p: p.stat().st_mtime)
        exe = str(hits[-1]) if hits else None
    if not exe:
        return None
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe]            # npm installs these as .cmd, only startable through cmd
    return [exe]


def claude_argv(head: list, model: str, sys_file, settings_file, effort: str | None = None) -> list:
    """The one and only place in the whole file that assembles claude's arguments. The only inputs from outside
    are model/effort, and both come from closed sets (B27).
    ⚠️When started through cmd /c on Windows, the arguments get parsed a second time by cmd ⇒ what protects this is
    the closed set, never escaping."""
    argv = list(head) + ["-p", "--model", _model_ok(model), "--safe-mode",
                         "--system-prompt-file", str(sys_file), "--exclude-dynamic-system-prompt-sections",
                         "--disallowedTools", "*", "--strict-mcp-config", "--disable-slash-commands",
                         "--settings", str(settings_file),
                         "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                         "--include-partial-messages"]
    if effort:
        argv += ["--effort", _closed(effort, EFFORTS, "effort")]
    return argv


def codex_argv(head: list, model: str, effort: str | None) -> list:
    """The one and only place in the whole file that assembles codex's arguments. The model goes through -c
    (app-server does not take --model).
    ⭐codex runs in the player's own CODEX_HOME (no login needed, Task 13c) ⇒ whatever is in his config.toml comes
      along by default. `-c` is the layer sitting on top of that: it overrides scalars entirely — the two lines
      below are exactly how that blocks his standing instructions and the external program he runs after every
      turn, each load-bearing on its own; tables cannot be overridden that way ⇒ the MCP servers he configured, the
      skills he installed (down to the skill list itself) get turned off one by one in thread/start
      (`_codex_user_off`).
    ⚠️app-server does not recognize `--ignore-user-config` / `--ignore-rules` (clap says `unexpected argument` on
      the spot, only `codex exec` has them).
    What this blocks, and what it does not, is read off from behavior case by case: 📎 NOTES.md::codex-user-home"""
    q = chr(34)
    argv = list(head) + ["app-server", "--listen", "stdio://",
                         "-c", "model=" + q + _model_ok(model) + q,
                         "-c", "model_reasoning_effort=" + q + _closed(effort or "low", EFFORTS, "effort") + q,
                         "-c", "web_search=" + q + "disabled" + q,
                         "-c", "developer_instructions=" + q + q,
                         "-c", "notify=[]"]
    for name in CODEX_OFF:
        argv += ["-c", "features." + name + "=false"]
    return argv


# What the agent session that starts the bridge sets for its own child processes to say "which session am I in"
#   (15c, lead item 7-1): exact names = written up in both families' packages / measured what they actually set for
#   a child process; prefix families copy Claude Code's own binding scheme for its eval sandbox. Never widen the
#   prefix to `CLAUDE_CODE_*`: the user's login method and Git Bash path also live under it (B22).
#   `TRACESTATE` and `TRACEPARENT` are the same W3C pair (fix1 item 12).
SESSION_VARS = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENT", "TRACEPARENT", "TRACESTATE", "CLAUDE_CODE_ENTRYPOINT",
                "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_INVOKED_SKILLS", "CODEX_CI", "CODEX_VERSION",
                "CODEX_INTERNAL_ORIGINATOR_OVERRIDE")
SESSION_PREFIXES = ("CLAUDE_CODE_SESSION_", "CLAUDE_CODE_MESSAGING_", "CLAUDE_CODE_HOST_", "CLAUDE_CODE_REMOTE", "CLAUDE_CODE_SDK_",
                    "CLAUDE_CODE_RELAUNCH_", "CLAUDE_CODE_BRIDGE_", "CLAUDE_BG_", "CODEX_THREAD_", "CODEX_SESSION_",
                    "CODEX_SANDBOX", "CODEX_NETWORK_PROXY_")


def session_bound(name: str) -> bool:
    """Does this variable describe "the agent session that started me" (strip it) or the user's own setting (keep
    it). win32 names are case-insensitive ⇒ compare uppercased."""
    return name.upper() in SESSION_VARS or name.upper().startswith(SESSION_PREFIXES)


def child_env() -> dict:
    """The child process's environment = our own environment with the session-bound families (`session_bound`)
    stripped out, everything else as-is (proxy variables, his own CODEX_HOME, his own login method, settings that
    change behavior — doctor lists the latter), never adding a single one. 📎 NOTES.md::child-env-session-vars
    ⭐codex uses the player's own CODEX_HOME (no login needed, Task 13c): anyone who wants scv's codex to use a
      different directory sets this environment variable themselves — that is the escape hatch, and it needs not
      one line of code. ⚠️Returns a copy: changing it must never change this process's own environment.
    ⚠️What this cannot cover: a new exact name that does not look session-related (the `CLAUDE_EFFORT` kind) needs
      a human to add it to the list (doctor lists the names actually passed down = a new one gets seen); when
      codex's network proxy is on, the session-bound part is the value of `HTTP(S)_PROXY`, and the name is generic
      — stripping by name cannot catch that."""
    return {k: v for k, v in os.environ.items() if not session_bound(k)}

