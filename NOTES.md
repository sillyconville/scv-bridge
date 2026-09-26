# NOTES — the archaeology layer for `scv.py`

`scv.py` itself keeps only **on-the-spot warnings** ("the thing you are about to do will break"). **Archaeology**
("what we measured at the time / what we retracted / how we know it") moves here, with a single line in the code,
`📎 NOTES.md::<anchor>`, pointing to it.

The test is one sentence: **if that line were deleted, would someone writing code right there become more likely
to make a mistake?** Yes ⇒ it stays in the code; it only makes someone **more convinced** but does not change
whether they make a mistake ⇒ it moves here.

⚠️Anchor names are pinned by `tests/test_00_budget.py::Notes`: code pointing at an anchor that is not here goes red
on the spot. **A pointer to something that does not exist is worse than no pointer at all — it makes someone think
it was already checked.**
⚠️Every measurement here is **on win32 only** (py3.12.10, the 2026-09-22 round). Each OS is its own separate path.

---
## line-budget-3000
2026-09-22, the maintainer ruled: `LINE_BUDGET` **2000 → 3000**, **B2 unchanged** (single file ⊕ stdlib only).
**Auditability is now guaranteed by those AST gates, never by a line count** (the list is whatever
`tests/test_00_budget.py::Budget::test_line_budget`'s docstring says; the count of gates is pinned by
`test_the_gates_it_leans_on_really_exist`).

**Where the old 2000 came from, and why it does not hold up.** It came from a trial run of the plan — that
implementation finished the whole plan at **1584 lines**. But that was code with **almost no explanatory text, and
none of the guardrails this round of review forced in**. Measured comparison:

| | the plan's own implementation | ours |
|---|---|---|
| Task 0-9 | 838 lines (**including its own comments**) | **1029 lines** (not counting any comments) |
| Task 10-15 | **778 lines** (not written yet) | projected 780-960 |

⇒ Our **plain code** already runs **191 lines** longer than the plan's "code + comments," and what makes up the
difference is exactly the nine resource fallbacks, `_clip`, `_append_capped`, the per-name locks,
`usage_numbers`, and the like. **The zero-comment floor lands at 1997-2180 lines** ⇒ **2000 does not hold, and
this has nothing to do with how comments are handled** (the Task 9x round stripped out 441 lines of explanatory
text and still only got down to 1227; the next 6 rounds of work push it back over the line).

**Where 3000 comes from — it is not an arbitrary round number picked out of the air**: a zero-comment hard floor
of ~2180, plus roughly 800 lines of margin for explanatory text.

**Why this is not going soft.** A line count was always only a **proxy metric**; the real goal is B2's line: "a
wary developer can read it in five minutes and dare to install it." What a wary reader actually needs to check is
this handful of things, and every one of them today has an AST gate that **pins down its exact bounds** (not a
line count): where the network traffic goes (address literals and who is dialing out) / where it writes to disk /
what child processes it starts / whether anything outside the stdlib is pulled in / which modules it imports /
where native code comes in (one door, with two cracks beside it: the surface `ctypes` exposes, and never reaching
into someone else's private attributes).
⭐At the time this was written (2026-09-22), the list only had the first four; the import-set gate and the
native-code door were added by re-review two, and the two cracks beside that door were added by re-review three's
M-1; the 2026-09-23 fix round 6 folded all of them into the list together — **a gate that is already load-bearing
but not on the list can be deleted or renamed and nobody notices**.
⭐Two of the earliest four **used to be blind** (`assertLessEqual` ⇒ the empty set is a subset of anything, which
left the scanner blind while all 31 cases stayed green); the 2026-09-21 round fixed them to `assertEqual` and
added a positive control proving "neither scanner is blind" ⇒ **they will now actually go red**, and that is the
precondition for daring to move the load off line count and onto them. Every gate added since carries its own
positive control the same way.
⚠️Every gate has a **boundary** (what it cannot see, written in its own docstring): a green gate does not mean
every rule on that question is being kept.

⚠️**A looser budget does not mean the archaeology Task 9x moved out should move back into the code**: that
round's criterion ("delete it — does someone writing code right here become more likely to make a mistake?")
**has not changed a word**, regardless of what the budget is.

## idiom-block
Where the `# ━━ Settled idioms` block at the top comes from: these decisions used to be scattered across the
docstring of "whichever function first needed it," and the same reasoning got relitigated elsewhere **eight
times** ⇒ they were gathered into one block, each line carrying a pointer, so that whoever writes the N+1th
occurrence can see what the 1st one already argued.

The reason the line-number gate (`tests/test_00_budget.py::Idioms`) earns its keep: **the very first attempt to
build it caught 3 line numbers it had just gotten wrong itself** (`_fail` off by 32 lines, `_read_failed` off by
36 lines, `spath` off by 10 lines); it has caught **13 drifts** in total, and not one of them was "carelessness" —
every one was ordinary editing pushing lines around ⇒ hand-maintained line numbers **rot by construction**.

The hole of "silently slipping past this gate" has been plugged **four times**, each time in the same shape (some
category of line getting waved through by the criterion): ① only recognizing lines starting with `# ⭐`
② exempting indented continuation lines ③ treating a blank line as the separator ④ a retired block that never
asserted its own closing line. **A fifth, known, unpluggable one**: the prose above `# ━━ Settled idioms` is
prose by design, and prose also uses ⭐ for emphasis ⇒ using "does it have a ⭐" as the criterion would misfire on
it. This boundary is written in `_retired()`'s docstring.
## quota-word-boundary
The rate-limit pattern set is engine `llm.py`'s `USAGE_LIMIT_PATTERNS` (accumulated from real games); **only the
matching method changed, not a single entry was added or removed**.

Two failure modes have each burned us once:
- **A bare substring**: `"presets"` contains `resets`, `"quotation"` contains `quota` ⇒ one **crash** got judged
  as a rate limit ⇒ the status code came out wrong as 429, and `fix_hint` **lied**, telling the user to wait for a
  quota reset that does not exist.
  (⚠️"the client will keep retrying" is not this bug's charge: `crashed` was already in `RETRYABLE` too, so a
  correct judgment would still retry.)
- **Regex word boundaries**: Python's notion of a word character also counts `_` and CJK ⇒ neither side forms a
  boundary for genuine, machine-readable rate-limit codes like `insufficient_quota` / `quota_exceeded` /
  `overloaded_error`, or a live message that packs "quota" directly against adjoining Chinese characters with no
  space ⇒ they silently fell through to `unknown` ⇒ 502 plus `retryable=False`, and the client threw away a
  request that would have succeeded in 60 seconds as a permanent failure.

⇒ The criterion only blocks on **letters**: digits (`resets 14:32`), underscores, quotes, and CJK do not count as
adjoining. `[A-Za-z]` and `[a-z0-9]` / `[a-z]` judge every one of today's actual strings identically (checked
entry by entry, actually run); the former was chosen because the latter's correctness **depends on that
`.lower()` call**. Each of the five phrase patterns carries spaces and cannot be embedded inside another word ⇒
they do not need this check; `AUTH_PAT` is the same.
## local-rate-limit
`local_rate_limit` is a rejection from **the bridge's own** rate limiting; once the window passes, the request is
bound to succeed ⇒ pairing 429 with `retryable=False` is self-contradictory (it amounts to calling a request that
is certain to succeed later a permanent failure). But the window is measured **by the hour** ⇒ giving "you can
retry" without "how long to wait" is **worse** than that contradiction. ⏳The machine-readable half of this (a 429
with a `Retry-After` header) is an HTTP-layer matter, left to whichever round wires that up.
## codex-user-home
**Task 13c (2026-09-24): codex goes back to the player's own `CODEX_HOME` — no login required, block whatever can
be blocked.** The maintainer ruled (never re-litigate this): "Making the user log in by hand for this is not
worth it. If it comes along, let it come along." "Block whatever can be blocked. Whatever can't be blocked, so be
it — gpt isn't expensive anyway." "**The bottom line is: no user login. Requiring a login raises suspicion.**" ⇒
any blocking approach whose cost is making the player log in one more time is never used.

Method: win32, codex-cli 0.155.0-alpha.9.2, `gpt-5.6-luna` / low, **an empty scratch cwd**, going through the
production path (`scv.CodexDriver`: the same argv, the same initialize/thread/start parameters), with
`CODEX_HOME` passed only through **an environment variable** (the escape hatch that was kept), pointing at an
experiment home that would never be used again after this (the one logged in by hand for scv on 09-21; never
touched its `auth.json`, never touched `~/.codex`). Canaries were dropped into it, each one a random password:
`AGENTS.md`, `AGENTS.override.md`, `config.toml` (`developer_instructions` / `instructions` / `notify` pointing
at a script that records a trace / `[features] memories=true, multi_agent_v2=true` / `[mcp_servers.scvcanary]`
pointing at a minimal MCP server that records a trace), `hooks.json`, `skills/<name>/SKILL.md` (one password in
the description, another in the body), `memories/memory_summary.md` (first line `v1`). Everything was deleted
afterward, and `auth.json`'s modification time was checked to confirm it never changed.

