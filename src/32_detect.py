PASTE_PLAIN = re.compile("[A-Za-z0-9._/:+" + chr(0x4E00) + "-" + chr(0x9FFF) + "-]+")   # what all three shells treat as a plain character (measured)
SHELL_QUOTES = "'" + chr(0x2018) + chr(0x2019) + chr(0x201A) + chr(0x201B)   # PowerShell treats the last four as single quotes too


def paste_cmd(argv: list) -> str:
    """A list of arguments → a piece of text that can be pasted and just run (handing someone a command that will
    not run is the same as lying). ⭐The one and only ruler in the whole file for quoting/shell rules: the
    self-reference command (`self_cmd`), the CLI's own login command (`login_cmd`), and the `git diff` line printed
    after `scv update` all go through it.
    ⭐One line if every shell agrees; otherwise one line per shell, label on one line and the command on the next
    (they select and paste the whole line), and lines that happen to match get grouped under one label.
    On win32 the three shells are worked out separately (every branch here has been run in that machine's real
    shell, 📎 NOTES.md::paste-cmd):
      - backslashes always become `/`: Git Bash swallows an unquoted backslash (13c review M3), and all three
        shells' own programs accept `/`;
      - entirely made of `PASTE_PLAIN` characters ⇒ no quoting; otherwise PowerShell uses single quotes (a
        quote-like character gets doubled), and prepends `& ` when the leading word is quoted (in 5.1 a quoted
        leading word is just a string, not a command); cmd uses double quotes, and never gets a `%` (cmd expands
        `%X%` even inside double quotes);
      - Git Bash uses double quotes, switching to single quotes for `$` / backtick / `!` / `"` (an interactive
        bash's double quotes still expand `!` as history);
      - starting with `.cmd` / `.bat` (an npm-installed CLI): all three shells hand it to cmd to parse a second
        time ⇒ never give it a `%` for any shell (`%PATH%` would get expanded), and never give Git Bash `& ( ) ^ , ; =`
        (it does not quote those, and cmd breaks the line right there).
    On POSIX, bash/zsh share the same single-quoting rule ⇒ always one line (⏳ written to the rule, never run on
    POSIX)."""
    ws = [str(w) for w in argv]
    if os.name != "nt":
        return " ".join(w if PASTE_PLAIN.fullmatch(w) else shlex.quote(w) for w in ws)
    ws, q = [w.replace(chr(92), "/") for w in ws], chr(34)
    bat, pct = ws[0].lower().endswith((".cmd", ".bat")), any("%" in w for w in ws)
    if bat and pct:
        return "(cannot be written as one pasteable line: %s is a .cmd, cmd would expand the %% in the path a second time)" % ws[0]

    def each(quote):
        return " ".join(w if PASTE_PLAIN.fullmatch(w) else quote(w) for w in ws)

    ps = each(lambda w: "'" + "".join(c * 2 if c in SHELL_QUOTES else c for c in w) + "'")
    lines = {"PowerShell": ps if PASTE_PLAIN.fullmatch(ws[0]) else "& " + ps,
             "cmd": None if pct else each(lambda w: q + w + q),
             "Git Bash": None if bat and set("&()^,;=") & set("".join(ws)) else
             each(lambda w: shlex.quote(w) if set("$`!" + q) & set(w) else q + w + q)}
    by = {}
    for shell, line in lines.items():
        if line is not None:
            by.setdefault(line, []).append(shell)
    if len(by) == 1:
        return next(iter(by))
    return NL.join("run this in " + "/".join(shells) + ":" + NL + line for line, shells in by.items())


LOGIN_ARGS = {"claude": ["auth", "login"], "codex": ["login"]}     # the CLI's own login subcommand (`fix_hint`'s two lines are also built from this)


