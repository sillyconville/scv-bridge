# scv — a local agent bridge

`scv.py` is one Python file (standard library only, Python 3.9 or newer). It lends the agent CLIs that are already installed and logged in on this machine — Claude Code and Codex — to other programs through an OpenAI-compatible HTTP API on `127.0.0.1`. If, and only if, you pair it with a remote service, it also takes jobs from that one service over an outbound connection.

It does not log in for you and does not ask you to log in for it. It starts the CLI binaries it finds, unmodified, and hands them the prompt; calls count against your own plan.

This file says what `scv.py` connects to, writes and runs, what it keeps out of the calls, and what it cannot keep out. Many names, paths and numbers below, and the two session command lines, are compared with the code by `tests/test_97_docs.py`; the other statements about the code were checked by reading it (some also have their own test, named where it applies). Statements about behaviour say which measurement they rest on.

## Reading the source

Three things are each kept in one place near the top of `scv.py`:

- **① network addresses** — the section marked `# ━━ ①`;
- **② paths it writes** — the `WRITES` tuple in the section marked `# ━━ ②`; every write that uses one of those names goes through `spath()`; `update` writes `scv.py` itself without it (see "What it writes");
- **③ the command lines of the CLI sessions** — `claude_argv()` and `codex_argv()`, which the section marked `# ━━ ③` points to. The quick probes' short command lines are built where they run (listed under "What it runs").

Ten structural tests hold these places and a few related rules. Read each test's docstring and the judging function it calls: they say what the test checks, and several also say what it cannot see.

| Question | Test |
|---|---|
| Where can it send network traffic? | `tests/test_00_budget.py::Budget::test_urls_only_in_section_one` |
| Which functions dial out? | `tests/test_00_budget.py::Budget::test_network_calls_only_in_whitelisted_functions` |
| Where does it write? | `tests/test_00_budget.py::Budget::test_state_dir_only_reached_through_spath` |
| Which functions touch the disk? | `tests/test_00_budget.py::Budget::test_disk_writes_only_in_named_functions` |
| Do `--disallowedTools` and `app-server` appear only in the two command-line builders? | `tests/test_00_budget.py::Budget::test_cli_subcommand_literals_only_in_argv_builders` |
| Any third-party package? | `tests/test_00_budget.py::Budget::test_stdlib_only` |
| Which modules does it import? | `tests/test_00_budget.py::Budget::test_the_import_list_is_pinned` |
| Where does native code come in? | `tests/test_00_budget.py::Budget::test_native_code_has_one_door` |
| The two edges of that door | `tests/test_00_budget.py::Budget::test_the_ctypes_surface_is_pinned`, `tests/test_00_budget.py::Budget::test_no_private_attribute_is_reached_off_self` |

Which places start a process is held by `tests/test_90_cli.py::NoConsoleWindows` (every such place, counted) and by `SPAWN_SITES` in `tests/test_97_docs.py`.

In this repository `scv.py` is kept as pieces, `src/NN_name.py`, each a run of whole top-level statements. `tools/build.py` joins them in name order, byte for byte, and the committed `scv.py` is that join (`tests/test_01_build.py` fails otherwise), so the file you download is the file the tests run. Line numbers quoted in comments and in this file refer to `scv.py`. To change the code, edit the pieces, run `python tools/build.py`, and commit both.

## What it connects to

