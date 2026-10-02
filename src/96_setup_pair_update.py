def cmd_setup(args) -> int:
    """Run once after install: every probing action lives here and is deterministic, the agent's only job is to
    read the output back to the person exactly as it is (B31).
    ⭐= doctor (`--live` passed through) plus a "what's next" block. ⛔Never installs a startup launcher, never
      changes PATH, never logs in on someone's behalf (adding a startup launcher would mean re-judging E20's
      `allow_remote` window). The subcommands named in "what's next" are checked against real output by
      tests/test_70_local_api.py::PromisedCommands."""
    cfg = load_config()                       # also casts the local token while it is at it
    print("scv %s: configured at %s (the local token is already in there). First, a health check — facts, never conclusions:" % (VERSION, spath("config.json")))
    rc = cmd_doctor(args)
    print(NL.join(["", "What's next (copy the whole command line; if it's split by shell, pick the line for the one you use):",
                   "Start the bridge (OpenAI-compatible base_url: %s/v1):" % (LOCAL_URL % int(cfg.get("port") or 8765)), self_cmd("start"),
                   "See the local token (put it in the api_key field of an OpenAI-compatible client):", self_cmd("token"),
                   "One real call per family plus the canary self-check (costs a little quota):", self_cmd("doctor", "--live"),
                   'Only needed if you are connecting remote work (never paired = not one byte goes out to the internet): append " <url> --code <pairing code>" to this line and run it:',
                   self_cmd("pair"),
                   "Installing from setup.md for someone? Once the bridge is running, finish its step 6 (ask them about an audit) and step 8 (note where the bridge is, and tell them where you noted it)."]))
    return rc


PAIR_TOKEN_RE = re.compile("[!-~]{1,512}")     # printable ASCII, no whitespace: it has to land on disk, and go into the `Authorization` header of every call


def cmd_pair(args) -> int:
    """§6 step 5 (optional): trade a one-time pairing code for a token, stored in config.json. ⚠️Only handles the
    seed-stage path (a code sent by hand).
    ⭐Every failure path is judged out before config.json is ever written ⇒ the sentences saying "config.json was
      not touched" are true.
    ⭐The token is data from the far side of the network ⇒ it only lands on disk once its shape passes (one with a
      newline in it would only blow up urllib the next time it dials). ⚠️The code and the token never go into the
      log, never show on screen."""
    url = str(args.url).strip().rstrip("/")
    why = remote_url_refused(url)
    if why:
        return _cmd_failed("not paired (not one byte was sent, config.json was not touched): " + why,
                           "ask the other side for an address starting with https, swap it into the command above, and run it again")
    req = urllib.request.Request(url + REMOTE_PATHS["pair"], method="POST", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"code": str(args.code), "bridge_version": VERSION}).encode("utf-8"))
    try:
        reply = json.loads(_fetch(req, 30).decode("utf-8") or "{}")
    except NET_ERRORS as e:
        return _cmd_failed("could not pair (config.json was not touched): %s ⇒ %s: %s" % (url, type(e).__name__, repr(str(e))[:128]),
                           "403 = the code is wrong or already used (a pairing code is one-time, ask the other side for a new one); could not connect = "
                           "check the address, network, and proxy first (the proxy variables are visible in doctor):" + NL + self_cmd("doctor"), "the original words in full: " + _no_ctrl(_one_line(e)))
    token = reply.get("token") if isinstance(reply, dict) else None
    if not (isinstance(token, str) and PAIR_TOKEN_RE.fullmatch(token)):
        return _cmd_failed("could not pair (config.json was not touched): %s did not reply with a usable token (needs 1-512 printable ASCII characters, no whitespace)" % url,
                           "this is a problem on the other side: show this line to them")
    cfg = load_config()
    old = str(cfg.get("remote_url") or "")
    cfg.update(remote_url=url, remote_token=token, wake_on_start=True)   # 0.3.0 (spec B35): the first start after pairing is awake
    save_config(cfg)
    swapped = " (replacing the previous %s)" % old if old and old != url else ""
    log("pair: paired to %s%s" % (url, swapped))
    print(NL.join(["paired: %s%s. The token is stored in %s" % (url, swapped, spath("config.json")),
                   "the next time the bridge starts it is awake and dials out to: %s (so the service sees it right away); "
                   "every start after that is asleep until the service's page or the wake subcommand wakes it" % ", ".join(p for k, p in REMOTE_PATHS.items() if k != "pair"),
                   "if the bridge is currently running, it is not using this yet: stop it, then start it again:", self_cmd("stop"), self_cmd("start"),
                   "to disconnect this side: delete remote_token from config.json",
                   "installing from setup.md for someone? after the restart, finish its step 6 (ask them about an audit) and step 8 (note where the bridge is, and tell them where you noted it)"]))
    return 0


COMMIT_RE = re.compile("[0-9a-f]{7,40}")       # ⭐always `fullmatch`: `match` plus `$` lets a trailing newline through (`_model_ok` fell into this same hole)
SHA256_RE = re.compile("[0-9a-f]{64}")
SCV_PY_MAX_BYTES = 2 * 1024 * 1024            # how big a scv.py can be (about 270 KB today): over this, whatever was fetched is not it (review M-7)