def login_cmd(family: str, head: list | None) -> str:
    """Gives the user a login command he can paste and just run — the CLI's own login command (what codex logs
    into is the same home the bridge uses: his own). Never "logging in again for scv's sake": it is the same login
    he would normally do to use this CLI day to day. Turning it into something pasteable is `paste_cmd`'s job.
    ⚠️Uses the resolved executable, never the bare name: codex is mostly not on PATH (the desktop build's copy,
      found by globbing); claude having `claude_bin` configured is also usually exactly because it is not on PATH
      (13b review M4).
    ⚠️npm installs it as `.cmd`, and `cli_head` hands back `cmd /c <it>` ⇒ the pasted line drops that shell: all
      three shells can start a `.cmd` directly, and Git Bash would rewrite `/c` as a path, `C:/` (both measured on
      this machine)."""
    parts = list(head or [family])
    return paste_cmd((parts[2:] if parts[:2] == ["cmd", "/c"] else parts) + LOGIN_ARGS[family])


def _claude_auth_from_json(raw: str) -> dict | None:
    """`claude auth status --json` → three fields; returns `None` = there is no JSON object with a `loggedIn` field
    anywhere in this output.
    🔴F-1: never read "could not read it" as "not logged in": claude has no such thing as "an unrecognized
      subcommand" — a version that does not recognize `auth status` treats it as a real prompt and replies with
      something the model said (an earlier measurement) ⇒ that is "could not tell", and it is `auth_status`'s job
      to complain about it.
    ⭐F-6: try parsing from every line-start `{`, never `raw.index("{")` — a stray brace earlier in some noise line
      would otherwise make the rest of the parse fail.
    ⭐Values are folded to one line at the border where they come in (an earlier measurement): `method` gets
      printed in doctor's own line, and also goes into bridge.log."""
    dec = json.JSONDecoder()
    for m in re.finditer("(?m)^[ ]*[{]", raw):
        try:
            d = dec.raw_decode(raw, m.end() - 1)[0]
        except ValueError:
            continue
        if isinstance(d, dict) and "loggedIn" in d:
            return {"logged_in": bool(d.get("loggedIn")), "method": _one_line(str(d.get("authMethod") or "")),
                    "plan": _one_line(str(d.get("subscriptionType") or ""))}
    return None


def _codex_auth_from_text(raw: str, rc: int) -> dict:
    """codex `login status`'s output → three fields. ⭐The status line is picked by content, never by position: it
    prints noise before and after, `lines[-1]` gets whichever line luck hands it ⇒ `method` can quietly turn into
    an unrelated sentence (and it is meant to be printed for the user to read).
    ⚠️When not one line is recognizable, the whole original text is kept (B21) — never throw the truth away just to
    "pick a line"."""
    lines = [x.strip() for x in raw.splitlines() if x.strip()]
    hit = [x for x in lines if any(m in x for m in CODEX_STATUS_MARKS)]
    # ⚠️when nothing is recognizable, the whole original text is kept (B21) — but folded to one line first before
    #   handing it out: `method` gets printed into blocked for the user to read, and also goes into bridge.log,
    #   and a multi-line value is broken at both ends. Folding is never truncation — not a word gets dropped.
    # ⭐it has to say something even when it said nothing at all (an earlier measurement): never let the caller
    #   downstream read this as "(codex said:)" with nothing after it
    said = hit[0] if hit else (_one_line(NL.join(lines)) or "(it said nothing at all, rc=%d)" % rc)
    return {"logged_in": rc == 0, "method": said, "plan": ""}


def _probe_dir() -> Path:
    """The cwd for a zero-budget probe (`--version` / `--help` / login status / feature list): `work/probe`, an
    empty directory.
    🔴Never run this in the caller's own cwd (usually the user's own repository): claude treats an argument it does
    not recognize as a real prompt and actually calls out with it, and also reads the current directory's
    repository context (an earlier measurement). Every call today uses arguments it recognizes ⇒ this is defense
    in depth (Task 12 review M-7)."""
    p = spath("work/probe")
    p.mkdir(exist_ok=True)
    return p


