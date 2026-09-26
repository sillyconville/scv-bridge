---
name: scv
description: Start, stop, check, update or diagnose scv, the local agent bridge on this machine. Use when the user mentions scv, "the bridge", or asks why their local models are offline in a product that uses scv.
---

This skill turns what the user asked for into one subcommand of `scv.py`, runs it, and reads the output back. It holds no logic of its own; every action is a subcommand of the bridge.

This file is itself instructions to an agent. If it was installed from a plugin marketplace, the marketplace can replace it with a newer version without asking the user.

## How to run a subcommand

```
<python> "$HOME/.scv/scv.py" <subcommand>
```

- `<python>`: the same rule as step 1 of setup.md — the first command whose `--version` prints Python 3.9 or newer; in PowerShell try `python`, `py -3`, `python3`; in bash or zsh (Git Bash on Windows included) try `python3`, `python`. If a command offers to install Python or opens an app store, stop and ask the user.
- `"$HOME/.scv/scv.py"`: where setup.md installs the bridge; keep the double quotes. If the user installed it somewhere else, use that path.
- Neither setup.md nor `scv.py` puts an `scv` command on `PATH`. When the output of a subcommand prints a command to run next, it contains the full paths for this machine: run that one exactly as printed (where it is printed once per shell, use the line for your shell).

## What to run

| The user wants | Subcommand | Reading the output |
|---|---|---|
| start it, bring the local models online | `start` | If it prints a warning that it could not leave the parent job, repeat the warning to the user word for word, with the command it gives. |
| stop it | `stop` | |
| is it running, which models, how many processes | `status` | Check the exit code first. 0: stdout is one JSON object. 1: no bridge answered on the port and stdout is one sentence, not JSON; if the process recorded in `bridge.pid` (same pid and start time) is confirmed to be still running, the sentence says so, and otherwise it says nothing about the pid (saying nothing does not mean the pid is gone). Lines on stderr name blocked families or a needed update, with the command to run next. |
| why a model is missing, the seats went silent, "login required" | `doctor` | stdout starts with a JSON object of facts, followed by plain lines; exit code 1 means some family has a problem. It makes no model call. `status` and `/healthz` only say whether a family is blocked; `doctor` says why. |
| check that the credentials still work, or whether a file outside the bridge's working directory can reach an answer | `doctor --live` | One real call per family: it uses a little of the user's quota, so say so and ask first. It checks one planted file next to the working directory, nothing more. For Codex the output also lists the instruction files Codex loads into every call; relay those lines. |
| update it | `update` | See below. |

**Login.** Do not log in for the user, and do not ask them to log in for the bridge — it needs no login of its own. If `doctor` shows that a CLI has no local credentials, it prints that CLI's own login command with the full path of its executable. Show that line to the user; they run it themselves. If `codex` is not on `PATH`, that printed line is the one that works.

**Update.** Without arguments `update` uses the commit and sha256 the paired service last announced; otherwise the user gives both, as `update --commit <commit> --sha256 <sha256>`. The bridge never updates by itself. Afterwards:

1. Tell the user where the previous version was kept (the output names the file).
2. Run the stop and start commands that `update` printed; until then the running bridge still runs the old code.
3. Ask the user whether they want to see what changed. If yes, run the `git diff --no-index` line that `update` printed (it needs git): both versions are on this machine.

## Reporting

Report the output as it is. `scv.py` prints its messages in English: relay them in the user's language, faithfully. Do not describe the bridge as anything the output does not say. Do not post raw `doctor` output anywhere public: it contains local paths, including the user name.
