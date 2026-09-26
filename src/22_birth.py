# ━━ A process's birth id / memory (win32 goes through ctypes, POSIX goes through ps). ⭐One block: only called by
#   the registry / sweep / snapshot, never reads any other global
# 🔴win32 used to start one powershell per question: even serialized, that came back rc=2 (empty stderr) about
#   1% of the time, and under concurrency there were also .NET access conflicts; "could not tell" and "does not
#   exist" got merged into the same empty string ⇒ roughly 1 in every 100 process starts had a perfectly healthy
#   turn reported as crashed (re-review addendum, item 9, measured). ⇒ switched to `OpenProcess` + `GetProcessTimes`:
#   stdlib, microsecond-scale, starts no child process, and can tell "pid does not exist" (ERROR_INVALID_PARAMETER)
#   apart from "could not tell" (access denied etc). 📎 NOTES.md::birth-cert-ctypes
BIRTH_WIN = "ft:"          # win32 birth id prefix: creation FILETIME. ⚠️old versions wrote .NET Ticks (no prefix) ⇒ see `birth_known`
_WIN_ERROR_INVALID_PARAMETER, _WIN_EXITED, _WIN_STILL_RUNNING = 87, 0, 0x102
_WIN_QUERY, _WIN_SYNC, _WIN_VM_READ = 0x1000, 0x00100000, 0x0010
# ⭐`_k32()` hands out only these five functions, never the whole of kernel32: kernel32 itself also has
#   CreateProcessW / CreateFileW / LoadLibraryW / GetProcAddress (start a process, write to disk, load another
#   DLL) — handing out the whole object would open a door right next to the child-process gate and the disk gate.
#   🔴But this is a door against slipping, never against a deliberate bypass: every ctypes function object holds
#   its DLL in a private `_objects` dict (`_k32().OpenProcess._objects["0"]` is the whole of kernel32, the
#   re-review addendum's third pass measured this working). Adding one more function ⇒ add one field here,
#   tests/test_00_budget.py::Budget::test_native_code_has_one_door goes red; reaching for a private attribute like
#   `_objects` ⇒ tests/test_00_budget.py::Budget::test_no_private_attribute_is_reached_off_self goes red; a
#   string-built reflection has no gate at all.
_K32 = collections.namedtuple("_K32", "OpenProcess GetProcessTimes WaitForSingleObject K32GetProcessMemoryInfo "
                                      "CloseHandle")


class _WinMem(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint32), ("faults", ctypes.c_uint32), ("peak_ws", ctypes.c_size_t),
                ("ws", ctypes.c_size_t), ("a", ctypes.c_size_t), ("b", ctypes.c_size_t), ("c", ctypes.c_size_t),
                ("d", ctypes.c_size_t), ("pagefile", ctypes.c_size_t), ("peak_pagefile", ctypes.c_size_t)]


@functools.lru_cache(maxsize=None)
def _k32():
    """⚠️Every function needs its `argtypes`/`restype` written out: the default `c_int` truncates a 64-bit handle
    (Task 11's resource survey hit `handles: -1` once because of this). Load our own `WinDLL`, never the shared
    `ctypes.windll`: changing a signature on that one changes it for everyone else too.
    🔴The one and only place in the whole file that loads native code, and it returns a `_K32` (five functions),
    never the DLL object itself — the reason is in the `_K32` line above."""
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.OpenProcess.argtypes, dll.OpenProcess.restype = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32], ctypes.c_void_p
    dll.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_uint64)] * 4
    dll.GetProcessTimes.restype = ctypes.c_int
    dll.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    dll.WaitForSingleObject.restype = ctypes.c_uint32
    dll.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WinMem), ctypes.c_uint32]
    dll.K32GetProcessMemoryInfo.restype = ctypes.c_int
    dll.CloseHandle.argtypes, dll.CloseHandle.restype = [ctypes.c_void_p], ctypes.c_int
    return _K32(dll.OpenProcess, dll.GetProcessTimes, dll.WaitForSingleObject, dll.K32GetProcessMemoryInfo,
                dll.CloseHandle)


