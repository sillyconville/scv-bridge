# scv wire protocol (PROTOCOL = 1)

The bridge knows only the fields written here. It does not know "game," "seat," or "identity" — those belong to
the dispatcher.

> This document's readers are **implementers of the dispatcher (the server side)**, never the bridge's own authors.
> Every "the bridge does …" is a fact about **what the bridge already does**; every "the dispatcher should …" is
> **this protocol's requirement on the other half**.
> Reference implementation: `tests/fake_dispatcher.py` (the bridge's tests run against it).

## Authentication
Except for `/bridge/pair`, every request the bridge sends carries `Authorization: Bearer <token obtained when
pairing>`.
**A given token should only receive jobs that belong to its owner** — this is checked by **the dispatcher** before
dispatching a job; the bridge cannot check it (it does not know whose job it is).

When the bridge **has not paired** (either `remote_url` or `remote_token` is empty), it **sends not a single byte
to the outside network**.

🔴**The dispatcher must never reply with a 3xx**: for the requests that carry a token (`/bridge/hello` /
`/bridge/stream` / `/bridge/result`) the bridge **never follows a redirect** at all; when that request fails (the
result post still retries 3 times as usual, the stream still reconnects with backoff as usual), the log gets one
line, "requests carrying a token never follow redirects," carrying the status code and the `Location`.
Why: when Python's urllib follows a redirect it carries the `Authorization` header over to the new address as-is
(across hosts, and even downgrading from https to plain text).
`/bridge/pair` (which carries no token) only follows a 3xx that does **not** downgrade (https→https); its request
body (the pairing code) would already be dropped on a 301/302/303 anyway, and urllib does not follow a 307/308 POST
⇒ ⭐the dispatcher's pairing address should also not reply with a 3xx — reply with `{"token"}` directly.

`remote_url` **must start with `https://`**, or the bridge does not start the remote leg and logs a line saying so
(a loopback `http://` is the exception, kept for tests). This is enforced by the bridge itself **at the point it
actually dials** ⇒ if the dispatcher's pairing address is not https, the bridge simply, quietly ends up **with
only the local leg**, rather than sending the token in the clear.

## POST /bridge/pair
Request `{"code": "<one-time pairing code>", "bridge_version": "0.1.0"}` → reply `{"token": "…"}`; a wrong code
replies 403.
- `token` must be **1–512 printable ASCII characters, no whitespace** (it lands in `config.json`, then in every
  request's `Authorization` header); any other shape (an empty string, one with a space or newline, non-ASCII, not
  a string, or a reply that is not a JSON object) ⇒ the bridge treats pairing as failed and leaves `config.json`
  untouched.
- The pairing address follows **the same rule** as the remote leg (the "Authentication" section above): anything
  not `https://` (except a loopback `http://`) ⇒ the bridge sends not a single byte, so the pairing code is never
  sent in the clear either.

## POST /bridge/hello
The request body has **exactly** these keys (a whitelist; one extra key is a bug):
`protocol`(int) · `bridge_version` · `os`(`nt`/`posix`+`sys.platform`) · `python` ·
`families`(`[{"family","cli_version"}]`) · `models`(`["claude/haiku", …]`) · `max_concurrent`(int) · `local_port`(int).
Never an e-mail address, organization, user name, host name, or path.
⚠️`cli_version` is only ever the **version-shaped** part (`1.2.3`) or an empty string: what a CLI reports about
itself can carry the executable's full path embedded in it, and the bridge filters it by shape before sending it
out (`scv.py::_cli_version`).

Reply `{"ok": true, "min_supported": "0.1.0", "latest": {"version": "…", "commit": "…", "sha256": "…"}}`.
When the bridge's version is below `min_supported` ⇒ the bridge **does not connect the stream**; it prints "bridge
vX is too old, the server requires ≥ 'Y': update first (the update subcommand; status will print the whole
command)" and stops the remote leg (the local API is unaffected).
`min_supported` must be 1–3 dot-separated numeric segments, each 1–9 digits (`0.1.0`, `1.2`, `3`; the whole string
matches `scv.py::MIN_SUPPORTED_RE`); any other shape ⇒ the bridge does not connect the stream, and that line only
reports how many characters it had (never echoing the original text: that goes into bridge.log and the tokenless
`/healthz`), then it keeps sending hello with the usual backoff (spacing out further each time, capped at
`scv.py::MAX_BACKOFF_S` seconds): once the dispatcher fixes it, the bridge connects on its very next hello and that
line is cleared too (no need to restart the bridge).

The bridge stores `latest`'s three values into `latest.json` for `scv update` to use. Before storing them it keeps
only these three keys, each truncated to 128 characters;
🔴`commit` gets spliced by `scv update` into the download address ⇒ the shape check sits at the point the address
is built: `commit` must be **7–40 lowercase hex digits**, `sha256` must be **64 hex digits** (either letter case),
and a mismatch in either shape ⇒ `scv update` refuses and changes nothing. What `scv update` downloads is
`https://raw.githubusercontent.com/sillyconville/scv-bridge/<commit>/scv.py` (i.e., the bytes committed at that
commit), and it is **those bytes'** sha256 that gets compared ⇒ the `sha256` the dispatcher supplies must be
computed from the bytes of `git show <commit>:scv.py`.

`local_port` is the port of the bridge's local API on its own machine (loopback only; `0` = not known). A dispatcher's
web page can open `http://127.0.0.1:<local_port>/wake` in a small window of its own to wake a sleeping bridge (next
section). Bridges before 0.3.0 do not send it.

## A bridge that sleeps (0.3.0)

A bridge 0.3.0 or newer **starts asleep**: its local API is up, but it sends no `hello` and opens no stream, so the
dispatcher sees it as offline. Two things wake it: `GET http://127.0.0.1:<local_port>/wake` on its own machine (no
token; a page, never JSON — a dispatcher's web page opens it in a small window when the player clicks) and the
`wake` subcommand. Awake, it connects exactly as described below. After `idle_sleep_s` seconds (600 by default;
`0` = never) with no job queued or running, counting from the later of waking and the end of the last job, it closes
its stream and stops dialing; it does not tell the dispatcher first. The one start that is awake by itself is the
first start after `scv pair`. A job pushed at the moment it goes to sleep gets no `ack` ⇒ handle it as "never
delivered": the bridge's next stream after it wakes carries no `Last-Event-ID`. Bridges before 0.3.0 never sleep.

## GET /bridge/stream (SSE)
Every event carries an increasing integer `id:` (**1–20 decimal digits**, with an optional single space after the
colon; the bridge does not adopt any other shape — see the hard-limits table). On reconnect the bridge sends
`Last-Event-ID`, and the dispatcher resends everything after that. A `: keepalive` comment line comes every 15
seconds.

| event | data |
|---|---|
| `job` | `{"job_id", "session"?, "model", "system", "messages": [{"role","content"}…], "opts": {"effort"?, "first_token_timeout"?, "timeout"?}}` — `messages` is **always the full list**, and the last one is the user's |
| `cancel` | `{"job_id"}` |
| `close_session` | `{"session"}` |

- `model` must be a name this bridge reported in `hello.models`; `opts.effort` ∈ `low/medium/high`. Any other value
  ⇒ `error: bad_request`.
- The same `job_id` arriving again ⇒ the bridge does not rerun it, and only replies with one `ack` (`dup: true`).
  ⚠️This has a window — see the "idempotency memory" row in the hard-limits table below.
- The bridge's side that receives the stream **never waits for the result post**: one `job`'s `ack` (and a `dup`
  `ack`, and the `error` that immediately follows a rejection) is handed to a dedicated sending thread, sent one at
  a time in the order the `job`s arrived. ⇒ While `/bridge/result` is slow or stuck, a `cancel` / `close_session`
  pushed afterward is still handled on the spot (an in-flight job is cancelled right then). The queue has a cap;
  hit it and see the "jobs waiting to send an `ack`" row in the hard-limits table.
  - When `cancel` arrives and that job has already arrived and is still queued waiting for its `ack` ⇒ the bridge
    notes this `job_id`; once that job's turn comes and its `ack` goes out, it is `error: cancelled` right then,
    never starting the CLI, never waiting for the `ack`s queued behind it.
    ⚠️A `cancel` for a `job_id` the bridge **has not yet received** does nothing ⇒ if `cancel` is pushed before the
    `job` with the same `job_id`, that job runs as normal.
    When the same `job_id` is queued twice at once (for example, resent because its `ack` never arrived), `cancel`
    still takes effect against whichever copy was accepted later.
  - Which of `close_session` and a `job` for the same `session` comes first is decided **by their order in the
    stream**, never by timestamp — even two sent back-to-back are told apart:
    - `close_session` first, `job` after ("close, then ask") ⇒ that job still runs, rebuilt from the full message
      list; `done`'s `rebuilt` explains why;
    - `job` first, `close_session` after, and that job has not started yet ⇒ its final state is `error: cancelled`
      (never rebuilding a session that has already been closed); one already answering carries on as before: the
      close is graceful (close the CLI's stdin first, wait out a 10-second grace period, then force-kill) ⇒ the
      turn in hand may still finish answering with a final state of `done`; one collected before it finished is
      `cancelled` (never `crashed`). Whether a CLI answers the turn in hand or aborts when its stdin closes is up
      to the CLI itself: the reference implementation's fake CLI finishes answering; the real Claude Code / Codex
      has not been measured. If `close_session` arrives at the moment the CLI is starting (the new session not
      registered yet) ⇒ likewise `cancelled`, and the process that had just started is killed on the spot, never
      left running;
    - Another `close_session` arriving while the previous one is still closing (closing one takes the grace
      period) ⇒ never starts a second close, but the closing moment resets to this new one: a job arriving between
      the two is handled per the previous bullet, and the session it rebuilds is closed along with it.
  - **The `session` id used here lives in its own namespace inside the bridge**, kept apart from a local caller's
    `session` on the local API's `POST /v1/chat/completions` / `POST /v1/sessions/close` (the bridge namespaces it
    before it is ever handed to session management) ⇒ two dispatchers, or a dispatcher and a local client, that
    happen to pick the same string never rebuild, close, evict or reuse each other's session.
- 🔴**A `job_id` only ever gets one sequence, rejections included**: the moment the bridge sends the `ack` with
  `seq=0` for some `job_id`, it records that in its idempotency memory — **even when what follows right after is
  an `error`** (for example `local_rate_limit`). ⇒ Resending the same `job_id` only gets a `dup` ack back, **and
  never runs again**. ⇒ **When the dispatcher retries (including retrying an error with `retryable: true`), it
  must use a new `job_id`.**
  Why it is defined this way: `seq` counts from 0 for each `job_id` (see `POST /bridge/result`); if the same id
  could run a second sequence, that sequence's `ack` / `started` (seq 0/1) would be dropped as a duplicate by a
  dispatcher that deduplicates on `(job_id, seq)`.

### Hard limits on the bridge's side (read each row for "what happens when you hit it"; the rows where a job gets dropped say "no `ack`" ⇒ handle it as "no `ack` means treat it as never delivered")
| Limit | Value | What happens when you hit it |
|---|---|---|
| Length of `session`/`model`/`effort` | 128 characters | `error: bad_request` |
| `job_id` | a string of 1–128 characters, never with a lone surrogate | the bridge **drops** this one and logs a line (never echoing it back); **no `ack`**: with a bad `job_id` there is no way to answer |
| A lone surrogate in any string among the fields the bridge recognizes (`model`/`session`/`system`/`messages`/`opts.effort`) (an unpaired escape such as `\ud800` in the JSON) | never | `error: bad_request` (for the `job_id` cell see the row above; the bridge does not look at fields it does not recognize) |
| `opts.first_token_timeout`/`opts.timeout` | 0 < x ≤ 3600 seconds | `error: bad_request` |
| Number of jobs in flight at once | `max(4, 4 × max_concurrent)` | `error: local_rate_limit` (the message itself says which one was hit) |
| Number of jobs waiting to send an `ack` (queued inside the bridge: `/bridge/result` slow or stuck, or more than this many pushed at once) | 4x the in-flight cap (64 by default) | the bridge **drops** the extra jobs (`cancel` does not take a slot in this queue, never occupies one), logs a line, and logs a line with the new total once a slot frees up; **these jobs get no `ack`**. The same applies to whatever is still queued when the bridge stops |
| Jobs accepted per hour | `remote_jobs_per_hour` (600 by default) | `error: local_rate_limit` |
| Number of resident sessions alive at once | `max(24, max_concurrent + 1)` | the least recently used one is reclaimed; it comes back with `rebuilt` next time |
| A single SSE event | 256 KiB, counted as **the whole event's raw bytes** (the `id:`/`event:`/`data:` lines all count) | the bridge gives up on this one, logs a line, and disconnects to reconnect (skipping it via `Last-Event-ID`, ⚠️see the `id:` section below for the precondition); **this job never gets an `ack`** |
| Idempotency memory ("this `job_id` has been seen") | the most recent **512** `job_id`s, evicted first-in-first-out; **in memory only, cleared on every bridge restart** | a resend outside this window (the 513th new id after that one, sent again, or a resend after the bridge restarts) **is run for real, as a new job** |
| Interval between two dials of `/bridge/stream` | at least 1 second; if it drops in under 1 second after getting a 2xx (or never gets a 2xx at all) with zero events making progress, or it repeatedly gives up on the same oversized event ⇒ **the interval doubles each time, capped at about 60 seconds** (measured intervals 2/3/5/9/17/33/61 seconds: the 1-second minimum interval plus backoff of 1/2/4…/60) | a dispatcher that "closes the stream right after a 200" cannot be turned into hundreds of redials per second; the cost is that once the dispatcher recovers, the bridge may take up to about 61 seconds to come back |
| `/bridge/stream` replies non-2xx / cannot connect (while `hello` is fine) | the same doubling interval, capped at 60 seconds; **a successful `hello` never clears the backoff**. Only a pipe that **actually connected** clears it: got a 2xx, and either received an event or stayed alive for 1 second counting from **the moment it got the 2xx** — **regardless of whether it ended normally or broke abnormally, such as a read timeout or a connection reset**. A slow reply that eventually comes back with a 5xx also counts as never having connected (no matter how long the wait, it still backs off); a slow reply that eventually comes back with 200 and then closes the stream or resets right away also still backs off (the lifetime is never counted from the moment of dialing) | when the stream endpoint alone is broken, a bridge does not fire one `hello` plus one `stream` every second; and when a perfectly healthy pipe is cut by the network, the bridge comes back in about 1 second. The bridge log gets a line on every reconnect, `⚠️ remote leg dropped, reconnecting in Ns: <exception>` (with a status code or exception name in it) |
| `hello`'s response body | 8 MiB | the bridge treats this request as failed and reconnects (the bridge reads not a single byte of `/bridge/result`'s response body — see that section) |
| An event's `id:` | 1–20 decimal digits (20 digits fit any 64-bit unsigned integer; the one space after the colon does not count) | any other shape (empty, whitespace or other characters at either end, more than 20 digits) ⇒ the bridge **does not adopt** this `id`, still carries the last valid one on reconnect, and logs a line (never echoing it back) ⇒ the resend starts over from right after the last valid `id`, and this event is handled as usual |

⚠️The bridge does **not guarantee** that a job is run only once: a job whose connection drops before its `ack`
will be resent, and will genuinely be run again (idempotency only starts counting from the moment the bridge sends
the `ack`), and a resend outside the idempotency window in the table above will also genuinely be run. ⇒ **The
dispatcher should treat `ack` as the source of truth**: without an `ack`, treat it as not delivered; **it should
also deduplicate on its own side by `job_id`** — never bet "runs only once" entirely on the bridge.

### 🔴The dispatcher must put `id:` before `data:`
**Requirement**: in every event, the `id:` line must appear before any `data:` line (`event:` can go anywhere).

**Why**: the bridge counts bytes as it reads an event. Once an event goes over 256 KiB, the bridge **gives up on it
partway through reading it**, sets `Last-Event-ID` to **that event's own `id`**, and reconnects — so the resend
starts **after** it, and it never comes back. But the bridge can only send the `id` it has **already read**: if
`id:` comes after `data:`, at the moment the bridge hits the limit it has not yet seen this event's `id`, and can
only reconnect carrying the **previous** event's `id` ⇒ the dispatcher's resend starts with this same event again
⇒ the bridge gives up on it again ⇒ **every event after this one can never be delivered again** (including
`cancel` and `close_session`), and the whole remote leg is effectively dead.

**The symptom on the bridge's side when this is violated** (usable to reconcile against):
- The bridge's `bridge.log` first gets a line, `⚠️ a remote event exceeded 262144 bytes ⇒ dropping it and reconnecting`, and then, after giving up on the same one **3 times in a row** with `Last-Event-ID` not moving
  forward at all, it complains loudly:
  `⚠️ dropped the same oversized event 3 times in a row with zero progress: the dispatcher put `id:` after `data:` (that way we can never skip past it) ⇒ backing off now (the interval doubles each time, capped at about 60s)`
- What the dispatcher's side sees: this same bridge's `/bridge/stream` connections growing sparser (the interval
  doubling: 2/3/5/9/17…seconds, capped at about 61 seconds), each one carrying the **same** `Last-Event-ID` value
  every time, and every job pushed after that gets not a single `ack`.

**How the dispatcher can self-check** (doing it once before launch is enough): push an event whose `data` is over
256 KiB, immediately followed by a normal job, then check: ① the `Last-Event-ID` the bridge carries on its next
reconnect **equals that oversized event's own `id`**; ② the normal job right after it got an `ack`; ③ the
oversized event itself has **no** `ack` (it was given up on — how to make up for that is the dispatcher's business,
for example splitting it smaller and sending it again under a new `job_id`). The reference implementation's
`push()` in `tests/fake_dispatcher.py` already puts `id:` first; the bridge's test `tests/test_80_remote.py::Pacing`
runs through both orders.

⚠️A `system`-role message mixed into `messages` **does not error**: it gets merged into `system` (the bridge's two
legs share the same intake handling, and that is how the local compatibility table defines it). This is **wider**
than the table above, never narrower — do not rely on it erroring.

## POST /bridge/result
`{"job_id", "seq", "event", …}`; `seq` increases from 0 **within the same `job_id`** (the dispatcher deduplicates /
orders by `(job_id, seq)`).
**The bridge does not read `/bridge/result`'s response body** (it receives not a single byte of it, only looks at
the status code): a 2xx counts as delivered, whatever the response body is and however big. ⚠️The bridge hangs up
the moment it sees the status code, never waiting for the response body ⇒ the dispatcher needs to have recorded
this one **before writing out its status line** (a handler that answers the status first and processes afterward
will see the connection hung up partway through processing).
⚠️**Increasing, but with possible gaps**: when a given result post fails to get through (tried at most 3 times;
the bridge never tries the next attempt once it is stopping; and one whose content cannot be encoded as UTF-8 is
never dialed at all), the `seq` it occupied is **never resent** (the next one keeps counting on from there).
⇒ The dispatcher must never use "`seq` is contiguous" as a test for "nothing was lost"; treat the final state's
`done.text` as the text of record.
⚠️A `job_id` only has **one** sequence (rejections included — see the `GET /bridge/stream` section above) ⇒
**within the idempotency window** (the most recent 512 `job_id`s, and only while the bridge has not restarted —
see the hard-limits table), a second `seq=0` never appears under the same `job_id`; a resend outside the window is
treated as a new job, and only then does a second sequence starting from 0 appear.

