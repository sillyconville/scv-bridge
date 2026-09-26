def main(argv: list | None = None) -> int:
    if sys.version_info < MIN_PY:
        print("this bridge needs Python %d.%d or newer" % MIN_PY, file=sys.stderr)   # stdout is reserved for the subcommand's own output
        return 2
    # 🔴when piped / redirected, stdout follows the locale encoding (gbk on Chinese Windows) and is strict ⇒ a single ✅ becomes a UnicodeEncodeError
    #   (an agent reading the output happens to always go through a pipe). stderr is already backslashreplace.
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(errors="replace")
    # 🔴when it is not a console (piped / redirected), switch both streams to UTF-8 (Task 14 review I6 measured: on Chinese Windows an agent
    #   reading through a pipe got GBK bytes, and on an English locale the Chinese all turned into `?`). Never touch the console path (CPython
    #   goes through wide characters on the Windows console, unrelated to the locale encoding).
    for stream, errs in ((sys.stdout, "replace"), (sys.stderr, "backslashreplace")):
        with contextlib.suppress(AttributeError, ValueError):
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors=errs)
    ap = argparse.ArgumentParser(prog="scv", description="local agent bridge")
    sub = ap.add_subparsers(dest="cmd")
    # ⭐one `add_parser` call per name (never a loop): the `PromisedCommands` gate recognizes, by literal text, "the subcommand named in an error message really exists"
    sub.add_parser("version", help="print just the version number")
    sub.add_parser("run", help="start the bridge in the foreground (Ctrl+C to stop)").add_argument("--ticket", default="", help=argparse.SUPPRESS)
    sub.add_parser("start", help="start the bridge in the background")
    sub.add_parser("stop", help="stop the bridge running in the background")
    sub.add_parser("status", help="whether the bridge is running, and its state if so")
    sub.add_parser("token", help="print the local API's token")
    sub.add_parser("doctor", help="health check").add_argument("--live", action="store_true",
                                                    help="one real call per family (costs a little quota), and runs the canary")
    sub.add_parser("setup", help="run once after install: health check + what to do next").add_argument(
        "--live", action="store_true", help="one real call per family during the health check (costs a little quota)")
    pr = sub.add_parser("pair", help="connect to a remote dispatcher with a one-time pairing code (optional)")
    pr.add_argument("url")
    pr.add_argument("--code", required=True)
    up = sub.add_parser("update", help="switch to the public repository's scv.py (for some commit, pinning its sha256)")
    up.add_argument("--commit", default="")
    up.add_argument("--sha256", default="")
    args = ap.parse_args(argv)
    if args.cmd == "version":
        print(VERSION)
        return 0
    table = {"run": cmd_run, "start": cmd_start, "stop": cmd_stop, "status": cmd_status, "token": cmd_token,
             "doctor": cmd_doctor, "setup": cmd_setup, "pair": cmd_pair, "update": cmd_update}
    if args.cmd not in table:
        ap.print_help()
        return 2
    try:
        return table[args.cmd](args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as e:      # ⭐the top-level handler (Minor 6): any exception a subcommand does not catch lands on disk and gets a human sentence, never just a bare traceback
        # ⚠️never touch `spath()` here: it creates directories, and the reason execution reached this point may be exactly "the state directory cannot be written to"
        hint = ("most likely the state directory (SCV_HOME, ~/.scv by default) cannot be read or written: disk full? no permission? pointed somewhere wrong?" if isinstance(e, OSError)
                else "this is an error the bridge itself failed to catch: paste the last few lines of bridge.log to the maintainer")
        return _cmd_failed("the %s subcommand did not succeed: %s: %s" % (args.cmd, type(e).__name__, _one_line(e)),
                           hint + " (this line has already been logged to bridge.log, if it could be written)")


if __name__ == "__main__":
    sys.exit(main())
