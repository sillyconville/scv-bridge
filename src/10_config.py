class BridgeError(Exception):
    """`klass` = the category (auth_required / quota / timeout / crashed / cancelled / bad_request /
    local_rate_limit / unknown), `raw` = the CLI's original words, not a character changed (B21).
    `logged` = has this one already logged a line to `bridge.log` (`_fail()` sets it). ⭐It is an explicit flag,
    never guess from `klass` who already wrote it to disk: the same `bad_request` can come from `_fail()` (already
    logged) or from `SessionManager` / the HTTP layer's argument validation (not logged) — and it is the latter
    that the layer that finally catches it needs to make up for.
    `on_disk` = the words used for the line that fills in the logging (defaults to `raw`): the spot in the original
    words that quotes the other side's free text is swapped for a sentence that does not quote the original
    (`resolve_model`)."""

    def __init__(self, klass: str, raw: str, family: str = ""):
        super().__init__(klass + ": " + raw)
        self.klass, self.raw, self.family, self.on_disk = klass, raw, family, raw
        self.logged = False


DEFAULT_CONFIG = {"port": 8765, "max_concurrent": 4, "remote_url": "", "remote_token": "",
                  "remote_jobs_per_hour": 600, "allowed_origins": [], "claude_bin": "", "codex_bin": "",
                  "keep_awake_s": 600, "extra_models": {"claude": [], "codex": []}}

_cfg_lock = threading.RLock()   # reentrant: the path that mints a token re-enters save_config() once more


def _tmp_path(name: str) -> Path:
    """The one constructor for a disk write buffer: the name carries a pid so it never steps on the one another
    process is currently writing (writing the same target file concurrently is a straight WinError 32 on Windows)."""
    return spath("tmp/" + name + "." + str(os.getpid()))


def _cfg_tmp() -> Path:
    """The write buffer for config.json."""
    return _tmp_path("config.json")


def _replace_file(tmp: Path, dest: Path, data) -> None:
    """The one way to replace a whole file: write the buffer first, then `os.replace` (`write_text` truncates on
    open, so dying halfway through means the previous copy is gone too).
    🔴The failure path cleans up its own buffer: the old code wrote this three times over with not one `except`
      between them ⇒ every failure left a copy behind (even a half-written one). ⭐`finally` plus a flag (the
      first item in the settled-idioms block). `bytes` is written as bytes; `str` is written as utf-8 text
      (newlines become CRLF on win32).
    ⚠️This file has only two callers: `_atomic_write` (files in the state directory) and `cmd_update` (the
      installed scv.py — its buffer has to sit right beside it: `os.replace` across drives blows up); the release
      tool in the repository, `tools/release.py`, calls it once more (to write setup.md, not on the bridge's own
      run path). Gate: tests/test_90_cli.py::TmpSweep::test_every_buffered_write_goes_through_one_place"""
    done = False
    try:
        if isinstance(data, bytes):
            tmp.write_bytes(data)
        else:
            tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, dest)
        done = True
    finally:
        if not done:
            with contextlib.suppress(OSError):
                tmp.unlink()


def _atomic_write(name: str, data) -> None:
    """Whole-file replace for a file in the state directory: the buffer sits at `tmp/<name>.<pid>` (only the kind
    of leftover that piles up across restarts is swept up by `sweep_tmp()` when the bridge starts)."""
    _replace_file(_tmp_path(name), spath(name), data)


def sweep_tmp() -> list:
    """When the bridge starts, clean out the write buffers in `tmp/` whose process is no longer there (the name's
    tail is the pid that wrote it, see `_tmp_path`).
    Never touches a live one (this process itself is live too): that could be the very copy another `scv` process
    is about to `os.replace`, and deleting it blows up on the spot.
    ⚠️The one whose pid the system recycled to a different live process stays until that process dies (bounded,
    never guessed)."""
    d, gone = spath("tmp"), []
    for p in (d.iterdir() if d.is_dir() else ()):
        pid = p.name.rpartition(".")[2]
        if pid.isdigit() and proc_start_id(int(pid)) == "":
            with contextlib.suppress(OSError):
                p.unlink()
                gone.append(p.name)
    if gone:
        log("cleaned up %d write buffer(s) left behind by dead processes in tmp/: %s" % (len(gone), ", ".join(gone[:8])))
    return gone


def save_config(cfg: dict) -> None:
    p = spath("config.json")
    with _cfg_lock:
        _atomic_write("config.json", json.dumps(cfg, ensure_ascii=False, indent=2))
        with contextlib.suppress(OSError):
            os.chmod(p, 0o600)