| event | other fields | meaning |
|---|---|---|
| `ack` | `dup`(bool) | Received. **Before getting the ack, the dispatcher should consider this job not delivered** |
| `started` | `queued_ms` | The moment it got **a concurrency slot in the whole bridge** (the one `max_concurrent` governs). ⚠️Never "the CLI started answering": see "How to read the four timings" below |
| `chunk` | `text` | Incremental text, batched roughly every 0.3 seconds. ⚠️Once a batch fails to get through, this job **stops sending `chunk`** (the final state still gets sent; `done.text` is the full text) |
| `done` | `text`(the full text) · `usage` · `ttfc` · `latency_s` · `rebuilt`(null or a reason) | Final state |
| `error` | `error: {"message"(the CLI's own words, with local paths replaced: see below the table), "type"/"code"(the class), "retryable", "fix_hint", "family"}` | Final state. Classes: `auth_required` `quota` `timeout` `crashed` `cancelled` `bad_request` `local_rate_limit` `unknown` |

- ⚠️`error.message` is filtered by **shape** before it leaves the machine (`scv.py::_no_local_paths`, the same
  idea as the `cli_version` cell): the home directory becomes `~` (recognized spellings: `/` and backslashes mixed
  any way, doubled backslashes as in a repr, either letter case; a drive letter is also recognized as Git Bash's
  `/c/…` and WSL's `/mnt/c/…`. A `%20`-encoded form, an 8.3 short name, or `/cygdrive/c/…` are never recognized as
  the home directory, but are caught by the next pass as a local path), and anything shaped like a local path
  becomes `<local path>`; when something was replaced, a sentence is appended at the end: "(N local paths in the
  original words were not sent; the original is in bridge.log on this machine, which keeps only the first 2048
  bytes of each line)"; if nothing was replaced, it is the original words, untouched. Only this leg works this way
  (the local API and bridge.log get the original words). ⇒ The dispatcher must never do anything with a path found
  in `message`.