def auth_status(family: str, head: list) -> dict:
    """Zero-budget login status. ⚠️Only says "there are local credentials", never "the credentials still work" —
    the real test is doctor --live's actual call.
    ⭐`probe_error` is always present: `logged_in=False` has two different origins — "we asked, and he is not
      logged in" and could not even ask at all. Reading the latter as the former sends the user off to run a login
      that will not fix anything. Never tell the two apart by "is this key present": an absent key and "asked, no
      error" look identical."""
    try:
        if family == "claude":
            p = run_cli(list(head) + ["auth", "status", "--json"], timeout=PROBE_MAX_SECONDS,
                        env=child_env(), cwd=_probe_dir())
            st = _claude_auth_from_json(decode(p.stdout))
            if st is None:
                said = decode(p.stdout + p.stderr).strip()
                if "{" in said:     # ⚠️this could be a full set of identity info (email/orgId) ⇒ never quote the original, only say the shape (the same idea as B24's whitelist)
                    said = "(there is a brace in the output, but no JSON with a loggedIn field could be parsed out of it; not quoting the original: it might carry an email)"
                said = _clip(_one_line(said) or "(it said nothing at all)", 300)
                why = "claude auth status --json did not return a JSON object with loggedIn (rc=%d): %s" % (p.returncode, said)
                log(why + " ⇒ treated as could-not-tell; ⚠️if this version does not recognize auth status, it may have just sent this as a real prompt")
                return {"logged_in": False, "method": "", "plan": "", "probe_error": why}
        else:
            p = run_cli(list(head) + ["login", "status"], timeout=PROBE_MAX_SECONDS, env=child_env(),
                        cwd=_probe_dir())
            st = _codex_auth_from_text(decode(p.stdout + p.stderr), p.returncode)
        return dict(st, probe_error="")
    except (OSError, subprocess.SubprocessError) as e:
        log("%s's login-status probe never ran (%s): %s" % (family, " ".join(head), _one_line(e)))
        return {"logged_in": False, "method": "the status command never ran: %s" % _one_line(e), "plan": "",
                "probe_error": _one_line(e)}


def _claude_safe_mode_gate(head: list) -> str:
    """The claude family's admission check: does this version of the CLI recognize `--safe-mode`.
    ⭐"Could not tell" and "asked, and the answer is no" are two different things, never merged into one message:
      the probe fails and `"--safe-mode" not in ""` comes out true anyway ⇒ the user gets sent to upgrade a CLI
      that was never broken, upgrades it, and nothing changes.
    ⭐`--help` is read from both channels: whether the CLI puts it on stdout or stderr is its own business ⇒
      reading only one channel means some version that switches pipes triggers the false refusal above.
      📎 NOTES.md::cannot-tell-vs-answered-no"""
    try:
        p = run_cli(list(head) + ["--help"], timeout=PROBE_MAX_SECONDS, cwd=_probe_dir())
    except (OSError, subprocess.SubprocessError) as e:
        log("claude's --help probe never ran (%s): %s" % (" ".join(head), _one_line(e)))
        return "could not tell whether this version of Claude Code recognizes --safe-mode (%s); never report this family until there is an answer" % _one_line(e)
    if p.returncode != 0:
        # 🔴the rc axis: it starts fine but rc != 0 never raises an exception ⇒ this never reaches the except above,
        #   and `--safe-mode` is not in the output either ⇒ it falls through to the same "please upgrade" line below.
        #   "raised an exception" and "rc != 0" both have to count as "could not tell".
        why = _one_line(decode(p.stdout + p.stderr).strip()) or "(it said nothing at all)"
        log("claude's --help probe returned rc=%d (%s): %s" % (p.returncode, " ".join(head), why))
        return ("could not tell whether this version of Claude Code recognizes --safe-mode (--help exited rc=%d, it said: %s); "
                "never report this family until there is an answer" % (p.returncode, why))
    if "--safe-mode" not in decode(p.stdout + p.stderr):
        return ("this version of Claude Code does not recognize --safe-mode (without it, your ~/.claude/CLAUDE.md would go into every single call); "
                "please upgrade Claude Code")
    return ""


