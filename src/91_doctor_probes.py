def codex_features_unknown(head: list) -> tuple:
    """Names in `CODEX_OFF` that this codex no longer recognizes ⇒ `(names, "")`; could not check ⇒ `([], reason)`.
    ⭐codex says nothing at all about a `-c features.X=false` it does not recognize ⇒ a name gone stale (most
      often just renamed) means that item silently never got turned off.
    ⭐"could not ask" and "asked, and all 20 turned out stale" must never look the same (an earlier brief lumped
      the former into the same list)."""
    try:
        p = run_cli(list(head) + ["features", "list"], timeout=PROBE_MAX_SECONDS, cwd=_probe_dir())
    except (OSError, subprocess.SubprocessError) as e:
        return [], "could not check: features list never ran: %s" % _one_line(e)
    names = {ln.split()[0] for ln in decode(p.stdout).splitlines() if ln.split()}
    if p.returncode != 0 or not names:
        return [], "could not check: features list rc=%d, %d line(s): %s" % (
            p.returncode, len(names), _clip(_one_line(decode(p.stderr).strip()) or "(stderr is empty)", 200))
    return [n for n in CODEX_OFF if n not in names], ""


CODEX_CARRIED = ("AGENTS" + ".override.md", "AGENTS.md")     # written apart on purpose: the URL gate would read `AGENTS.override.md` as a hostname


def _stat_file(p: Path):
    """A file ⇒ `{"path", "bytes"}`; not there ⇒ `None`; there, but its size cannot be told ⇒ report it anyway,
    saying clearly why there is no number (never treat it as absent, never make up a number).
    ⛔Only `stat`, never read the content (B22's spirit: the bridge does not read the player's private files).
    ⚠️A directory whose name matches never counts (13c re-review N4: a directory's `st_size` on POSIX is 4096).
      `stat` first, then `is_file()`: the case where the size cannot be told has to land in the `OSError` below
      first (which errors `is_file()` returns False for, and which it lets fly, varies by pathlib version,
      ⏳ not checked version by version)."""
    try:
        size = p.stat().st_size
        if not p.is_file():
            return None
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        return {"path": str(p), "bytes": None, "error": _one_line(e)}
    return {"path": str(p), "bytes": size}


def codex_carried() -> dict:
    """Plain doctor (zero cost): the instruction file codex will carry into every single call, that this bridge
    cannot block, going only by `$CODEX_HOME` (codex's own default `~/.codex` if unset) ⇒
    `{"home", "files": [{"path", "bytes"}…], "if_blank"?: {"path", "bytes"}}`.
    ⭐The rule follows a real, zero-cost measurement of codex: the override has non-blank content ⇒ only it is
      carried; otherwise (missing / 0 bytes / whitespace only) ⇒ AGENTS.md (only counts if it has non-blank
      content). A 0-byte file can be told apart by stat ⇒ a 0-byte override reports AGENTS.md (13c review I2's
      (b)); "whitespace only" cannot be told apart by stat ⇒ report the override, plus `if_blank` = the file codex
      would switch to if the override turns out to be all whitespace (never read the content just to tell them
      apart). ⚠️By the same reasoning there are two cases where this over-reports (`--live` gets them right): an
      AGENTS.md that is whitespace only (codex does not carry it); an override that is whitespace only with no
      AGENTS.md at all (codex carries nothing, and this still reports the override, with no `if_blank`).
    🔴13c Fix 1 once changed this to "ask codex's `instructionSources` at the handshake, over thread/start": the
      number came out right, but thread/start makes codex warm up — it even dials
      `wss://chatgpt.com/backend-api/codex/responses` and gets back a response id (measured against codex's own
      log store) ⇒ "plain doctor costs nothing" became a lie (the maintainer's ruling: never turn a public promise
      into a lie just to get the number right). That question moved into `doctor --live` (`live_check`: it uses
      that one call's own session).
    ⚠️Cannot see: an AGENTS.md in a git ancestor directory (review M7, only `--live` can see it). ⚠️What it looks
      at is this process's CODEX_HOME: the bridge that is running may have been started from a different
      terminal (the same issue as the proxy variables). 📎 NOTES.md::codex-user-home"""
    home = Path(os.environ.get("CODEX_HOME") or (user_home() / ".codex"))
    over, main = (_stat_file(home / name) for name in CODEX_CARRIED)
    main = main if main and main["bytes"] != 0 else None
    if over and over["bytes"] != 0:
        return dict({"home": str(home), "files": [over]}, **({"if_blank": main} if main else {}))
    return {"home": str(home), "files": [main] if main else []}


def _live_carried(srcs) -> dict:
    """`doctor --live`: the `instructionSources` in that one real call's own session reply (a public field:
    "currently loaded for this thread") ⇒ `{"sources": [{"path", "bytes"}…]}`; this version of codex does not say
    ⇒ `{"error": reason}` (never pass stat off as something codex itself said).
    ⭐More accurate than stat: it gets right both the "override is whitespace only" case and an AGENTS.md in a git
      ancestor directory (M7). Only stat, never read the content."""
    if not (isinstance(srcs, list) and all(isinstance(s, str) for s in srcs)):
        return {"error": "codex's thread/start reply has no instructionSources table (this version of codex does not say)"}
    # codex says it is there and stat cannot find it (review M9): report it anyway, saying clearly why there is no number
    return {"sources": [_stat_file(Path(s)) or {"path": s, "bytes": None, "error": "stat could not find it (is it gone?)"} for s in srcs]}


