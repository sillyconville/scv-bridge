def _children_facts() -> list:
    """§8 "how many child processes are being managed, how much memory": reads the registry (the whole SCV_HOME),
    counting one alive (and reporting its memory) only when its birth id matches."""
    out = []
    for c in children():
        now = proc_start_id(int(c["pid"]))
        alive = None if now is None or not birth_known(c["born"]) else now == c["born"]
        out.append({"pid": c["pid"], "family": c["family"], "alive": alive,
                    "rss_kb": proc_rss_kb(int(c["pid"])) if alive else None})
    return out


CLI_NAMESPACE = ("CLAUDE", "CODEX", "ANTHROPIC_", "OPENAI_", "AI_AGENT")      # the range of names doctor lists (never the values)
# variables the user set themselves that change how a CLI answers, going around the closed set of argv (B27) (15c ⑦2: keep it and disclose it): name -> what it changes (from the docs' own wording, never made up)
BEHAVIOR_VARS = {"CLAUDE_CODE_EFFORT_LEVEL": "it overrides the --effort the bridge passes (docs: overrides --effort)",
                 "MAX_THINKING_TOKENS": "set to 0 to turn thinking off (except Opus 5.5 / Fable), other values only take effect under a fixed thinking budget (docs)",
                 "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING": "Opus 4.6 / Sonnet 4.6 fall back to a fixed thinking budget governed by MAX_THINKING_TOKENS (docs)",
                 "CLAUDE_CODE_DISABLE_THINKING": "turns thinking off (not written in the docs; 15c measured thinking tokens on haiku going 142 to 0)",
                 "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "it changes the output cap for each turn's answer; set it too small and the answer gets cut off (the package's own words: when a reply exceeds the output cap, "
                                                  "set the CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable; claude 2.1.282)"}
# the ones that cause trouble if missing (settled by 15c's zero-cost readings): on win32, missing APPDATA / HOME / USERPROFILE (two families) still behave normally at the --version / login-status
#   level; only when LOCALAPPDATA alone is missing does `cli_head` fail to find the desktop build's own bundled codex. POSIX ⏳ not measured. 📎 NOTES.md::child-env-session-vars
CRITICAL_ENV = ("LOCALAPPDATA",) if os.name == "nt" else ()


def env_facts() -> dict:
    """doctor's "environment" field, names only, never values: the two families of namespaced variables passed to
    the child CLI / the session variables stripped out (`session_bound`) / the ones that change behaviour
    (`BEHAVIOR_VARS` plus `ANTHROPIC_DEFAULT_<alias>_MODEL`: the docs say this alias actually runs the model it
    names) / the critical variables that are missing.
    ⭐Computed from the copy actually handed down (`child_env()`), never freshly from `os.environ` (15c review
      I1). What it describes is this process's own: `cmd_run` records it into bridge.pid when the bridge starts,
      and doctor reports the bridge's own copy while the bridge is running (I2)."""
    sent, said = child_env(), {}
    for k in sorted(sent):
        m = re.fullmatch("ANTHROPIC_DEFAULT_([A-Z]+)_MODEL", k.upper())
        what = BEHAVIOR_VARS.get(k.upper()) or (m and "the name `%s` actually runs the model it points to (docs)" % m.group(1).lower())
        if what:
            said[k] = what
    return {"passed": sorted(k for k in sent if k.upper().startswith(CLI_NAMESPACE)), "stripped": sorted(set(os.environ) - set(sent)),
            "changes": said, "missing": [k for k in CRITICAL_ENV if env_missing(k)]}


def _env_whose(own: dict, rec) -> dict:
    """Whose environment names doctor reports (15c review I2): if the bridge is running and bridge.pid recorded
    its `env_facts()` from the moment it started ⇒ report the bridge's own (`of_bridge`); otherwise report this
    one's own, saying clearly whose it is. A shape that does not fit (an older version never recorded it, or it
    got corrupted) ⇒ treated as not recorded. The critical missing variables (`missing`) follow the same
    reasoning (fix1's addition, the maintainer's ruling ⑦3)."""
    e = rec.get("env") if isinstance(rec, dict) else None
    ok = (isinstance(e, dict) and all(isinstance(e.get(k), list) and all(isinstance(x, str) for x in e[k])
                                      for k in ("passed", "stripped", "missing"))
          and isinstance(e.get("changes"), dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in e["changes"].items()))
    whose = ("the bridge that is running (pid %s, from the moment it started)" % rec.get("pid") if ok else
             "this terminal (the bridge is not running; when the bridge starts, its own environment at that moment is what counts)" if rec is None else
             "this terminal (the running bridge did not record this: it was started by an older version)")
    return dict(e if ok else own, whose=whose, of_bridge=ok, own_missing=own["missing"])   # ⭐doctor's own missing items get their own separate field (fix2 N-M3)