- The `seq` on a `dup: true` reply is **-1** (it does not belong to that job's original sequence — never use it
  for ordering).
- There is **only ever one** final-state event, and the bridge failing to deliver it **never** rewrites a
  successful job into an `error` — it only complains loudly on its own side.
  ⇒ The dispatcher needs to tolerate "a job that only got ack/started/chunk, with no final state" (the other side
  dropped off) and wrap it up by its own timeout.
- `local_rate_limit` has **two sources**, and `message` says clearly which one it is: the hourly job count reaching
  `remote_jobs_per_hour` (the wait needed is on the scale of an hour), or the number of jobs in flight at once
  reaching its cap (the wait needed is a few seconds). Both are `retryable: true`, and **retrying needs a new
  `job_id`**.

### How to read the four timings (⚠️read this section before using them for accounting)
A job passes through the bridge in this order: ① a concurrency slot in the whole bridge (`max_concurrent`) →
② **the turn lock for that `session`** (the same session answers one turn at a time; later ones queue behind the
one before) → ③ (when needed) starting the CLI process / handshaking, or rebuilding from the full message list →
④ sending this question to the CLI and waiting for it to answer.

| Field | Start → end | Includes ① the slot queue | Includes ② the turn-lock wait | Includes ③ starting the process |
|---|---|---|---|---|
| The moment of `started` | The instant ① is obtained | — (it comes right after ①) | Never included: after `started` is sent it may still be waiting on ② | Never included |
| `queued_ms` | Starting to queue for ① → obtaining ① | ✅ included | Never included | Never included |
| `ttfc` | ④ sending this question → the first non-empty piece of text | Never included | Never included | **Differs by family**: never for codex (the handshake finishes synchronously before the question is sent); **included for claude** — the CLI process starts in the background, and the bridge writes this question in and starts the clock as soon as the process has started, so the CLI's startup time falls inside `ttfc` (when the session is new or being rebuilt). When rebuilding from the full message list, the time the CLI spends reading the whole history is **also** included |
| `latency_s` | The bridge receiving this job (right after sending `ack`) → before sending `done` | ✅ included | ✅ included | ✅ included |