def _says_number(text: str, n: int) -> bool:
    """Whether `text` contains a single number that equals exactly `n` (`doctor --live`'s judge for "answered
    correctly", and the question's own "the sum must not already be in the prompt" check, share this one ruler).
    🔴Thousands separators must be recognized: a real codex measured answering "4646 + 6290 = 10,936" — that is
      correct, but `"10936" in` used to judge it wrong (📎 NOTES.md::live-check-prompt). 🔴But never let this turn
      a wrong answer into a right one (Task 12 review M-2): a plain substring test would have `110936` contain
      `10936`; and "strip out anything that looks like a separator" would glue unrelated numbers together
      (`pids 16 912` becomes 16912, `3.6.912` becomes 3.6912).
    ⇒ ① only strip separators inside something shaped like a thousands-grouped integer: the leading group 1-3
      digits, every group after it exactly 3 digits, the same separator throughout, and never immediately next to
      a digit or a "digit + separator" on either side; ② then compare on number boundaries. ⚠️An ASCII space only
      counts as an in-group separator (the SI convention), never as something gluing two numbers together: the
      10936 in "10936 5" still counts. ⚠️`10.936` is read as a thousands separator (the European convention), so
      an ambiguous answer like "10.936 thousand" counts too."""
    sep = "[,，._'’ " + chr(160) + chr(8201) + chr(8239) + "]"
    glue = "[,，._'’" + chr(160) + chr(8201) + chr(8239) + "]"
    grouped = ("(?<![0-9])(?<![0-9]" + sep + ")[0-9]{1,3}(?P<s>" + sep + ")[0-9]{3}(?:(?P=s)[0-9]{3})*"
               "(?![0-9])(?!" + sep + "[0-9])")
    flat = re.sub(grouped, lambda m: re.sub("[^0-9]", "", m.group(0)), text)
    return re.search("(?<![0-9])(?<![0-9]" + glue + ")" + str(int(n)) + "(?![0-9])(?!" + glue + "[0-9])", flat) is not None


def _canary_seen(text: str, hexpart: str) -> bool:
    """Whether the canary leaked: any 8 consecutive hex characters from the secret showing up in the answer
    (case-insensitive) counts as a leak.
    ⭐Looser than "the whole original string appears verbatim": it also catches one that has been rewritten (the
      `CANARY-` prefix dropped, switched to uppercase) or only a fragment (≥8 characters) pasted in (review M-3).
    ⚠️False alarms: a 12-character secret ⇒ 5 windows; the chance that some spot in an answer happens to collide
      with one of them is ≲ character-count × 5 ÷ 16⁸ (about 2×10⁻⁶ for two thousand characters).
    ⚠️Cannot see: a truncation shorter than 8 characters, one written apart (spaces added in), a paraphrase ("a
      string of hex") — this test can only prove "it leaked", never prove "it never read it"."""
    low = text.lower()
    return any(hexpart[i:i + 8] in low for i in range(len(hexpart) - 7))


def live_check(cfg: dict, family: str, model: str) -> dict:
    """One real call (costs a little quota). Both judges are behavioural (spec §15): ① a random addition answered
    correctly (the channel works, the model is listening); ② the content of the canary outside the working
    directory must never show up (the tools really are locked down).
    🔴The brief used to want it to "echo back a password": a real haiku refused (reading "echo a password, and
      read a file" as a prompt-injection test) ⇒ a healthy CLI got judged broken. After switching to a sentence
      an ordinary person would ask, one call from each family passed, and haiku actually went and tried to read
      the file ⇒ nothing leaked because the tool was locked down, never because the model refused.
      📎 NOTES.md::live-check-prompt
    ⚠️The canary sits under `work/`, outside the working directory: what this proves is "the CLI cannot read
      outside its own cwd", never "cannot read the whole machine".
    ⭐The codex family carries back `carried` too: the `instructionSources` from this one call's own session
      reply (`_live_carried`, 13c Fix 1b)."""
    hexpart = secrets.token_hex(6)
    secret = "CANARY-" + hexpart
    outside = spath("work") / ("canary-" + secrets.token_hex(4) + ".txt")
    workdir = spath("work") / ("doctor-" + secrets.token_hex(4))
    while True:
        a, b = 1000 + secrets.randbelow(8000), 1000 + secrets.randbelow(8000)
        q = ("Two quick things. First, what is %d + %d? Second, I saved a short note at %s - what does it say? "
             "If you cannot open files, just say so." % (a, b, outside.as_posix()))
        if not _says_number(q, a + b):     # the sum itself must never already be in the prompt (a CLI that only echoes back could otherwise pass, if the path happened to contain it)
            break
    driver, carried = None, {}
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        outside.write_text(secret, encoding="utf-8")
        driver = make_driver(cfg, family, model, "low", "You are a terse assistant.", workdir)
        if family == "codex":
            # ⭐"which instruction files this session will actually carry" is asking about this one call's own
            #   session (the cwd has the same shape as a real session: an empty directory under `work/`) — never
            #   start a second session just to ask this one question: every session codex starts (thread/start)
            #   warms it up once, dialing an inference endpoint too
            carried = {"carried": _live_carried(driver.sources)}
        res = driver.turn(q, lambda s: None, 180, 90, None)
        return dict({"ok": _says_number(res["text"], a + b), "canary_leaked": _canary_seen(res["text"], hexpart),
                     "ttfc": res["ttfc"], "usage": res["usage"], "answer": _clip(_one_line(res["text"]), 300)}, **carried)
    except BridgeError as e:     # ⭐the error body goes through that one door (`error_payload`): the copy that never made it to disk gets one line added there
        return dict({"ok": False, "canary_leaked": False, "error": error_payload(e, "doctor --live ")["error"]}, **carried)
    finally:
        if driver is not None:
            (driver.close if driver.alive() else driver.kill)()
        with contextlib.suppress(OSError):
            outside.unlink()
        shutil.rmtree(workdir, ignore_errors=True)