def doctor_facts(cfg: dict, live: bool) -> dict:
    """⭐Reports facts only (B14); the verdict lives separately, in `doctor_verdict`. `proxy_env` belongs to
    doctor's own process; `bridge_proxy_env` was recorded when the running bridge started (§8: the two processes
    can be started from two different terminals, and see different proxies)."""
    found = detect(cfg)
    port = int(cfg.get("port") or 8765)
    state, rec = bridge_owner()
    health = our_health(port)
    facts = {"scv": VERSION, "python": sys.version.split()[0], "os": os.name + "/" + sys.platform,
             "config": str(spath("config.json")), "port": port, "running": health is not None,
             # ⭐I-4: keep the whole dict, never just the names — `doctor_verdict` also needs each one's own
             #   `blocked` flag (already only a version shape plus a bool, no paths, see `Bridge.health`).
             "bridge_families": health["families"] if isinstance((health or {}).get("families"), dict) else None,
             "bridge": {"state": state, "pid": rec.get("pid"), "port": rec.get("port")},
             "children": _children_facts(), "remote_url": cfg.get("remote_url") or "(not paired: no outside address is reached)",
             "proxy_env": proxy_env(),
             "bridge_proxy_env": _remask(rec.get("proxy_env")) if state == "alive" else "(no bridge is running)",
             "env": _env_whose(env_facts(), rec if state == "alive" else None), "families": {}}
    for family, info in found.items():
        entry = {"path": info["head"][-1], "version": info["version"], "blocked": info["blocked"]}
        if family == "claude" and info["blocked"]:
            # 🔴F-1: never ask `auth status` of a claude old enough to be blocked — if it does not recognize that argument, it will send this as a real call with it as the prompt
            entry["auth"] = {"logged_in": False, "method": "", "plan": "",
                             "probe_error": "not asked: this family is blocked (see blocked); an old version of claude would send an argument it does not recognize as a real call with it as the prompt"}
        else:
            entry["auth"] = auth_status(family, info["head"])
        # ⭐both families' login commands use the resolved executable (`fix_hint` goes out over the network and can only give the bare name, which may not be on PATH)
        entry["login_cmd"] = login_cmd(family, info["head"])
        if family == "codex":
            entry["codex_off_unknown"], entry["codex_off_error"] = codex_features_unknown(info["head"])
            entry["carried"] = codex_carried()      # never starts a session (zero cost); `--live` has its own copy of what codex itself says (`live.carried`)
        if live and not info["blocked"]:
            # ⭐the cheapest tier of each family (never opus)
            entry["live"] = live_check(cfg, family, {"claude": CLAUDE_MODELS, "codex": CODEX_MODELS}[family][0])
        facts["families"][family] = entry
    if unused_codex_home(cfg):
        facts["unused_codex_home"] = str(cfg["codex_home"])
    return facts


def unused_codex_home(cfg: dict) -> str:
    """`codex_home`, written into config.json by an old install (Task 0c-13, that era's default, never something
    he chose) ⇒ one sentence of plain English; nothing there ⇒ an empty string.
    ⛔Never reads it any more, never rewrites config.json for him (the maintainer's ruling); doctor prints it, and
    starting the bridge also logs one line to bridge.log (13c review M10: someone who deliberately set up
    isolation back then would have it quietly disappear ⇒ it has to complain loudly in at least two places, and
    also tell him this key can be deleted)."""
    if "codex_home" not in cfg:
        return ""
    return ("config.json's codex_home (%s) is no longer used: the codex this bridge starts uses your own CODEX_HOME (~/.codex if unset); "
            "to have it use a different directory, set the CODEX_HOME environment variable; this key can be deleted from config.json" % cfg["codex_home"])


