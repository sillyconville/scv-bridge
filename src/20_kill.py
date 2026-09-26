_SIGKILL = getattr(signal, "SIGKILL", 9)


CREATE_NO_WINDOW = 0x08000000


def new_session_kw() -> dict:
    """The one keyword door for starting a child process. POSIX: the child gets a group of its own, so killing the tree
    has a group to kill whole.
    win32: no group (there taskkill /T finds the tree by parent and child), but always `CREATE_NO_WINDOW` — 🔴when the
      parent has no visible console (the background bridge that `scv start` starts, a future autostart / pythonw),
      Windows opens a new, visible black window for every console child (cmd, node, codex, taskkill, python): measured
      on a user's desktop, flashing non-stop. ⭐The background bridge itself also gets a hidden console
      (`spawn_detached`); each layer has its own test — with one layer gone the other stays green, so never take
      "nothing flashed end to end" as proof of either layer.
    Gate: tests/test_90_cli.py::NoConsoleWindows::test_every_spawn_site_goes_through_the_no_window_door"""
    return {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}


def kill_pid_tree(pid: int) -> None:
    """Killing only the direct child is not enough: on Windows a CLI often starts through cmd /c; on Linux codex is two
    layers, a node launcher → the native binary.
    🔴On POSIX, first ask "is it the leader of its own group?": if not, `os.getpgid(pid)` returns the bridge's own
      group, one killpg takes the lot down, and the `suppress(OSError)` below makes sure it happens without a sound.
    ⭐The guard has to sit where the shot is fired: `kill_pid_tree` takes a bare pid that anyone can hand it, so the
      registration gate in `child_add` does not cover this path. Relying on callers to remember "never call it with
      an ungrouped pid" is relying on people remembering, which is exactly what this guard replaces."""
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15,
                           **new_session_kw())
        return
    try:
        leader = os.getpgid(pid) == pid
    except OSError:
        leader = False   # can't tell ⇒ treat it as not a leader: better to miss a few descendants than kill our own group
    if leader:
        with contextlib.suppress(OSError):
            os.killpg(pid, _SIGKILL)
    else:
        log("⚠️ pid %d is not the leader of its own group (it was started without new_session_kw()) ⇒ killing only "
            "it, its descendants may be left as orphans; not killing the group, that would take the bridge's own "
            "group down with it" % pid)
    with contextlib.suppress(OSError):
        os.kill(pid, _SIGKILL)


def kill_tree(proc: subprocess.Popen) -> None:
    kill_pid_tree(proc.pid)
    with contextlib.suppress(OSError):
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=15)