⇒ `latency_s − queued_ms/1000 − ttfc` **never equals** "how long generation took": ② and ③ are sandwiched in
between, and when the same session sends back-to-back, ② can be the biggest piece. Measured on this machine (the
fake CLI sleeps 3 seconds at the start of every turn): two jobs on the same session, `max_concurrent=2` ⇒ the one
queued behind the turn lock has `started` at 0.1 seconds, `queued_ms=0`, `ttfc=3.0`, but its first `chunk` comes at
8.3 seconds, `latency_s=8.25` (the extra 4-plus seconds is spent waiting on the other job's turn on the lock);
switch to different sessions with `max_concurrent=1` ⇒ the second job's `started` is at 4.0 seconds,
`queued_ms=3880`.

⚠️**Bucket before using `ttfc` for accounting**: for the claude family, a **new or rebuilt session** (`rebuilt`
non-null, or this session's first turn) has a `ttfc` that includes the CLI process's startup time (the real claude
CLI is node, and it counts in seconds); a resident session's later turns do not include it. Averaging the two
buckets together reads "a new session is slow" as "the model is slow." Measured on this machine (win32, the fake
claude sleeps N seconds before starting, 3 new sessions for each): N=0 ⇒ `ttfc` 0.05/0.05/0.05; N=3 ⇒ `ttfc`
3.06/3.06/3.06 ⇒ **the whole startup time sits inside `ttfc`**. (Before this fix, once the bridge started the
process it first had to start a powershell to look up the process's birth id, about 0.75 seconds, overlapping with
the CLI's startup and masking part of it: at review time this measured N=3 ⇒ `ttfc=2.25`. That step was switched
to a system call and no longer masks it.)
⏳If `ttfc` is to genuinely exclude startup, the bridge would have to wait for claude to emit its initialization
event before sending this question and starting the clock — that is a behavior change, and it has not been made.

## Local API compatibility table (`POST /v1/chat/completions`)
In `README.md`'s "Local API" section (the English, public-facing **copy**; the gate keeping both in step is
`tests/test_97_docs.py::CompatTable`). Never copy another one in here: the table that used to be here, missing
`modalities` and two timeout parameters compared to the code, is exactly what a copied-out table drifting apart
looks like.