def _carried_lines(stat, said) -> list:
    """The lines for "what codex will carry into every call". `said` = what codex itself said in that one real
    session from `doctor --live`: if there is one, trust only it (more accurate than stat: it gets both "the
    override is whitespace only" and an AGENTS.md in a git ancestor directory right); none / could not tell ⇒
    plain doctor's lines (a stat-only estimate), and it has to say when it could not tell (`--live` must never
    say less than plain doctor, and must never pass an estimate off as something codex said). 📎 NOTES.md::codex-user-home"""
    def size(f):
        return "%d bytes" % f["bytes"] if f.get("error") is None else "could not read its size: " + f["error"]

    def one(f):
        return "⚠️ codex will carry your %s (%s) into every call: codex has no switch to turn it off, and this bridge cannot block it" % (f["path"], size(f))

    if said and "sources" in said:
        return [one(f) for f in said["sources"]]
    stat = stat or {}
    out = [one(f) for f in stat.get("files") or ()]
    if stat.get("if_blank"):
        out.append("⚠️ the override is there, but if it is all whitespace codex will switch to AGENTS.md (%s): %s "
                   "(a --live check asks codex itself which one it is using)" % (size(stat["if_blank"]), stat["if_blank"]["path"]))
    if said:
        out.append("⚠️ could not tell which instruction files this session will carry: %s ⇒ the lines above only "
                   "estimate from files under CODEX_HOME, and cannot see one in a git ancestor directory"
                   % said["error"])
    return out