def _mint_token(cfg: dict, p: Path) -> dict:
    """Mints local_token. ⭐Only one winner is allowed across processes: the content is written to tmp first, then
    a hard link races to grab config.json; whoever loses just uses the winner's copy (overwriting someone else's
    token is a silent kind of wrong: the other side gets a 401, one restart fixes it, and nobody ever knows it
    happened).
    Never fall back to `open(p, "x")`: that creates config.json at 0 bytes before the content is written, and in
    that instant another process's `exists()` reads true while what it reads back is an empty string. A hard link
    makes "exclusive creation" and "complete content" hold at the same time — the moment p shows up, it is already
    the finished copy."""
    cfg["local_token"] = "scv-" + secrets.token_urlsafe(24)
    tmp = _cfg_tmp()
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.link(tmp, p)
        with contextlib.suppress(OSError):
            os.chmod(p, 0o600)
    except FileExistsError:
        disk = json.loads(p.read_text(encoding="utf-8"))
        if disk.get("local_token"):
            cfg.update(disk)
        else:
            save_config(cfg)   # the copy on disk has no token (hand-edited / an old version) ⇒ fill one in
    except OSError as exc:     # this filesystem cannot make hard links ⇒ complain loudly, then re-raise it, never fall back silently
        # ⭐make the complaint useful (an earlier measurement): exFAT/FAT32-style USB drive formats are the most common case ⇒ say so right away
        log("minting local_token failed, this filesystem cannot make hard links (exFAT/FAT32 are the common case) "
            "⇒ point SCV_HOME at a directory on NTFS/ext4/APFS and run again: " + str(exc))
        raise
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    return cfg


_extra_warned: set = set()


def _extra_once(why: str, msg: str) -> None:
    """A complaint about `extra_models` is logged once per category, never once per occurrence.
    🔴The old claim that "`load_config()` runs once per process" was false: measured, calling it three times in
      the same process complains three times, and Task 12 has every subcommand call `load_config()` once ⇒ one
      bad config still gets rinsed into background noise — exactly the thing this call site exists to avoid.
    ⭐`why` is the category (the shape + which family), never the whole sentence — the whole sentence carries the
      specific name, and swapping in a different bad name makes it look like a "new" message all over again
      (measured: changing the config 40 times = 40 lines). This file's other two siblings (`_rotate_warned` by
      name, `_refuse_warned` by code) are both closed sets — never invent a third kind here."""
    if why not in _extra_warned:
        _extra_warned.add(why)
        log(msg)


def _check_extra_models(cfg: dict) -> None:
    """A name in `extra_models` that never makes it into the catalog complains once at the border where it comes
    in (`catalog()` silently filters those out).
    ⭐The call site is here (`load_config()`), never `catalog()`: that side may be called once per request.
      ⚠️But "only once here" is not free — it is earned by `_extra_once` (see it).
    ⚠️Both sides must use the same one ruler (`MODEL_RE.fullmatch`, see `_model_ok`): if this side says a name is
      fine and that side filters it out, the user ends up holding a log line that says "no problem" while hunting
      for a model that is not in `/v1/models` at all."""
    extra = cfg.get("extra_models")
    if not isinstance(extra, dict):
        if extra:
            _extra_once("kind", "config.json's extra_models is not an object (it is %s) ⇒ the whole thing had no effect"
                        % type(extra).__name__)
        return
    for family, names in extra.items():
        fam = str(family)[:32]
        if family not in ("claude", "codex"):
            _extra_once("family:" + fam, "config.json's extra_models has a family this bridge does not recognize: %s"
                                         " ⇒ that whole family had no effect" % fam)
        elif not isinstance(names, list):
            _extra_once("list:" + fam, "config.json's extra_models[%s] is not a list (it is %s) ⇒ that whole family had no effect"
                        % (fam, type(names).__name__))
        else:
            bad = [str(m)[:32] for m in names if not (isinstance(m, str) and MODEL_RE.fullmatch(m))]
            if bad:
                _extra_once("names:" + fam,
                            "config.json's extra_models[%s] has some names shaped wrong for a model, they did not make it into /v1/models: %s"
                            % (fam, ", ".join(bad)))


def load_config() -> dict:
    p = spath("config.json")
    with _cfg_lock:   # the whole read-modify-write goes inside the lock: otherwise several threads in the same process would each mint their own
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        if p.exists():
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
        if not cfg.get("local_token"):
            cfg = _mint_token(cfg, p)
    _check_extra_models(cfg)   # ⭐outside the lock: it writes to disk (`log`), and `_cfg_lock` should only guard the read-modify-write part
    return cfg