- **It listens** on `127.0.0.1` only, on port 8765 unless `port` in `config.json` says otherwise. If `config.json` says 8765 and another program holds it when the bridge starts, the bridge takes the first free port of the next 20, writes it to `config.json` and says so in `bridge.log` and in the output of `start`; a port you set yourself is never moved.
- **The local API** answers `GET /healthz`, `GET /v1/models`, `POST /v1/chat/completions` and `POST /v1/sessions/close`. A request is refused unless its `Host` header is `127.0.0.1`, `localhost` or `::1`; a request with an `Origin` header is refused unless that origin is listed in `allowed_origins`; `POST` bodies must be `application/json`; everything except `/healthz` and the browser's `OPTIONS` preflight needs `Authorization: Bearer <local token>`.
- **Pairing and the remote leg.** A running bridge that is not paired sends nothing to any outside address. The `pair` subcommand sends a one-time code to `<url>/bridge/pair`. From the next start on, the bridge talks to that one host, on `/bridge/hello`, `/bridge/stream` and `/bridge/result`. The address must start with `https://` (plain `http://` is accepted only for loopback IPs, for tests). Requests that carry the token do not follow redirects. `PROTOCOL.md` describes the requests and events of this protocol. `hello` sends exactly these keys: `protocol`, `bridge_version`, `os`, `python`, `families`, `models`, `max_concurrent` — no e-mail, account, user name, host name or path.
- **`update`**, and only when you run it, downloads `https://raw.githubusercontent.com/sillyconville/scv-bridge/<commit>/scv.py` and refuses it unless its sha256 equals the value it was given.
- **Proxies.** Outbound requests use the proxy variables in the environment. The subcommands' requests to the bridge's own `/healthz` bypass them. `doctor` shows the proxy variables it sees and the ones the running bridge saw, with passwords masked.
- **The CLIs** it starts connect to their vendors as they do when you use them yourself. Codex also does this when a session starts — see "Codex: a request when a session starts" under Known limits.

## What it writes

Everything goes under one state directory: `$SCV_HOME` if it is set, otherwise `.scv` in your home directory.

| Name | What it is |
|---|---|
| `config.json` | Settings (keys below), the local API token, and after pairing the remote address and token. File mode 0600 where the OS supports it. |
| `bridge.pid` | The running bridge's pid, process start id, port, one-time start ticket, the proxy variables it saw (passwords masked), and, names only, never values: the CLI-related environment variables it passes on to the CLIs, the ones it strips, the ones that change a CLI's behaviour (with a note on what each one changes), and the critical ones that are missing. |
| `children.json` | The CLI processes the bridge started, so that the next start or stop can find and stop ones that were left behind. |
| `children.json.bad` | The previous `children.json` if it could not be read or had a bad shape; only the latest one is kept. |
| `jobs.log` | One JSON line of metadata per job (fields below). |
| `jobs.log.1` | The previous generation of `jobs.log`. |
| `bridge.log` | Events and errors, one line each. |
| `bridge.log.1` | The previous generation of `bridge.log`. |
| `latest.json` | The newest version, commit and sha256 announced by the paired service; `update` without arguments uses them. |
| `work` | Working directories: one per CLI session or one-off call (removed when the session ends; a directory that outlives its process because `stop` had to hard-kill it — every `stop` on Windows — is swept the next time `start` or `stop` runs, never left indefinitely), `work/probe` for the quick probes, and the files of a `doctor --live` run (removed afterwards). |
| `tmp` | Write buffers: a file is written here first and then moved into place. Buffers left by processes that no longer exist are removed when the bridge starts. |
| `scv.prev.py` | The version that `update` replaced; only the latest one is kept. |

`jobs.log` and `bridge.log` rotate: when one reaches 4194304 bytes it is moved to its `.1` name (one generation is kept), and no line is longer than 2048 bytes. So each of these names stays under 2 × (4194304 + 2048) bytes, as long as the move succeeds.