def doctor_verdict(facts: dict) -> tuple:
    """facts -> human-facing lines + the families with problems. ⭐The pasteable command (the login one inside
    blocked) is printed with real line breaks: the copy in the JSON is escaped.
    ⚠️Wording (an earlier measurement): `auth status` / `login status` only say "there are local credentials",
    never "the credentials still work" — the real test is `--live`.
    ⭐The whole `--live` line is only printed once, at the end (hanging one on every family's own line would
      flood the screen)."""
    lines, bad, want_live = [], [], False
    for fam in ("claude", "codex"):
        env = facts["env"]
        e, gone = facts["families"].get(fam), (env["missing"] if fam == "codex" and not env["of_bridge"] else [])
        if e is None:
            if gone:        # ⭐the missing variable is exactly why it could not be found (15c ⑦6): never call it "not on PATH", never send the user to install something already installed
                bad.append(fam)
                lines.append("❌ codex: not found — %s's environment has no %s (the desktop build's own bundled codex is found through it), most likely an "
                             "agent running commands for you filtered the environment ⇒ please start the bridge in your own terminal:" % (env["whose"], ", ".join(gone)) + NL + self_cmd("start"))
            elif fam == "codex" and env["own_missing"]:     # the bridge is running, and only this terminal itself is missing it (fix2, re-review N-M3): say clearly this only concerns this terminal, never a ❌
                lines.append("· codex: this terminal's own probe did not find it — this terminal's environment has no %s (the desktop build's own bundled codex is found through it); "
                             "this says only about this terminal, and has nothing to do with the bridge that is running" % ", ".join(env["own_missing"]))
            else:
                lines.append("· %s: not found on this machine (not on PATH, and config.json's %s_bin does not point to one either)" % (fam, fam))
            continue
        auth, live = e["auth"], e.get("live")
        if e["blocked"]:
            bad.append(fam)
            lines.append("⛔ %s: this family is not reported: %s" % (fam, e["blocked"]))
        elif auth["probe_error"]:
            bad.append(fam)
            lines.append("⚠️ %s: could not tell whether there are local credentials: %s" % (fam, auth["probe_error"]))
        elif not auth["logged_in"]:
            bad.append(fam)
            lines.append("⚠️ %s: no local credentials ⇒ run this yourself (copy the whole line): %s%s" % (fam, NL, e["login_cmd"]))
        else:
            want_live = want_live or live is None
            lines.append("✅ %s %s: local credentials found (%s)%s" % (
                fam, _cli_version(e["version"]), "/".join(x for x in (auth["method"], auth["plan"]) if x),
                " — whether the credentials still work needs one real call with --live to know (the command is at the end)" if live is None else ""))
        if not e["blocked"] and facts["running"]:
            # ⭐I-4: doctor's own detect() is fresh, but the running bridge only ever detected once, at start
            #   (README says so) ⇒ a family that comes back afterward (the player logs Codex in while the bridge
            #   keeps running) stays off until restarted, and doctor must not look all green about it. Judged by
            #   the bridge's own copy (`bridge_families`): missing entirely counts the same as `blocked: true`.
            bfam = (facts.get("bridge_families") or {}).get(fam)
            if bfam is None or bfam.get("blocked"):
                bad.append(fam)
                lines.append(("⚠️ the running bridge still has %s off (it checks only when it starts) ⇒ stop, "
                              "then start:" % fam) + NL + self_cmd("stop") + NL + self_cmd("start"))
        if live is not None and "error" in live:
            bad.append(fam)
            err = live["error"]
            step = e["login_cmd"] if err["type"] == "auth_required" else err["fix_hint"]
            # ⭐with no ready-made next step (`fix_hint` empty: the `unknown` classes), it settles for the original words — the handshake-refusal sentences already carry their own next
            #   step (`CODEX_REFUSED`); this used to tack on an empty arrow and a new line saying "see the original words above" (outside 13c's re-review scope)
            lines.append("❌ %s's real call failed (%s): %s" % (fam, err["type"], err["message"]) + (" ⇒" + NL + step if step else ""))
        elif live is not None and live["canary_leaked"]:
            bad.append(fam)
            lines.append("🔴 %s read the canary outside the working directory ⇒ this family's tools are not locked down, never connect it" % fam)
        elif live is not None and not live["ok"]:
            bad.append(fam)
            lines.append("⚠️ %s answered, but got the addition we asked wrong ⇒ read its own words and judge for yourself: %s" % (fam, live["answer"]))
        elif live is not None:
            # ⚠️only say what the judge actually knows (review M-3): what it judges is "is there a fragment of the secret in the answer", never "did it read it" — read it and stayed
            #   quiet, paraphrased it, pasted only a small fragment — this ruler cannot see any of those ⇒ never write it as "never read it"
            lines.append("✅ %s's real call went through (first token in %ss); the canary outside the working directory never showed up in the answer" % (fam, live["ttfc"]))
        if e.get("codex_off_unknown"):
            lines.append("⚠️ codex no longer recognizes these feature names it was told to turn off (most likely renamed ⇒ the new name may not be off): %s"
                         % ", ".join(e["codex_off_unknown"]))
        elif e.get("codex_off_error"):
            lines.append("⚠️ codex's list of features to turn off, this time, %s" % e["codex_off_error"])
        lines += _carried_lines(e.get("carried"), (live or {}).get("carried"))
    env = facts["env"]      # ⭐names only (15c ⑦2): the ones that change behaviour are his own settings ⇒ keep them, say clearly what they change; the stripped ones belong to the identity of the agent session that started it
    if env["of_bridge"] and env["missing"] and "codex" not in (facts["bridge_families"] or ["codex"]):
        bad.append("codex")         # ⭐judged by the bridge's own copy (fix1's addition): the environment it started with is missing it, and its /healthz genuinely has no codex either (could not tell ⇒ never draw a conclusion)
        lines.append("❌ codex: %s could not find it — the environment it started with has no %s (the desktop build's own bundled codex is found through it), most likely an agent running "
                     "commands for you filtered the environment ⇒ stop it, then restart in your own terminal:" % (env["whose"], ", ".join(env["missing"])) + NL + self_cmd("stop")
                     + NL + self_cmd("start"))
    lines.append("· the two families of variables passed to the child CLI by %s (names only): %s" % (env["whose"], ", ".join(env["passed"]) or "(none)"))
    lines.append("· the session variables stripped by %s (the identity of the agent session that started it, never passed to the child CLI): %s" % (env["whose"], ", ".join(env["stripped"]) or "(none)"))
    lines += ["⚠️ %s is in the environment (%s; you set it yourself, passed to the child CLI unchanged): %s" % (k, env["whose"], v) for k, v in sorted(env["changes"].items())]
    if facts.get("unused_codex_home"):
        lines.append("· " + unused_codex_home({"codex_home": facts["unused_codex_home"]}))
    kids = [c for c in facts["children"] if c["alive"]]
    lines.append("✅ the bridge is running (port %d)" % facts["port"] if facts["running"] else
                 "· the bridge is not running (no answer on port %d) ⇒ start it:" % facts["port"] + NL + self_cmd("start"))
    lines.append("· %d CLI child process(es) in the registry are still alive, %d MB total" % (len(kids), sum(c["rss_kb"] or 0 for c in kids) // 1024))
    if want_live:
        lines.append("· whether the credentials still work: one real call per family plus the canary self-check (costs a little quota):" + NL + self_cmd("doctor", "--live"))
    return lines, sorted(set(bad))


def cmd_doctor(args) -> int:
    """stdout starts with the whole JSON of facts (machine-readable: `json.JSONDecoder().raw_decode`), then the
    human-facing verdict."""
    facts = doctor_facts(load_config(), live=bool(getattr(args, "live", False)))
    lines, bad = doctor_verdict(facts)
    print(json.dumps(facts, ensure_ascii=False, indent=2) + NL)
    print(NL.join(lines))
    if bad:
        log("doctor: families with problems: %s" % ", ".join(bad))
        print("⇒ families with problems: %s (each one's own line above says what to do)" % ", ".join(bad))
    return 1 if bad else 0