def _win_ask(pid: int, access: int, fn):
    """Open a handle to ask one thing. Returns `fn(k, h)`'s result; `""` = does not exist (including "already
    exited, just someone is still holding the handle"); `None` = could not tell."""
    if not 0 < pid < 2 ** 32:
        return ""
    k = _k32()
    h = k.OpenProcess(access | _WIN_SYNC, 0, pid)
    if not h:
        return "" if ctypes.get_last_error() == _WIN_ERROR_INVALID_PARAMETER else None
    try:
        state = k.WaitForSingleObject(h, 0)
        # ⭐exited but someone is still holding the handle (say, our own Popen we have not waited on yet) ⇒ counts
        #   as "does not exist", same as the old Get-Process behavior
        if state == _WIN_EXITED:
            return ""
        # ⭐any other return value (`WAIT_FAILED` = 0xFFFFFFFF) = this particular question failed ⇒ could not tell,
        #   never does not exist (re-review addendum two, M-7)
        if state != _WIN_STILL_RUNNING:
            return None
        return fn(k, h)
    finally:
        k.CloseHandle(h)


def _win_birth(k, h):
    t = [ctypes.c_uint64() for _ in range(4)]
    if not k.GetProcessTimes(h, *(ctypes.byref(x) for x in t)):
        return None
    return BIRTH_WIN + str(t[0].value)


def _win_rss(k, h):
    m = _WinMem()
    m.cb = ctypes.sizeof(_WinMem)
    return int(m.ws // 1024) if k.K32GetProcessMemoryInfo(h, ctypes.byref(m), m.cb) else None


def _birth_once(pid: int):
    """One attempt. Returns the birth id / `""` (does not exist) / `None` (could not tell)."""
    if os.name == "nt":
        return _win_ask(pid, _WIN_QUERY, _win_birth)
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return decode(out.stdout).strip() if out.returncode == 0 else ""


def proc_start_id(pid: int):
    """A process's "birth id". The system recycles PIDs, and an orphan sweep that only trusts the PID would kill
    someone else's process.
    ⭐Three values: a birth id (non-empty string) / `""` = this pid does not exist / `None` = could not tell
    (retried once, still no).
    🔴The old code had the latter two share one empty string (the docstring itself said "the empty string is
      ambiguous"), leaving callers no choice but to treat it all as "uncertain" ⇒ one stray could-not-tell would
      kill a perfectly healthy, freshly started process and report it as crashed.
    ⭐"Could not tell" gets retried once, "does not exist" never does (that is a settled answer — retrying would
      only waste time).
    ⚠️The POSIX side (`ps -o lstart=`) has not changed a character; only OSError/timeout now read as "could not
      tell" instead of "does not exist"."""
    got = _birth_once(pid)
    if got is None:
        time.sleep(0.05)
        got = _birth_once(pid)
    return got


def birth_known(born: str) -> bool:
    """Does this version recognize the format of the birth id sitting in this registry row. 🔴On win32, old
    versions wrote powershell's .NET Ticks (plain digits, local time); the new one is `ft:<FILETIME>`: the two are
    never comparable ⇒ an old row is treated as "unrecognized" — never killed, and never treated as "matches"
    either (the matching logic here happens not to skip killing it either way, but that is a coincidence, never
    rely on it). The POSIX format has not changed."""
    return bool(born) and (os.name != "nt" or born.startswith(BIRTH_WIN))


def proc_rss_kb(pid: int) -> int | None:
    """⚠️On POSIX, an un-reaped dead child (a zombie) gets 0 back, not None: `ps -o rss=` prints 0 for a zombie and
    still returns 0 as its exit code. This is exactly the "0 gets read downstream as measured, using 0KB" that the
    Rss test itself warns about ⇒ whoever Popens it has to reap it.
    ⭐The same cut on win32 goes through `K32GetProcessMemoryInfo` instead (used to start one powershell per pid,
    ≈0.7s each)."""
    if os.name == "nt":
        got = _win_ask(pid, _WIN_QUERY | _WIN_VM_READ, _win_rss)
        return got if isinstance(got, int) else None
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, timeout=20)
        return int(decode(out.stdout).strip()) if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None