def cmd_update(args, target: Path | None = None) -> int:
    """B29: what gets pinned is the single file's sha256, never a tag. The hash comes from the dispatcher (or a
    release page), the file comes from the public repository — only if both sides are compromised at once does
    anything go wrong.
    ⭐Every judgment that can fail happens before anything is touched (shape / could not fetch / hash mismatch /
      not Python) ⇒ the sentences saying "nothing was touched" are true; the two writes to disk that follow are
      each atomic and each say clearly what they did: save the previous version first, and never swap if it
      cannot be saved (swapping without keeping a previous version means having nothing to roll back to).
    ⭐Written and compared by bytes: text mode on win32 turns LF into CRLF, and the hash would no longer be that
      hash. 📎 NOTES.md::update-bytes
    ⚠️`target` defaults to `__file__`: this is no longer a single-file-era assumption ("the whole bridge is this
      one file") that the module split has to revisit — it holds for good, since players always get the one file
      the build joins together, so `__file__` is the whole bridge."""
    target = Path(target) if target is not None else Path(os.path.abspath(__file__))
    commit, want = str(getattr(args, "commit", "") or ""), str(getattr(args, "sha256", "") or "").lower()
    source = "the command line"
    if not (commit or want):                  # never let half from the command line and half from latest.json get spliced into a pair that nobody actually gave
        latest = {}
        with contextlib.suppress(OSError, ValueError):
            latest = json.loads(spath("latest.json").read_text(encoding="utf-8"))
        latest = latest if isinstance(latest, dict) else {}
        commit, want = str(latest.get("commit") or ""), str(latest.get("sha256") or "").lower()
        source = "latest.json (given by the dispatcher at the bridge's last hello)"
    if not (COMMIT_RE.fullmatch(commit) and SHA256_RE.fullmatch(want)):
        return _cmd_failed("not updated (nothing was touched): need a pair, --commit <a 7-40 character lowercase hex commit id> and --sha256 <64 hex characters>, "
                           "what came from %s was %s / %s" % (source, repr(commit)[:48], repr(want)[:72]),
                           "give both together; give neither and it uses the pair the dispatcher gave a paired bridge's last hello (%s)" % spath("latest.json"))
    try:
        current = target.read_bytes()
    except OSError as e:
        return _cmd_failed("not updated (nothing was touched): could not read the current copy at %s: %s" % (target, _one_line(e)), "see the OS's own words above")
    if hmac.compare_digest(hashlib.sha256(current).hexdigest(), want):
        print("already at this version (%s's sha256 is already %s), nothing was touched" % (target, want))
        return 0
    url = "%s/%s/scv.py" % (UPDATE_BASE, commit)
    try:
        data = _fetch(urllib.request.Request(url), 60, limit=SCV_PY_MAX_BYTES)
    except NET_ERRORS as e:
        return _cmd_failed("not updated (nothing was touched): could not fetch %s ⇒ %s: %s" % (url, type(e).__name__, repr(str(e))[:128]),
                           "404 = the public repository has no such commit; could not connect = check the network and proxy first (the proxy variables are visible in doctor):" + NL + self_cmd("doctor"),
                           "the original words in full: " + _no_ctrl(_one_line(e)))
    got = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(got, want):
        return _cmd_failed("not updated (nothing was touched): the sha256 does not match. expected %s / actual %s / file from %s" % (want, got, url),
                           "the expected value came from %s; one side or the other is wrong — never work around this, go ask whoever gave you this pair" % source)
    try:
        ast.parse(data.decode("utf-8"))
    except (SyntaxError, ValueError, RecursionError) as e:
        return _cmd_failed("not updated (nothing was touched): the hash matches, but the content is not valid Python: %s: %s" % (type(e).__name__, _one_line(e)),
                           "show this line to whoever published this pair")
    prev = Path(os.path.abspath(spath(SCV_PREV)))   # every place a person sees uses an absolute path (a relative SCV_HOME follows the cwd; outside the re-review's scope ④)
    try:
        _atomic_write(SCV_PREV, current)
    except OSError as e:
        return _cmd_failed("not updated (nothing was touched): could not save the previous version (%s): %s" % (prev, _one_line(e)),
                           "if the previous version cannot be saved, never swap: fix the state directory's (SCV_HOME) problem first, then run that same command again")
    try:
        _replace_file(target.with_name(target.name + ".new"), target, data)   # the exception is written into the derived gate (review M-2)
    except OSError as e:
        return _cmd_failed("did not swap: %s is still the previous version (%s); %s is already a copy of it" % (target, _one_line(e), prev),
                           "most likely something else has it open (an editor / antivirus) or there is no write permission; fix that, then run that same command again")
    log("update: %s was swapped to %s (sha256 %s, this pair came from %s); the previous version is at %s" % (target, commit, got, source, prev))
    # ⭐both paths are turned absolute (`state_dir()` never expands `~` or makes paths absolute: a relative path would follow the cwd at the moment it is pasted, review M-3)
    diff = paste_cmd(["git", "diff", "--no-index", os.path.abspath(prev), os.path.abspath(target)])
    print(NL.join(["updated to %s (sha256 %s; this pair came from %s)" % (commit, got, source),
                   "the previous version is left as-is at: %s (to roll back, copy it back to %s)" % (prev, target),
                   "to see what changed this time: both copies are on this machine; if git is installed, run (copy the whole line):", diff]
                  + ["⚠️ if the bridge is currently running, it is still the old code: stop it, then start it again:", self_cmd("stop"), self_cmd("start")]))
    return 0