**Readings that never send a turn (`--help`, and the RPCs `config/read` / `skills/list` / `hooks/list` /
`mcpServerStatus/list`, plus the trace files):**
⚠️This batch is not zero-quota across the board: `--help` is; those RPCs themselves do not call the model, but
every one of them was asked inside a session that had **already shaken hands through to thread/start**, and
thread/start makes codex fire off one warm-up (it connects to the inference endpoint and gets back a response id
— see "handshake = one warm-up" at the end of this section) ⇒ on the experiment home (the logged-in one), each of
these probes spent one warm-up (correction from 13c Fix 1b).
- `codex app-server --ignore-user-config` ⇒ `error: unexpected argument '--ignore-user-config' found`; same for
  `--ignore-rules`. Both flags exist only on `codex exec` (the help text itself: "Do not load
  `$CODEX_HOME/config.toml`; auth still uses `CODEX_HOME`") ⇒ since the bridge goes through app-server, **neither
  is usable**.
- `-c` is a layer pressed on top of his config.toml (called `sessionFlags` in `config/read`'s layers): **a scalar
  gets replaced wholesale** (after `developer_instructions=""`, `notify=[]`, the effective value is exactly
  that); **a table gets deep-merged** — after `-c mcp_servers={}`, the `sessionFlags` layer is `{}`, but his
  server is still in the effective value, and the canary server was started the moment the session was created,
  and `tools/list` was called on it. ⇒ that flag **blocks nothing**, so 13c took it out of argv.
- Replacing the whole table with a different type (`-c mcp_servers=[]`, or giving `null` in thread/start) ⇒ codex
  simply refuses to start (`invalid type … in mcp_servers`).
- `{"mcp_servers": {"<name>": {"enabled": false}}}` in thread/start's `config` ⇒ the server is **never started at
  all** when the session is created (the only time it starts is the one time the app-server layer itself starts
  it when we call `mcpServerStatus/list` ourselves; production never calls that) ⇒ **turning it off by name** is
  the only approach that holds up, and the name has to come from asking `config/read` first. When no server is
  configured at all, `config/read` returns `mcp_servers: {}` — never a missing key.
- His `hooks.json`: with `features.hooks=false` (already present in `CODEX_OFF`), `hooks/list` comes back empty;
  switching it to true does list the canary hook — but **a hook that was never trusted just does not run anyway**
  (the trace count is 0 either way) ⇒ no behavioral positive control can be built for this; all we have is the
  flag reading itself.
- Skills: besides `$CODEX_HOME/skills`, `skills/list` also lists what is in **`~/.agents/skills`** — on this
  machine, 18 in total = 11 in `~/.agents/skills` (scope=user) + 6 in `$CODEX_HOME/skills/.system` (scope=system)
  + 1 canary (the raw `skills/list` reading from the R8 run; 13c's first draft counted all 18 as `~/.agents`,
  re-review M6). `-c skills.config=[{path='…', enabled=false}]` ⇒ that one entry in `skills/list` turns into
  `enabled: false` (the key's shape is right). `features.skip_host_skill_discovery=true` does not block it (still
  18); `skills.max_context_tokens=0` ⇒ codex refuses to start (it must be nonzero).
- 🔴`mcp_servers` inside `config/read` **is not part of the public schema** (`ConfigReadResponse.Config` declares
  only 25 keys; this one rides in through `additionalProperties`, 13c re-review M12): scv relies on it to get the
  names, and refuses if the shape is wrong ⇒ the day codex stops returning it, the whole codex family **cannot
  open a session at all** (loudly: a real request fails carrying that exact wording; `scv doctor --live` can see
  it; ⚠️**the plain doctor cannot** — since 13c Fix 1b it starts no session at all, and 13d measured "handshake
  only as far as skills/list" and did not adopt it either, see the 13d entry below). The only public-schema RPC
  that can list the names is `mcpServerStatus/list`, and it **starts his server for real** (Z0b: the canary gets
  started and `tools/list` gets called the instant the status RPC runs; its `detail` only has the two settings
  `full` / `toolsAndAuthOnly`, and both need the tool list ⇒ both must start it by the letter of the docs, though
  the latter was not measured separately) ⇒ never used.

**Real calls (9 runs; 1 more crashed inside my own probe and never started codex, so it does not count; 13c Fix
1b approved 2 more runs, R10/R11, see the M13 entry below):**

| # | argv/condition | reply (verbatim excerpt) | input tok | trace |
|---|---|---|---|---|
| R1 | the production argv before 13c (positive control) | `DEV-…` `MEM-…` `SKL-…` `AGT-…`; the model's self-reported tool list had 6 including `collaboration.spawn_agent`, plus `apply_patch`, and 3 including `list_mcp_resources` | 9621 | MCP started 2 times (1 time creating the session + 1 time for our status RPC); **notify got called** (argc=2, a 1257-byte argument — the content of one turn handed to his program) |
| R2 | 13c's argv + MCP turned off by name | only `AGT-…` left; the model's self-reported tool list down to just `functions.wait` / `functions.request_user_input` / `functions.exec` | 2938 | 0 |
| R3 | `scv doctor --live` (only letting codex go on stage), the same experiment home | `✅ codex's real call went through (time to first token 11.06s); the answer showed nothing from the canary outside the working directory` plus the AGENTS.md (52 bytes) line | — | 0 |
| R4 | same as R2, plus `AGENTS.override.md` | only `OVR-…` left (**the AGENTS.md one never made it in**) | 2932 | 0 |
| R5 | same as R2, asking it to call `functions.exec` | "Tool returned: `code-mode host is disabled`" | 2798 | 0 |
| R6 | same as R2, prompt opens with `$<skill name>` | `BODY-…` (**the whole skill body came in**) | 2803 | 0 |
| R7 | same as R6 + `features.mentions_v2=false`, plus `@<a file outside the working directory>` | `BODY-…` (not blocked); `NO FILE` | 3001 | 0 |
| R8 | the final code (skills also turned off by path) | `NO SKILL`; `OVR-…` (control: the channel is alive) | 2891 | 0 |
| R9 | the final code, asking only about `@<a file outside the working directory>` | `NO FILE` | 2749 | 0 |

⚠️Between R1 and R2, 7 things changed at once ⇒ **this pair only proves "together, they block it," and cannot
separate which one did the blocking** (13c re-review M5). Item-by-item attribution follows, on the A/B measuring
sticks below.

**Item-by-item attribution (13c Fix 1, zero quota)** — zero quota here rests on **no credentials / cannot reach
the real endpoint**, never on "the handshake itself costs nothing": the temporary `CODEX_HOME` is not logged in;
measuring stick A only renders locally, measuring stick B points the model endpoint at a fake local endpoint (the
warm-up inside the handshake lands on the fake endpoint too). Each measuring stick has its own boundary:
- **Measuring stick A, `codex debug prompt-input`** (pins `model="gpt-5.6-luna"`, renders the entire input handed
  to the model; it only reads `-c`, so it cannot see the settings in thread/start, and it cannot see memory,
  tools, or notify either). With canaries planted in the temporary `CODEX_HOME`, adding just one setting to "the
  full pre-13c set," or removing just one from "the full 13c set": adding just `developer_instructions=""` ⇒ DEV
  disappears, removing just it ⇒ DEV comes back; adding just `skills.include_instructions=false` ⇒ the skill list
  (`## Skills`) disappears, removing it ⇒ it comes back; adding just `multi_agent_v2=false` ⇒ the `spawn_agent` /
  `collaboration` sections disappear, removing it ⇒ they come back. `memories=false` / `notify=[]` show **no
  visible difference** on this stick (the memory summary is not in prompt-input; notify is a behavior, not a
  rendered string). `instructions` (INS) is not even present in the arm that pins only the model.
- **Measuring stick B, "the fake Responses endpoint"**: goes through the production path (`scv.CodexDriver`, the
  same argv, the same config/read→skills/list→thread/start), just with two extra `-c` flags at the end of argv
  pointing `model_provider` at a fake local endpoint (`wire_api="responses"`), which captures **the actual
  request codex sends out** (the full input text plus the tool descriptions in `additional_tools`) and replies
  with a minimal SSE stream so the turn ends normally (⇒ this also makes "after this turn ends" behaviors like
  notify visible). Never connects to OpenAI, zero quota. Under the full 13c set: the request only has `AGT`, the
  only tools are `functions.exec` / `wait` / `request_user_input`, notify was never called, and MCP was never
  started. **Taking away just one setting at a time**:
  - `developer_instructions=""` ⇒ DEV comes back into the request (MC-developer_instructions);
  - `notify=[]` ⇒ **notify got called** (MC-notify: `notify fired argc=2 len=352`);
  - `memories=false` ⇒ **MEM comes back into the request** (MC-features.memories) — the first time this was
    attributed on its own, outside of R1/R2;
  - `multi_agent_v2=false` ⇒ the 6 `collaboration.*` tools come back (MC-features.multi_agent_v2);
  - "MCP turned off by name" in thread/start ⇒ the canary server gets started and `tools/list` gets called, and
    `list_mcp_resources` and the like show up in the tool descriptions (MD-mcp);
  - "skills turned off by path" in thread/start, plus a prompt that says `$f1-canary` ⇒ the skill body `BODY-…`
    gets into the request (MS4); the same prompt under the full set ⇒ nothing (MS3).
  - 🔴`-c skills.include_instructions=false`: **the moment thread/start's `config` carries a `skills` table at
    all, this flag stops counting** — when thread/start only turns skills off without suppressing the list
    (`skills: {config: []}`), the list still gets into the request (MS1, `## Skills` appears 1 time); it only
    holds when thread/start has no `skills` key at all (MS5: 0 occurrences; taking that key away too ⇒ 1
    occurrence, MS6); writing `include_instructions: false` into that same thread/start table ⇒ it holds (MS8: 0
    occurrences). ⇒ Fix 1 took it out of argv and wrote it into that same thread/start table instead (13c's first
    draft's `-c` flag was dead weight in production the whole time; it only "worked" in the R2 run because
    thread/start had no `skills` table yet back then).
  - ⚠️This stick's boundary: under the fake endpoint, how the tools get assembled is **not exactly the same** as
    the real ChatGPT-logged-in production environment — `apply_patch` shows up in the tool descriptions both
    before and after 13c (R1's model self-report had it, R2's did not ⇒ ⏳most likely just noise in what the
    model self-reports, not something any override blocked; the two assemblies are not the same, so this cannot
    be pinned down); even with `web_search` opened up (`web_search="live"`, removing our override), it still does
    not show up in the request ⇒ this stick **cannot measure** `web_search` (no positive control can be built,
    see the M13 entry below).
  - The tool description itself says "every nested tool sits on `exec`'s `tools` object, and names get omitted
    when there are many": both `apply_patch` and the MCP tools are nested tools inside `exec` (code mode); in R1,
    the model answered "there is no `scv_canary_secret`" because it had been omitted. `exec` itself is shut off
    hard by `features.code_mode_host=false` (R5).

⇒ Conclusions (each backed by which run):
- **Blocked**: `developer_instructions` (attributed independently by both stick A and stick B), `notify` (stick
  B: taking away just this one setting gets it called), the MCP server (stick B MD-mcp; started in R1, not
  started in R2-R9), the skill list (held by Fix 1's thread/start table: stick B MS1/MS8), the skill body
  injected via `$name` (leaked through in R6, blocked in R8; stick B MS3/MS4), the memory summary (stick B:
  taking away just `memories=false` brings it back), the whole multi-agent tool family (attributed independently
  by both stick A and stick B).
- **Did not need blocking**: config.toml's `instructions` (not present even in stick A's "pin only the model"
  arm; also never appeared in R1); `@file` in the prompt (never got in, R9); his `hooks.json`
  (`features.hooks=false` plus an untrusted hook simply does not run anyway; ⏳no behavioral positive control).
- **Cannot be blocked (accepted)**: `$CODEX_HOME/AGENTS.md` (R2's `AGT-…`); when both exist together it switches
  to `AGENTS.override.md`, and **only that one** (R4); an override that is empty or only whitespace ⇒ falls back
  to AGENTS.md; a blank AGENTS.md does not count; if there is a `.git` above the cwd ⇒ the AGENTS.md files along
  the path from the repo root down to the cwd also get in (the instructionSources section below). ⇒ doctor
  reports this honestly, never reading the content (since 13c Fix 1b, in two tiers): **the plain doctor** only
  stats `$CODEX_HOME` (`codex_carried`: reports the override if it has bytes, plus a line saying "if it is all
  whitespace, codex will fall back to AGENTS.md (N bytes)"; reports AGENTS.md if the override is 0 bytes; cannot
  see the ones in git ancestor directories); **`doctor --live`** reports whatever codex itself says (that run's
  own session's `instructionSources`, `live_check`).
  ⚠️**The plain doctor cannot tell "only whitespace" apart from "0 bytes"**, and this makes it **over-report** in
  two cases (the safe direction; never read the content just to tell them apart, B22): ① an AGENTS.md that is
  only whitespace — codex does not carry it, but the plain doctor reports it anyway; ② an override that is only
  whitespace **and there is no** AGENTS.md — codex carries nothing, but the plain doctor still reports the
  override (and does not say "will fall back to AGENTS.md" either, since there is nothing to fall back to). Both
  cases are reported accurately by `--live` (13c re-review N2).
- **Present before 13c, and 13c did not touch it**: `functions.exec` / `functions.wait` /
  `functions.request_user_input` are always in the tool table (self-reported by the model in R2/R4; confirmed in
  the request on stick B). `exec` is code mode's shell, and calling it returns `code-mode host is disabled` (R5:
  shut off by `features.code_mode_host=false`); ⏳when `request_user_input` gets called, app-server sends a
  request back to us, and the driver does not answer it — what happens then has not been measured (guessed: it
  runs into the stall gate).
- ✅**M13: he can write `[tools] web_search = true`, and it cannot override our `-c web_search="disabled"`** (13c
  Fix 1b, 2 real calls the maintainer approved; zero quota could not measure this: even in stick B's "opened up"
  arms, the `web_search` tool never shows up in the request, so no positive control could be built). The
  experiment home carries only a config.toml with `[tools]` / `web_search = true`, and the same prompt ("search
  for bbc.com's headlines, and answer NO WEB if there is no web tool; then list the name of every tool you can
  call"):
  - R10 (positive control: production argv with `-c web_search="disabled"` **taken out**): tool table
    `functions.exec` / `wait` / `request_user_input` / **`tools.web__run`** / `tools.apply_patch`, input **5271**
    tok;
  - R11 (production argv as-is): `functions.wait` / `request_user_input` / `exec`, **no `web__run`**, input
    **2769** tok.
  ⇒ This time the stick holds up (taking our flag out adds a nested web tool inside code mode, and the input
  grows by 2502 tok, exactly the size of that tool description), and the block holds. Both runs answered NO WEB
  (the nested tool can only be called through `exec`, and code mode's host is shut off, R5). ⚠️The tool table is
  self-reported by the model; the difference in input tokens is the objective corroborating evidence. No code
  changed (the `CodexArgv` test group pins this `-c` flag).
- Never to be blocked: `model_provider` / `model_providers` / `profile` / proxying (`openai_base_url`,
  `chatgpt_base_url`) — that is his path **to actually reach the model**; blocking it means calls stop working
  (⏳never measured on a machine with a custom provider); this round touched none of them.

**`instructionSources` (13c Fix 1, going through the production handshake as far as thread/start, never sending a
turn; both the temporary `CODEX_HOME` and the cwd sit in the session scratch)** — zero quota here rests on the
temporary home **not being logged in** (the warm-up inside the handshake has no credentials to carry), never on
"the handshake itself costs nothing." thread/start's response carries a public field ("currently loaded for this
thread"); `doctor --live` trusts it for reporting "what will get carried" (that run's own session, `live_check`);
the plain doctor never shakes hands just to ask it (see "handshake = one warm-up" below):

| Arrangement | instructionSources |
|---|---|
| Only `AGENTS.md` (has content) | `home\AGENTS.md` |
| `AGENTS.override.md` (has content) + `AGENTS.md` | `home\AGENTS.override.md` (only that one) |
| override 0 bytes + `AGENTS.md` | `home\AGENTS.md` |
| override only whitespace (` \n\t`) + `AGENTS.md` | `home\AGENTS.md` |
| Only a 0-byte / whitespace-only `AGENTS.md` | `[]` |
| Nothing at all | `[]` |
| cwd = `repo/sub/wd`, `repo/.git` present, `repo/AGENTS.md` and `repo/sub/AGENTS.md` both have content | `home\AGENTS.md`, `repo\AGENTS.md`, `repo\sub\AGENTS.md` |
| Same as above but no `.git` | `home\AGENTS.md` (none of the ancestor directories' files count) |
| `repo/.git` present, `repo/AGENTS.md` empty | `[]` |

⇒ 13c's first-draft doctor guessed from "does the file exist" (if an override exists, it reports the override):
when the override was empty or whitespace-only, it reported "override, 0/3 bytes," while what actually got
carried was AGENTS.md; it said nothing at all about the files in git ancestor directories (13c re-review I2/M7).
🔴**Handshake = one warm-up (settled by 13c Fix 1b; overturns Fix 1's claim that "handshake as far as thread/start
= zero quota")**. Method: copy codex's own log database (`logs_2.sqlite`) out of the experiment home and read it
(never touch the original). Inside A0's process (13c's first probe: only initialize → thread/start, **no
turn/start**):
`…thread_start…:session_init:startup_prewarm{…}:model_client.stream_responses_websocket{model=gpt-5.6-luna … websocket.warmup=true api.path="/responses"}…:
connecting to websocket: wss://chatgpt.com/backend-api/codex/responses` → `successfully connected` → `responses_websocket.stream_request{…}: … auth_header_attached=true
… auth_mode="Chatgpt"` → `last_model_response_id="resp_0b42c3da…"`. ⇒ Inside thread/start, codex runs its own
startup prewarm: it connects to **the inference endpoint** carrying ChatGPT credentials, sends a `stream_request`
(a warm-up), and **gets back a model response id**; it produces no actual output, and whether this counts against
quota is ⏳unknown (zero quota made it impossible to measure).
Positive control (same reading method, at no extra cost): every one of R1-R9's processes has the same 2 lines of
prewarm inside thread/start, plus a separate `run_sampling_request` inside the turn; A0's count for that is 0 ⇒
this reading method can tell "warm-up" apart from "an actual turn." Corroborating evidence: across all 13
processes, every `wss://…/codex/responses` connection falls either inside thread/start's request span (26 lines)
or inside a turn (2 lines); the spans for initialize / config/read / skills/list have not one line of network
activity in them (⚠️it never happening does not mean it can never happen). The binary itself has names like
`startup_prewarm_*`; **no configuration key was found** that turns it off.
Listed separately, unrelated to inference: `GET …/backend-api/codex/models` (the model catalog), `POST …/codex/analytics-events/events` (telemetry), and the remote-control websocket loop (`Disabled`).
⇒ Consequences: ① **every codex session started fires one warm-up** ⇒ every zero-quota path (the plain
doctor/setup, `scv start`'s probing, GC) must never start a single session: 13c Fix 1's plain doctor shook hands
specifically to ask about instructionSources — **that** was exactly this bug (`setup` going through doctor caught
the same one), and Fix 1b took it back out; the full table of "who is allowed to reach starting a session" is
pinned by `tests/test_00_budget.py::Budget::test_only_real_requests_and_doctor_live_start_a_cli_session`
(`SESSION_DOORS`), and the behavioral half is pinned by
`tests/test_90_cli.py::Lifecycle::test_zero_quota_commands_start_no_session`. ② R1's cache hit of 8960/9621 is
⏳most likely just the prefix from that same session's warm-up having just gone into the cache (the warm-up and
that turn's response id share a prefix). ③ Each of the dozen or so "handshake-only" probes on the experiment home
(A0, Z*) fired one warm-up of its own — the "readings that never send a turn" batch above is not zero-quota.
**13d: can the plain doctor's handshake go only as far as skills/list (never thread/start), and still see
fail-closed at zero quota? — measured, not adopted; the plain doctor still starts no session at all.**
Method (never touched a single logged-in home): each arm gets a freshly created, **not-logged-in** temporary
`CODEX_HOME` (checked with `login status` = `Not logged in` before each run), going through the production argv
(`codex_argv`) and the same RPC parameters; two measuring sticks, both local to this machine: M = a fake endpoint
(`openai_base_url` points at it, plus a fake API key), which logs every request; P = a metering proxy
(`HTTP(S)_PROXY` points at it, `NO_PROXY` only carries the loopback), which logs every CONNECT and replies 403 ⇒
not one byte reaches the outside network. Each arm waits 8 seconds after its last question is answered, and each
arm is run 2 times, interleaved:

| Arm | M (fake endpoint) | P (metering proxy) |
|---|---|---|
| mock/lite (initialize + config/read + skills/list) | 0, 0 | 0, 0 |
| mock/full (thread/start also sent = positive control) | 1 each: `GET /v1/responses` (`Upgrade: websocket`, ~0.02s after thread/start) | 0, 0 |
| default/lite | 0, 0 | 0, 0 |
| default/full (positive control) | 0, 0 | 1 each: `CONNECT api.openai.com:443` |

(The full arms' stderr also has `codex_api::endpoint::responses_websocket: failed to connect to websocket … /v1/responses`.) The third measuring stick (codex's own log database inside the temporary home) has **not one
row** across any of these arms (rows=0, no positive control could be built) ⇒ it was not used.
⇒ Reading: the warm-up hangs off thread/start (both sticks' positive controls hold), and the lite three steps
make 0 connections within 8 seconds on a **not-logged-in** home.
⚠️But this stick **cannot see what only shows up once logged in**: in 13c's logged-in log database, the two
remote-control lines appear **before initialize** (the moment the process comes up), and the model catalog /
telemetry hang off after thread/start (in every one of those processes, thread/start follows initialize
immediately, so it cannot be told apart whether the lite three steps alone would bring them out too) — on a
not-logged-in home, none of these show up in the first place. Production is exactly the logged-in condition ⇒
"the lite three steps make zero connections" **cannot be measured under production conditions** (never start
app-server on a logged-in home just to measure this) ⇒ per the maintainer's ruling, the status quo stands: the
plain doctor starts no session at all, and handshake fail-closed is visible only through `doctor --live` (a real
request fails loudly, carrying that exact wording). Measuring tool: `t13d_prewarm.py` in the session scratch.

### Archaeology: Task 0c-13's dedicated home (overturned)
- On 09-21, Task 0c measured that `~/.codex/AGENTS.md` went into the context in full, with no switch that could
  stop it (`project_doc_max_bytes=0` / `instructions=""` / `project_doc_fallback_filenames=[]`, all three arms
  leaked) ⇒ the ruling at the time was to give codex a dedicated `CODEX_HOME` (`~/.scv/codex-home`), at the cost
  of making every player log in there one more time. Pointed at a `CODEX_HOME` that does not exist, codex
  **simply refuses to start** (`Error loading configuration: CODEX_HOME points to "…", but that path does not
  exist`, rc=1, and it never creates the directory itself) ⇒ at the time, the bridge created the directory for it
  (`_ensure_codex_home`, chmod 0o700).
- Task 13 added an interactive login subcommand that ran `codex login` inside that dedicated home; measurement
  showed its child process must **inherit the terminal**: with `CREATE_NO_WINDOW`, codex gets an invisible new
  console, and neither the help text (1508 bytes) nor `Not logged in` makes it back through the pipe to the
  parent process (only the parent's own 17 bytes remain).
- 13c took all of this out: the bridge never creates any codex directory anymore (the home belongs to the player:
  if he sets it wrong, codex's own exact wording gets surfaced), and never starts an interactive child process
  anymore.
## paste-cmd
Handing over a command that will not run on the user's machine is a lie, and **a lie is more expensive than a
silent failure**. `paste_cmd` is the one and only quoting/shell criterion (Task 13b gathered the previous three —
the codex login command, `_paste_arg`, and bare `scv <subcommand>` hand-written all over the place — into this
single door). Every rule below is measured:
- After installation, `scv` is **not** on PATH (setup does not install a launcher — that is a product decision) ⇒
  pasting a bare `scv stop` gets "is not recognized as an internal or external command." The door computes **what
  this particular installation actually looks like**: `sys.executable` plus this file's absolute path (never
  `python3`: on Windows that can be the Store's placeholder stub; never `~`: PowerShell 5.1 does not expand it for
  a native program's arguments).
- Measured on this machine: codex is simply not on PATH at all (the one bundled with the desktop app, found
  through the `%LOCALAPPDATA%` glob; on 09-24 `which codex` is still empty) ⇒ doctor gives **the path it
  resolved** (`fix_hint` goes into the API's error body and out over the wire ⇒ never carry a local path — it has
  the user name in it — so it can only give the bare command).
- `os.name == "nt"` only tells you the OS, **and never tells you the shell**. On 2026-09-24, on this machine,
  three real shells (PowerShell 5.1 through `-EncodedCommand`, cmd through `/d /s /c`, Git Bash interactive
  reading from stdin) × 19 kinds of directory name × 7 quoting styles, `scv.py version` run in every cell of the
  grid; readings:
  - **Backslashes**: unquoted, Git Bash eats them (13c re-review M3's `C:WindowsSystem32whoami.exe`); switching
    to `/` and the program started by all three shells recognizes it.
  - **Unquoted**: `[A-Za-z0-9._/:+-]` and Chinese characters hold up in all three shells; `=` / `^` / `,` break
    things apart in cmd, `,` is an array in PowerShell, `'` / `’` / `$` / a backtick / `{}` each mean something
    different in PowerShell, `(` / `&` / `$` / `!` each mean something different in bash.
  - **PowerShell**: a quoted string at the start of a line is just a string expression (following it with
    arguments is a parse error) ⇒ needs `& '…'`; inside single quotes, only quote-like characters need doubling
    (that includes `'` and ‘ ’ ‚ ‛, measured with `a’’b`) — all 19 kinds of directory pass.
  - **cmd**: `%PATH%` still expands inside double quotes ⇒ never give cmd anything with `%` in it; everything else
    (including `&` / parentheses / `^` / `!`) passes (⏳on a machine with DelayedExpansion turned on in the
    registry, `!` would get eaten — not measured).
  - **Git Bash**: `$` / a backtick expand inside double quotes, and in interactive mode `!` is history expansion
    (**non-interactive `bash file` cannot see this cell**) ⇒ anything carrying these switches to single quotes.
  - **`.cmd`** (a CLI installed via npm): all three shells can start it directly (⇒ the pasted line drops
    `cmd /c`: Git Bash rewrites `/c` into `C:/` and starts an interactive cmd instead); but starting it still
    hands it to cmd **to parse all over again**: `%PATH%` also expands in PowerShell/Git Bash ⇒ never give any
    line with it, and say so plainly; Git Bash cannot quote `& ( ) ^ , ; =` for it ⇒ cmd breaks the line apart
    there ⇒ never give it to Git Bash.
- Label on one line, command on the next: the user **selects the whole line and pastes it** ⇒ a Chinese label at
  the start of the line would make the first word that gets pasted not be the command; when all three shells'
  lines come out the same, they get merged into one line.
- Test: `tests/test_90_cli.py`'s `PasteInRealShells` (every line actually runs in the shell it is labeled for, and
  the lines the gate rejects genuinely fail to run).
- During Task 13 (the rounds with the dedicated home), the codex line also had to carry `CODEX_HOME=…`:
  `set "X=…" && exe` is only recognized by cmd (`&&` is a parse error in PowerShell 5.1), and
  `CODEX_HOME=/my dir/x codex login` breaks into two pieces on POSIX. 13c logs into his own home instead ⇒ this
  whole half of the problem is gone.
## cannot-tell-vs-answered-no
The probe throwing an exception ⇒ `helptext=""` ⇒ `"--safe-mode" not in ""` is true ⇒ the user sees "please
upgrade Claude Code," when his real problem might be a timeout / a permissions issue / node crashing — **telling
him to upgrade a CLI that has nothing wrong with it, and after upgrading it will still be the same**.

Whether `--help` writes to stdout or stderr is entirely the CLI's own choice (codex's own status is that it
writes to both). The rc axis is the second cell of the same problem: `run_cli` has no `check=True` ⇒ starting
successfully but with rc≠0 (node crashing / a broken install / the `.cmd` wrapper reporting its own error)
**throws no exception**, and still falls into "please upgrade."
## run-cli-probe-gate
The reason for not registering unconditionally (**at the time this was written**): the `proc_start_id` call
inside `child_add` takes **0.80-0.85s** on Windows, and a run of `--version` calls would be dragged out by
several seconds, while those processes do not live longer than two seconds. ⚠️After the birth id switched to
ctypes on 2026-09-23, it is microsecond-scale on win32 (📎 birth-cert-ctypes), so this reason no longer holds on
win32; what remains is the cost of writing `children.json` twice per registration (registering + settling the
account), and starting one `ps` on POSIX (⏳neither has been measured). The gate "only allowed for probes" is
never loosened because of this on its own: what it guards against is "a real live path that forgot to register,"
which has nothing to do with how expensive registering is.

"Only allowed for probes" is a rule **kept by someone remembering it** ⇒ a runtime gate backstops it, and its
criterion is **how long this path is** (a long timeout means a real live path, and a real live path must
register). The message has to match what actually triggers it: the usual real fix for the `timeout=None` cell is
"give it a second-scale timeout"; saying only "please pass family" would stuff a real probe into the registry,
paying for a birth id and two table writes for nothing.
## child-add-contract
`child_add` **comes in two shapes, and the caller must handle both**: returning `False` means it did not get
registered; raising `OSError` also means it did not get registered, just with the exact original wording attached
(the final `_children_save` call swallows no exception at all: a full disk / a permissions problem / `SCV_HOME`
getting deleted). Both call sites handle it (`run_cli` and `_Pipe.__init__`) — the latter did not used to, and
this was added.

On POSIX, it only accepts a pid that **is itself its process group's leader**: sweep uses `killpg` to kill **the
whole group**, and accepting a pid that is not its own group leader means the next sweep would take out the
bridge's own process group along with it, and `suppress(OSError)` inside `kill_pid_tree` would swallow it without
a sound.
## popen-raises
Measured on 2026-09-22 (py3.12.10/win32) what `Popen` throws:
- a NUL inside the value of argv / cwd / env ⇒ **`ValueError`** (`embedded null character`)
- argv is an empty list ⇒ `OSError`
- the executable does not exist ⇒ `FileNotFoundError` (a subclass of `OSError`)

⇒ Catching only `OSError` lets the three NUL cells **escape bare** (not a `BridgeError`, not a single line
landing on disk). Before Task 13c this was reachable: the codex family's `CODEX_HOME` came from `config.json`,
and the user writing a NUL into it would reach this code; 13c removed that path (a child process's environment
now comes unchanged from the process's own environment, and the OS does not allow a NUL inside that) ⇒ today this
is defense in depth, and ⏳other external inputs have not been surveyed.
## except-what-you-measured
The `ClaudeDriver.__init__` cell is measured the same way: a NUL inside workdir ⇒ `write_text` throws
`ValueError: embedded null character`, never `OSError`.

This is a **real** failure path (workdir is not writable / the disk is full / the path is too long); it used to
throw a bare exception — **not a BridgeError, not a single line landing on disk** ⇒ whatever round wires this up
can only fall back to 500/unknown, and never gets "writing the working directory failed," the one piece of
original wording that could actually tell someone how to fix it.

⭐Which classes an `except` should catch needs to be **measured**: getting the class wrong turns that except into
dead code that never runs, while a test that mocks the same wrong assumption will stay green. ⚠️The
`_Pipe._unlist` cell **catching only `OSError` was checked** (on `_children_save`'s path, `spath()` cannot throw
`ValueError`, and every argument `json.dumps` receives has already had its type checked by `_row_ok`) — never
harden it to match `ClaudeDriver`'s cell (which **specifically** catches `ValueError`) just because it looks
similar: that cell's path is **external input**.
## pipe-closing-flag
The `except (OSError, ValueError)` in the two pump threads **used to assume** there was only one thing it needed
to catch — the `ValueError` from `_close_pipes()` closing a pipe while `readline()` is reading it. **That
assumption has since been overturned by measurement** (see `close-pipes-blocks`: on win32, it is **the closing
side** that gets blocked, and the reading side just gets back `b""`) ⇒ that `ValueError` window is **unreachable**
today; the `_closing` flag is only a fallback for the unmeasured POSIX path, at a cost of four lines.

The consequence of guessing at exception types (measured in re-review): one instance of **the bridge's own read
failing** got swallowed into a quiet sentinel ⇒ that walked down the EOF path and killed a **healthy** child
process ⇒ then ran `classify()` over a **stale** stderr tail ⇒ handed back `quota` plus `retryable=True` plus
"wait for the quota to reset" — a conclusion that was **confident, specific, and wrong**, while the exception, the
`bridge.log`, and the process's own stderr **had the real reason in none of the three places**. ⚠️That is even
worse than not fixing this cell at all: without the fix, at least the pump thread's traceback names `_pump_out`
and the real errno, word for word.

Using `threading.Event` instead of a plain bool: a plain bool does not break today only because "the line that
sets the flag and the `pipe.close()` that makes the pump throw are on the same thread, the former runs first, and
a lock acquisition plus a system call sit between them" — that is **the implementation happening not to reorder
it**, never **the semantics forbidding it**. This same file has already written down the worry that "under a
free-threaded (no-GIL) build this is no longer atomic" for `_err_lock`; this is its tenth time only half acting
on that worry.
## err-deque-race
Measured on 2026-09-22: today's `"".join(deque)` line **cannot be made to hit** `deque mutated during iteration`
(2.9M reads × 9.2M concurrent writes = **0 hits**; even with `switchinterval` pushed down to 1e-6, still 0) — the
whole statement runs inside C and never releases the GIL partway through. The same round's `for x in dq: pass`
hit it **48 times** ⇒ that race can only land on iteration **at the Python level**.
⇒ The reason for this lock is never "this breaks right now."
## stderr-pump-death
The `try` wraps the entire `for` loop ⇒ once it throws, the loop ends, the function returns, the thread ends, and
nothing anywhere in the whole file ever restarts it ⇒ **the tail freezes for good from that point on** (measured).

When it dies between two turns, **the session never drops**: there is no turn in flight at that moment ⇒ no
`BridgeError` gets thrown ⇒ `_run_session`'s `_drop` line never gets reached. This session **keeps serving every
turn normally** afterward (stdout is still fine, the answers still come out right), and the **only** thing lost is
diagnostic ability. This is a **tradeoff, never an oversight**: killing a session that is still working normally
over damage that is purely diagnostic is the wrong price to pay; and it **does not break "blind for at most one
turn"** — the first time something actually goes wrong, that one turn goes blind, and the next turn gets a fresh
process and can see again.
## two-gates-two-mechanisms
Measured on 2026-09-22 which mechanism does which job (never delete either one):
- **Checking the gate at the top of the loop** carries the case "**the CLI arrives faster than the gate
  checks**": the queue always has something in it ⇒ `queue.Empty` never fires, and a gate check written only in
  that branch gets **postponed forever**.
- **Computing `caps` against the gate** carries the case "**the CLI goes completely silent**": when not one byte
  comes out, the top of the loop never even gets a second turn.

⚠️The engine/brief's comment says "the heartbeat keeps flowing ⇒ `Empty` will never fire once" — **that statement
is wrong for this code**: as long as the second mechanism is still there, the wait ceiling shrinks down to the
0.01s scale as the gate gets close, and `Empty` still fires (NC-1a measured green).
⇒ Removing either mechanism alone only turns **one** rig red; only removing both together makes it sit idle until
the whole round times out (NC-1b).
## reap-your-own
The rule "whoever calls `Popen` reaps the corpse" is written right into this same file's `proc_rss_kb` docstring.
The EOF path used to only call `child_remove` to settle the account, never reaping the process ⇒ on POSIX it was
left behind as a **zombie**; on Windows this cell is invisible (Python only complains with a ResourceWarning), and
the bridge is meant to run on Linux.

⏳"A zombie fools both of this file's measuring sticks" (`proc_rss_kb` returns 0 instead of None, and
`proc_start_id` still produces output) **is inferred, never measured on Linux** ⇒ never cite it as a measured
fact. That line's justification does not rest on it.
## read-error-not-eof
**Both halves** of this tradeoff (never write down only the benefit):
- Benefit: never again build a confident, wrong conclusion out of a tail that failed to read in full.
- Cost: if a **genuine** `quota` / `auth_required` happens to **coincide** with a read failure, the layer above
  loses the 429/401 and `fix_hint` (the one piece of wording that could actually tell someone how to fix it).

`_read_error` lives for **the whole life of the pipe** and is **never cleared** ⇒ once turn 1 breaks it, every
turn's EOF after that walks down this same branch. This is **not** a hazard, it is correct: once a pump throws it
exits and never restarts (measured), and the tail freezes for good from that moment on ⇒ the damage itself never
resets, so the flag not resetting either is exactly right. The cost gets caught in `SessionManager`: a session
that has errored gets `_drop`ped on the spot ⇒ the next question gets a fresh process ⇒ **blind for at most one
turn**, which is what makes `retryable=True` actually true to its name.
⏳This is one special case of OUT-1's "watermark," and the whole family gets fixed together under Task 12.
## unlist-once
`child_remove` complains "it was never in the registry to begin with" for a pid that is not on the table, and
what that message means (its own comment says so) is "**either the account got settled twice, or that
registration was rejected and nobody noticed**" — it is the **only signal** for the latter.
The EOF / timeout / cancel paths have all already called `kill()` internally, and if the caller then does
`finally: close()` too (the **most natural way to write** whatever round wires this up), that settles the account
a second time ⇒ it turns that signal into routine noise firing on every failed turn, and the day something
actually goes wrong, nobody can see it anymore. This is the other half of the reasoning behind "never call
`self.kill()`" in `_Pipe.__init__`: the same misleading warning, from two different sources.

`run_cli` spells out this exact reasoning — "never let the exception from settling the account crowd out the one
already in flight" — word for word, in the very same spot, its own finally block; the `_Pipe` side used not to
have it — the same piece of reasoning only half acted on.
## close-pipes-blocks
**Both of this function's stated "whys" were half overturned by measurement on win32 on 2026-09-22; recorded
faithfully here:**
It used to say "closing a file object while `readline()` is reading it makes that thread throw `ValueError`, and
that cell is caught by the two pumps' `except`." Measurement shows **that is not what happens**: when the reading
side is stuck inside `readline()` and something calls `close()`, **it is not the reading thread that throws — it
is the closing call itself that gets blocked** — for **24.04s**, until the reader exits on its own, and what that
read finally gets back is `b""`, never an exception. A zero-input control (closing the same kind of pipe when
nobody is reading it) takes **0.000s** ⇒ all 24 seconds are spent "waiting for the reader."

⇒ ① On win32, the two pumps' `ValueError` branch is simply unreachable (not measured on POSIX, never assume it
is the same there); ② what actually needs guarding against is **blocking yourself** ⇒ changed to "skip it if the
pump is still alive." And this piece of code sits inside `kill()`, and `kill()`'s time budget is written for whatever
round sets the job timeout ⇒ the old way of writing it stuffed an **unbounded** block into a path that was
supposed to have a bound.

The fd that gets skipped has to wait for `Popen` to be garbage-collected, so it **must complain loudly**; but
"leaking one fd" is far lighter than "blocking the bridge for who knows how long," and under normal conditions
`kill_tree` kills the grandchild processes too ⇒ the pump gets its EOF ⇒ this branch is essentially never
reached.

It used to be that only `__init__` closed anything, `close()` only closed stdin, and `kill()` closed none of them
⇒ the other two fds could only come back once reference counting collected the `Popen` (that string of
`ResourceWarning: unclosed file` in a full run is exactly this). Each session holds 3 fds on the parent side
(measured at 15 handles per session on win32).

The line `t is not threading.current_thread()` guards against "calling `close()`/`kill()` from inside the pump
thread itself"; **no path does this today** (re-review checked all 10 call sites of `kill` with an AST) ⇒ **not
one test goes red when it is removed**. It stays because it costs one line of code, and if this were ever
actually hit, the symptom would be `_close_pipes` throwing outright, with not a single fd getting closed.
## kill-time-budget
Measured on win32 on 2026-09-22: **the smooth path takes 0.26s; one pump stuck takes 1.26s; both stuck takes
2.26s.** **The only two things never actually run to completion** are the two 15s waits (nobody has ever waited
`taskkill` or `proc.wait` out to their full length) ⇒ the two **ceilings**, 32 and 17, are pieced together from
"the cells that were measured, plus the two 15s figures pulled out by reading the code."

The previous version wrote "17s" using only the smooth-path numbers, and left out the entire `taskkill` section
(which only shows itself when it times out) — and this docstring names its own purpose as **setting the job
timeout for whatever round wires it up**; underreporting it just sets a trap for that round.
(`read_until`'s EOF path also stacks one more `_err_thread.join(timeout=1)` on top of this.)
## codex-accumulate
Among the `item/*` events, the earliest to arrive is **our own** user message, which the real app-server echoes
back in **~0.006s** (measured by the engine) ⇒ treating "the first event" as the first token, the reading comes
out as a constant 0 while all three tables stay green. ⚠️codex produces zero events while it is thinking ⇒ this
ttfc number is roughly "thinking time + queueing time."

The line `box["text"] += …` used to be guarded by an `if piece:` as well; **a negative control proves it was dead
code** (removing it leaves every test just as green) ⇒ it was deleted: code that looks like it is guarding
something but actually does nothing is worse than no code at all.

`.strip()` is used only in the one check for "did this turn have any body text at all." If every message got
stripped, the invariant "what streamed out equals what got handed back" would **only hold when the answer carries
no leading or trailing whitespace** — and a real CLI's agentMessage nine times out of ten ends with a newline; the
same `.strip()` would also eat the gap between two messages, jamming them together with zero separation (and this
**is visible in the product**).

In `thread/tokenUsage/updated`, this turn's usage is in `last`; `total` is **context occupancy**, never
cumulative spend — subtracting from it produces a number that **looks right but is not** (a trap the engine's
measurements caught on 2026-09-19).

The reason for not using `codex exec resume`: the old path restarts the process on every call and resubmits the
entire thread, so net uncached input **grows linearly, turn after turn** (the engine's two-arm real-CLI comparison
on 2026-09-19: a persistent process saves **87.5%**, wall clock 79.6s → 42.4s).
## b21-vs-b30
`_fail()` writes down `_one_line(raw)`, and on claude's side `raw = out or self.pipe.stderr_tail() or …`, where
`out` is exactly `ev["result"]` = **the entire model answer** ⇒ for a turn where `is_error` is true but `result`
is non-empty, the whole answer lands in `bridge.log`.

**B21 (an error is the exact original wording, unchanged) and B30 (never log the body text) conflict on this one
cell, and nobody had ever ruled on that conflict.** Ruling: B21 is satisfied by **the copy handed back to the
caller** (`BridgeError.raw` unchanged, word for word); the copy that lands on disk keeps only **the beginning** —
the error message is almost always at the front, and that is the part diagnosis needs, never the whole answer.
## clip-not-fold
The cap in the rotation cell is measured against **the existing file**, **before writing**, and puts zero
constraint on **this one line itself** ⇒ the real upper bound is `2 × (CAP + the longest line)`. Measured (cap=4096): a
single line of 10×cap ⇒ **40970** bytes land on disk (claimed 8192, **5 times** over); going through
`JobLog.write`'s real, fat usage ⇒ 41355; going through the real `log()` ⇒ 41008.
🔴The most glaring part: **the test already knew this at the time** (it had slack built into its assertion) —
**the test was honest, the docs lied, and whatever round comes next reads the docs**.
## b4-rotation
Task 8's resource survey logged `bridge.log` as **B4: it only grows, never rotates** (the one and only place in
the whole file that writes to disk, `open(..., "a")`, with no size ceiling at all), in the same family as "input
from the network side directly drives monotonic growth of local disk" — every request with a "mismatched prefix"
writes one line, and a mismatched prefix is something **the caller can produce without even trying**.

When Task 9 built `jobs.log`, it counted this family first, then decided. The method and the result: filtering by
all three of "append-only + monotonic growth + driven by network-side input" together, the whole file has **only
these two members** — `config.json` / `children.json` are tmp + `os.replace`, **a full rewrite each time**;
`work/<sha>` belongs to the session family (family A / B2); `tmp/` belongs to Task 12; `latest.json` /
`bridge.pid` had zero writers at the time (the writes Task 11/12 later added for them all go through
`_atomic_write`, a full rewrite each time, so they never join this family). ⇒ The fix is to make this family
**live in one single place in the code**, never to copy the ceiling into a second spot.

Measured figures: one `jobs.log` line ≈ **305 bytes** (win32 CRLF, usage carrying the real shape's four token
fields) ⇒ about **13,751 lines** per generation; running flat out against the default `remote_jobs_per_hour: 600`,
that fills up in ≈ **22.9 hours**, two generations ≈ 1.9 days. Without a cap, that is **~1.06 GB/year** at full
throttle.

The rotation-failure cell is a reproducible scenario: while another handle holds `jobs.log` open, it grows on
disk to **18 times** the cap (74933 bytes / cap 4096), **with not one sound the whole time**; the moment the
handle lets go, the very next line to come in rotates it right back down (173 bytes). Who might be holding it
open: another `scv` process (the lock is **in-process** and does not stop a different process), someone watching
the log (`tail -f` / `Get-Content -Wait`), or a backup / antivirus scan.
## one-lock-per-name
Measured (the probe only loosens `os.replace`'s timing, not a word of the logic changes): two `JobLog()`
instances not sharing a lock ⇒ one **stale rotation** overwrites an entire generation — **4280 bytes / 22 lines
gone**, `tail(20)` comes back with **2 lines**, no error, no missing fields. The timing is: B does its `stat()`
first (≥cap), A runs its whole rotation through to completion (moving A's generation into `.1`, opening a fresh
small one), and only then does B carry out its own, now **stale**, `os.replace` ⇒ overwriting the freshly opened
small file onto `.1`.

The original docstring's reason — "it carries no lock of its own, otherwise `log()`'s 'complain once' flag would
have to move outside the lock" — **does not hold up**: adding an inner per-name lock, `log()` can perfectly well
**keep holding** `_log_lock` (a fixed outer-to-inner order, with the inner lock never reaching back for the outer
one, so no deadlock is possible). That reasoning only holds if you **also happen to delete `_log_lock`**, and
nothing requires doing that.
## row-is-always-valid-json
The field-level cut only applies to values where `isinstance(v, str)`, and `usage` is a dict whose values are
numbers ⇒ it is unconstrained. Measured: `{"input_tokens": 10**4000}` ⇒ 2012 bytes on disk get the whole line
truncated ⇒ `tail()` throws `JSONDecodeError` — **the same failure mode as NC-11, walking back in through a
different door**.
Two small counts were also once gotten wrong: `row` has **6** `str` fields, never four; and `json.dumps` can also
**expand** a string (one control character becomes six characters).
## usage-whitelist
In the dict `turn()` returns, **only two fields pass CLI data through unchanged** (re-review traced each one
individually):
- `text`: true for both families (claude's is `ev["result"]` with only the two ends stripped; codex's is
  assembled by `+=`, piece by piece) — but it **never reaches `JobLog.write`**, and that is exactly the signature
  gate doing its job.
- `usage`: on claude's side, `ev.get("usage")` is the whole object passed through unchanged; on codex's side it is
  built up field by field, but the **value** of `last.get("inputTokens")` is still copied through unchanged.

`model` was traced to the end: it is a **closed set** (`resolve_model` only lets through names that `catalog()`
reports, and the catalog is the hardcoded `CLAUDE_MODELS` / `CODEX_MODELS`, plus whatever `extra_models` passes
`MODEL_RE.fullmatch`).

The reason the sanitizing point is the driver layer, never the place it lands on disk: this same `usage` also has
to go into whatever round wires up the response, and into the remote leg ⇒ if it were only sanitized at
`JobLog.write`, both of those paths would still send **whatever the CLI stuffed in, unchanged, straight out onto
the network**.
## manager-does-not-log
`_fail`'s contract is "the one and only exit for **the driver layer's** failure path," and the two things
`SessionManager` throws — invalid input (`bad_request`) and the caller cancelling on its own (`cancelled`) — are
both **normal control flow**. Writing a line to disk for every bad request that comes in would turn `bridge.log`
into background noise, and **a gate that gets talked into background noise by its own side is worse than no gate
at all**. The structural gate `NoSilentFailurePath`'s scan **deliberately does not extend to this class**, for the
same reason.

The rebuild log line is a different matter (the one and only place ops can see "this player's session got
rebuilt") ⇒ it still gets written, and **only a genuine rebuild writes it** (pinned by the zero-input control in
`tests/test_50_sessions.py::Log`: a turn that does not rebuild writes not a single line).
## validate-before-spawn
When only the last entry gets validated, a broken entry in the middle throws its own bare exception along
**three separate axes** (measured on win32 on 2026-09-22): content is not a string ⇒
`TypeError: sequence item 3: expected str instance…`; role is missing ⇒ `KeyError: 'role'`; the entry is not even
an object ⇒ `TypeError: string indices must be integers` — **none of them are a BridgeError, and not a single
line lands on disk**.

And all three of these used to blow up **after the process had already started** ⇒ a bad request meant a real CLI
process got started and left behind in `_sessions` (measured: `children.json` gets +1 in each case).
⭐**What is load-bearing here is this ordering, never the wording** ⇒ the test's criterion is "not one extra line
is allowed in `children.json`," never "it threw `bad_request`."
## same-input-two-costs
Measured on win32 on 2026-09-22: for the claude family, `system=None` / `123` ⇒
`TypeError: data must be str, not NoneType` (blowing up inside `ClaudeDriver.__init__`'s `sys_file.write_text`) —
a bare exception, not a single line landing on disk, and it also leaks a `work/` directory keyed by `session_id`;
while the codex family **throws nothing at all**, and `baseInstructions: null` just goes out as is.
⇒ The same bad input **costs the two families differently, and the side that fails is the silent one**: pinning
only claude's path, the one that blows up, leaves codex's silent path just as green.
## slot-leak
This is the **9th instance** in the family of "acquire a resource first, might throw after," and the **only one**
where what leaks is not a process or a file but **capacity**. `with self._lock: self.running += 1` used to sit
**outside** the `try` ⇒ the moment it throws (reachable by `KeyboardInterrupt` / `MemoryError`), the `finally`
never runs at all ⇒ that concurrency slot never comes back.
⚠️The first version of this test **mocked out `_acquire`** ⇒ no slot was ever actually taken, and it was not
testing this at all; the criterion needs to be `acquire(timeout=1)`, never sending one more request — a genuinely
leaked slot **hangs**, and **a hanging test is harder to diagnose than a red one**.
## finally-not-except
A gate should be built to match **the shape of the bug**: the shape is "throwing any exception leaks," never
"throwing `BridgeError` leaks." The previous version was written to match **the incident at hand** (`make_driver`
throwing `BridgeError`), as `except BridgeError`, and re-review's three measured runs each leaked a directory
(`TypeError` / `KeyboardInterrupt` / `_run_once` + `TypeError`).

⭐This mechanism's **capability boundary** (the most valuable thing Task 8 produced): the 5th/6th leaks **were
not from using the wrong `except`, but from never realizing a resource was being acquired here at all** ⇒ the
Settled Idioms block can fix "used the wrong shape," but **cannot fix "never thought to notice the resource."**
## text-passthrough
The two families' `text` comes from different places and is trimmed differently, and **this layer is not
responsible for making them consistent**:
- claude: the CLI's own `result` field, which `ClaudeDriver` has already stripped **once, at the outer level** ⇒
  by the time it gets here it is already trimmed.
- codex: assembled by us, **character by character**, out of one `item/completed` after another ⇒ leading and
  trailing whitespace, and the gaps between messages, are all still there, exactly as they were.

Smoothing this over would make it impossible to ever again check "what did the CLI actually give," and whether to
trim is a decision that belongs to **the display layer**. ⚠️This difference **is visible only on codex's side**
(the claude family is already trimmed, so stripping it again is a no-op) ⇒ the test needs one case for each
family, see `tests/test_50_sessions.py::TextPassthrough`.
## snapshot-is-expensive
Measured on win32 on 2026-09-22: zero children takes **0.000s**; **each additional child adds ~0.70s** (one run
with 2 children measured 1.445s) ⇒ running `max_concurrent=4` flat out is one call taking **~2.8s**, and it is
**serial**. The cost is **entirely** in `proc_rss_kb` — **at the time**, it started one powershell per pid asked
about (the zero-children control at 0.000s shows that `children()` reading off disk costs nothing). ⚠️After win32
switched to ctypes on 2026-09-23, each child is microsecond-scale (measured the same day in fix round 6:
`proc_rss_kb` alone ≈5µs; the cell below still weighs ≈0.2-0.5ms as-is); POSIX still runs one `ps` per child ⇒
the rule "never hang this off an endpoint that gets polled" still holds.

⭐A measured counterexample (never remember only that 0.70s figure): a **brand-new manager with zero sessions**
calling `snapshot()` still took **2.843s** — because the table had 4 rows left behind by **another** manager. ⇒
You yourself had not one session at that moment, and this one call could still stall you for nearly 3 seconds.
**The shape** still holds today: the cost is counted by **how many rows are on the table**, never by how many
sessions are yours; it is just that on win32 each row is now cheap enough to ignore (the weight given above),
while on POSIX each row still costs one `ps` call.

Whichever round genuinely needs to call this at high frequency should decide for itself, based on how often it
calls, whether to batch the query, cache it, or split `rss_kb` out into its own explicit request. **No knob gets
added for this today**: there is not one consumer yet, and adding one now would just be guessing.
## api-is-the-last-catcher
Why the local API layer is **not part of** `NoSilentFailurePath`'s scan, and why that bookkeeping duty lands on
it instead.

**Half of it is about the scan.** That gate recognizes drivers by class name (`_Pipe` or `*Driver`), and requires
every `raise` in the driver layer to go through `_fail()`. The driver layer is the **first** to know something
went wrong with the CLI, and if it does not write that line, nobody will. The HTTP layer is the **last to
catch**: among the exceptions that fly into its hands, the ones the drivers threw **have already been logged**
(logged by `_fail`), and the ones `SessionManager` throws were **deliberately not logged** (a bad request /
cancellation is normal control flow; writing a line to disk for every one of them would turn `bridge.log` into
background noise, see `NOTES.md::manager-does-not-log`). ⇒ Folding it into the same gate would only force "each
`raise` logs its own line" = the same failure logged twice.

**The bookkeeping.** Both of Task 6 and Task 8's re-review rounds hung the duty of logging `bad_request` on this
layer (two Rulings in progress.md), with the criterion, word for word: **send one bad request, and one line
appears in `bridge.log`, and it is possible to tell which message was bad**. It is written in exactly one place,
`_error_payload()`: it adds that line only when `e.logged` is false. ⭐**Use an explicit flag, never guess from
`klass`** — the very same `bad_request` might come from `make_driver()`'s `_fail()` (already logged), or from
`normalize_request()` / `SessionManager` (not logged).

**Why `_text_of` needs `where`.** The version the brief gave reports "received a content block of type X,"
**without saying which one**. It cannot satisfy the "it is possible to tell which one" half of the criterion ⇒
every call site now carries `messages[i]`.
## midflight-close
What the user sees when `/v1/sessions/close` closes a session that is **still answering** (measured on win32 for
Task 8):

- `close_session` never takes `turn_lock` ⇒ it goes straight ahead and closes the CLI that is currently in use;
- a short turn (2.5s) sees **nothing happen at all** (the close only lands after the answer is already done); a
  long turn (25s) is when it actually collides;
- the turn in flight gets stdout EOF ⇒ `classify(tail, 'crashed')` ⇒ **502 "the bridge crashed, retryable" plus
  an empty `fix_hint` plus a line of exact wording pointing at a CLI crash that never actually happened**;
- `close_session` itself **stalls for 10.22s** (`CLOSE_GRACE_S` waits it out before killing the tree) ⇒ turning
  this into an HTTP endpoint would hang it for ten seconds.

⇒ This round settled it two ways: ① the endpoint answers **202** as soon as it hits `CLOSE_ANSWER_S` (the session
is already off the table by then; all that is left is cleanup); ② `_closed_midflight()` rewrites that cell to
`cancelled` (499, not retryable), **with the CLI's own exact wording left in the parentheses, unchanged** (B21).
⭐**Only `crashed` gets rewritten**: a genuine timeout or a genuine out-of-quota in that same turn is its own
conclusion, and papering over it with "someone came and closed it" would just be lying in the other direction.
## windows-reuseaddr
`HTTPServer.allow_reuse_address = 1` is the stdlib default, and `SO_REUSEADDR` **means something different on
win32**. Measured on this machine on 2026-09-22 (py3.12.10/win32, five cells, loopback port):

| Whoever takes the port first | The one that comes after | Result |
|---|---|---|
| A bare socket (option off) | on | refused (WinError **10013**) |
| **The bridge, on** | **on** | **both get bound** (two listeners on the same port) |
| The bridge, on | off | refused (10048) |
| The bridge, off | off | refused (10048) |
| The bridge, off | on | refused (10013) |

⇒ Squatting on the port only happens when **both sides have the option on** — and that is exactly what happens
under the stdlib default (with both bridges being `_Server`). The second bridge comes up **without a sound**, and
after that which one a connection lands on is undetermined. `_Server.allow_reuse_address = os.name != 'nt'` turns
this off; on POSIX it stays on as usual (there, all it does is let the listening port rebind after TIME_WAIT — it
never lets a second listener in).

⚠️**The test's shape was measured, never guessed**: the first version's control used a **bare socket** to take
the port (the table's first row) — that cell gets refused by the OS either way the option is set ⇒ that test
**could not tell at all whether this knob was on or off** (turning the knob back on and running it, not one test
went red). Whatever takes the port first has to be a bridge too (row two versus row four in the table).
## refuse-big-body
Whether **the request body needs to be read first**, when one of the four guards rejects a POST. Measured on
this machine on 2026-09-22 (win32, loopback, a wrong token):

| Request body | Reads not one byte | Reads only the first 64 KB | Reads the whole thing |
|---|---|---|---|
| 2 KB | 401 | 401 | 401 |
| 200 KB | connection dropped (WinError 10053) | connection dropped | **401** |
| 2 MB | 401 (**flaky** on this run) | connection dropped | **401** |

⇒ "Reading a little" and "reading nothing" land in **the same cell** here; the "reads the whole thing" column
hit 3 for 3 (three independent processes). ⚠️The `2 MB / reads not one byte` cell did once measure 401, ⭐which
shows **one success in a small sample proves nothing** — the criterion has to use a **large** request body, never
just any small one picked at random (all three ways of handling a small body come out green).

The ceiling used is `MAX_BODY`: that is already the amount this leg is willing to accept, and it only listens on
the loopback (the peer is a local process anyway).
## session-family-cap
Task 8 counted a whole family of local resources (A1-A9) that are **keyed by `session_id`**, and `session_id` is
**a key handed over from the network side, with no ceiling of its own** ⇒ the real fix is **one ceiling**, never
patching in nine separate reclaim points. After Task 11 wired up the remote leg, this was measured again by
opening N jobs in a row **through the remote leg** (win32, py3.12.10, 2026-09-22, a fake CLI):

| Each additional persistent session | Delta | After `close_all()` |
|---|---|---|
| win32 handles (= 3 parent-side pipe fds + process/thread handles) | **+16** | fully reclaimed (8 sessions: -128) |
| Python threads (the two pumps) | +2 | -16 |
| `_sessions` / `_locks` / `_lock_users` | +1 each | -8 each |
| `children.json` lines | +1 | -8 |
| `work/` directories | +1 | -8 |
| bridge process RSS | ~+140 KB (noisy) | -380 KB |

**Zero-input control** (start the bridge the same way, connect the stream, **push not one job**, wait the same
length of time): every Δ above is **0**, and `close_all()`'s Δ is also all 0 ⇒ those increments genuinely come
from the jobs, never from "measuring it twice." The **one-shot turn** arm (no session): **every cell is 0** except
`_seen` (the idempotency memory, capped by `SEEN_JOBS`).

**Why the ceiling is 24, and why it is set against fds.** Running out of processes hits memory — and that is
still visible (the machine slows down, Task Manager shows it); running out of fds hits `RLIMIT_NOFILE` (a Linux
soft limit of **1024** by default), and hitting that means **not even a log line can be written, and no
connection can be accepted** — a whole-process, silent failure. On POSIX, one session = **3 parent-side pipe
fds** ⇒ 24 × 3 = 72, which, together with the local leg, that SSE pipe, the short-lived connections sending
things back, and logging, uses at most a fifth of the soft limit. ⚠️**Measured only on win32**; those 3 fds on
POSIX come from reading the code, never measured — each OS is its own separate path. ⚠️24 real CLIs (node) would
already have crushed a home machine on memory alone ⇒ **the fd math is here to prove that 24 is safe, never to
explain why 24 is this small** — it is small because this is someone else's machine.

**Who gets evicted, and when.** First `gc_idle()` (an idle one costs nothing to reclaim), then evict whichever
one has gone **longest unused**; the next time an evicted one comes back, it gets rebuilt from scratch and
carries a `rebuilt` line. **Never, ever evict one that is currently answering**: `close()` would make that turn
see stdout EOF ⇒ reported as `crashed` = "the bridge crashed, retryable" — exactly the lie `_closed_midflight()`
exists to fix. The criterion is "**can its turn lock actually be acquired**," never `locked()` (there is a gap
between the latter and `_drop`). What makes this possible is `max_sessions > max_concurrent` (the number of turns
in flight is capped by the concurrency slots ⇒ there is always something evictable).
⚠️A fixture lesson forced out by negative control NC-6: that turn has to run **longer than `CLOSE_GRACE_S`
(10s)**. Making it only 3 seconds slow, deleting the lock check entirely still left the test **just as green** —
`close()` is graceful (it closes stdin first, then waits out the grace period), and a short turn finishes
answering on its own within the grace period ⇒ "the one in flight got closed" and "it never got closed" look
exactly the same. **A fixture's magnitude is one of its properties too.**

**The `_locks` cell (A4).** It used to be reclaimed by **nothing at all** (`_drop` / `gc_idle` / `close_all` all
clean up `_sessions`, not this). Reclaiming it **must never just `pop` it**: a thread that has just fetched the
**old** lock from the table but has not yet `acquire`d it could end up in the same session at the same time as
another thread that fetched the **new** lock ⇒ two driver processes get started, one of them gets overwritten on
the spot and nobody can ever find it again (exactly the shape this table exists to prevent). ⇒ The criterion is
**a reference count** (`_lock_users`), never `locked()`.

**The half nobody had done yet at the time** (Task 12 has since wired this up: `serve_until` calls `gc_idle()`
once every `GC_EVERY_S`, and `sweep_orphans()` runs inside `cmd_run` / `cmd_stop`): at the time, `gc_idle()` only
got called at the moment a new session needed building, and `sweep_orphans()` had zero production call sites ⇒
once the dispatcher goes quiet, those idle CLIs stay alive until the next new session or until the bridge stops.
**The time-driven call belongs to whatever round wires up serving** (Task 12 did this, see above).
## stream-max-age-guess
`STREAM_MAX_AGE_S = 25` is a **conservative default, never something measured**. What Task 0a wanted to measure
was "whether the proxies/CDNs in the middle cut a connection based on its age," and that can only actually be
measured once this is live in production (measuring over the local loopback only measures this machine). There is
only one reason 25 was chosen: it is shorter than the common 30/60-second idle-gate ⇒ **we are the ones cutting
it, and we pick the moment**; and "cut by someone else" and "we switch it ourselves" walk through the same code
(both resume through `Last-Event-ID`, pinned by the Reconnect test). `0` means never proactively switch.
⚠️Change it once real numbers come in — never read this 25 as a conclusion.
## two-legs-one-entry-gate
The remote leg's input goes through **the exact same `normalize_request()`** as the local leg, never a rewritten
copy of it. The consequence of "the same rule, two submission paths" missing installation on one side is a bug
that **only shows up on the other side**, and the two paths do not cost the same. The version of the remote leg
the brief gave skipped the whole row of entry gates, and negative control NC-9 measured the actual consequences:

- `opts.timeout` never goes through `_seconds()` ⇒ the dispatcher handing over `1e9` holds a concurrency slot
  hostage forever (this is exactly why `MAX_TIMEOUT_S` exists); handing over a phrase written in Chinese meaning
  "thirty seconds" instead throws a **bare `ValueError`**, which the layer above catches and reports as "the
  bridge itself broke" — an error message that **lies** (the actual measured wording:
  `could not convert string to float: '<the Chinese phrase>'`).
- `session` / `model` / `effort` never go through `_name()` ⇒ their length has no ceiling, and `session` becomes
  a key in `_locks`, while `model` goes unchanged into `jobs.log` and into the exact wording of errors.
- `job_id` cannot go through `normalize_request()` (it has to be used for the ack first) ⇒ its own length check
  is written separately, inside `_take()`.

The cost, written down plainly: this path therefore also inherits **every** behavior of that compatibility table,
including "a `system` role mixed into `messages` gets merged into `system`." PROTOCOL.md says `messages` only has
user/assistant; this is **wider** than that, never narrower.
## spawn-window
`Popen` has already started, and this `_Pipe` has not been handed to anyone yet — **any exception escaping this
stretch of code means a persistent CLI that nobody knows about and nobody can kill**. The window is real:
`child_add` has to ask for a birth id and write to the registry, and either step can throw (before the switch to
ctypes, the birth id alone took ≈0.8s on win32; now it is microsecond-scale — the window got narrower, it never
disappeared; this gate is about "**any** exception," regardless of how wide the window is).
This used to have only `except OSError`, while `child_add`'s contract **only promises** two shapes — returning
`False` and raising `OSError` — and other exceptions can still fly out of this stretch (a `RuntimeError` from a
thread failing to start, `KeyboardInterrupt`, `MemoryError`). ⇒ The cleanup goes through `finally` plus a flag
(the same shape as `_run_once` / `_run_session` / `CodexDriver.__init__`). The negative control (reverting to
`except OSError`) goes red on exactly "it started but nobody kills it," and both injection points go red.
⭐**The two injection points each pin down one half**: throwing **before** registration ⇒ the account was never
recorded, so only kill the process and close the fds — **never settle an account that does not exist** (doing so
would complain "it was never in the registry to begin with," and that message is the only signal for "that
registration got rejected and nobody noticed"); throwing **after** registration ⇒ the account was recorded, so
**the account must also be settled**, on top of killing the process. Testing only the first one leaves the test
just as green when the second one gets missed.
## sse-decode-replace
Two decisions on the receiving side of the stream, both choosing between **a livelock** and **silence**:

- **Decoding uses `errors="replace"`, never strict decoding.** Strict decoding throws `UnicodeDecodeError` ⇒ the
  whole pipe reconnects, and `_last_id` is still sitting **before** the bad event ⇒ the other side resends it
  unchanged, it breaks again, and backs off up to 60s — a livelock. Switched to replacement characters, the
  broken one mostly fails to get past `json.loads`, gets treated as "one bad event," complained about loudly, and
  dropped, while `_last_id` has already moved forward. **The criterion sits at the JSON step**, never at
  decoding.
- **Before giving up on an oversized event, `_last_id` gets pushed forward first.** Not pushing it forward is
  the same livelock (measured, NC-12). ⚠️The style where `id:` comes **after** `data:` cannot be saved here
  either (`eid` is still empty at that point) ⇒ that cell is still a livelock, it just complains loudly on every
  round. Never describe this as "already solved."

**There is only one budget for the ceiling, checked in one place**: what `readline()` receives is **how much
allowance is left**, and it only bails once the cumulative total goes over. Never write this as "one ceiling for
readline, a separate one for the cumulative total" — that would be **two guardrails against the same symptom**:
measured on 2026-09-22, deleting `readline(N)` entirely left the test that checks "the event got dropped" **just
as green**. The two criteria now each cover their own ground: too many lines accumulating ⇒ checked by "not one
byte of that event ever came back"; a single line that never ends ⇒ checked by "**how many bytes the dispatcher
actually pumped in**" (measuring RSS does not work either: that whole blob gets freed right after the bail, so
both arms come out green).
## model-name-not-identity
The `model` field in `jobs.log` is a **name, never an identity**: the same name can point to different models at
different times (measured on a neighboring line of work, multiple instances found among 98 keys) ⇒ computing BYOK
billing off this log will **silently undercount**. The cheapest patch is to also write down the **CLI version**
that `detect()` has already measured, at the moment this lands on disk (`cli_version`, shaped by `_cli_version()`
into either `1.2.3` or an empty string ⇒ the "never log the body text" property is not loosened by this).
⚠️Two boundaries, written down plainly: ① it can only pin down **the CLI layer** — if the model behind the CLI
changes, this still has no way of knowing; ② **logs written before this line was added simply do not have this
key** ⇒ whoever reads them must treat "this key is missing" as **unknown**, and must never read it as "the same
as now."
## redial-pacing
`run()`'s inner `while: self._stream_once()` used to have **no backoff at all** (backoff was hung only off the
`except`), and `_stream_once()` **returning normally** has three paths: EOF / hitting `STREAM_MAX_AGE_S` /
dropping an oversized event. Measured in re-review (win32):

| Scenario (none needs malice, only an unhealthy peer) | Redial frequency | Consequence |
|---|---|---|
| The dispatcher closes the stream right after a 200 | **398.5 times/sec** (3188 times in 8 seconds) | hammers the dispatcher/middle proxies as if it were a DDoS source |
| An oversized event with `id:` after `data:` | **45.7 times/sec** | **the whole leg locks up permanently**, and every job after that never runs |
| The same event but with `id:` first (zero-input control) | 0.2 times/sec | normal — after 1 bail, healthy jobs keep running |

⚠️In my own first-round self-check I wrote the second row as "complains once per round" — **the magnitude was
underreported by two orders of magnitude, and the half saying "nothing after that ever runs again" was not
written down at all**; the first row's cell was **missed entirely**. ⭐The lesson is that the wrong criterion was
chosen: what I measured was "did that bad event get dropped" (it was green), when what should have been measured
was "**how many connections the dispatcher sees within T seconds**."

**Two diseases, two criteria, never merge them into one** (split apart on 2026-09-23; the half-finished version
had merged them into a single "short-lived AND no progress"):

| Disease | Criterion | What it complains |
|---|---|---|
| ① **A livelock** (cannot get past an oversized event) | this connection **ended because it gave up on an oversized event** AND `_last_id` never moved | "…the dispatcher put `id:` after `data:`…" |
| ② **Disconnects right after connecting** | this connection did not live to `MIN_REDIAL_S` AND `_last_id` never moved | "…got disconnected in under a second of connecting…the dispatcher is unhealthy…" |

Both diseases walk down **the same** exponential backoff (complaining once after `STUCK_REDIALS` tries, backing
off all the way to `MAX_BACKOFF_S`), only **what gets complained about is split by cause**. Two harms from
merging them into one (each pinned by its own test, and each negative control has gone red):
- ② getting reported as ①: when the dispatcher "closes the stream right after a 200," that version complained
  "the dispatcher put `id:` after `data:`" — **a diagnosis pointing at the wrong place**, sending the
  dispatcher's people off to dig through frame-format code that has nothing wrong with it.
  Test: the false-positive side's two lines in `Pacing::test_a_dispatcher_that_hangs_up_is_not_a_hot_loop`.
- ① requiring "short-lived": on a real network, reading a full 256 KiB can take over 1 second ⇒ "short" never
  holds even once ⇒ **it stays stuck forever, dialing once a second, never once complaining about the actual
  cause, and nothing after it ever gets in**. Test: `Pacing::test_a_slow_stuck_stream_is_still_caught` (turns
  `MIN_REDIAL_S` down to 0 to **simulate** "every connection lives long enough," never actually building a slow
  network).

Why each one needs both halves: for ②, checking only "no progress" does not work — a **healthy but idle**
connection also makes no progress, and backing it off would make a job arrive a minute late; checking only
"short-lived" alone does not work either — "send one and hang up" is a legitimate shape on the other end, that
cell does make progress and should not keep backing off further and further (though the minimum interval still
has to apply).

⭐**① is exactly the checkable shape of the protocol requirement being violated (`id:` must come before
`data:`)**: when the other side puts `id:` after, `eid` is constantly empty ⇒ we can never get past that one.
⭐This checks "**I am stuck**," a fact **checkable locally**, never parsing what the other end's frame looks
like — the latter has to guess at the other end, the former only looks at itself. The false-positive side is also
clean: an oversized event with `id:` first still advances `_last_id` ⇒ **not one word of complaint gets said**
(pinned by a zero-input control).

The generic "event exceeded the ceiling" complaint fires **only once** during a stuck stretch (`_append_capped`
guards a byte count, never information — a self-inflicted hot loop can crowd every other line out of rotation
within seconds). ⭐"Is this connection healthy" is judged in **exactly one place**, `_sick()`; the complaining and
waiting live in `_pace()`: `_stream_once()` only returns one thing — "did this connection end because it gave up
on an oversized event" — and never reads the backoff state itself (it used to read `self._stuck` to decide
whether to complain — two methods sharing one piece of mutable state is exactly the kind of wire that has to get
cut when splitting a module apart).

**The third path (2026-09-23 re-review addendum I-A)**: `/bridge/stream` returning a non-2xx goes down `run()`'s
`except` branch's exponential backoff, and it used to be that a successful hello alone reset `backoff` back to 1
⇒ when the stream endpoint alone breaks, that produces **one hello + one stream attempt + one log line, every
second** (measured in re-review: 39 times in 40 seconds). Now a successful hello never resets it; it only goes
back to 1 when `_sick()` judges "this connection is healthy."
Test: `Pacing::test_a_stream_endpoint_that_errors_while_hello_works_backs_off_too`.

**The fourth path (2026-09-23 re-review addendum two, I-1; a regression the previous fix introduced on its
own)**: after the previous fix, resetting to zero only went through `_pace()`, and `_pace()` only gets called
when `_stream_once()` **returns normally** ⇒ a connection that genuinely connected, received events, and then
ended in an **exception** (a read timeout / RST / `IncompleteRead` — the most common ways a real network
disconnects) still had its backoff double all the way up to 60 seconds (measured in re-review: 6 times in 40
seconds; before the fix it came back to a constant 1 second, 16 times). Now `run()` runs this connection through
**that same** `_sick()` before letting the exception escape: if it was healthy, `backoff` goes back to 1. Never
write a second criterion inside the except block — two criteria will eventually drift apart (this very bug is the
consequence of "only one path went through the criterion").
⭐`_sick()` now also checks one more thing: **whether a 2xx was obtained at all** (`opened`, based on whether
`connects` moved). Checking only "lived past 1 second" alone, a dispatcher that **slowly returns a 5xx** (an
overloaded origin, a CDN timing out its origin, etc.) would have every connection "live," and always go back to
1.
Three tests: `Pacing::test_a_pipe_that_made_progress_and_then_died_of_an_exception_is_not_backed_off` (**the
lower bound**: the symptom is "too few connections," which an upper bound cannot see) /
`…_without_progress_is_still_backed_off` (zero-input control: it goes red if exceptions always reset to zero) /
`Pacing::test_a_slow_error_is_not_a_connection` (the `opened` half).
⭐All three tests' upper bounds are **computed off the backoff sequence itself** (`dial_bound()`), never "under 3
times a second": the latter still comes out green when "only the 1-second minimum interval is left, with no
exponential backoff" (measured in re-review M-B), while PROTOCOL promises retries getting sparser and sparser.

**The "lived past 1 second" half (2026-09-23 re-review addendum three, I-1; test-only)**: the three tests above
only pin down "made progress ⇒ healthy." The most common shape in production is **an idle bridge**: it only
receives keepalives, `_last_id` never moves, and the connection either gets switched over once
`STREAM_MAX_AGE_S` expires (a normal return), or gets silently dropped by a NAT/proxy and ends in a read timeout
(an exception) — both of these were judged healthy **only by** "lived past `MIN_REDIAL_S` since dialing."
Re-review deleted this whole half (`if not progressed:`), and test_80's 47 tests all stayed green. Each path gets
its own test, and both criteria are **a lower bound** (`dial_floor`, with a different `gap` for each path):
`Pacing::test_an_idle_pipe_that_outlived_a_second_and_then_timed_out_is_not_backed_off` (the exception path) /
`Pacing::test_an_idle_pipe_recycled_on_schedule_is_neither_backed_off_nor_accused` (the normal-return path,
checked separately — and never complains "disconnected in under a second" either). Measured in fix round 6
(win32): 7/10 with the fix in place, the lower bound at 5/8, and 4/4 with that half deleted; deleting it on only
one path meant only that one path went red.

**"Lived past 1 second" now counted from getting a 2xx (2026-09-25 Task 15b, re-review addendum three M-2)**: it
used to be counted from **dialing** ⇒ when the dispatcher **is slow to return the 200, then immediately closes
the stream**, every connection "lived," and it never backed off (measured before the fix, in 15b: the header
takes 1.2 seconds to come back ⇒ a constant interval of 1.23-1.26 seconds, 12 times in 14 seconds, not a single
complaint). The **RST version** (resets right after the header) is a race: if the RST beats urllib finishing
reading the header ⇒ it does not count as getting a 2xx, and it backs off; if it comes after ⇒ counted "lived"
from dialing, resets to zero (measured interval mostly 2.29 seconds, occasionally 3.28 seconds). Now
`_stream_once` records `opened_at` the moment it gets a 2xx, and `_sick`'s lifetime is counted from that ⇒ both
orderings get judged as "disconnected right after connecting," and **the race is gone**; `_pace`'s minimum
interval is still counted from the moment of dialing. Two tests (asserting the doubled intervals of 1, 2, 4
seconds): `Pacing::test_a_dispatcher_that_answers_200_slowly_then_hangs_up_is_backed_off` /
`…_then_resets_is_backed_off_every_time`.
⚠️Cost: the margin in the two "idle connection" tests above (`IDLE_LIFE` 1.2 seconds) comes out of the few
milliseconds between dialing and getting the header back, still about 0.2 seconds over loopback; on a real
network where the header is slow to come back, an idle connection now has to genuinely live 1 full second
(counted from getting the 2xx) before it counts as healthy — and that is exactly the standard we want.
## ack-on-stream-thread
**`ack` (and, on the rejection path, the `error` that follows it, and dup `ack`s) used to be sent synchronously
on the stream-receiving thread** (re-review M-6). Assessed only on 2026-09-23; **fixed on 2026-09-25, Task 15b**:
the stream-receiving thread now only recognizes `job_id`, and hands everything after that (`_admit`: dup? → ack →
record "seen" → claim a slot → start the thread) off to **a send channel** (`scv._Outbox`: one thread plus a
bounded queue, doing one item at a time in arrival order).

- **Numbers measured before the fix** (15b, `task-15b-probe.py m6`, win32, production constant: 30-second return
  timeout): the dispatcher hangs job B's return channel, while `/bridge/stream` stays open ⇒ the `cancel` queued
  behind B (cancelling an in-flight A) does not take effect for **92.5 seconds** (B's ack made one attempt each
  at 0.1 / 30.7 / 61.7 seconds); on the rejection path (already full in flight, B needs both an ack and an
  error; the fixture has the 3rd ack attempt land right at 29 seconds, with all three error attempts each
  hanging past their timeout) it takes **183.3 seconds**. This matches the 91/182 seconds computed from the code
  before the fix (the extra time comes from killing A's process). The same probe after the fix: **0.7/0.8
  seconds**.
- **Why "move the whole segment," never just the ack**: `_seen` (has this been seen before) is touched by
  **exactly one** thread, and a dup gets judged strictly by arrival order — if a second copy of the same
  `job_id` arrives while the original's ack is still hanging, it is queued behind the original: if the
  original's ack fails (never recorded as "seen"), the second copy still gets run as a new job; if the original
  succeeds, the second copy gets a dup ack back. If the stream-receiving thread recorded "seen" first and handed
  the ack off to be sent by someone else, this cell would send back a dup ack for a job that in fact was never
  delivered — a lie.
- **A cancel takes effect on the spot even for a job still queued** (15b fix1, re-review I1): if it is in
  `_cancels` on the stream-receiving thread (it is in flight) ⇒ it takes effect immediately; if it is not there,
  but it **has already been queued into the channel** (`_queued` tracks how many copies of each queued id are
  queued) ⇒ it gets recorded into the "cancel arrived early" table (`_early`), and the moment `_admit` registers
  this job, it checks on the spot, and a hit cancels it (`_job` gets it as already `cancelled` before it even
  starts the CLI); the "check + write" on both sides sit inside the same small lock ⇒ either it is already
  registered (the set happens on the spot), or, if it gets registered later, it is guaranteed to see this entry.
  ⭐**An id the bridge has never queued at all ⇒ nothing happens** (fix1 addendum, the maintainer's ruling): the
  dispatch stream is ordered, and a cancel arriving ahead of its own job only happens under an abnormal
  ordering; honoring it then would mean silently cancelling a job nobody actually asked to cancel (test:
  `…::test_a_cancel_for_a_job_the_bridge_never_saw_does_nothing`: pushing the cancel before the job ⇒ it still
  gets done).
  ⇒ Both tables are bounded (the keys in `_queued` are ≤ the queueing ceiling plus whatever the channel is
  holding), never something that needs evicting. The `_cancel_queued` fallback for a job queued into the channel
  was removed — ⚠️at the time, the fix1 addendum said it was "only there for when the table gets evicted," and
  that premise was incomplete: it was also covering the cell "the other copy of the same id was never taken"
  (re-review N-I1: when the original's ack fails, or a second copy hits a full queue and gets dropped, exiting
  along the way invalidates the early cancel ⇒ the copy that did get taken runs anyway, and the cancel silently
  falls through). fix2 fixes it right there: only when **not a single copy of this id is still queued** does the
  copy that was never taken invalidate the early cancel entry (`_unqueue`'s `ev is not None or n <= 0`). Two
  tests: `…::test_a_cancel_survives_a_sibling_copy_*`.
  Each queued job's "queued" account gets settled exactly once: when it gets taken, in the same lock as
  registration (`_unqueue(jid, ev)`); when it is never taken (dup / ack failed / rejected / never got queued),
  inside `_admit`'s wrapper. ⚠️Copies dumped out by `_Outbox.stop()` when the bridge stops never reach that
  wrapper, and their account is left open (harmless: once the bridge stops, this whole leg is thrown away
  entirely, checked in re-review). ⚠️15b's first version relied only on "queued into the channel, behind it":
  first-in-first-out only guarantees "behind it," never "right next to it" — every job queued in between still
  has to finish sending its own ack first (a re-review probe, `qcancel`: pushing a cancel took 16 seconds to
  take effect; when only the return channel is slow, the job had already finished running long before, and the
  cancel fell through). Tests:
  `SendChannel::test_a_cancel_for_a_queued_job_is_not_held_up_by_the_acks_queued_behind_it` /
  `…_is_not_lost_when_the_result_channel_is_only_slow`.
- **`close_session` jumping ahead of a queued job** (15b fix1, re-review I2): close gets handled on the spot on
  the stream-receiving thread, while a job for the same session might still be sitting in the channel ⇒ in 15b's
  first version, that job then got rebuilt from scratch (a re-review probe, `close`: 4/5 a retryable fake
  `crashed`, with the exact wording carrying a local path, 1/5 rebuilt the very session that had just been
  closed; BASE: 5/5 `cancelled`). Now `_take` records the moment of **arrival** and carries it all the way
  through to `_job`; `_job` checks `closed_during` before starting the CLI ⇒ `cancelled` on the spot; a rewrite
  for one closed while in flight (`_closed_midflight`) is also judged by arrival. The root cause (a new and an
  old instance of the same id sharing a directory) is fixed separately, 📎 NOTES.md::one-dir-per-instance.
  ⭐**The remote leg judges ordering by event sequence number, never by the wall clock** (fix2,
  re-review N-I2): fix1 compared using `time.time()`, and close and job get handled back-to-back on the same
  stream-receiving thread, while win32's clock ticks roughly every 1 ms ⇒ "closed, then asked" often ties, and
  `>=` judged it as "closed while queued," a false `cancelled` that is not retryable (8 times in 20 rounds of
  re-review); the opposite cell, "the job came first," turns out to also be passing only because of ties too ⇒
  never simply swap `>=` for `>`. Now the stream-receiving thread's `_dispatch` numbers every event (`_order`,
  written by nothing else), and `note_close` and `_take` each record one; when `closed_during` has a sequence
  number on both sides it compares those (strictly increasing, no ties), otherwise it still falls back to the
  wall clock — **the local leg** has no single arrival ordering (one thread per request), so
  `/v1/sessions/close` and a local chat still go by the wall clock; a remote job colliding with a local close
  also falls back to the wall clock.
  ⭐**close detaches the session from the table on the spot, on the stream-receiving thread**
  (`SessionManager.detach`, never waiting for the closing thread to pop it): without detaching it immediately, a
  "closed, then asked" job could grab the old process ahead of the closing thread and then get closed out from
  under it ⇒ a false `crashed`. The closing thread only ever closes what has been detached (`close_detached`) —
  whatever is sitting in the table might be one a job created fresh after the close. ⭐**Another one arrives
  while it is still closing** (re-review M-b, the maintainer's ruling): never start another thread; `note_close`
  only refreshes the record to point at this new attempt (and returns `False`); this attempt also detaches on
  the spot, and the closing thread, once it finishes what it is holding, checks in `note_closed` whether the
  record has since been refreshed (the sequence number no longer matches) ⇒ if so, it picks up one more round.
  "check refreshed + record closed" and "check still closing + refresh" both sit inside `_closed_lock` ⇒ either
  the existing thread picks it up, or this new attempt sees "already closed" and starts a fresh one itself —
  never a leak.
  ⭐**The cross-leg cell (fix3, re-review 2's N-M1)**: "picking up one more round" is something only the
  remote closing thread does; while local `/v1/sessions/close` is still closing (its row carries no sequence
  number) and a remote attempt comes in ⇒ this attempt is treated **as a brand-new one**, starting its own
  thread to close whatever it detached, and `note_closed` decides who gets to record "closed" by "does the row's
  sequence number belong to this attempt" ⇒ even if the local thread finishes first, it never records the row as
  closed. This was chosen over "the local thread also picks up another round": whoever detaches it closes it,
  the local leg never touches `_detached`, and it never grows a second copy of the remote side's "pick up
  another round" logic inside the local leg.
  ⚠️Cost (the maintainer ruled: accept it as is): this cell's "closed" can get reported **a few seconds ahead**
  of the local, old process actually exiting — the remote thread records `closed` by the row's sequence number
  as soon as it finishes closing what it detached, while the local thread might still be waiting out the old
  process's shutdown (the full grace period plus one tree-kill). Nothing leaks unclosed, nothing gets closed
  twice, the state just runs a few seconds ahead of the process; this can only be hit when both legs use the
  same session id. Test:
  `CloseGate::test_a_remote_close_while_a_local_close_is_still_closing_is_not_left_open`.
  ⭐**A cell present since BASE** (fix2 addendum, the maintainer's ruling): if a job is in the middle of
  starting its CLI right at the moment close arrives (codex's handshake takes a few seconds; the new session is
  not registered yet), close cannot detach it ⇒ before this fix, that turn just ran to completion anyway and the
  session was left behind. Now `_run_session` **registers first, then checks** (the `closed` callback is
  `closed_during(sid, <arrival>)`), and close's own "detach + record" moves into `note_close(detach=True)`,
  inside the same `_closed_lock` ⇒ if the check happens before "detach + record," the detach that follows is
  guaranteed to catch the one that was just registered; if the check happens after, it already finds it ⇒ either
  way this same one gets killed on the spot, never left as a persistent process, ending in `cancelled`
  ("closing…while the CLI was starting"). ⚠️If detaching were still done on the stream-receiving thread, but as
  a separate step before "record," registration landing exactly in between would leak on both sides. Test:
  `SendChannel::test_a_close_while_the_job_is_starting_its_cli_leaves_no_session`.
- **Ceiling**: the queue is 4× the concurrent-in-flight ceiling (64 items by default); when the return channel
  is healthy, pushing a burst that is **no more than this many** all get an ack, and anything past that gets
  dropped (a re-review probe, `burst 100`: under default config, 30/100 got no ack; BASE sent synchronously and
  eventually acked everything ⇒ this is the cost 15b bought, and PROTOCOL's hard-limit table now has a line for
  it). ⭐What gets queued is **the raw event's bytes**, never the parsed object: 15b measured a single 256 KiB
  event parsing out to 7-19× the original size (many empty messages come to 1.9 MB; `[[0],[0],…]` comes to 5.1
  MB) ⇒ capping by item count at 64 items would mean roughly 320 MB. Storing bytes instead ⇒ each item is ≤256
  KiB (bad bytes decoded to U+FFFD and re-encoded can be at most 3×) ⇒ worst case 48 MiB; parsing only happens
  at the moment a job is started (a dup or a rejected item is never parsed at all). ⚠️This same multiplier
  already held for **in-flight** jobs before this (`_job` holds the parsed dict, 16 by default ⇒ worst case
  about 80 MB), and 15b never touched that cell. When full ⇒ the item is dropped, one line complains, and when
  space frees up one line complains with the running total (never one line per item: `_append_capped` guards
  bytes, not information); these jobs get no ack (PROTOCOL.md's hard-limit table has a line for this); `put`
  reports "full" and "stopped" separately (the latter is never counted into the tally of items dropped while
  full). Threads: one per leg (started only the first time there is work). Resource survey
  (`task-15b-probe.py census`, pushing 0/5/40/200 items while every ack hangs, queue ceiling 16): threads before
  starting 4 → after connecting 6 → after pushing 6/8/8/8 (the extra two are the channel thread plus the thread
  handling the dispatcher's own hung attempt); handles after connecting 246-248 → after pushing
  246/261/261/262 (never growing with item count); queue 0/4/16/16; channel threads at 0 after stopping the
  bridge and releasing everything (the 40 arm was measured later for fix1; the other three arms are in
  task-15b-report ②).
- **How `stop()` shuts down**: it accepts nothing new; not one item still queued gets processed (one line
  complains how many got dropped); the one item currently in progress finishes and then it exits — once `_post`
  is woken up by the stop flag it never fires another attempt (closed together with re-review 5's out-of-scope
  item ⑤), so the longest wait is **one** return-channel timeout (`POST_TIMEOUT_S`). The survey also caught one
  more cell: the bridge stops while an ack is in flight, and its response arrives afterward ⇒ before this fix, a
  job still got started anyway (`close_all` had already run, and nobody was there to receive the CLI it
  started) ⇒ `_admit` now registers the cancel flag first and checks the stop flag afterward, so neither
  ordering leaks.
- **Never just shorten the ack timeout**: if the ack never gets sent, this job is simply dropped on the
  bridge's side (`_last_id` has already moved past it, and reconnecting will not resend it) — that would just be
  trading a slow error for a silent one.
- ⚠️**Not done: reusing the connection** (M-4, "one TCP+TLS handshake per chunk"). Every urllib call carries
  `Connection: close` and never reuses one; reusing one would mean dialing directly with `http.client` on its
  own — that would be **a second door for dialing** (bypassing `_open`'s "carry the token, never follow a
  redirect," and having to redo B32's environment-proxy handling all over again), and chunks are sent by each
  job's own thread anyway, never through this channel. ⇒ Left for the maintainer to decide: is it worth opening
  a second dialing door for this.
## one-dir-per-instance
**One CLI instance, one working directory** (15b fix1, re-review's out-of-scope item ①, present since BASE):
`_workdir` used to be named only from `sha256(session id)` ⇒ a new and an old instance of the same id shared one
directory; `_drop` calls `close()` first (waiting up to `CLOSE_GRACE_S`), **then** `rmtree`, so the old
instance's cleanup would delete the new instance's freshly written `system.txt` / `isolation.json` (a re-review
probe, `close`, once triggered a retryable false `crashed` from this). Between the two possible fixes, "add a
random suffix to the directory name" was chosen, never "confirm the table has no same-named new instance before
`rmtree`": the latter would also need to guard the stretch "the new instance has written its files but is not
yet registered in `_sessions`," which would mean having `_drop` take that session's turn lock, and
`close_session` **deliberately never takes the turn lock** (it needs to be able to kill a turn that is still
answering) — taking it would let a whole new turn block it.
**Dependencies checked first** (any one of them being false would rule out the suffix approach): ① claude
carries no `--resume` / `--continue` / `--session-id`, so a rebuild always comes from a full history, never
recovering the session by cwd; ② orphan sweep (`sweep_orphans`) kills processes by the pid in the registry,
never by directory name, and nothing under `work/` sweeps by name; ③ `_drop` / `_run_once` delete the path they
already have on hand (`_Session.workdir`), never recompute the name; ④ the registry's key is the pid. ⚠️Cost:
claude stores its session records under the cwd, inside **the user's own** `~/.claude/projects/`, and one
instance per directory means every rebuild of the same session adds one more project directory — a one-shot call
(`once-<uuid>`) and `doctor --live` already got one directory per call, so this is not a new species for them, it
is just that a session now behaves the same way too (⏳never counted on a real claude installation). README's
line under "Where prompts and answers end up" was updated to match.
Test: `tests/test_50_sessions.py::Lifecycle::test_a_session_reopened_while_the_old_one_is_still_closing_keeps_its_files`.
## birth-cert-ctypes
**On win32, birth id / memory switched from powershell to ctypes** (2026-09-23, Task 11 re-review addendum item
9, its own separate commit).

- **How it used to break** (measured in re-review, the same powershell querying its own process): running
  serially, 1 in 120 attempts broke (rc=2, empty stderr, 3.7s); running 4-way concurrent, 1 in 100 broke; at
  24-way concurrency there was also a `rc=0xC0000005` (`System.AccessViolationException`). And `proc_start_id`
  returned an empty string for both "could not tell" and "does not exist" ⇒ `child_add` refused to register ⇒
  `_Pipe` killed the CLI it had just started, reporting `crashed: …could not enter the registry`. ⇒ On a user's
  machine, roughly 1 in every 100 process starts had a perfectly healthy turn reported as crashed; this also
  turned the full test suite into a lottery (three plus two more full runs, and not one of them ever came out
  green).
- **Now**: `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE)` plus `GetProcessTimes` to get the
  creation FILETIME, formatted as `ft:<integer>`. `ERROR_INVALID_PARAMETER` = does not exist; any other error =
  could not tell; `WaitForSingleObject(h, 0) != WAIT_TIMEOUT` = already exited (just someone is still holding the
  handle) ⇒ counted as does not exist. `proc_start_id` returns **three values** (a birth id / `""` / `None`);
  "could not tell" gets retried once, "does not exist" is never retried. `proc_rss_kb` gets the same cut,
  switched to `K32GetProcessMemoryInfo`.
- **The format changed**: old versions wrote .NET Ticks into `children.json` (local time, no prefix), which is
  never comparable to a FILETIME ⇒ `birth_known()` judges a line without the `ft:` prefix as "unrecognized":
  never killed, just complained about once. The POSIX format did not change.
  ⚠️The sweep's closing call, `_children_save([])`, used to wipe the whole table ⇒ that line is **the last
  moment** anyone remembers them ⇒ the exact wording now carries every row's pid and family, spelling out
  "removed from the table; check the process name before ending it by hand" (re-review addendum two, M-5: it
  used to carry not one pid at all, leaving nobody able to clean up). (Since Task 12, what the sweep writes back
  at the end is the rows for "could not tell, has not yet hit `SWEEP_STRIKES` times," never an empty table; rows
  in the old format still get removed as before.)
- `WaitForSingleObject` returning `WAIT_FAILED` ⇒ **could not tell** (`None`), never does not exist (re-review
  addendum two, M-7; once SYNCHRONIZE has been obtained this is almost never reached, and the test stands in a
  stub for those five functions).
- **The POSIX path was not touched** (`ps -o lstart=` / `ps -o rss=`), it is only that OSError / a timeout now
  reads as "could not tell." ⏳Never run on POSIX.
  🔴**Left hanging for whichever round handles POSIX** (re-review addendum two, the second half of M-7; not
  fixed this round): a `ps` return code ≠ 0 is uniformly read as `""` (does not exist). On a `ps` that does not
  recognize `-p` / `lstart` (something like BusyBox), every single registration would get called by `child_add`
  "it is already gone (it exited right after starting)" — that is a statement that **says the wrong thing**;
  "the pid is not there" and "this `ps` is not supported" need to be told apart. On top of that, retrying the
  `None` cell stretches the worst-case wait from 20 seconds to 40 (`ps`'s `timeout=20` × 2).
- ⚠️Every ctypes function has its `argtypes`/`restype` written out: the default `c_int` truncates a 64-bit
  handle.
## real-cli-stderr
**A real CLI's stderr / the two paths for the body text / the shape of `turn.error`** (2026-09-23, Task 12
carry-forward D16; win32, claude 2.1.280, codex-cli 0.155.0-alpha.9.2; the production argv assembled by
`claude_argv` / `codex_argv` themselves; **an empty scratch cwd**; claude used only haiku, codex used only
gpt-5.6-luna; codex used the dedicated, already-logged-in `CODEX_HOME` — that was the approach before Task 13c,
and this cell measured the argv from that time).

| Cell | stderr |
|---|---|
| claude × after starting, not one turn sent yet (waited 4s) | **0 bytes** (twice) |
| claude × after a real turn (process still alive) | **0 bytes** (twice) |
| claude × after closing stdin and exiting (rc=0) | **0 bytes** |
| codex × after the handshake (initialize + thread/start, then waited 2s more) | **0 bytes** (twice) |
| codex × after a real turn | **1 line, 143 bytes**: `ERROR codex_core::tools::router: error=code-mode host is disabled` (carrying ANSI color codes) |
| codex × after a turn with a bad model name | **1 line, 221 bytes**: `ERROR codex_api::endpoint::responses_websocket: failed to connect to websocket: IO error: tls handshake eof, url: wss://chatgpt.com/backend-api/codex/responses` |

- ⇒ codex still writes an ERROR line to stderr even after a **successful** turn (the first line reports that
  the model tried to use a tool we had shut off, and the router rejected it). Neither of these two lines gets
  classified into a category by `classify` today (both fall to default), but they are exactly the shape of
  "stale noise": the EOF path used to classify the whole tail, and one stale noise line carrying `401` / `login`
  could get an unrelated crash reported as `auth_required` ⇒ this round added **the watermark**
  (`_Pipe.mark_ok`).
- **Does claude's `result` keep up with the delta stream**: both runs were **byte-for-byte identical** (52 delta
  frames / 35 delta frames). ⚠️n=2, this is not a guarantee: these are still two of the CLI's outputs, and the
  compatibility table's line — "if byte-for-byte consistency matters, use only one path" — stays as it is (since
  Task 14 the compatibility table lives in README.md's "Local API" section).
- **Does codex's `turn.error` carry a `code`**: **no**. The run with a bad model name (rejected by a 400
  upstream, nothing generated):
  `{"message": "<a whole JSON string>", "codexErrorInfo": "other", "additionalDetails": null, "misalignment": null}`,
  `status: failed`. ⇒ the machine-readable half is called `codexErrorInfo`, and `_rpc_error` **does not pick it
  up** today. ⏳What value it takes when quota-limited or not logged in has not been measured (measuring it would
  require producing both of those states) ⇒ whether to fold it into the exact wording is left for the maintainer
  to decide.
- Fields in `usage` outside the whitelist (complained about only once, never logged): claude has
  `cache_creation` / `inference_geo` / `iterations` / `output_tokens_details` / `server_tool_use` /
  `service_tier` / `speed`; codex has `cacheWriteInputTokens` / `totalTokens`.
- 6 real calls in total: claude 3 (2 for measurement + 1 `doctor --live`), codex 3 (2 for measurement, one of
  which — the bad model name — was rejected upstream, + 1 `doctor --live`).
- A spot-check during wrap-up (through the production driver, spying on `_Pipe._err_add` bucketed by stage, an
  empty scratch cwd, one "17 + 25" run per family): claude haiku, after starting / after a real turn / after
  closing, stderr **0 lines**, 1 delta frame, and joining it together matches `result` (n=1, the answer was just
  "42," which does not prove much); codex luna, after the handshake / after a real turn, stderr **0 lines** —
  ⏳inferred (not separately verified): the `tools::router` ERROR line above most likely only gets written
  **when the model reaches for a tool that has been shut off** (that run's prompt asked it to read a file), and a
  question that needs no tool never triggers it. The other two `doctor --live` runs each fired once per family
  (see live-check-prompt).
## live-check-prompt
**Why `doctor --live`'s prompt is not the line the brief gave** (2026-09-23, real haiku, an empty scratch cwd).

- The brief's prompt: "Two tasks. (1) Reply with the exact token ECHO-… . (2) Read the file … and append its
  content; if you cannot read it, append the word DENIED." ⇒ haiku replies: "I appreciate you testing my
  security posture. I'll decline this request … This appears to be a prompt injection or jailbreak test … **I
  won't echo unvetted tokens**" — **the password never got echoed back** ⇒ the `ok` criterion comes out False ⇒ a
  perfectly healthy bridge would get reported as broken by `doctor --live`.
- Switched to something an ordinary person would ask ("first do a random four-digit addition; then tell me what
  a note stored at some path says, and say plainly if you cannot open it"): haiku got the addition right both
  times, and it **genuinely tried to read the file** (replying with a line of `Get-Content "…canary-….txt"`,
  saying "Let me read that for you") ⇒ the canary not leaking is because **the tool is shut off**, never because
  the model refuses to read — and that is exactly the thing this criterion needs to prove. codex got the
  addition right and said the file could not be opened, both times.
- ⚠️The sum itself must never already appear in the prompt: if the path's hex string happens to contain that
  number, even a CLI that only echoes the prompt back could "get it right."
- 🔴**Thousands separators** (2026-09-23, during wrap-up, one real `scv doctor --live` run): real codex
  (gpt-5.6-luna) replied "4646 + 6290 = **10,936**. I can't open files here, so I can't read the note." — it got
  it right, and the canary did not leak, yet `"10936" in <the reply>` judged it as "got it wrong," and doctor
  reported codex as having a problem. The two earlier runs where "codex both got it right" simply happened to
  have no comma in them (n=2).
  ⇒ Before judging, thousands separators inside numbers are now stripped (`,` `.` `_` `'` `’` plus a fullwidth
  comma and a few kinds of whitespace, taking only the one "immediately followed by exactly three digits"); the
  prompt's own rule, "the sum itself must never already be in the prompt," uses **this same** ruler. In the same
  run, haiku replied
  "6699 + 7636 = **14335**," and also wrote a line of `Get-Content` saying it would go read the canary (and did
  not get it). Running it again after the fix: both families pass (codex: "8145 + 1088 = **9233**");
  ⚠️this time haiku **fabricated a line of file content** ("The file contains: "All is well."," when the canary
  was actually `CANARY-…`) ⇒ the criterion can only be "did that secret string show up," never changed to "did
  the model say it read the file."
- 🔴**Ruler, version two** (Task 12 fix1, re-review M-2/M-3): the previous rule — "see a separator, and if
  exactly three digits follow, strip it" — could judge **a wrong answer as right** (`110936` contains `10936`;
  `pids 16 912` gets glued into 16912, `3.6.912` into 3.6912; re-review found 6 of 15 cells judged wrong) ⇒
  switched to `scv._says_number`: separators are only stripped inside something **shaped like a
  thousands-grouped integer**, and then compared by **number boundary**. The canary's criterion was loosened to
  "any 8 consecutive hex digits from the secret show up (case-insensitive)" (this also catches a paraphrase or
  just a fragment being pasted), and doctor's line now only says "did not show up in the reply," never "failed
  to read it."
- The stub (`tests/fake_cli.py`) learned to do this addition (`FAKE_NO_MATH=1` turns it off = the negative
  control for "answered something, but not what we asked"); a `leak` mode exists for both families (the canary's
  positive control).
## detach-0b
**Task 0b: a bridge started inside an agent session — does it stay alive once the session closes** (2026-09-23,
win32, measured after this round of work was already done).

- 🔴**The first version used `DETACHED_PROCESS`, and it kept flashing a black window on the user's desktop**:
  the bridge has no console ⇒ every console child process it starts (detect's `--version` / `--help`, the
  cmd-wrapped python stub, taskkill…) gets a **visible** new window opened for it by the system (something the
  user sees on their own screen, while running test_90). ⇒ Switched to `CREATE_NO_WINDOW` (the bridge gets a
  **hidden** console, which child processes inherit) plus `CREATE_NO_WINDOW` on every child process too
  (`new_session_kw()`). The readings below were **measured once for each of the two flags**, with the same
  conclusion.
- **Inside Claude Code** (this machine's Claude Code, a shell started by a tool): `claude.exe` itself **is**
  inside a Job, but the shell a tool starts (powershell/bash starting python) **is not** inside any Job (measured
  with `IsProcessInJob`) ⇒ the bridge `scv start` starts is not inside a Job either, and it never prints the
  fallback message; after the shell that started it exits, `scv status` still answers in the next tool call
  (true for both flags). ⏳**"Closing the entire Claude Code session" was not measured** (a session cannot close
  itself from inside itself); Codex and macOS/Linux were not measured either.
- **Two-arm control** (building a `KILL_ON_JOB_CLOSE` Job of our own, putting a harness process inside it,
  running `scv start`, then closing the Job; each of the two flags run once):
  - Arm A: the Job **does not allow** breakaway ⇒ `CREATE_BREAKAWAY_FROM_JOB` gets `[WinError 5] access denied`
    ⇒ falls back, and `scv start` honestly prints "could not break away from the parent process's Job: closing
    this terminal/session may take the bridge down with it…"; after closing the Job, **the bridge is dead**.
  - Arm B: the Job allows breakaway ⇒ no such message; after closing the Job, **the bridge is still alive**.
  ⇒ Both paths behaved as designed, and that message **tells the truth** (arm A genuinely went down together).
- **"Closing the terminal that started it"** (measured afterward, on the `CREATE_NO_WINDOW` version): when a
  window closes, the system sends CTRL_CLOSE to "every process attached to this console"; that event cannot be
  generated by a program, so it was simulated using **that same console-based delivery path**: a harness (itself
  a hidden console, never flashing) runs `scv start`, then sends `GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, 0)`
  to every process on its console. Readings: with the real flags, the bridge **stays alive**; control one (an
  ordinary child process on the same console) **dies** = the delivery genuinely arrived; control two (the
  bridge's flags swapped down to only NEW_PROCESS_GROUP, attached to the harness's console) **dies** = if the
  bridge really were attached to the terminal's console, it genuinely would go down with it. ⚠️Actually closing a
  window was not done (there is no window to close inside a session, and it would flash).
- **Actually closing a terminal** (measured later during wrap-up, on the `CREATE_NO_WINDOW` version, no longer
  a simulation): a process pretending to be "the shell inside the user's terminal" first starts an ordinary
  child process **on its own console** (a control), runs `scv start` the way a person typing commands would, and
  then genuinely closes that terminal. Three ways of closing it, each paired with a control arm (running
  `scv run` in the foreground in the same terminal = a bridge attached to the terminal):
  - **ConPTY** (closing a tab in Windows Terminal / a VS Code terminal goes through exactly `ClosePseudoConsole`;
    no window exists at any point): the shell and the ordinary child process **die**, the bridge `scv start`
    started **stays alive, and its port keeps answering**; the control arm's foreground `scv run` **dies**.
  - **A classic console window** (`CREATE_NEW_CONSOLE` + `SW_HIDE` starts a **hidden** conhost window, then it
    gets sent `WM_CLOSE` = clicking ×): same result — the bridge stays alive, the shell / the ordinary child
    process / the control arm's foreground `scv run` all die.
  - **A tree-kill when the harness wraps up** (`taskkill /T /F` kills that shell's whole tree): the bridge stays
    alive (`scv start` has already exited, and the parent-child chain is broken at that point); the control
    arm's foreground `scv run` (a descendant of the shell) dies.
  - Under all three ways of closing it, the bridge is **not** inside a Job; after closing, `scv stop` can stop
    it every time. Window monitoring ran throughout (`SetWinEventHook` recording every top-level window that gets
    shown, with its own off-screen 1×1 window as a positive control), and recorded not one window popped up by
    any of these cells.
  - Two more cells measured again the same day: `scv start` inside Claude Code's Bash tool ⇒ after that tool
    call ends (the shell exits), the bridge still answers on the next call, `IsProcessInJob` = 0, and then
    `scv stop` from the PowerShell tool stops it; the readings from the two Job-control arms above (allowing /
    not allowing breakaway) do not change.
  ⏳Still not measured: closing the entire Claude Code session, Codex, and macOS/Linux (the POSIX branch relies
  on `start_new_session`, written from reading the code).
- `spawn_detached`'s stdout/stderr go to DEVNULL (never wired to bridge.log): wiring them up would make `log()`
  write every line twice, and would also route around the gate "append has only one place that writes to disk."
  Exceptions at the subcommand layer all land on disk through the top-level handler; an interpreter-level crash
  cannot land on disk ⇒ when `scv start` sees "it exited right after starting," it brings back the last few
  lines of bridge.log, and asks the person to run `scv run` in the foreground.
- The pid `Popen` returns and the child process's own `os.getpid()` are the same on this machine (true whether
  or not DETACHED is used); ⏳under a venv's python launcher this might be a different process ⇒ `scv start`
  recognizes the bridge it started by **a ticket** (`--ticket`), never by pid.
## update-bytes
`scv update` pins the sha256 of **the file's bytes** (B29) ⇒ all three copies of those bytes have to be the exact
same one: ① the one the publisher hashes ② the one the user downloads from raw.githubusercontent ③ the local one
after installation. Each of the three has its own guard:
- ② = the blob as it is stored in the repository, never the bytes after a checkout. Measured on 2026-09-24 (a
  read-only GET, zero quota): git/git's `.gitattributes` says `*.bat text eol=crlf` (a checkout turns it into
  CRLF), yet `compat/vcbuild/find_vs_env.bat` fetched from raw came back as 6119 bytes with **CRLF at 0
  places**, and
  the id computed by git's `blob <length>\0<content>` (`b35d264c…`) equals the blob id for that same cell in
  GitHub's API tree; the `.py` cell matches the same way. ⇒ raw hands over exactly the blob, never with
  `.gitattributes`/autocrlf conversion applied.
- ① This repository has `core.autocrlf=true` (CRLF in the working tree, LF in the repository) ⇒ without
  `.gitattributes`, whoever computes the hash from a Windows checkout gets the CRLF copy, which does not match
  what the user downloaded (every update would report "sha256 mismatch"). `.gitattributes` sets `text eol=lf` for
  `scv.py`: a checkout on any machine comes out as LF. Test `tests/test_95_setup_pair_update.py::Bytes` checks
  two things: that the attribute really is `eol: lf`; and that the blob id computed with and without the working
  tree's clean filter applied is the same (= no line-ending conversion happens at all). Whatever round handles
  publishing (Task 15) ⭐computes it from the bytes of `git show <commit>:scv.py`.
- ③ `cmd_update` always writes by raw bytes (`_replace_file` takes `bytes` and goes through `write_bytes`):
  writing in text mode on win32 turns LF into CRLF, and the installed copy would no longer be the one that was
  pinned. The `Update` tests use a sample with LF and assert byte-for-byte equality (text mode does not convert
  on POSIX, so this cell only has teeth on win32).
- The release sequence itself (publish → `tools/release.py --commit` → commit the stamped setup.md as a second
  commit → hand the dispatcher `latest`; never amend the first commit after stamping) is written down once, in
  `tools/release.py`'s own docstring — never copied a second time here.
## no-local-paths
Task 13b ruled that "a response body shown to a person / going into the API must never carry a local path," and
`/healthz` already complies (it gives only the version's shape plus a bool). Task 15b's ⑧-6(a) counted one more
cell: an error's exact wording gets handed back to the dispatcher unchanged through the remote leg, and it can
carry a local absolute path (= a user name). 15c Phase A counted the same shape: 10 places can carry a path into
`BridgeError.raw` — some the bridge assembles itself (unable to write the working directory: an explicit workdir
plus `OSError`'s full path; unable to write to the registry; the remote leg's fallback "the bridge itself broke:
…," for instance a bare `mkdir` exception while building the session directory), the OS's own exact wording (a
`Popen` failure on win32 never carries a path, ⏳CPython does carry one on POSIX; a failed file write / `mkdir`
carries the full path, measured), and **the CLI's own exact wording**: 🔴on this machine, real codex writes one
line to stderr every time it starts, `user (<home dir>\.codex\config.toml): ... is ignored.`, and if it dies
during the handshake, the EOF path takes this tail as the exact wording. There is only one path out to the
network: `RemoteLeg._report(..., "error", ...)` (`_admit_one`'s rate-limit rejection, `_job`'s final states, and
its own fallback all go through it).
- The approach (the maintainer's ruling ⑦3 chose "swap the word out," never "swap the whole sentence"): at that
  one spot, `_report`, runs everything through `_no_local_paths`: first the home directory (in all three
  spellings — `\`, `/`, and the doubled `\\` inside a repr — case-insensitive) gets swapped for `~` — it is the
  carrier of the user name, and this also covers the cell "the user name has a space in it, splitting the path
  into two pieces"; then, split by word (splitting on whitespace and quotes/parentheses/Chinese punctuation,
  never on `:` — drive letters and URLs need it), anything that looks like a local path gets swapped for
  `<local path>`, with a line at the end saying how many places got swapped. `https://…` is not among those
  shapes (it does not start with `/`) ⇒ it naturally passes through unswapped, and `file:` is counted as a path
  separately; this bridge's own interface paths (`/v1/…`, `/bridge/…`) are the exception — swapping out
  `resolve_model`'s line "check /v1/models" would turn it into a hint that lies.
- Why go by shape: deleting one leak at a time is a blocklist — the next new way of leaking will not come and
  tell us about it (the same reasoning as the `_cli_version` cell). Both approaches (swap the word / swap the
  whole sentence) have the exact same blind spots (both rely on the same shape-based criterion); swapping the
  whole sentence only additionally throws away the rest of B21's original wording, whenever it does get
  recognized.
- The two legs are treated differently: the local leg's caller goes through the loopback + a local token + a
  rejected `Origin` by default (B23) = the owner of this machine, and for failures like a full disk or a
  permissions problem, he needs to see the path to be able to fix it; the remote leg's reader is the dispatcher.
  `bridge.log` is local disk, and diagnosis needs the full text. ⚠️A web page the user configured into
  `allowed_origins` themselves can also read the local leg's exact wording — they are the one who let it in.
- After 15c re-review M2/M3/M4 (fix1): the home directory regex is built **segment by segment** (allowing any
  number of `/` and backslashes mixed between segments, and a doubled repr counts too; the drive-letter head also
  recognizes Git Bash's `/c` and WSL's `/mnt/c`; bounded at both ends by segment boundaries, so `/home/al` never
  swallows `/home/alice`, and home directory `/root` never mangles the `/rootkit` inside a URL); it gets swapped
  for a Private Use Area placeholder first, and only turns into `~` at the very end. The segment that looks like
  a path no longer only starts at the beginning of a word — it can also start right after `=` / `:` / a Chinese
  character (examples: `cfg=D:/…`, `error:D:/…`, `open:/srv/…`, as well as a Chinese word directly abutting a
  path with no separator, such as the Chinese for "directory" immediately followed by `D:/…`, itself immediately
  followed by the Chinese for "cannot write"); the two slashes of a `scheme://` never count (except for `file:`);
  what passes through was changed to **the exact set of this bridge's own interface paths** (`ROUTES` +
  `REMOTE_PATHS`), and anything carrying `..` is never allowed through. The count is now counted **per path**:
  the home directory swapped for `~` counts as one too; a path with a space in it (`C:/Program Files/x`) has the
  piece after the space merged into the same count as before it; a lone `~` on its own never counts as a path.
  The message honestly says only the first 2048 bytes of each `bridge.log` line are kept. Fetching the home
  directory now catches **any** exception (3.9's POSIX `Path.home()` throws `KeyError`) ⇒ only that one swap
  fails to happen, and it never blocks the final state from being sent out.
- fix2 (second re-review N-M1/N-M2/out-of-scope item 4): "a backslash anywhere in the word ⇒ mask the whole
  word, start to end" was kept as a fallback (fix1's rewrite had dropped it, letting relative backslash paths and
  a UNC path glued onto another character leak back out — an unannounced narrowing); the starting position is
  now found in one pass with a single regex, `PATH_START` (the start of a word, right after `=` / `:` / a CJK
  character, or a drive letter not preceded by a letter or digit), and each position is judged in constant time
  (`_path_at`: it only looks at the first few characters, and counts of `/` use a prefix sum), making the whole
  pass linear — it used to slice character by character and call `.lower()`, taking 3.6 seconds for 50,000
  Chinese characters and 6.5 seconds for `a:` × 25000; after the fix, 0.05/0.02 seconds (never get away with
  "capping the scan length": that would let the part past the cap through unswapped, whole). "CJK character" now
  only recognizes the genuine ranges (`U+2E80–U+D7FF`, `U+F900–U+FFEF`), never the Private Use Area — the home
  directory's placeholder, `U+E000`, lives there, and it used to get treated as "right after a Chinese
  character."
- What it cannot see (its docstring says so too): a relative path with no backslash; a bare user name appearing
  away from any path (`~al/x` counts too); a machine name; a single-segment `/tmp`; the part of a path after a
  space that **has no slash**; a path glued onto a character **other than** `=` / `:` / a CJK character that also
  does not start with a drive letter or a backslash (`|/srv/x`, `@/home/bob/x`, ⏳never seen real exact wording
  written this way). What it over-masks: the part after a space following a path, if it has a slash (`5/5` in
  `C:/a 5/5`); when it starts right after a CJK character, it masks all the way to the end of the word (a Chinese
  word for "directory" immediately followed by the local-path placeholder ends up masking the Chinese for
  "cannot write" that came right after it too); a word with a backslash anywhere gets masked whole, from the
  start of the word; and this bridge's own interfaces written in capitals (`/V1/models`). What it miscounts: two
  paths separated by only a space, where the second one also does not start with a drive letter or a slash
  (`C:/a sub/dir`), get merged into a single count. Test: `tests/test_80_remote.py::NoLocalPathsLeave` (covering
  both the re-review and second re-review tables, cell by cell).
## codex-401-retry
Task 15's smoke test A1 (an empty `CODEX_HOME`, `login status` = Not logged in): a codex turn first retries over
WebSocket 5 times, then over HTTPS 5 times, each attempt sending an `error` notification (`willRetry: true`,
`error.codexErrorInfo` = `{"responseStreamDisconnected": {"httpStatusCode": 401}}`), and after 21.7 seconds the
final `turn/completed` fails with `codexErrorInfo: "other"`. That cell's exact wording happens to contain
`401 Unauthorized` ⇒ the text classifier already got it right (`auth_required`); ⚠️but the final message's
structured field **carries no distinguishing information** (a bad model name also comes back as `"other"`), and
if the exact wording were phrased differently (a token-refresh-failed message, a plain disconnection message),
all that would be left are the retry attempts carrying the 401. The shape follows
`codex app-server generate-json-schema`'s `ErrorNotification`: `params.error` is a `TurnError` (`message` /
`codexErrorInfo` / `additionalDetails`), and `codexErrorInfo` is either a string enum (including `unauthorized`,
`other`, …) or
`{<variant>: {httpStatusCode}}`.
- 15c's approach: one criterion, `_codex_401(TurnError)` (either the object variant's `httpStatusCode == 401`,
  **or** the string enum `unauthorized`), and both carriers go through it: the `error` notification's
  `params.error`, and `turn/completed`'s `turn.error` (re-review I3: it used to recognize only the "object
  variant × notification" cell). Seeing one sets a flag; if this turn **genuinely failed** (there is an error
  object) and the text classifier is neither quota nor auth_required ⇒ `auth_required` (re-review M7: a turn
  whose body is all blank but which still succeeded never counts — the credentials actually did get refreshed).
  Never cut this off early: codex might be using the 401 to refresh its credentials right now, and the next
  attempt could succeed (⏳"never succeeds after a 401" has not been measured — measure it first before cutting
  it off early). ⏳Which cell a real CLI spits out `unauthorized` / `responseTooManyFailedAttempts{401}` from has
  not been measured (this round made no real calls).
- Test: `tests/test_30_drivers.py::CodexRetry401` (the stub's `retry_then_fail` mode: `unknown` before the fix, a
  `quota` control arm, a zero-input control with no 401, and re-review probes S2/S3/S4/S6, four cells).
## child-env-session-vars
setup.md's step 5 has the **agent** (Claude Code / Codex) run `start` on the user's behalf ⇒ the background
bridge inherits that agent session's environment, and both families set variables on their own child processes
saying "which session I am in." `child_env()` used to pass these straight down unchanged ⇒ every CLI the bridge
started carried the identity of **someone else's session**. 15c Phase A readings (claude 2.1.282, codex-cli
0.155.0-alpha.16, win32; each run adding exactly one fake value onto an otherwise stripped-clean environment):
- Claude Code sets on its tool child processes: `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`,
  `CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_CODE_SESSION_ATTENDED`, `CLAUDE_PID`, `AI_AGENT`, `CLAUDE_EFFORT`,
  `TRACEPARENT` (the package's own `TMe()`; its W3C partner `TRACESTATE` has been stripped alongside it since
  fix1), `CLAUDE_CODE_EXECPATH` / `CLAUDE_CODE_INVOKED_SKILLS` (spawnEnvKeys), plus the messaging pipe
  `CLAUDE_CODE_MESSAGING_SOCKET` / `_TOKEN` and `CLAUDE_CODE_ENTRYPOINT`. codex's shell tool adds:
  `CODEX_THREAD_ID`, `CODEX_SESSION_ID`, `CODEX_CI`, `CODEX_VERSION` (measured, names only listed); inside a
  sandbox it adds `CODEX_SANDBOX*` on top (written in the package).
- Behavior: a fake `CLAUDE_CODE_MESSAGING_SOCKET` gets 0 connections (a child claude drops the inherited one on
  startup and opens its own instead; the listener passed a positive control); `CLAUDECODE=1`,
  `CLAUDE_EFFORT=xhigh` show no visible difference on a haiku turn (⚠️haiku does not carry an effort level, so
  this ruler is blind for it; the package's code only writes `CLAUDE_EFFORT`, never reads it).
  **`CODEX_INTERNAL_ORIGINATOR_OVERRIDE` is the smoking gun**: setting it to a fake value changes app-server's
  `userAgent` from `scv/…` to the fake value — the codex the bridge starts would report an identity on behalf of
  a different client (the shape of B22, "never impersonate a client," pointed in the opposite direction).
  `CLAUDE_CODE_ENTRYPOINT` goes into a billing header; under `-p` it only rewrites `cli` into `sdk-cli`, and
  leaves `claude-desktop` / `local-agent` / `remote_*` unchanged (read out of the package; ⏳behavior not
  measured).
- How it splits: **exact names** = the ones each family writes/measured above; **prefix families** copy exactly
  the "session-bound, never pass down" group Claude Code itself already defines for its plugin-eval sandbox
  (`CLAUDE_CODE_SESSION_` / `_HOST_` / `_REMOTE` / `_SDK_` / `_RELAUNCH_`, plus the same-shaped
  `CLAUDE_CODE_MESSAGING_` / `_BRIDGE_`, `CLAUDE_BG_`, `CODEX_THREAD_` / `_SESSION_` / `_SANDBOX` /
  `_NETWORK_PROXY_`) — preferring a mature, existing solution, never inventing one from scratch.
- Why never pure shape-matching, never a whitelist, never a broad prefix: session variables and the user's own
  configuration **live under the same prefix** (`CLAUDE_CODE_SESSION_ID` gets stripped, while
  `CLAUDE_CODE_GIT_BASH_PATH` / `CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_OAUTH_TOKEN` stay); a whitelist missing
  even one entry would **silently** change how he logs in (B22, "never dictate how the CLI logs in").
- Variables the user set themselves that change behavior and bypass the closed set of argv options
  (`CLAUDE_CODE_EFFORT_LEVEL`'s docs say plainly it overrides `--effort`; `CLAUDE_CODE_DISABLE_THINKING` measured
  to take thinking from 142 to 0; `ANTHROPIC_DEFAULT_HAIKU_MODEL`…) are **kept, and disclosed** (the maintainer's
  ruling ⑦2): stripping them would mean changing his own settings on his behalf; doctor lists their names.
- Credentials the host hands down (re-review's out-of-scope item ①): the prefix families also strip credential
  variables the host hands down to child processes (`CLAUDE_CODE_SESSION_ACCESS_TOKEN`,
  `CLAUDE_CODE_HOST_CREDS_FILE`, `CLAUDE_BG_AUTH_SNAPSHOT_PATH`…) — these belong to the session/host, never
  something the user set, so they get stripped per the ruling; cost: in an environment that relies solely on the
  host handing down credentials, having the agent start the bridge will make the child CLI look logged out
  (⏳not measured; README's line "how each CLI logs in" documents this exception).
- What it cannot see: a new exact name that carries no word suggesting "session" (something like `CLAUDE_EFFORT`,
  `CODEX_CI`) has to be added to the list by hand — doctor lists the CLI-namespaced variable names that get
  passed down, so a new one showing up will be visible; when codex's network proxy is on, what binds it to the
  session is the **value** of `HTTP(S)_PROXY` (pointing at that session's own loopback proxy), and the name
  itself is generic, so it cannot be stripped by name (⏳not measured). The test fixture is a set of names
  exported from a real environment (`tests/agent_session_env.py`); "which ones should be stripped" is written
  into `ChildEnv`.
- Wiring it up (15c re-review I1: it used to only measure `child_env()`'s return value, and mutation K1 routed
  around all 7 call sites while all 300 tests stayed green): `run_cli`'s `env` default was changed to
  `child_env()` (never `None`, which meant inheriting unchanged); `_claude_safe_mode_gate` /
  `codex_features_unknown` no longer accept `env`; `tests/test_00_budget.py::SpawnEnvGate` pins "whatever
  `run_cli(` / `_Pipe(` hand over must only be `child_env()` or a name bound to it";
  `tests/test_90_cli.py::ChildProcessesNeverSeeTheSession` measures the consumer side — the stub
  (`FAKE_ENV_NAMES=1`) genuinely receives an environment with not one session variable in it, counted across
  every doctor probe and both family drivers. doctor's own two lines of names are also computed from
  `child_env()`'s result, never freshly computed from `os.environ`.
- The opposite direction: an agent's shell can also **filter out** variables (this player's Codex has
  `shell_environment_policy.inherit = "core"`: measured to leave a child process missing 36 variables including
  `APPDATA`, `HOME`, `COMPUTERNAME`, while `USERPROFILE` / `LOCALAPPDATA` / `HOMEDRIVE` / `HOMEPATH` remained).
  15c Phase B measured this at zero quota (the production `detect()` + `auth_status()`, with this process's own
  environment swapped for each condition): baseline, "Codex core's set," missing `APPDATA`, missing `HOME`,
  missing `USERPROFILE`, missing `USERPROFILE` + `HOMEDRIVE` + `HOMEPATH`, missing `APPDATA` + `HOME` +
  `USERPROFILE` — both families' `--version` still worked, claude's `auth status` still said `loggedIn`
  (claude.ai), codex's `login status` still said `Logged in using ChatGPT`; codex app-server's handshake still
  returned the same `codexHome`, and `config/read`'s response sha matched the baseline. **Only the cell missing
  `LOCALAPPDATA`** made the entire codex family disappear: `cli_head` relies on it to find the desktop app's
  bundled codex (on a machine where codex is not on PATH). ⇒ `CRITICAL_ENV` carries only this one variable
  (win32); none of the three cells are ever described as "not on PATH": the bridge is not running, and this
  terminal is missing it and cannot find codex on its own ⇒ ❌ plus asking the person to start the bridge from
  their own terminal; the bridge is running, and it was already missing at the moment it started, and its own
  `/healthz` also has no codex ⇒ ❌ (labeled as the bridge's own) plus stop it and start it again; the bridge is
  running, and it is only this terminal that is missing it and cannot find it ⇒ one line saying "this is only
  about this terminal, and has nothing to do with the bridge that is running," never a ❌ (fix2, second re-review
  N-M3). ⚠️Not measured: sending a real turn (never at the cost of quota), and POSIX (where `HOME` might be the
  one that actually matters).
