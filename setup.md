# Set up scv, the local agent bridge, on this machine

You are an agent running on the user's own machine. Do these steps in order. After each step, tell the user what actually happened; do not summarise a step you did not run.

Every step that runs a command gives one block for **PowerShell** (Windows) and one for **bash / zsh** (macOS, Linux, and Git Bash on Windows). Use the block for the shell you are running commands in. The bridge is installed at `$HOME/.scv/scv.py`; keep the double quotes around that path.

Rules for the whole setup:

- Do not log in to anything on the user's behalf, and do not ask the user to log in for the bridge — it needs no login of its own. If a CLI has no local credentials, `setup` prints that CLI's own login command; show it to the user and let them decide whether to run it themselves.
- Do not post the raw output of `setup` or `doctor` anywhere public: it contains local paths, including the user name.
- `scv.py` prints its messages in English. Relay them in the user's language, faithfully, without adding conclusions of your own.

## 1. Find Python 3.9 or newer

Try these in order and use the first one that prints `Python 3.9` or newer; skip any that prints something else or is not found. If a command offers to install Python, or opens an app store, stop and ask the user before going on. In the rest of this file, `<python>` means that command.

```powershell
python --version
py -3 --version
python3 --version
```

```bash
python3 --version
python --version
```

If none of them prints Python 3.9 or newer, **ask the user before installing anything**.

## 2. Download the bridge

Use `curl` exactly as below, not a fetch tool that summarises pages: the bytes must be exact. `--create-dirs` creates `$HOME/.scv` if it does not exist.

```powershell
curl.exe -fsSL --create-dirs https://raw.githubusercontent.com/sillyconville/scv-bridge/65acedff4e1ab32858f5228391392e2eb657c520/scv.py -o "$HOME/.scv/scv.py"
```

```bash
curl -fsSL --create-dirs https://raw.githubusercontent.com/sillyconville/scv-bridge/65acedff4e1ab32858f5228391392e2eb657c520/scv.py -o "$HOME/.scv/scv.py"
```

## 3. Check its sha256

Compute the file's sha256 with the Python from step 1:

```powershell
<python> -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$HOME/.scv/scv.py"
```

```bash
<python> -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$HOME/.scv/scv.py"
```

It must equal this value — sha256 `fc8998fd0c4aa9fd6e6e88f7730005821960d097a10ddad8143f7af095db63eb`. If the two differ, stop, do not run the file, and tell the user both values.

## 4. Run `setup`

```powershell
<python> "$HOME/.scv/scv.py" setup
```

```bash
<python> "$HOME/.scv/scv.py" setup
```

Show the user the whole output. It starts with one sentence, then a JSON object of facts, then plain lines about each CLI, then the next commands for this machine. Those commands already contain the full path of this Python and of `scv.py`; from here on, run them exactly as printed. Where a command is printed once per shell (a label line, then the command), use the line for your shell.

`setup` makes no model call. `setup --live` (or `doctor --live` later) makes one real call per CLI and uses a little of the user's quota — ask the user before running it.

## 5. Start the bridge

Run the `start` command that `setup` printed. If `setup` printed a line starting with ❌ that says a variable is missing from the environment, do not run `start` yourself: a bridge started from a shell that lacks the variable would inherit that. Give the user the commands printed under that line — `start`, or `stop` and then `start` when the line is about a bridge that is already running — and ask them to run them in their own terminal. If `start` prints a warning that it could not leave the parent job, repeat that warning to the user word for word, together with the command it gives.

## 6. Ask about an audit

Ask the user whether they want you to audit `scv.py` before they use it.

If they do, read `$HOME/.scv/scv.py` yourself, the whole file, independently — do not rely on this document or on the README. Report what it connects to, what it runs and which files it writes, including anything that concerns you. Report only what you read in the file, and give no verdict of your own. The sections marked ①, ② and ③ near the top of the file are a place to start, not a substitute for reading the rest.

## 7. Pairing (only if the user asks for it)

Only if the user wants the bridge to take jobs from a remote service and has a pairing code from it:

```powershell
<python> "$HOME/.scv/scv.py" pair <url> --code <code>
```

```bash
<python> "$HOME/.scv/scv.py" pair <url> --code <code>
```

Then run the stop and start commands that `pair` prints. The bridge started right after pairing is awake, so the service sees it at once; every later start is asleep (it sends nothing) until the service's page wakes it or someone runs the `wake` subcommand, and after 10 minutes without a job it goes back to sleep. Without pairing, a running bridge sends nothing to any outside address; `update` fetches a file from GitHub only when it is run, and the CLIs the bridge starts connect to their own vendors as usual.

## 8. Remember where the bridge is

So that next time the user can just say "wake the bridge" or "start the bridge" (on a later day, in a new session), save one short
note to whatever you keep across sessions for this user — a memory feature, or a user-level instructions file.
The note says: the bridge is `scv.py` at `$HOME/.scv/scv.py`, run with the Python from step 1; its subcommands
`start`, `stop`, `status`, `wake`, `doctor` and `update` are run as `<python> "$HOME/.scv/scv.py" <subcommand>`; and the
thin skill for Claude Code, `skill/SKILL.md` in the bridge's repository, maps what the user asks for to one
subcommand. Write the note with the real Python command from step 1 in place of `<python>`.

Tell the user what you saved and where. If you have no place that lasts across sessions, tell the user so, and
give them the path above to keep.