def _codex_gate(head: list) -> str:
    """The codex family's admission check: are there local credentials. "Could not tell" and "asked, and not
    logged in" each get their own wording, never impersonating each other.
    ⭐Asks the player's own CODEX_HOME (the same one the real work uses: both sides use `child_env()`) ⇒ not logged
      in just means asking him to log in there once — the same login he would do to use codex day to day, never
      "logging in again for scv's sake" (a hard constraint the maintainer ruled on in Task 13c)."""
    st = auth_status("codex", head)
    if st["probe_error"]:
        return "could not tell whether codex has local credentials (%s); never report this family until there is an answer" % st["probe_error"]
    if not st["logged_in"] and not any(m in st["method"] for m in CODEX_STATUS_MARKS):
        # 🔴the rc axis, codex's case: rc != 0 but it never said one word about login status (a broken config.toml,
        # a broken install...) ⇒ we genuinely do not know whether this is about credentials at all. Never call a
        # crash "you are not logged in" and send him off to run a login that will not fix anything.
        return "could not tell whether codex has local credentials (codex said: %s); never report this family until there is an answer" % st["method"]
    if not st["logged_in"]:
        # ⚠️wording only claims "whether there are credentials": `login status` never says whether they still work (necessary, not sufficient).
        # ⚠️the command goes on its own line: when the path has a space it becomes several per-shell lines, and tacking it onto "please run:" would read as "please run: run this in PowerShell: …"
        return "codex has no local credentials (codex said: %s); please run:%s%s" % (st["method"], NL, login_cmd("codex", head))
    return ""


def detect(cfg: dict) -> dict:
    """⭐Every probe always uses `timeout=PROBE_MAX_SECONDS` (a real CLI measures 0.05~0.43s, 20x headroom): a
    probe over budget would trip the run_cli gate every single time ⇒ a gate our own noise turns into wallpaper is
    worse than no gate.
    ⭐A probe's env = the real one used for actual work (both are `child_env()`): never probe one CODEX_HOME and
      actually run another, or the probe's conclusion is not about the same thing.
    ⭐Every failure path logs one line to bridge.log and returns to the caller: could-not-tell says "could not
      tell" plus the OS's original words, never explain away a probe that never ran at all as "your CLI is too old"
      or "you are not logged in"."""
    found = {}
    for family in ("claude", "codex"):
        head = cli_head(family, cfg)
        if not head:
            # 🔴the third shape of a family vanishing entirely: it `continue`s further up than this ⇒ the "log one
            #   line if blocked is non-empty" line below never sees it, while the user sees the exact same thing
            #   (this family is missing from /v1/models). Never let it walk away silently.
            log("refusing to report the %s family: this machine has no executable for it "
                "(not on PATH, and config.json's %s_bin does not point to one either)" % (family, family))
            continue
        try:
            # ⚠️this truncation is deliberate, never a missed `_one_line`: that rule covers the original error text,
            #   this one wants exactly one field (a version number should only ever be one line). ⇒ the test is
            #   "do I want the original words, or one field".
            ver = decode(run_cli(list(head) + ["--version"], timeout=PROBE_MAX_SECONDS,
                                 cwd=_probe_dir()).stdout).strip().splitlines()[0]
        except (OSError, IndexError, subprocess.SubprocessError) as e:
            ver = "the version command never ran: %s" % _one_line(e)
            log("%s's --version probe never ran (%s): %s" % (family, " ".join(head), _one_line(e)))
        blocked = _claude_safe_mode_gate(head) if family == "claude" else _codex_gate(head)
        if blocked:
            # ⭐the decision path needs to be loud too: this family disappears from /v1/models entirely, and all
            #   the user will ever see is "my claude is gone". The log lines above cover "the probe failed", this
            #   one covers "and so this family is not reported" — two different things, and the second one is what
            #   he actually needs to look up.
            #   ⭐built at the `detect` layer, never on those individual `return`s: one more refusal reason later
            #   gets logged automatically too.
            #   ⚠️fold, never truncate: `blocked` can be multiple lines (codex's case carries two pasteable command
            #     lines, a crashed CLI's original text can run several lines), pouring it in as-is would wreck "one
            #     line, one entry", and `splitlines()[0]` would throw away the reason itself (node's crash puts the
            #     stack head in the first two lines, the actually useful sentence comes after). Both constraints
            #     have to hold at once.
            log("refusing to report the %s family: %s" % (family, _one_line(blocked)))
        found[family] = {"head": head, "version": ver, "blocked": blocked}
    return found