**Outside the state directory** it writes one thing: `update` replaces `scv.py` itself, through a temporary `scv.py.new` next to it that is removed if the replacement fails. If you installed with setup.md (`$HOME/.scv/scv.py`) and `SCV_HOME` is not set, that is usually the state directory itself, so `scv.py` and, during an update, `scv.py.new` sit there too without going through `spath()`. (Python takes "your home directory" from `USERPROFILE` on Windows; Git Bash's `$HOME` can differ if you changed `HOME`.)

`config.json` keys: `port`, `max_concurrent`, `remote_url`, `remote_token`, `remote_jobs_per_hour`, `allowed_origins`, `claude_bin`, `codex_bin`, `keep_awake_s`, `extra_models`, `local_token`.

**Files it reads outside the state directory:** `scv.py` itself, during `update`. It also checks, without opening them, whether the CLI executables exist and how large `AGENTS.override.md` and `AGENTS.md` in your Codex home are; with `doctor --live`, also the size of each file Codex says it loaded. It does not read any credential file.

## What it runs

It looks for each CLI first at the path in `claude_bin` or `codex_bin` in `config.json`, then as `claude` or `codex` on `PATH`. For Codex on Windows it also looks for the desktop app's copy, `%LOCALAPPDATA%/OpenAI/Codex/bin/*/codex.exe`. A `.cmd` or `.bat` file (npm installs) is started through `cmd /c`. Child processes get the bridge's environment with one kind of variable removed: the ones an agent session (Claude Code, Codex) sets to say which session a process belongs to — session and thread ids, its messaging socket, its entrypoint, its client name (`SESSION_VARS` and `SESSION_PREFIXES` in `scv.py`). This matters because setup has an agent run `start`, and the CLIs would otherwise carry that session's identity. Nothing is added and nothing else is removed: your own settings reach the CLIs as you set them — proxies, `CODEX_HOME`, how each CLI logs in (except credentials that a host such as a desktop app or a remote session hands to its own child processes in session variables: those are stripped with the rest, so a bridge started by an agent in an environment that gets its login only that way would find its CLIs not logged in — not measured), and settings that change how it answers, such as `CLAUDE_CODE_EFFORT_LEVEL`, which overrides the bridge's `--effort`. `doctor` lists the names of both kinds (never the values) and says what each setting of that last sort changes; while a bridge is running it reports the names that bridge recorded (in `bridge.pid`) when it started, and says so; otherwise it reports its own environment and says that a bridge will get the environment of whatever starts it. If the environment lacks `LOCALAPPDATA` (an agent's shell can filter it out), the copy of Codex that comes with the desktop app cannot be found, and `doctor` says so and asks you to start the bridge from your own terminal (while a bridge is running, it judges by what that bridge recorded when it started and by whether that bridge lists Codex); a missing `APPDATA`, `HOME` or `USERPROFILE` made no difference to either CLI's `--version` or login check on Windows. In particular it does not set `ANTHROPIC_BASE_URL` or `CODEX_HOME` (`tests/test_20_argv.py::ChildEnv`). Two limits: a new session variable whose name does not look like one is passed on until it is added to the list; and when Codex's network proxy is on, the proxy variables point at that session's own proxy, which a name cannot show.

The only request values that can reach a command line are a model name the bridge itself listed in `/v1/models` and an effort from `low`, `medium`, `high`.

**A Claude Code session.** The prompt goes in on stdin as stream-json. The command line:

```json
["<claude>", "-p", "--model", "<model>", "--safe-mode",
 "--system-prompt-file", "<work-dir>/system.txt", "--exclude-dynamic-system-prompt-sections",
 "--disallowedTools", "*", "--strict-mcp-config", "--disable-slash-commands",
 "--settings", "<work-dir>/isolation.json",
 "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
```

followed by `"--effort", "<effort>"` when the request sets an effort. The system prompt is written to `system.txt` in the session's work directory; the settings file `isolation.json` next to it contains `{"disableAllHooks": true}`.

**A Codex session.** JSON-RPC over stdin and stdout; `<effort>` is `low` when the request sets none. The command line:

```json
["<codex>", "app-server", "--listen", "stdio://",
 "-c", "model=\"<model>\"", "-c", "model_reasoning_effort=\"<effort>\"",
 "-c", "web_search=\"disabled\"", "-c", "developer_instructions=\"\"", "-c", "notify=[]",
 "-c", "features.shell_tool=false", "-c", "features.apps=false", "-c", "features.browser_use=false",
 "-c", "features.browser_use_external=false", "-c", "features.computer_use=false",
 "-c", "features.code_mode_host=false", "-c", "features.image_generation=false",
 "-c", "features.multi_agent=false", "-c", "features.plugins=false", "-c", "features.skill_search=false",
 "-c", "features.sleep_tool=false", "-c", "features.tool_suggest=false", "-c", "features.unified_exec=false",
 "-c", "features.view_image=false", "-c", "features.goals=false", "-c", "features.hooks=false",
 "-c", "features.workspace_dependencies=false", "-c", "features.in_app_browser=false",
 "-c", "features.skill_mcp_dependency_install=false", "-c", "features.tool_call_mcp_elicitation=false",
 "-c", "features.memories=false", "-c", "features.multi_agent_v2=false"]
```

Then the bridge calls `initialize`, `config/read`, `skills/list` and `thread/start`. `thread/start` carries the system prompt as `baseInstructions`, `"sandbox": "read-only"`, `"ephemeral": true`, and a `config` that switches off every MCP server (by name) and every skill (by path) that `config/read` and `skills/list` reported, and sets `include_instructions` to false for the skill list. If either reply has a shape this version does not recognise, the session is not started. The values in those replies (an MCP server's environment, for example) are not kept or logged; only names and paths are used. `doctor` checks the feature names against `codex features list` and says which ones Codex no longer knows.

**Quick probes.** They do not call a model; their working directory is `work/probe`.

| Probe | CLI | When |
|---|---|---|
| `--version` | both | when the bridge starts, `doctor`, `setup` |
| `--help` | Claude Code (does it know `--safe-mode`?) | when the bridge starts, `doctor`, `setup` |
| `login status` | Codex | when the bridge starts, `doctor`, `setup` |
| `auth status --json` | Claude Code | `doctor`, `setup` |
| `features list` | Codex | `doctor`, `setup` |

**Other processes.** The `start` subcommand launches the `run` subcommand in the background with the same Python and a random `--ticket`. On macOS and Linux it stops a process tree by signalling the process group; on Windows it gets a process's start time and memory from the Win32 API through `ctypes`. The other programs it starts:

| Program | Fixed arguments | When |
|---|---|---|
| `taskkill` | `/PID`, `/T`, `/F` | Windows: stopping a process tree |
| `ps` | `-o`, `lstart=`, `rss=`, `-p` | macOS and Linux: a process's start time and memory |

## What reaches the model, and what is kept out

Two kinds of things on this machine could reach a call: **tools that can act** (run commands, read or write files, MCP servers) and **content** (instruction files, memories, skill text). The bridge switches tools off. It keeps content out where the CLI offers a way; where it does not, this section and `doctor` say so.

"Measured" below means a behaviour reading, not a reading of flags — a planted canary string, a trace file written by a planted program, the request Codex actually sent, Codex's own list of the files it loaded, Codex's own log database, or a real call — and each item says which. Several flags that look as if they should work were measured not to (listed at the end of this section).

### Claude Code

- **Tools:** `--disallowedTools "*"`, `--strict-mcp-config` with no MCP configuration given, and hooks off through the settings file. Measured with a canary file in `doctor --live` runs (2026-09-23, haiku): asked what a file outside its working directory says, the model replied with a command for reading it, and the file's content did not come back.
- **`--safe-mode`:** Claude Code's `--help` describes it as turning off CLAUDE.md, skills, plugins, hooks and MCP servers, with login, model selection, built-in tools and permissions working normally. Measured for `~/.claude/CLAUDE.md` only, with a canary: it came back without the flag and not with it (2026-09-21). The other items rest on the help text.
- A Claude Code build whose `--help` does not mention `--safe-mode` is not offered at all, and `doctor` prints the reason. If `--help` cannot be run, the family is not offered either, and `doctor` says it cannot tell.
- Not measured on its own: Claude Code's automatic per-project memory. Each session runs in a newly created working directory under `work/`, into which the bridge writes only `system.txt` and `isolation.json`.

### Codex

Measured 2026-09-24 on Windows with codex-cli 0.155.0-alpha.9.2 and `gpt-5.6-luna`.

**Carried into every Codex call — the bridge cannot keep these out:**

- `AGENTS.md` in your Codex home (`$CODEX_HOME`, or `~/.codex` if it is not set). If `AGENTS.override.md` there has content, it is carried instead and `AGENTS.md` is not; an empty or whitespace-only override falls back to `AGENTS.md`; an empty or whitespace-only `AGENTS.md` is not carried. Measured with small canary files; whether Codex cuts a large file (its configuration showed `project_doc_max_bytes: 32768`) was not measured.
- If the bridge's work directory (`$SCV_HOME/work/…`) is inside a git repository — for example because your home directory is one — Codex also carries the `AGENTS.md` files on the path from that repository's root down to the work directory (the list of files Codex reported loading).
- `doctor` starts no session and uses no quota: it reports the first case from file sizes alone, without opening the files (`tests/test_90_cli.py::DoctorSaysWhatCodexCarries`), and cannot see the git case. `doctor --live` reports the files Codex itself says it loaded for that session (`instructionSources`).
- The model's tool list still contains `functions.exec`, `functions.wait` and `functions.request_user_input` (as the model reported them in real calls). Asked to use `exec`, it got back `code-mode host is disabled`. What happens when the model calls `request_user_input` has not been measured; the bridge does not answer it.

**Kept out — each by one setting, each measured by removing only that setting and seeing the item come back:**

- `developer_instructions` in your `config.toml` (`-c developer_instructions=""`; a canary in the request Codex sent);
- the program named in `notify` (`-c notify=[]`; without it, the program ran and was given the turn's content — a trace file);
- your MCP servers (switched off by name in `thread/start`; without that, the server was started — a trace file — and its tools were in the request Codex sent);
- memories (`features.memories=false`) and the multi-agent tools (`features.multi_agent_v2=false`) (the request Codex sent);
- your skills: the skill list, through `include_instructions` set to false in `thread/start`, and the skill text that `$skill-name` in a prompt would pull in, through switching each skill off by path (canaries in the request Codex sent, and a real call). Skills in `~/.agents/skills` are in Codex's skill list and are switched off the same way; no separate canary was placed there;
- web search: `[tools] web_search = true` in your `config.toml` does not override `-c web_search="disabled"` (two real calls: with the setting removed, a web tool appeared in the tool list the model reported, and the request grew by 2502 input tokens).

Also measured: `instructions` in `config.toml` did not reach the model even with no setting against it (a canary), and `@<path>` in a prompt did not pull a file in (a real call).

**Not touched:** `model_provider`, `openai_base_url`, profiles and proxy settings — they are how Codex reaches its model. Not measured on a machine that uses a custom provider.

Codex runs with your own Codex home and your own login. To make the bridge's Codex use another directory, set `CODEX_HOME` yourself (you need to be logged in there). A `codex_home` key written into `config.json` by an older version is no longer read; `doctor` says so.

### Flags measured not to work

- `-c mcp_servers={}` does not switch Codex's MCP servers off: it merges into your table and your servers still start. The bridge does not use it.
- `codex app-server` does not accept `--ignore-user-config` or `--ignore-rules`; only `codex exec` does.
- `-c skills.include_instructions=false` stops working once `thread/start` carries a `skills` table, so the bridge sets it inside that table instead.

### Checking it yourself

`doctor --live` makes one real call per family, which uses a little of your quota. It writes a file with a random secret into `work/`, outside the session's own working directory, asks the model a random addition and what that file says, and reports whether any 8 consecutive hex characters of the secret appear in the answer. It can show that the secret leaked; it cannot show that the file was not read — a model can read without repeating, and haiku once made up the file's content. The file sits next to the working directory, so this checks "cannot read outside its own directory", not the whole disk.

## Where prompts and answers end up

- For Claude Code, the system prompt is in `system.txt` in the session's work directory (`work/` followed by the first 16 hex characters of the sha256 of the session id, or of a random id for a one-off call, then a random suffix, so that each CLI process has a directory of its own) while that process lives, and afterward if `stop` had to hard-kill it (every `stop` on Windows) — that leftover directory is swept the next time `start` or `stop` runs.
- In the CLIs' own storage. Claude Code writes session transcripts under `~/.claude/projects/`; this was seen in an earlier measurement outside this repository (claude 2.1.270, stream-json) and was not re-checked with `--safe-mode`. Codex threads are started with `"ephemeral": true`, and in the Codex measurement above no new files appeared in its `sessions/` folder; Codex still writes its log and state databases into your Codex home, and whether those hold prompt text was not checked.
- `jobs.log` holds one line of metadata per job — `ts`, `leg`, `job_id`, `model`, `cli_version`, `klass`, `usage`, `latency_s`, `ttfc`, `queued_ms`, `rebuilt` — and `JobLog.write` has no parameter that could carry a prompt or a reply (`tests/test_60_joblog.py::Log::test_signature_cannot_carry_text`). `model` is always a name the bridge listed in `/v1/models`; any other value is written as its length in bytes only. The one field with caller text: on the remote leg, `job_id` is chosen by the paired service (the local API writes its own ids), so a line can carry up to 256 bytes of its text (counted before JSON escaping; escaped, the field can take more bytes on disk).
- `bridge.log`: every line has its control characters escaped (a backslash, `x` and two hex digits), and the line and paragraph separators U+2028 and U+2029 too (a backslash, `u` and four hex digits), so that no line is split in two when it is read back; a line is at most 2048 bytes. On a failure, one line can carry up to 2048 bytes of the CLI's raw output, and for Claude Code that output can be the model's answer. These lines quote a short piece of something that came from outside, escaped first and then cut, so at most 128 characters as written: a bad `role`, `effort` or content-block type; a refused `Host` header, `Origin` header or path (these three are logged once per kind); the path of a local API request that hit an internal error or a second response; the name of an event from the paired service that it dropped, or the event itself when it was not valid JSON; the minimum version the paired service asked for; the error text (such as an HTTP reason phrase) of a failed call to the paired service, of the remote leg dropping, or of an error while handling one of its events; the error text of a failed `pair` or of a failed download in `update`; the commit and sha256 that `update` refused. A model name the bridge did not list is logged as its length only; the error response to the caller still quotes it. A minimum version from the paired service that is not one to three numbers of up to nine digits joined by dots is logged as its length only. A line about an internal error of the bridge can also carry the text of a Python exception, within the 2048-byte limit.
- With a paired service, prompts come from it and answers go back to it.

## Known limits

- **Measured on Windows only.** Every process and network reading behind this file was taken on Windows 11 with Python 3.12. The macOS and Linux code paths (process groups, signals, `ps`) are written but not measured, and CI runs the tests on Windows only (Python 3.9 and 3.12) until they are.
- **Detach.** On Windows, `start` launches the bridge with `CREATE_NO_WINDOW`, `CREATE_NEW_PROCESS_GROUP` and `CREATE_BREAKAWAY_FROM_JOB`. A bridge started this way was still answering after each of: closing its pseudo console (`ClosePseudoConsole`, which is what Windows Terminal and VS Code use when a tab is closed; not measured inside those two programs), closing a classic console window, `taskkill /T /F` on the shell's process tree, and the end of the Claude Code Bash tool call that started it. A bridge run in the foreground (the `run` subcommand) died in the first three. Inside a Windows Job that forbids breakaway, `start` prints a warning that the bridge may exit with the terminal, and it did exit when the Job was closed. Not measured: closing a whole Claude Code session, Codex as the harness, macOS and Linux (where `start` uses `start_new_session`).
- **Keeping the machine awake.** While the remote leg has a job running, and for `keep_awake_s` seconds (600 by
  default; `0` turns it off) after the last one ends, the bridge asks Windows every 30 seconds to reset its idle
  timer (`SetThreadExecutionState(ES_SYSTEM_REQUIRED)`, never `ES_CONTINUOUS`). It does not keep the display on,
  and a machine that is already asleep when a game starts cannot be woken from here. Not implemented on macOS or
  Linux.
- **Codex: a request when a session starts.** When Codex starts a session (`thread/start`), it connects to `wss://chatgpt.com/backend-api/codex/responses` with your login and gets a response id back, without producing text (seen in Codex's own log database, with a ChatGPT login). No setting to turn this off was found; whether it counts against your quota is not known. The bridge starts a Codex session only for a real request (including rebuilding a session) and for `doctor --live` or `setup --live`; `start`, `status`, `token`, `doctor`, `setup` and `stop` start none (`tests/test_90_cli.py::Lifecycle::test_zero_quota_commands_start_no_session`).
- **A client that hangs up.** While it waits for the first piece of an answer, and during the whole of a non-streaming call, the bridge checks every 2 seconds whether the client is still connected. Once a stream has started, it notices at the next write: a piece of text, or a keep-alive comment every 10 seconds. Noticing cancels the turn; a CLI process already working on it is killed.
- **Checked once, at start.** When the bridge starts, it looks for the CLIs, checks that Claude Code knows `--safe-mode`, and checks whether Codex has local credentials; Claude Code's login state is not checked then. After installing a CLI or logging Codex in, stop and start the bridge.
- **One work directory per CLI process.** The bridge gives each CLI process its own work directory (the session id's hash plus a random suffix), so a session that is rebuilt runs in a new directory. Where Claude Code writes session transcripts under `~/.claude/projects/` (see "Where prompts and answers end up"), it names their folders after the working directory, so each rebuild can add one more folder there. This follows from how Claude Code names those folders; the folders were not counted on a real Claude Code. Claude Code may also keep an entry per working directory in `~/.claude.json`, which would grow the same way; that was not checked on this machine.
- **Output language.** `scv.py` prints its messages in English. When its output goes to a pipe or a file it is UTF-8; in a console window it is printed as the console shows it.

## Commands

Neither setup.md nor `scv.py` puts an `scv` command on `PATH`. Run a subcommand as `<python> <path to scv.py> <subcommand>`; `setup` and the error messages print the exact line for this machine.

| Subcommand | What it does |
|---|---|
| `setup [--live]` | Run once after installing. Creates `config.json` with a new local token if there is none, prints where it is, then the same report as `doctor`, then the next commands for this machine. It does not start the bridge, install anything, change `PATH` or log anything in. |
| `start` | Starts the bridge in the background and waits up to 60 seconds for it to answer. |
| `stop` | Stops the background bridge and the CLI processes it started. |
| `status` | Asks the running bridge's `/healthz`. Exit code 0: stdout is one JSON object. Exit code 1: no bridge answered on the port, and stdout is one sentence; if the process recorded in `bridge.pid` (same pid and start time) is confirmed to be still running, the sentence says so, and otherwise it says nothing about the pid. |
| `token` | Prints the local API token. |
| `doctor [--live]` | Prints a JSON object of facts, then plain lines; exit code 1 if a family has a problem. Without `--live` it starts no CLI session. With `--live`, one real call per family and the canary check. |
| `pair <url> --code <code>` | Exchanges a one-time pairing code for a token and stores it; takes effect at the next start. |
| `update [--commit <commit> --sha256 <sha256>]` | Replaces `scv.py` with the file at that commit, if its sha256 matches; without arguments it uses `latest.json`. Keeps the previous version as `scv.prev.py` and prints a `git diff --no-index` line comparing the two files. It runs only when you run it. |
| `run [--ticket <ticket>]` | Runs the bridge in the foreground (Ctrl+C stops it). `start` runs it in the background with a random `--ticket`. |
| `version` | Prints the version. |

## setup.md and the skill

There are two ways in: `setup.md`, written for an agent in any harness, and `skill/SKILL.md`, a thin skill for Claude Code that only maps what the user asks for to one subcommand. `SKILL.md` is itself instructions to an agent. If it is installed from a plugin marketplace, the marketplace can replace it with a newer version without asking you; read it again after it changes.

## Local API

Base URL `http://127.0.0.1:<port>/v1`; the API key is the local token. `setup` prints the base URL and the command that prints the token.

**`GET /v1/models`** lists `claude/<model>` and `codex/<model>` for each family that is found and not blocked: `claude/haiku`, `claude/sonnet`, `claude/opus`, `codex/gpt-6-luna`, `codex/gpt-5.6-terra`, `codex/gpt-6-sol`, plus the names in `extra_models`. Each entry has only `id`, `object`, `created` and `owned_by`.

**`POST /v1/chat/completions`** — request parameters:

| Parameter | What the bridge does |
|---|---|
| `model` | Required. One of the ids from `GET /v1/models`. |
| `messages` | Required. `system` and `developer` messages are joined into the system prompt; `user` and `assistant` messages are the conversation, and the last message must be a `user` message; a message with any other role is rejected. `content` is a string or a list of text blocks. |
| `stream` | `true` streams server-sent events. |
| `session` | Not an OpenAI parameter. Keeps one CLI process for this id across calls, so that a follow-up sends only the newest message. A session idle for 1800 seconds is closed. When a call arrives for a session the bridge no longer has — or its process is gone, its model or effort changed, or its earlier messages differ — the session is rebuilt from the full message list and the response says why in `scv.rebuilt`. The remote leg's own session ids (PROTOCOL.md) live in a separate namespace before they ever reach this table, so a dispatcher and a local caller that pick the same id never rebuild, close or evict each other's session. |
| `effort`, `reasoning_effort` | `low`, `medium` or `high`. `effort` is the bridge's own name; `reasoning_effort` is taken as the same thing. |
| `first_token_timeout`, `timeout` | The bridge's own parameters. Seconds, at most 3600 (0 or no value means the default: 45 and 300). |
| `tools`, `tool_choice`, `functions`, `function_call`, `logprobs`, `top_logprobs`, `audio`, `prediction`; `n` other than 1; `response_format` with a type other than text; `modalities` other than a list holding only text; image or audio content blocks | **Rejected with HTTP 400**, not silently dropped: they would change the meaning of the request, and the CLI behind the bridge runs with its tools switched off. The first 8 are rejected when their value is not empty; an empty or false value (`[]`, `false`, `null`, `0`) is accepted, ignored and listed in `scv.ignored`. |
| anything else (`temperature`, `max_tokens`, `top_p`, …) | Accepted and ignored: the bridge does not pass them to the CLI. The ignored names are listed in the response's `scv.ignored`. |

A request with a lone surrogate in any of its strings (an unpaired escape such as `\ud800` in the JSON) is rejected with HTTP 400 (`bad_request`): that text cannot be passed on as UTF-8.

Responses follow the OpenAI shapes, with these differences:

- `usage` has only `prompt_tokens`, `completion_tokens` and `total_tokens`, with the numbers the CLI reported. The only `finish_reason` the bridge sends is `stop`; it does not report truncation.
- A successful non-streaming response, and the last chunk of a stream, carry an `scv` object: `ignored`, `rebuilt`, `session`, `queued_ms`, `ttfc`.
- The non-streaming `content` is trimmed; streamed pieces are passed through as they come. For Codex the two differ by leading and trailing whitespace. For Claude Code they come from two different outputs of the CLI (its final result and its stream); two real haiku calls gave identical bytes, but the CLI does not promise it. Use one path if you need byte-identical text.
- Errors have the body `{"error": {"message", "type", "code", "retryable", "fix_hint", "family"}}`. `type` and `code` are the class; `fix_hint` is empty when there is no fixed next step. Where `message` comes from:
  - the CLI's own words, not reworded: Claude Code's error result; the last 500 characters of the CLI's stderr; a Codex error's `message`, followed by ` (code=…)` when Codex gave a code (the whole error object when it has no `message`);
  - those words inside a sentence of the bridge's own: Codex's `config/read` or `skills/list` failing while a session starts, a broken pipe to the CLI, and a session closed while the call was in flight (class `cancelled`);
  - otherwise the bridge's own sentence, in English — for example when it refused or stopped the request (a bad request, a timeout, a cancel, its own rate limit), got no text back, could not start the CLI, or did not recognise a reply from Codex. A failed Codex turn with no error object gives `turn failed`.
  - On the remote leg only, that message is changed before it leaves the machine: your home directory becomes `~` (written with `/` or backslashes, mixed or doubled as in a Python repr, in any letter case, and for a drive letter also as Git Bash `/c/…` or WSL `/mnt/c/…`; a `%20`-encoded, 8.3 short-name or `/cygdrive/c/…` form is not recognised as the home directory but is caught as a local path), anything shaped like a local path becomes `<local path>`, and when something was replaced a sentence is appended saying how many and that the original is in `bridge.log` (which keeps the first 2048 bytes of a line). The local API and `bridge.log` keep the original words (`tests/test_80_remote.py::NoLocalPathsLeave`). A relative path, a user name outside a path or a machine name is not caught.

| Class | HTTP status | Retryable |
|---|---|---|
| `auth_required` | 401 | no |
| `quota` | 429 | yes |
| `local_rate_limit` | 429 | yes |
| `bad_request` | 400 | no |
| `cancelled` | 499 | no |
| `timeout` | 504 | yes |
| `crashed` | 502 | yes |
| `unknown` | 502 | no |

**`POST /v1/sessions/close`** with `{"session": "<id>"}` answers `200 {"closed": true}` when it closed the session, `202 {"closed": null, "closing": true}` while closing is still under way (ask again with the same body), and `200 {"closed": false}` when the bridge does not have that session.

**`GET /healthz`** needs no token. It reports versions, the model list, counts of sessions and processes, whether each family is blocked (not why — use `doctor` for that), and the remote leg's state. It contains no local paths (`tests/test_70_local_api.py::HealthzHasNoLocalPaths`).

**Borrowed from CLIProxyAPI** (MIT, written in Go; `scv.py` contains none of its code): reading the first piece of the answer before choosing the HTTP status, so that an error before any text is a real error status with a JSON body rather than a `200` stream that dies; a `: keep-alive` comment line during silence; an error in the middle of a stream sent as one `data:` error object followed by `data: [DONE]`; cancelling the work when the client hangs up; the error body's `message`, `type`, `code` and `retryable`; the four fields of a `/v1/models` entry; and the chunk shape, with a last chunk whose `choices` is empty and which carries `usage`.