CLI_VER_RE = re.compile("[0-9]+(?:[.][0-9]+)+")


def _cli_version(raw) -> str:
    """A CLI's self-reported line → just the part shaped like a version (`1.2.3`), empty if none is recognizable.
    🔴B24's whitelist covers key names, never values: the failure path above stored the OS's original words in this
      same `version` field, and `subprocess.TimeoutExpired`'s original text carries the full path to the
      executable — that is, the username, the directory layout — and this field gets sent to the network as-is by
      `hello_payload()`; `--version` timing out while `--help` does not is already enough to trigger it.
    ⭐The test is a shape whitelist, never a blacklist that strips out paths: the next new way to leak will not
      send a memo first.
    ⚠️At least two segments (`1.2`): with one segment, any run of digits sitting in a path (`C:/Users/alice/…`)
      would get reported as a version.
    ⚠️Never from inside a path: a directory can carry a version (nvm's `…/v20.11.0/claude.cmd`, a hosted Python's
      `…/Python/3.12.10/x64/python.exe`), and the failure text quotes the executable — the first CI run reported
      the runner's Python version as claude's. So a word (split at whitespace, quotes, brackets, commas) holding
      `/` or a backslash is skipped whole."""
    text = str(raw or "")
    for ch in "'" + '"' + "[](),;":
        text = text.replace(ch, " ")
    for word in text.split():
        m = None if ("/" in word or chr(92) in word) else CLI_VER_RE.search(word)
        if m:
            return m.group(0)
    return ""


def catalog(cfg: dict, found: dict) -> list:
    table = {"claude": CLAUDE_MODELS, "codex": CODEX_MODELS}
    out = []
    for family, info in found.items():
        if info.get("blocked"):
            continue
        # ⭐the catalog side and the assembly side must use the same one ruler (`fullmatch`, see `_model_ok`):
        #   letting it through here and rejecting it there means the user sees a model in /v1/models that
        #   bad_requests the moment it is picked.
        extra = [m for m in ((cfg.get("extra_models") or {}).get(family) or []) if isinstance(m, str) and MODEL_RE.fullmatch(m)]
        for m in list(table[family]) + extra:
            if family + "/" + m not in out:
                out.append(family + "/" + m)
    return out


def shown_model(model_id, cat: list) -> str:
    """The model name used when writing to disk (`jobs.log`'s `model`, `bridge.log`): only ever one this bridge has
    actually reported in /v1/models (a closed set); anything else gets written as a sentence that never quotes the
    original. A model name comes from the network: the one that did not clear the gate can be any text the other
    side chose to stuff in (Task 14 review I1, measured: a 500-character model name left 221 bytes of it sitting in
    jobs.log). ⚠️The error body handed back to the caller still carries the original name — that one goes back to
    the person who sent it."""
    if isinstance(model_id, str) and model_id in cat:
        return model_id
    return "(an unreported model name, %d bytes)" % len(str(model_id).encode("utf-8", "replace"))


def resolve_model(model_id, cat: list) -> tuple:
    if not isinstance(model_id, str) or model_id not in cat:
        e = BridgeError("bad_request", "this bridge has never reported this model: %r (see /v1/models)" % (model_id,))
        e.on_disk = "this bridge has never reported this model: %s (see /v1/models)" % shown_model(model_id, cat)
        raise e
    family, _, model = model_id.partition("/")
    return family, model

