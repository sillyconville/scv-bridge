# -*- coding: utf-8 -*-
"""The local API. ⭐Each of the four guards has one counterexample that shows "without it, this would get through";
the streaming cases pin the shape the real OpenAI client actually consumes.

⭐The tests in the back half pin down responsibilities that belong to this layer alone (outside the brief, a
carry-forward the lead brought over): the one door for error accounting, whether a bad request can be told apart by
which one it is, an expensive reading must never hang off a polled endpoint, what to say when closing a session
mid-flight, presentation-layer trimming.
"""
import argparse
import ast
import collections
import contextlib
import io
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import unittest
from unittest import mock

from tests import helpers
# ⭐The scanner and the ruler that measures "how many lines really got added to disk" must each have only one
#   implementation: never copy a second one in here (this same repository's `_no_backslash` / `gate_missing` exist
#   for exactly this reason).
from tests.test_00_budget import call_sites, string_constants
from tests.test_30_drivers import log_lines_during

import scv  # noqa: E402

_REAL_HEAD = scv.cli_head
B = PORT = TOKEN = None
NL = chr(10)
ORIGIN = "http://127.0.0.1:5173"


def setUpModule():
    global B, PORT, TOKEN
    helpers.fresh_home("local", unittest.addModuleCleanup)
    scv.cli_head = helpers.fake_head
    B, PORT, TOKEN = helpers.start_bridge()


def tearDownModule():
    B.stop()
    scv.cli_head = _REAL_HEAD


def chat(body, **kw):
    body.setdefault("model", "claude/haiku")
    return helpers.http("POST", PORT, "/v1/chat/completions", body=body, token=kw.pop("token", TOKEN), **kw)


def U(t):
    return {"role": "user", "content": t}


def src_text():
    with io.open(scv.__file__, encoding="utf-8") as f:
        return f.read()


def src_tree():
    return ast.parse(src_text())


def rows_since(seen):
    """Lines in `jobs.log` whose job_id is not in `seen` (the batch recorded beforehand) = newly written by this
    stretch. ⭐Never read `tail(1)[0]`: this module shares one bridge and one SCV_HOME, a late-arriving line from the
    previous test case (or the previous subTest in the same case) would get mistaken for this one, and its `klass`
    could very well happen to be exactly the value being asserted (re-review 2's M-3 audit). When someone else's line
    sneaks in, "exactly one line" goes red -- loudly."""
    return [r for r in B.joblog.tail(99) if r["job_id"] not in seen]


def job_ids():
    return {r["job_id"] for r in B.joblog.tail(99)}


def clear_warned():
    """⚠️These `*_warned` variables are all module-level globals => tests contaminate each other: if a previous test
    (even in a different module) already shouted about it, this one measures 0 lines, and that looks exactly like
    "this mechanism took effect".
    Never count on "my test runs first": `unittest` sorts by name, and renaming something changes the order."""
    scv._refuse_warned.clear()
    scv._extra_warned.clear()
    scv._children_warned.clear()


class Guards(unittest.TestCase):
    def setUp(self):
        os.environ["FAKE_MODE"] = "ok"

    def test_token_required(self):
        status, _h, body = chat({"messages": [U("甲")]}, token="")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "bad_token")

    def test_foreign_host_header_is_refused(self):
        """DNS rebinding: the request really did arrive at 127.0.0.1, but the browser thinks it is visiting
        evil.example."""
        status, _h, body = chat({"messages": [U("甲")]}, headers={"Host": "evil.example"})
        self.assertEqual((status, json.loads(body)["error"]["code"]), (403, "forbidden_host"))

    def test_any_origin_header_is_refused(self):
        status, _h, body = chat({"messages": [U("甲")]}, headers={"Origin": "https://evil.example"})
        self.assertEqual((status, json.loads(body)["error"]["code"]), (403, "forbidden_origin"))

    def test_simple_request_content_type_is_refused(self):
        """A POST with text/plain never triggers a CORS preflight -- any web page at all can send one."""
        raw = json.dumps({"model": "claude/haiku", "messages": [U("甲")]}).encode("utf-8")
        status, _h, body = helpers.http("POST", PORT, "/v1/chat/completions", raw=raw, token=TOKEN,
                                        headers={"Content-Type": "text/plain"})
        self.assertEqual((status, json.loads(body)["error"]["code"]), (415, "json_only"))

    def test_a_big_body_with_a_bad_token_still_gets_the_401(self):
        """⚠️Refusing a POST without draining the request body first: the unread data sitting in the kernel makes the
        other side get an RST, and what the caller sees is `WinError 10053`, never the carefully written 401 we
        meant to send.

        🔴The check has to use a big request body: a small body comes out all green under all three ways of writing
        this, so using it as a control tests nothing at all.
        ⚠️And "read a little" and "read nothing" are the same spot (a version that only reads the first 64 KB still
        breaks starting at 200 KB) -- both arms measured in 📎 NOTES.md::refuse-big-body."""
        for pad in (200000, 2000000):
            with self.subTest(pad=pad):
                raw = json.dumps({"model": "claude/haiku", "pad": "x" * pad,
                                  "messages": [U("甲")]}).encode("utf-8")
                status, _h, body = helpers.http("POST", PORT, "/v1/chat/completions", raw=raw,
                                                token="scv-not-the-right-token",   # never stuff non-ASCII in here: an HTTP header is latin-1
                                                headers={"Content-Type": "application/json"})
                self.assertEqual((status, json.loads(body)["error"]["code"]), (401, "bad_token"))

    def test_a_bridge_without_a_token_lets_nobody_in(self):
        """🔴Fail closed: `"Bearer " + str(cfg.get("local_token"))` produces `Bearer None` when the key is missing,
        and `Bearer ` when it is an empty string -- both are guessable passwords, and the token is the one and only
        authentication on this leg.
        ⭐Fire all three at once: no header at all, `Bearer `, `Bearer None`, never try just one (trying only the
          empty one would leave `Bearer None` as a hole just the same)."""
        b, port, _tok = helpers.start_bridge(local_token="")
        try:
            for auth in ("", "Bearer ", "Bearer None"):
                with self.subTest(auth=auth):
                    status, _h, body = helpers.http("POST", port, "/v1/chat/completions",
                                                    body={"model": "claude/haiku", "messages": [U("甲")]},
                                                    headers={"Authorization": auth} if auth else None)
                    self.assertEqual((status, json.loads(body)["error"]["code"]), (401, "bad_token"))
        finally:
            b.stop()

    def test_healthz_needs_no_token_but_still_checks_host(self):
        self.assertEqual(helpers.http("GET", PORT, "/healthz")[0], 200)
        self.assertEqual(helpers.http("GET", PORT, "/healthz", headers={"Host": "evil.example"})[0], 403)

    def test_listens_on_loopback_only(self):
        self.assertEqual(B.httpd.server_address[0], "127.0.0.1")

    def test_a_bracketed_ipv6_loopback_host_is_let_through(self):
        """⚠️`[::1]:8765` is what a legitimate loopback client looks like. Stripping the port with one cut of
        `rsplit(":", 1)` would slice it down to `[::1` => a 403 for someone who did nothing wrong; and a bare `::1`
        (no port) gets sliced by the same cut down to `::` => also a 403.
        ⭐This case pins the false-positive side: never loosen the "recognize only loopback" check itself just to fix
          this."""
        for host in ("[::1]:%d" % PORT, "::1", "localhost:%d" % PORT):
            with self.subTest(host=host):
                status, _h, _b = chat({"messages": [U("甲")]}, headers={"Host": host})
                self.assertEqual(status, 200)


class Compat(unittest.TestCase):
    def setUp(self):
        os.environ["FAKE_MODE"] = "ok"

    def test_models_has_exactly_the_four_fields(self):
        status, _h, body = helpers.http("GET", PORT, "/v1/models", token=TOKEN)
        data = json.loads(body)
        self.assertEqual((status, data["object"]), (200, "list"))
        self.assertEqual(sorted(data["data"][0]), ["created", "id", "object", "owned_by"])
        self.assertEqual([m["id"] for m in data["data"]], B.cat)

    def test_non_stream_shape_and_ignored_params_are_listed(self):
        mark = len(helpers.read_fake_log())
        status, _h, body = chat({"messages": [{"role": "system", "content": "你是甲"}, U("甲")],
                                 "temperature": 0.7, "max_tokens": 50})
        d = json.loads(body)
        self.assertEqual((status, d["object"]), (200, "chat.completion"))
        self.assertEqual(d["choices"][0]["message"], {"role": "assistant", "content": "echo[1]: 甲"})
        self.assertEqual(d["choices"][0]["finish_reason"], "stop")
        self.assertEqual(d["usage"], {"prompt_tokens": 10, "completion_tokens": len("echo[1]: 甲"),
                                      "total_tokens": 10 + len("echo[1]: 甲")})
        self.assertEqual(d["scv"]["ignored"], ["max_tokens", "temperature"])
        boots = [x for x in helpers.fake_log_since(mark) if x.get("family") == "claude"]   # never `[-1]`: see helpers
        self.assertEqual([b["system"] for b in boots], ["你是甲"])

    def test_a_bad_role_is_refused_at_this_layer_not_the_one_below(self):
        """⭐This layer and `SessionManager` each have their own role check (two guards against the same symptom) =>
        asserting only "400" means removing this layer's check turns not one single test red (re-review's audit
        measured: the full 499, all green) -- the two mechanisms cover for each other.
        The check is swapped for "which layer refused it": the entry point's wording and the session layer's wording
        are never the same."""
        status, _h, body = chat({"messages": [{"role": "tool", "content": "x"}]})
        self.assertEqual(status, 400)
        self.assertIn("unsupported role", json.loads(body)["error"]["message"])

    def test_params_that_change_meaning_are_refused_not_swallowed(self):
        for extra in ({"tools": [{"type": "function"}]}, {"n": 2}, {"response_format": {"type": "json_object"}}):
            status, _h, body = chat(dict({"messages": [U("甲")]}, **extra))
            self.assertEqual((status, json.loads(body)["error"]["type"]), (400, "bad_request"), extra)

    def test_image_parts_are_refused_text_parts_are_joined(self):
        """⚠️That one log line also needs checking: `_text_of` has two paths that produce "text only", and the image
        block one is the spot a real client hits most often (the OpenAI SDK's multimodal content blocks). Asserting
        only 400 means removing `messages[i]` on this path turns not one single test red (review measured: 52/52
        all green)."""
        box = {}
        lines = log_lines_during(lambda: box.__setitem__("r", chat(
            {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]})))
        status = box["r"][0]
        self.assertEqual(status, 400)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("messages[0]", lines[0])
        self.assertIn("image_url", lines[0])
        status, _h, body = chat({"messages": [{"role": "user", "content": [{"type": "text", "text": "甲"},
                                                                          {"type": "text", "text": "乙"}]}]})
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "echo[1]: 甲乙")

    def test_session_survives_across_requests(self):
        _s, _h, b1 = chat({"session": "api-1", "messages": [U("甲")]})
        a1 = json.loads(b1)["choices"][0]["message"]["content"]
        _s, _h, b2 = chat({"session": "api-1", "messages": [U("甲"), {"role": "assistant", "content": a1}, U("乙")]})
        d2 = json.loads(b2)
        self.assertEqual((d2["choices"][0]["message"]["content"], d2["scv"]["rebuilt"]), ("echo[2]: 乙", None))
        status, _h, body = helpers.http("POST", PORT, "/v1/sessions/close", body={"session": "api-1"}, token=TOKEN)
        self.assertEqual((status, json.loads(body)), (200, {"closed": True}))


class Stream(unittest.TestCase):
    def setUp(self):
        os.environ["FAKE_MODE"] = "ok"

    def test_stream_shape(self):
        status, headers, body = chat({"stream": True, "messages": [U("甲")]})
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/event-stream"))
        rows = helpers.sse_data(body)
        self.assertEqual(rows[-1], "[DONE]")
        chunks = [json.loads(r) for r in rows[:-1]]
        self.assertEqual(chunks[0]["choices"][0]["delta"], {"role": "assistant"})
        self.assertTrue(all(c["object"] == "chat.completion.chunk" for c in chunks))
        text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
        self.assertEqual(text, "echo[1]: 甲")
        self.assertEqual([c["choices"][0]["finish_reason"] for c in chunks if c["choices"]][-1], "stop")
        tail = [c for c in chunks if not c["choices"]]
        self.assertEqual(tail[-1]["usage"]["prompt_tokens"], 10)

    def test_immediate_failure_is_a_real_http_status_even_when_streaming(self):
        """Borrowed from CLIProxyAPI's "peek at the first chunk first": failing right at the start gives a real
        status code plus JSON, never a 200 stream that just dies partway through."""
        os.environ["FAKE_MODE"] = "auth"
        for stream in (True, False):
            status, _h, body = chat({"stream": stream, "messages": [U("甲")]})
            err = json.loads(body)["error"]
            self.assertEqual(status, 401, stream)
            self.assertEqual((err["type"], err["message"], err["fix_hint"], err["family"]),
                             ("auth_required", "Not logged in · Please run /login", "claude auth login", "claude"))

    def test_client_hanging_up_mid_stream_kills_the_turn(self):
        os.environ["FAKE_MODE"] = "trickle"
        req = json.dumps({"model": "claude/haiku", "stream": True, "messages": [U("甲")]}).encode("utf-8")
        s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
        s.sendall(b"POST /v1/chat/completions HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                  b"Authorization: Bearer " + TOKEN.encode() + b"\r\nContent-Length: " + str(len(req)).encode()
                  + b"\r\n\r\n" + req)
        self.assertIn(b"200", s.recv(4096))
        s.close()
        end = time.time() + 8
        while time.time() < end and B.sessions.snapshot()["children"]:
            time.sleep(0.2)
        self.assertEqual(B.sessions.snapshot()["children"], [], "the client hung up, and the CLI is still running")


def local_path_bits(text):
    """Local-machine-path fragments recognizable inside `text` (sorted, deduplicated): (1) the start of any Windows
    absolute path (`X:/`, `X:\\`); (2) every segment of the home directory (`USERPROFILE`) split on `/`, `\\` (>= 3
    characters: the username is in there); (3) a few locations this test harness itself knows about on this machine
    (the interpreter, the repository, SCV_HOME, CODEX_HOME), recognizing both kinds of slash. ⭐There is only one
    such check: the gate and its positive control both feed it the same one. ⚠️Blind to: POSIX's `/home/…` (this
    run is win32), and encoded paths."""
    found = set(re.findall("[A-Za-z]:[/" + chr(92) + chr(92) + "]", text))
    home = os.path.expanduser("~")
    found |= {seg for seg in re.split("[/" + chr(92) + chr(92) + "]", home) if len(seg) >= 3 and seg in text}
    for p in (home, sys.executable, helpers.ROOT, os.environ.get("SCV_HOME", ""), os.environ.get("CODEX_HOME", "")):
        found |= {v for v in {p, p.replace(os.sep, "/")} if len(v) >= 3 and v in text}
    return sorted(found)


class HealthzHasNoLocalPaths(unittest.TestCase):
    """13b review M3 (a spot that has existed since Task 12): `/healthz` needs no token ("is it up" is something
    anyone can ask), and `families` used to carry `detect()`'s `blocked` / `version` raw text verbatim -- when codex
    has no credentials, that pasteable login command is itself a local absolute path (with the username in it);
    `blocked`'s other sources (the OS's raw text when a probe blows up, claude's own gate's raw text), and `version`
    (`TimeoutExpired`'s raw text carrying the executable's full path) all have the same problem.
    => Collected at the `health()` layer: `families` only gives `{version: keeping only the version's shape, blocked:
    a boolean}`, and the details (the raw text, the pasteable command) are left to the local doctor."""

    def test_healthz_says_no_local_path_even_when_a_family_is_blocked_by_one(self):
        old = os.environ.get("FAKE_MODE")
        os.environ["FAKE_MODE"] = "auth"                       # codex has no credentials => blocked carries a login command with a local path in it
        self.addCleanup(os.environ.__setitem__, "FAKE_MODE", old or "ok")
        b, port, _tok = helpers.start_bridge()
        self.addCleanup(b.stop)
        self.assertNotEqual(local_path_bits(b.found["codex"]["blocked"]), [])      # precondition: the bridge really does have one with a path in it (otherwise this runs vacuously)
        exe = sys.executable
        b.found["claude"].update(blocked="探不出这一版 Claude Code 认不认 --safe-mode（[WinError 5] 拒绝访问。: '%s'）" % exe,
                                 version="版本命令没跑起来：Command '['%s', '--version']' timed out after 10 seconds" % exe)
        _s, _h, body = helpers.http("GET", port, "/healthz")
        text = body.decode("utf-8")
        self.assertEqual(local_path_bits(text), [], text)
        fams = json.loads(text)["families"]
        self.assertEqual(fams, {"claude": {"version": "", "blocked": True}, "codex": {"version": "0.0.0", "blocked": True}})

    def test_the_path_judge_is_not_blind(self):
        """Positive control: feeding the old `families` (raw text copied verbatim) into the same check must point out
        a path; zero-input control: today's clean version must never get flagged."""
        cmd = scv.login_cmd("codex", helpers.fake_head("codex"))
        self.assertNotEqual(local_path_bits(json.dumps({"families": {"codex": {"blocked": "请跑：" + NL + cmd}}})), [])
        self.assertNotEqual(local_path_bits(json.dumps({"x": sys.executable})), [])    # also recognizes the escaped kind of backslash found inside JSON
        self.assertEqual(local_path_bits(json.dumps({"families": {"codex": {"version": "0.0.0", "blocked": True}}})), [])


class Health(unittest.TestCase):
    def test_health_facts(self):
        _s, _h, body = helpers.http("GET", PORT, "/healthz")
        d = json.loads(body)
        self.assertEqual((d["version"], d["protocol"]), (scv.VERSION, scv.PROTOCOL))
        for k in ("uptime_s", "sessions", "queued", "running", "children", "models", "remote"):
            self.assertIn(k, d)

    def test_healthz_never_calls_the_expensive_snapshot(self):
        """⚠️`snapshot()`'s cost grows with the registry's row count, never with this manager's own session count:
        before the switch to ctypes, it started one powershell process per pid to ask about RSS, and measured, even a
        brand-new manager with zero sessions still took 2.843s (this same spot is now ~0.2-0.5ms on win32; POSIX
        still fires one ps per pid) => hanging this off an endpoint that gets polled is self-inflicted harm.
        📎 NOTES.md::snapshot-is-expensive

        ⭐The check is "did it get called at all", never "was this call fast": timing drifts with how busy the
          machine is, and when there are no children, `snapshot()` is already 0.000s to begin with => a check
          written as timing would be permanently green on today's machine (a false negative)."""
        spy = mock.Mock(side_effect=AssertionError("/healthz called snapshot()"))
        with mock.patch.object(B.sessions, "snapshot", spy):
            status, _h, body = helpers.http("GET", PORT, "/healthz")
        self.assertEqual((status, spy.call_count), (200, 0))
        self.assertIsInstance(json.loads(body)["children"], int)   # the registry's row count, never each row's RSS


class OneDoor(unittest.TestCase):
    """The one and only exit for an error response.

    🔴This layer is the last one to catch the exception: `bad_request` / `cancelled` thrown by `SessionManager`
      deliberately never go through `_fail()` (writing one line to disk per bad request would turn bridge.log into
      background noise) => making up for "nobody wrote that one to disk" can only happen here. ⭐And it must happen
      in exactly one place: writing one line per except clause would mean missing one is a silent failure, with all
      three tables green.
    ⭐The check borrows that one scanner from `tests/test_00_budget.py`, never copy a second one in here."""

    @staticmethod
    def _want(name):
        return name.split(".")[-1] == "error_body"

    def test_error_body_is_computed_in_exactly_one_place(self):
        """⚠️Task 11 lifted that exit from `Handler._error_payload` (a method belonging to the local leg itself) up to
        the module-level `error_payload()`: the remote leg is the second leg that can fail, and it cannot reach a
        handler's method => left as it was, it would just call `error_body()` once on its own, so every single
        bad_request on that leg would be zero lines on disk (measured: the version that got wired up did exactly
        this, and this gate is what caught it red). ⭐There must be only one exit, shared by both legs."""
        hits = {fn for fn, _ln, _n in call_sites(src_tree(), self._want)}
        self.assertEqual(hits, {"error_payload"},
                         "the error response body may only ever be computed by `error_payload()`: the logic that makes up for the missing log line lives inside it")

    def test_the_refused_shape_is_built_in_exactly_one_place(self):
        """The family of four guards (they are never a `BridgeError`, they go through a different door) must likewise
        have only one exit."""
        hits = {fn for fn, _ln, v in string_constants(src_tree()) if v == "refused"}
        self.assertEqual(hits, {"_refuse"})

    def test_the_scanner_is_not_blind(self):
        """Positive control: feeding the check a synthetic sample must catch it, and the function that was never
        called must never get flagged."""
        probe = ast.parse(NL.join(("def door(e):", "    return error_body(e)",
                                   "def elsewhere(e):", "    return 1")))
        self.assertEqual({fn for fn, _ln, _n in call_sites(probe, self._want)}, {"door"})


class Accounting(unittest.TestCase):
    """Accounting: one failure equals exactly one line in `bridge.log`, never zero lines (both layers thinking the
    other one is recording it), and never two lines either."""

    def setUp(self):
        os.environ["FAKE_MODE"] = "ok"

    def test_a_bad_request_lands_one_line_that_says_which_message_broke(self):
        box = {}
        lines = log_lines_during(lambda: box.__setitem__(
            "r", chat({"messages": [U("甲"), {"role": "user", "content": 7}]})))
        self.assertEqual(box["r"][0], 400)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("bad_request", lines[0])
        self.assertIn("messages[1]", lines[0])     # you can tell which one is bad, never just a line saying "text only"

    def test_a_good_request_lands_nothing(self):
        """Zero-input control: remove the "bad" and measure again. Without this line, the case above cannot be told
        apart from "it writes a line for every single call"."""
        lines = log_lines_during(lambda: chat({"messages": [U("甲")]}))
        self.assertEqual(lines, [])

    def test_a_cli_failure_lands_one_line_not_two(self):
        """🔴The other end: the driver layer's `_fail()` has already written one line to disk => this layer must never
        write a second one. ⭐What this relies on is the explicit `BridgeError.logged` flag, never guessing who
        already wrote to disk based on `klass`."""
        os.environ["FAKE_MODE"] = "auth"
        box = {}
        lines = log_lines_during(lambda: box.__setitem__("r", chat({"messages": [U("甲")]})))
        self.assertEqual(box["r"][0], 401)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("auth_required", lines[0])

    def test_an_error_nobody_logged_yet_does_get_its_line_here(self):
        """Positive control: one where `logged` has not been set yet must have its line made up for right here --
        otherwise the case above's "exactly one line" cannot be told apart from "this door never writes to disk at
        all"."""
        def boom(**_kw):
            raise scv.BridgeError("local_rate_limit", "这座桥自己的限速把它拦下来了")

        with mock.patch.object(B.sessions, "run", side_effect=boom):
            lines = log_lines_during(lambda: chat({"messages": [U("甲")]}))
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("local_rate_limit", lines[0])

    def test_the_audit_row_can_be_matched_to_the_answer_the_caller_got(self):
        """⭐`jobs.log`'s `job_id` records the id we ourselves sent out: the exception in `jobs.log`'s rule "at most
        `LINE_CAP_BYTES // 8` bytes of caller-supplied free text per line" does not exist on this leg (we never
        accept a job_id), and the audit row still matches the response the caller has in hand."""
        _s, _h, body = chat({"messages": [U("甲")]})
        self.assertEqual(B.joblog.tail(1)[0]["job_id"], json.loads(body)["id"])
        self.assertEqual(B.joblog.tail(1)[0]["leg"], "local")

    def test_a_failed_turn_is_audited_too(self):
        os.environ["FAKE_MODE"] = "quota"
        seen = job_ids()
        chat({"messages": [U("甲")]})
        self.assertEqual([r["klass"] for r in rows_since(seen)], ["quota"])

    def test_a_hangup_while_writing_a_mid_stream_error_records_only_one_row(self):
        """M-3: `failed()` already records the true class (here, `crashed`) before the error frame is even
        attempted; if writing that frame then hits a client that is already gone, `done` has to already be True,
        or the `except OSError:` below this loop records a second, misleading `cancelled` row for the very same
        job -- one job, two `jobs.log` rows. Staged with a mocked `sessions.run` (never a real CLI): it streams one
        delta, waits until the client has actually hung up (a hard RST close, never a plain FIN: a FIN alone can
        leave the write that follows still succeeding once, which would make this case flaky), then raises."""
        seen = job_ids()
        released = threading.Event()

        def boom(**kw):
            kw["on_delta"]("bit")
            self.assertTrue(released.wait(10), "the client never finished hanging up")
            raise scv.BridgeError("crashed", "CLI 进程退出（exit=1），stderr 为空", "claude")

        raw = json.dumps({"model": "claude/haiku", "stream": True, "messages": [U("甲")]}).encode("utf-8")
        s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
        s.sendall(b"POST /v1/chat/completions HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                  b"Authorization: Bearer " + TOKEN.encode() + b"\r\nContent-Length: " + str(len(raw)).encode()
                  + b"\r\n\r\n" + raw)
        with mock.patch.object(B.sessions, "run", side_effect=boom):
            buf = b""
            end = time.time() + 10
            while b"bit" not in buf and time.time() < end:
                buf += s.recv(4096)
            self.assertIn(b"bit", buf, "the fixture's own precondition: the first chunk never arrived")
            # SO_LINGER(on, 0 seconds) then close => the kernel sends RST, never a FIN (the same technique
            # tests/fake_dispatcher.py uses for its own "rst" hangup mode).
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("HH" if os.name == "nt" else "ii", 1, 0))
            s.close()
            released.set()
        end = time.time() + 10
        while time.time() < end and not rows_since(seen):
            time.sleep(0.1)
        rows = rows_since(seen)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["klass"], "crashed")


class Echoing(unittest.TestCase):
    """Not a single string coming in from the network may ever be echoed straight back -- this rule is written down
    by `_name()` itself."""

    def setUp(self):
        os.environ["FAKE_MODE"] = "ok"
        clear_warned()

    def test_a_huge_effort_is_refused_without_echoing_it_back(self):
        """🔴`effort` is the third "string coming in from the network" at this layer, and `model` / `session` both go
        through `_name()`, while this one does not -- two lines short. The version that was missing this one,
        measured: the `message` handed back to the caller was 200020 characters (all 200,000 of the Es, not one
        missing), stderr was 200067 characters; by contrast, the `model` case is 27 characters, 0 xs.
        On disk, `_clip` catches it, but the response body and stderr have nothing catching them at either end."""
        for key in ("effort", "reasoning_effort"):
            with self.subTest(key=key):
                box = {}
                lines = log_lines_during(lambda: box.__setitem__("r", chat(
                    {key: "E" * 200000, "messages": [U("甲")]})))
                status, _h, body = box["r"]
                self.assertEqual((status, json.loads(body)["error"]["type"]), (400, "bad_request"))
                self.assertNotIn("E" * 200, body.decode("utf-8"))
                self.assertEqual(len(lines), 1, lines)
                self.assertNotIn("E" * 200, lines[0])

    def test_the_boundary_is_where_a_huge_effort_stops(self):
        """🔴The case above only proves "it was not echoed back", and that is something two mechanisms can each
        satisfy on their own (`_name()`'s length cap, `_closed()`'s truncation) -- measured: tearing out either one
        alone, the case above still does not go red. => The two guardrails each need their own pin.
        This case pins that it is this layer (the entry point) that stops it, never letting it leak through to be
        truncated downstream."""
        _s, _h, body = chat({"effort": "E" * 200000, "messages": [U("甲")]})
        self.assertIn("at most", json.loads(body)["error"]["message"])

    def test_the_closed_set_error_clips_the_value_it_shows(self):
        """The other guardrail pinned on its own: `_closed()` has other callers too (`messages[i]`'s role), and that
        path never goes through `_name()` -- Task 11's remote leg feeds `SessionManager` directly => this cut has to
        stand on its own."""
        with self.assertRaises(scv.BridgeError) as cm:
            scv._closed("E" * 200000, scv.EFFORTS, "effort")
        self.assertLess(len(cm.exception.raw), 100)
        self.assertIn("E", cm.exception.raw)      # ⭐truncate, never delete: what the reader wants is exactly "what did I send"

    def test_a_short_wrong_effort_still_tells_you_what_you_sent(self):
        """⭐The other end: truncate, never delete. What the reader wants is exactly "what did the value I sent look
        like" -- removing the echo entirely would make this error message useless."""
        status, _h, body = chat({"effort": "turbo", "messages": [U("甲")]})
        self.assertEqual(status, 400)
        self.assertIn("turbo", json.loads(body)["error"]["message"])

    def test_a_good_effort_still_goes_through(self):
        """Zero-input control: remove the "bad" and the same path must go through as usual (never block off effort
        entirely). ⚠️Take the boot record within a window: a process another test case started with `low` would
        satisfy "low is in argv" just as well (reading it via `[-1]` would be a false green)."""
        mark = len(helpers.read_fake_log())
        status, _h, body = chat({"effort": "low", "messages": [U("甲")]})
        self.assertEqual((status, json.loads(body)["choices"][0]["message"]["content"]), (200, "echo[1]: 甲"))
        boots = [x for x in helpers.fake_log_since(mark) if x.get("family") == "claude"]
        self.assertEqual(len(boots), 1, boots)
        self.assertIn("low", boots[0]["argv"])          # asserts the correct value: it really did reach the command line

    def test_both_outlets_of_one_log_line_carry_the_same_capped_line(self):
        """🔴`_clip` used to only protect the copy on disk: `log()`'s last line, `print(…, file=sys.stderr)`, went
        through the uncut copy => the same cap was only half implemented, and this holds true for every single log
        line, not just this one spot with effort. ⭐The check is "the two outlets get the same line", never "stderr
        is a bit shorter"."""
        p = scv.spath("bridge.log")
        before = p.read_text(encoding="utf-8") if p.exists() else ""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            scv.log("甲" * 5000)
        disk = p.read_text(encoding="utf-8")[len(before):].splitlines()
        self.assertEqual(len(disk), 1, disk)
        self.assertEqual(err.getvalue().splitlines(), disk)
        self.assertLessEqual(len(disk[0].encode("utf-8")), scv.LINE_CAP_BYTES)
        self.assertIn("truncated", disk[0])


class RefusalLog(unittest.TestCase):
    """The four guards' log line is the one and only record of "who is hitting this bridge with the wrong token",
    and it used to have zero coverage (swap it for `pass`, and not one of the 52 tests goes red)."""

    def setUp(self):
        os.environ["FAKE_MODE"] = "ok"
        clear_warned()

    def test_a_refusal_lands_a_line_that_says_which_guard(self):
        box = {}
        lines = log_lines_during(lambda: box.__setitem__("r", chat({"messages": [U("甲")]}, token="")))
        self.assertEqual(box["r"][0], 401)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("bad_token", lines[0])

    def test_the_same_kind_of_refusal_is_only_shouted_once(self):
        """⭐This half is the decision itself: the most common cause is "the client is misconfigured", the first line
        is a diagnosis, and the two-thousandth line would only squeeze some other error out of the rotation."""
        chat({"messages": [U("甲")]}, token="")
        lines = log_lines_during(lambda: chat({"messages": [U("甲")]}, token=""))
        self.assertEqual(lines, [])

    def test_a_different_guard_still_gets_its_own_line(self):
        """Never make deduplication mean "only shout about the first one": after `bad_token` has already shouted,
        `forbidden_host` must still get its own line (this same repository's `_rotate_warned` records by name for
        exactly this reason, never a single global bool)."""
        chat({"messages": [U("甲")]}, token="")
        lines = log_lines_during(lambda: chat({"messages": [U("甲")]}, headers={"Host": "evil.example"}))
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("forbidden_host", lines[0])


class RateLimitHeaders(unittest.TestCase):
    def setUp(self):
        os.environ["FAKE_MODE"] = "ok"

    def test_our_own_rate_limit_says_how_long_to_wait(self):
        """🔴`local_rate_limit`'s window is one hour => `retryable=True` without saying how long to wait means the
        client retries immediately, gets 429 again, and floods the entire hour. ⭐`Retry-After` is that `True`'s
        necessary companion, never decoration."""
        def boom(**_kw):
            raise scv.BridgeError("local_rate_limit", "这座桥自己的限速把它拦下来了")

        with mock.patch.object(B.sessions, "run", side_effect=boom):
            status, headers, body = chat({"messages": [U("甲")]})
        self.assertEqual((status, headers.get("Retry-After")), (429, str(scv.RATE_WINDOW_S)))
        self.assertTrue(json.loads(body)["error"]["fix_hint"])

    def test_an_upstream_quota_gets_no_made_up_number(self):
        """⭐A negative control, and it is itself the decision: we do not know when the upstream quota comes back =>
        never make up a `Retry-After` (an error message must never lie). The moment it recovers is in the CLI's own
        raw words."""
        os.environ["FAKE_MODE"] = "quota"
        status, headers, body = chat({"messages": [U("甲")]})
        self.assertEqual((status, headers.get("Retry-After")), (429, None))
        self.assertIn("resets", json.loads(body)["error"]["message"])   # the raw text handed up without a single character changed


class Retryability(unittest.TestCase):
    """Half the decision behind `crashed ∈ RETRYABLE`: whether this could really turn into "retrying a machine that
    will never get better, forever".
    ⭐The two cases are written side by side, because the decision is exactly this pair:
      - a whole family cannot be used (not installed / blocked) => it is not in `/v1/models` at all => it goes
        through `resolve_model`'s 400 bad_request (not retryable), and never reaches the driver layer's `crashed`;
      - the CLI died midway through answering => that spot is `crashed`, and it really is retryable (the session
        rebuilds from the full history, measured in `tests/test_50_sessions.py`).
    => The "what gets caught" axis and the "how it gets classified" axis are both settled at this one layer: it
      catches everything (whatever it cannot catch, nobody writes to disk), but once caught, never say all of it is
      retryable -- a bug in the bridge itself is `unknown` (see the `Startup` case)."""

    def setUp(self):
        os.environ["FAKE_MODE"] = "ok"

    def test_a_family_that_is_not_on_the_menu_is_a_bad_request_not_a_retryable_crash(self):
        status, _h, body = chat({"model": "gemini/pro", "messages": [U("甲")]})
        err = json.loads(body)["error"]
        self.assertEqual((status, err["type"], err["retryable"]), (400, "bad_request", False))

    def test_a_cli_that_dies_mid_turn_is_the_one_that_is_retryable(self):
        os.environ["FAKE_MODE"] = "crash"
        status, _h, body = chat({"messages": [U("甲")]})
        err = json.loads(body)["error"]
        self.assertEqual((status, err["type"], err["retryable"]), (502, "crashed", True))
        self.assertIn("Cannot find module", err["message"])   # the CLI's own raw text, the last line is in there too (B21)


class Numbers(unittest.TestCase):
    """Numbers and names taken in from the network: shape constraints live at this layer (this is the first place
    they ever enter the bridge)."""

    def setUp(self):
        os.environ["FAKE_MODE"] = "ok"

    def test_a_non_numeric_timeout_is_a_400_not_a_dead_connection(self):
        """🔴`float(body.get("timeout") or 300)` against `"thirty seconds"` throws `ValueError` -- it is not a
        `BridgeError` => it punches straight through to outside the handler: what the caller sees is a broken
        connection, and not a single character lands on disk."""
        for bad in ("三十秒", [1], {"a": 1}):
            with self.subTest(timeout=bad):
                status, _h, body = chat({"messages": [U("甲")], "timeout": bad})
                self.assertEqual((status, json.loads(body)["error"]["type"]), (400, "bad_request"))

    def test_an_absurd_timeout_is_refused(self):
        status, _h, _b = chat({"messages": [U("甲")], "timeout": 10 ** 9})
        self.assertEqual(status, 400)

    def test_a_lone_surrogate_in_any_string_is_a_400_not_a_crash(self):
        """Task 15b (D23 "count the same shape first", measured the same on both legs): an escape like "backslash u
        d800" in the request body is accepted as-is by `json.loads`, producing a lone surrogate that cannot be
        encoded to UTF-8, which used to blow up on an `encode` call far from the entry point and get blamed on
        something else entirely -- session => 502 `unknown` ("the bridge itself errored out"); system / body => 502
        retryable `crashed` ("could not write the working directory", "stdin is already closed", both lies).
        ⭐The entry point (`normalize_request`, shared by both legs) refuses it in one place => every single case gets
        400 `bad_request`, with the raw text able to say it is a lone surrogate; the remote leg's half of this is in
        tests/test_80_remote.py::EntryGates."""
        s = chr(0xD800)
        for field, over in (("session", {"session": "s" + s}), ("system", {"messages": [{"role": "system", "content": s}, U("甲")]}),
                            ("content", {"messages": [U("甲" + s)]})):
            with self.subTest(field=field):
                body = dict({"model": "claude/haiku", "messages": [U("甲")]}, **over)
                status, _h, out = helpers.http("POST", PORT, "/v1/chat/completions", token=TOKEN, headers={"Content-Type": "application/json"},
                                               raw=json.dumps(body, ensure_ascii=True).encode("ascii"))
                err = json.loads(out)["error"]
                self.assertEqual((status, err["type"], "lone surrogate" in err["message"]), (400, "bad_request", True), err)

    def test_a_sane_timeout_still_works(self):
        """Zero-input control: remove the "bad" -- the same path must go through as usual."""
        status, _h, body = chat({"messages": [U("甲")], "timeout": 30, "first_token_timeout": 20})
        self.assertEqual((status, json.loads(body)["choices"][0]["message"]["content"]), (200, "echo[1]: 甲"))

    def test_a_huge_model_name_is_refused_without_echoing_it_back(self):
        """⚠️`model` goes verbatim into one line of `jobs.log`, into the error's raw text, and then lands in
        `bridge.log` => without a length limit, the other end could use a 40 KB name to write into our audit log. It
        must never be echoed back in the error either."""
        big = "x" * 4096
        box = {}
        lines = log_lines_during(lambda: box.__setitem__("r", chat({"model": big, "messages": [U("甲")]})))
        self.assertEqual(box["r"][0], 400)
        self.assertNotIn("x" * 200, box["r"][2].decode("utf-8"))
        self.assertEqual(len(lines), 1, lines)
        self.assertNotIn("x" * 200, lines[0])

    def test_a_huge_session_id_is_refused(self):
        status, _h, _b = chat({"session": "s" * 4096, "messages": [U("甲")]})
        self.assertEqual(status, 400)


class ExtraModels(unittest.TestCase):
    """A name in `extra_models` that cannot get into the roster shouts once at the entry point.
    ⭐It lives in `load_config()` (once per process), never in `catalog()`: that one might get called once per
      request, and one bad piece of config would get turned into background noise -- a gate turned into background
      noise by its own team is worse than no gate at all."""

    def setUp(self):
        clear_warned()

    def _reload(self, extra):
        cfg = scv.load_config()
        cfg["extra_models"] = extra
        scv.save_config(cfg)
        try:
            return log_lines_during(scv.load_config)
        finally:
            cfg["extra_models"] = {"claude": [], "codex": []}
            scv.save_config(cfg)

    def test_the_same_complaint_is_only_shouted_once(self):
        """🔴The claim "`load_config()` is once per process" is false: measured, calling it three times in a row in
        the same process shouts three times (in the full test suite `load_config` gets called 30 times), and every
        single subcommand in Task 12 calls it once => without deduplication, one bad piece of config still gets
        turned into background noise -- which is precisely the thing this design choice is meant to avoid.
        ⚠️This test's own name already has `once` in it, and that half of the claim used to have no assertion pinning
          it at all."""
        bad = {"claude": ["bad name"]}
        first = self._reload(bad)
        second = self._reload(bad)
        self.assertEqual(len(first), 1, first)
        self.assertEqual(second, [], "the same complaint got shouted a second time")

    def test_a_different_bad_name_in_the_same_class_does_not_shout_again(self):
        """🔴The deduplication key has to be the category, never the whole sentence: with the whole sentence carrying
        the specific name, swapping in a different bad name makes it a "new" message all over again (re-review
        measured: changing the config 40 times in the same process = 40 lines, with the set growing to 40 keys).
        ⭐This same file's other two siblings (`_rotate_warned` by name, `_refuse_warned` by code) are both closed
          sets, and that rule is written down right in the neighboring `_children_once`'s docstring -- never invent
          a third way here.
        ⚠️The case above (reading the same bad config twice in a row) cannot tell these two ways of writing it apart:
          deduplicating by the whole sentence would come out green just the same."""
        self._reload({"claude": ["bad name"]})
        second = self._reload({"claude": ["另一个坏 名字"]})
        self.assertEqual(second, [], "swapping in a different bad name shouted all over again => the deduplication key is the whole sentence, never the category")

    def test_names_that_will_never_show_up_are_shouted_about_once(self):
        lines = self._reload({"claude": ["opus-4-6", "bad name"], "gemini": ["x"], "codex": "不是个列表"})
        text = NL.join(lines)
        self.assertIn("bad name", text)          # the one with the wrong shape (`MODEL_RE` does not accept a space)
        self.assertIn("gemini", text)            # a family this bridge does not recognize
        self.assertIn("codex", text)             # the value is not a list at all
        self.assertNotIn("opus-4-6", text)       # never shout about the ones that work too

    def test_a_clean_config_says_nothing(self):
        """Zero-input control: remove the "bad" and measure again."""
        self.assertEqual(self._reload({"claude": ["opus-4-6"], "codex": []}), [])


class CloseEndpoint(unittest.TestCase):
    """`close_session` never takes turn_lock, and it can drag on -- presenting both of these facts belongs to this
    layer."""

    def setUp(self):
        os.environ["FAKE_MODE"] = "ok"

    def test_closing_a_session_that_was_never_there(self):
        status, _h, body = helpers.http("POST", PORT, "/v1/sessions/close",
                                        body={"session": "没见过的"}, token=TOKEN)
        self.assertEqual((status, json.loads(body)), (200, {"closed": False}))

    def test_asking_again_says_closed_not_never_heard_of_it(self):
        """🔴`close_session()` returns "was it in the table just now" => the first call already popped it, so the
        second call always returns `False` -- and that response is byte-for-byte identical (review measured) to
        "this bridge never had this session at all".
        => After getting 202, the caller has no way at all to find out the outcome. The three states must be kept
        distinguishable."""
        chat({"session": "closeq-1", "messages": [U("甲")]})
        closed = helpers.http("POST", PORT, "/v1/sessions/close", body={"session": "closeq-1"}, token=TOKEN)
        again = helpers.http("POST", PORT, "/v1/sessions/close", body={"session": "closeq-1"}, token=TOKEN)
        never = helpers.http("POST", PORT, "/v1/sessions/close", body={"session": "从来没有过的"}, token=TOKEN)
        self.assertEqual((closed[0], json.loads(closed[2])), (200, {"closed": True}))
        self.assertEqual((again[0], json.loads(again[2])), (200, {"closed": True}), "asking the second time came back as \"never had it\"")
        self.assertEqual((never[0], json.loads(never[2])), (200, {"closed": False}))

    def test_a_close_that_failed_does_not_poison_the_id(self):
        """🔴A single failed close used to poison this id for roughly an hour (`CLOSE_MEMORY_S`):
        ① that branch returned before `note_close()` ever ran => the expiry-sweep path could never be reached at
          all;
        ② what got poisoned was the id, never that session instance => a fresh instance built afterward under the
          same-name id is clean and alive, yet can never be closed, with its CLI process stuck holding on
          indefinitely (at the time `gc_idle()` had zero production callers = no fallback at all; since Task 12 it
          runs on a clock, but still has to sit idle past `SESSION_IDLE_S` before it gets collected);
        ③ that message describes the previous time, while this time nothing even happened -- an error message must
          never lie.
        => The check is that once the failure has passed, it must really try again, never just recite the message
          from an hour ago."""
        def close(sid):
            return helpers.http("POST", PORT, "/v1/sessions/close", body={"session": sid}, token=TOKEN)

        with mock.patch.object(B.sessions, "close_session", side_effect=RuntimeError("盘着火了")):
            first = close("reused-id")
        self.assertEqual(first[0], 502)
        self.assertIn("盘着火了", json.loads(first[2])["error"]["message"])
        again = close("reused-id")           # the failure has passed => this really has to close it this time
        self.assertEqual((again[0], json.loads(again[2])), (200, {"closed": False}))
        chat({"session": "reused-id", "messages": [U("甲")]})   # builds a fresh one under the same id
        self.assertIn("reused-id", B.sessions._sessions)
        third = close("reused-id")
        self.assertEqual((third[0], json.loads(third[2])), (200, {"closed": True}), "the freshly built same-name session could not be closed")
        self.assertNotIn("reused-id", B.sessions._sessions)

    def test_the_memory_outlives_the_longest_possible_turn(self):
        """🔴This table is the one and only check for "closed while in flight => reported as cancelled", and a turn
        can run as long as `MAX_TIMEOUT_S` => a memory shorter than one turn (600s, what I originally wrote) means a
        long turn's memory could already have been trimmed away by the time it gets closed, falling back to "502 the
        bridge crashed, retryable" -- precisely the thing this rewrite exists to fix."""
        self.assertGreaterEqual(scv.CLOSE_MEMORY_S, scv.MAX_TIMEOUT_S + scv.KILL_BUDGET_S)

    def test_an_empty_session_is_a_bad_request(self):
        status, _h, body = helpers.http("POST", PORT, "/v1/sessions/close", body={}, token=TOKEN)
        self.assertEqual((status, json.loads(body)["error"]["type"]), (400, "bad_request"))

    def test_a_close_that_drags_answers_anyway_and_the_turn_reads_as_cancelled(self):
        """🔴Two things pinned together (they are the two ends of the same close):

        ① closing a session that is in flight: `close_session` has to wait out the full `CLOSE_GRACE_S` (10s) before
           tree-killing it => answering synchronously would hang this endpoint for over ten seconds => it answers
           202 as soon as `CLOSE_ANSWER_S` passes instead.
        ② what the in-flight turn sees is a stdout EOF => reporting that as-is would be 502 "the bridge crashed,
           retryable", with an empty fix_hint, plus a line of raw text pointing at a CLI crash that never happened.
           It has to say plainly: a person closed this.

        ⚠️This case is slow (~12s): the CLI never reads stdin during the heartbeat period, so it can only wait out
          that tree-kill."""
        os.environ["FAKE_MODE"] = "heartbeat_only"
        box = {}
        turn = threading.Thread(target=lambda: box.__setitem__("r", chat(
            {"session": "closing-1", "messages": [U("甲")], "first_token_timeout": 25, "timeout": 25})))
        turn.start()
        end = time.time() + 20
        while time.time() < end and "closing-1" not in B.sessions._sessions:
            time.sleep(0.1)          # never gamble on a sleep: wait until it has really come up before closing it
        self.assertIn("closing-1", B.sessions._sessions, "this turn never even started running, everything below is worthless")
        t0 = time.time()
        status, _h, body = helpers.http("POST", PORT, "/v1/sessions/close",
                                        body={"session": "closing-1"}, token=TOKEN)
        answered_in = time.time() - t0
        # ⭐Ask again while it is still dragging on: it still has to be "still closing", never "never had it", and
        #   never open a second call to close this same one either
        mid = helpers.http("POST", PORT, "/v1/sessions/close", body={"session": "closing-1"}, token=TOKEN)
        self.assertEqual((mid[0], json.loads(mid[2])["closing"]), (202, True))
        turn.join(90)
        # ⭐In the end it must be able to answer "it closed" -- it is never enough to only be distinguishable in that
        #   one 202 response and then become indistinguishable afterward
        end = time.time() + 60
        last = mid
        while time.time() < end:
            last = helpers.http("POST", PORT, "/v1/sessions/close", body={"session": "closing-1"}, token=TOKEN)
            if json.loads(last[2]).get("closed"):
                break
            time.sleep(0.5)
        self.assertEqual((last[0], json.loads(last[2])), (200, {"closed": True}), "it finished closing yet could not say \"it closed\"")
        self.assertEqual((status, json.loads(body)["closing"]), (202, True))
        self.assertLess(answered_in, scv.CLOSE_GRACE_S, "the endpoint hung for the entire close: that is exactly the spot this fixes")
        self.assertIn("r", box, "that turn still has not come back")
        st, _h2, b2 = box["r"]
        err = json.loads(b2)["error"]
        self.assertEqual((st, err["type"], err["retryable"]), (499, "cancelled", False))
        # ⭐These two lines guard the half of this that is "the category changed, but the raw text did not": `type`
        #   is already `cancelled`, but if the message still had the CLI's own "process exited" line, the reader
        #   would go chasing a crash that never happened. What is pinned is that the raw text says "it was closed",
        #   plus the CLI's own raw text still sitting in parentheses (B21). Never pin down which path did the
        #   closing: on the remote leg, it is a `close_session` event pushed by the dispatcher that closes it, and
        #   the old hardcoded raw text naming `/v1/sessions/close` was pointing at the wrong place on that leg
        #   (review M-1).
        self.assertIn("this session was closed", err["message"])
        self.assertIn("what the CLI side saw was", err["message"])   # the CLI's own raw text must never be papered over (B21)

    def test_a_crash_that_nobody_asked_for_is_still_a_crash(self):
        """A negative control (never the "remove the rewrite and it goes red" kind, this is the boundary of the check
        itself): when nobody came to close it, the same path must still report `crashed` -- otherwise that rewrite
        would amount to "every crash gets said to be someone closing it"."""
        os.environ["FAKE_MODE"] = "crash"
        status, _h, body = chat({"session": "not-closed-1", "messages": [U("甲")]})
        self.assertEqual((status, json.loads(body)["error"]["type"]), (502, "crashed"))

    def test_the_rewrite_only_touches_the_shape_it_was_built_for(self):
        """⭐The unit-test half: `_closed_midflight` only rewrites `crashed`. Within the same turn, a real timeout or
        a real lack of quota is its own conclusion, and papering over it with "someone came and closed it" would
        just be lying in a different direction."""
        crashed = scv.BridgeError("crashed", "CLI 进程退出（exit=1），stderr 为空", "claude")
        out = scv._closed_midflight(crashed, True)
        self.assertEqual(out.klass, "cancelled")
        self.assertIn("exit=1", out.raw)
        for klass in ("timeout", "quota", "auth_required", "bad_request"):
            with self.subTest(klass=klass):
                e = scv.BridgeError(klass, "原话", "claude")
                self.assertIs(scv._closed_midflight(e, True), e)
        untouched = scv.BridgeError("crashed", "原话", "claude")
        self.assertIs(scv._closed_midflight(untouched, False), untouched)   # nobody closed it => not one character changes


class Origins(unittest.TestCase):
    """`allowed_origins` must never be a knob that does nothing even once configured: a POST with `application/json`
    always sends a preflight first, and `BaseHTTPRequestHandler` returns 501 by default => configuring it would do
    no good at all."""

    def setUp(self):
        os.environ["FAKE_MODE"] = "ok"
        self.b, self.port, self.token = helpers.start_bridge(allowed_origins=[ORIGIN])

    def tearDown(self):
        self.b.stop()

    def test_a_configured_origin_gets_through_preflight_and_back(self):
        status, headers, _b = helpers.http("OPTIONS", self.port, "/v1/chat/completions",
                                           headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"})
        self.assertEqual((status, headers.get("Access-Control-Allow-Origin")), (200, ORIGIN))
        status, headers, body = helpers.http("POST", self.port, "/v1/chat/completions", token=self.token,
                                             body={"model": "claude/haiku", "messages": [U("甲")]},
                                             headers={"Origin": ORIGIN})
        self.assertEqual((status, headers.get("Access-Control-Allow-Origin")), (200, ORIGIN))
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "echo[1]: 甲")

    def test_an_unconfigured_origin_is_still_refused_at_the_preflight(self):
        status, headers, body = helpers.http("OPTIONS", self.port, "/v1/chat/completions",
                                             headers={"Origin": "https://evil.example"})
        self.assertEqual((status, json.loads(body)["error"]["code"]), (403, "forbidden_origin"))
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_no_origin_header_means_no_cors_header(self):
        """Never echo it back unconditionally: that would turn this guard into "anyone can come in"."""
        _s, headers, _b = helpers.http("GET", self.port, "/healthz")
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))


class Trim(unittest.TestCase):
    """The two families' `text` trim state was already different to begin with (claude's is the CLI's own `result`,
    already stripped by the driver; codex's is accumulated character by character) => whether to trim is a
    presentation decision made at this layer. 📎 NOTES.md::text-passthrough"""

    def setUp(self):
        os.environ["FAKE_MODE"] = "empty_tail_message"      # the codex family: the answer carries whitespace on both ends

    def tearDown(self):
        os.environ["FAKE_MODE"] = "ok"

    def test_the_answer_handed_back_is_trimmed(self):
        _s, _h, body = chat({"model": "codex/gpt-6-luna", "messages": [U("甲")]})
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "echo[1]: 甲")

    def test_the_stream_still_carries_what_the_cli_actually_said(self):
        """⭐This is the cost of that decision, written down plainly here: the delta passes through verbatim => what
        the codex family's stream pieces together has whitespace on both ends that the non-streaming version does
        not. Never touch the delta just to line the two paths up -- that would erase what the CLI actually said."""
        _s, _h, body = chat({"model": "codex/gpt-6-luna", "stream": True, "messages": [U("甲")]})
        rows = [json.loads(r) for r in helpers.sse_data(body)[:-1]]
        text = "".join(c["choices"][0]["delta"].get("content", "") for c in rows if c["choices"])
        self.assertEqual(text, "  echo[1]: 甲" + NL)


class Hangup(unittest.TestCase):
    """The caller hangs up the call => the concurrency slot must come back (B9 on the local leg)."""

    def setUp(self):
        os.environ["FAKE_MODE"] = "ok"

    def _post_and_hang_up(self, body):
        raw = json.dumps(dict({"model": "claude/haiku"}, **body)).encode("utf-8")
        s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
        s.sendall(b"POST /v1/chat/completions HTTP/1.0\r\nHost: 127.0.0.1\r\n"
                  b"Content-Type: application/json\r\nAuthorization: Bearer " + TOKEN.encode()
                  + b"\r\nContent-Length: " + str(len(raw)).encode() + b"\r\n\r\n" + raw)
        end = time.time() + 20
        while time.time() < end and B.sessions.counts()["running"] == 0:
            time.sleep(0.1)
        self.assertEqual(B.sessions.counts()["running"], 1, "this turn never even started running, everything below is worthless")
        s.close()

    def test_a_non_stream_caller_that_hangs_up_frees_the_slot(self):
        """🔴The cost is never "one extra turn runs", it is a concurrency slot held by someone who has already left,
        for the entire `timeout`: `BoundedSemaphore` leaks it out and never heals itself, and `max_concurrent`
        defaults to only 4.
        Measured (before the fix): at 2s / 5s / 10s after hanging up, `running=1` all three times, all the way until
        the gate finally fires.
        ⭐The check is kept separate from "the gate itself fired": this turn's first-token gate is 25s, the whole
          turn is 25s, so coming back within 8 seconds can only mean a cancellation.

        🔴🔴Both output shapes are each run through once, because this is exactly "a fixture that happens to satisfy
          the contract": with only `heartbeat_only` (the queue sitting empty the whole time), the probe only ever
          runs when `q.get` times out -- while under `trickle` (one delta every 0.2s), `q.get` never times out at
          all, and the probe starves entirely (measured: the slot took 15.41s to come back, versus 2.67s for the
          empty-window case). A real CLI spends most of a turn doing exactly this, dripping text continuously =>
          what gets starved is exactly the spot most common in production."""
        for m in ("heartbeat_only", "trickle"):
            with self.subTest(mode=m):
                os.environ["FAKE_MODE"] = m
                seen = job_ids()
                self._post_and_hang_up({"messages": [U("甲")], "first_token_timeout": 25, "timeout": 25})
                end = time.time() + 8
                while time.time() < end and B.sessions.counts()["running"]:
                    time.sleep(0.2)
                self.assertEqual(B.sessions.counts()["running"], 0, "the caller hung up the call, and the slot is still being held")
                # ⭐The accounting also has to line up: this turn is never a success (never write this as a separate
                #   test case -- that would depend on it running after this one).
                # ⚠️Recognize it by "the lines newly written in this stretch", never read `tail(1)`: the previous
                #   subTest's line is also `cancelled`, and reading it before this turn's account has landed on disk
                #   is a false green (re-review 2's M-3 audit).
                end = time.time() + 8
                while time.time() < end and not rows_since(seen):
                    time.sleep(0.1)
                self.assertEqual([r["klass"] for r in rows_since(seen)], ["cancelled"])


class HealthzDisk(unittest.TestCase):
    def test_a_corrupt_registry_cannot_be_used_to_flood_the_log(self):
        """🔴`/healthz` is the one and only endpoint that needs no token, and it reads `children.json` once on every
        single call => the moment that table breaks, an unauthenticated poll turns into "unauthenticated input
        driving writes to this machine's own disk" (review measured: 5 calls = 5 lines). `LOG_CAP_BYTES` is only
        4 MiB => within a few hours this could rotate away the real diagnostics. Never fix this by adding a cache:
        that would let the reading go stale."""
        clear_warned()
        p = scv.spath("children.json")
        keep = p.read_text(encoding="utf-8") if p.exists() else "[]"
        p.write_text("{这不是 JSON", encoding="utf-8")
        try:
            box = {}
            lines = log_lines_during(lambda: box.__setitem__(
                "codes", [helpers.http("GET", PORT, "/healthz")[0] for _ in range(5)]))
        finally:
            p.write_text(keep, encoding="utf-8")
        self.assertEqual(box["codes"], [200] * 5)      # the table is broken, but it must never take this endpoint down with it
        self.assertEqual(len(lines), 1, lines)         # five calls, only one shout
        self.assertIn("children.json", lines[0])


class Routes(unittest.TestCase):
    def test_preflight_does_not_invent_routes(self):
        """⚠️If `OPTIONS` did not check the path, it would become the one and only method that returns 200 even for a
        path that does not exist -- the real request afterward would still 404 as usual, so this is never a hole,
        but it would leave this routing table disagreeing with itself. ⭐This also pins down that `ROUTES` really is
        that table (missing one entry goes red)."""
        for path in scv.ROUTES:
            with self.subTest(path=path):
                self.assertEqual(helpers.http("OPTIONS", PORT, path)[0], 200)
        status, _h, body = helpers.http("OPTIONS", PORT, "/no-such-path")
        self.assertEqual((status, json.loads(body)["error"]["code"]), (404, "not_found"))


def promised_missing(tree, src):
    """Subcommands that get named but are not really in the dispatch table (sorted). Two sources for "named": (1) the
    first argument of every `self_cmd(…)` call (since 13b, every self-referencing command shown to a person goes
    through this one door; a non-literal one is recorded as `<line N is not a literal>` = red on the spot: what
    cannot be checked is treated as not existing); (2) a backtick immediately followed by `` `scv name` `` in the
    source (docstrings / comments, meant for whoever reads the code, and this must never point at a subcommand that
    does not exist either). "Really in the dispatch table" means `add_parser` recognizes it AND `main` really
    dispatches it (a key of `table`, or the spot where `args.cmd == "name"`) -- never just looking at `add_parser`:
    a name argparse recognizes but nobody handles falls through into `print_help`.
    ⭐The arguments after the door's name are also checked (13b review M10): anything starting with `-` has to be an
      option that subcommand's `add_argument` recognizes (or `-h`), anything else counts as a positional argument,
      and the count must never exceed the positional arguments it has `add_argument`-ed; a non-literal one is
      recorded as `<subcommand> <not a literal>`. One that is not recognized is recorded as `<subcommand> <that
      argument>`.
      ⚠️Only checks "does it recognize this", never "is there enough of it": `self_cmd("pair")` deliberately gives
        only the first half (the setup line spells out what comes after).
    ⭐There is only one such check: the gate and the control "deleting one entry makes it go red" both feed it the
      same one."""
    door, bad = set(), set()
    opts, npos = accepted(tree)
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and getattr(n.func, "id", "") == "self_cmd"):
            continue
        if not (n.args and isinstance(n.args[0], ast.Constant)):
            door.add("<line %d is not a literal>" % n.lineno)
            continue
        sub, rest = n.args[0].value, n.args[1:]
        door.add(sub)
        words = iter([a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None for a in rest])
        seen = 0
        for w in words:
            if w is None:
                bad.add("%s <not a literal>" % sub)
            elif w.startswith("-") and w != "-h" and w not in opts.get(sub, {}):
                bad.add("%s %s" % (sub, w))
            elif w.startswith("-"):
                if opts.get(sub, {}).get(w):
                    next(words, None)                       # an option that takes a value: the next one is that value
            else:
                seen += 1
                if seen > npos.get(sub, 0):
                    bad.add("%s %s" % (sub, w))
    named = door | set(re.findall(r"`scv ([a-z][a-z0-9-]*)`", src))
    return sorted((named - dispatched(tree)) | bad), door


def accepted(tree):
    """What each subcommand's argparse recognizes => `({subcommand: {option: does it take a value}}, {subcommand: positional argument count})`:
    either `.add_argument` chained directly off `add_parser(name)`, or bound to a name
    first (`pr = sub.add_parser("pair")`) and then `pr.add_argument`. An option with `action=` never takes a value
    (`store_true`)."""
    names = {t.id: n.value.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Assign) and len(n.targets) == 1
             and isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.Call)
             and getattr(n.value.func, "attr", "") == "add_parser" and n.value.args
             and isinstance(n.value.args[0], ast.Constant) for t in n.targets}
    opts, npos = collections.defaultdict(dict), collections.Counter()
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "add_argument"):
            continue
        recv = n.func.value
        if isinstance(recv, ast.Call) and getattr(recv.func, "attr", "") == "add_parser" and recv.args:
            sub = recv.args[0].value
        elif isinstance(recv, ast.Name) and recv.id in names:
            sub = names[recv.id]
        else:
            continue
        flags = [a.value for a in n.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if any(f.startswith("-") for f in flags):
            opts[sub].update({f: not any(k.arg == "action" for k in n.keywords) for f in flags if f.startswith("-")})
        else:
            npos[sub] += 1
    return dict(opts), dict(npos)


def dispatched(tree):
    """Subcommand names `add_parser` recognizes AND `main` really dispatches (a key of `table`, or the spot where
    `args.cmd == "name"`)."""
    parsers = {n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "add_parser"
               and n.args and isinstance(n.args[0], ast.Constant)}
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    table = {k.value for n in ast.walk(main) if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "table"
             for k in n.value.keys}
    table |= {c.comparators[0].value for c in ast.walk(main) if isinstance(c, ast.Compare) and ast.unparse(c.left) == "args.cmd"
              and isinstance(c.comparators[0], ast.Constant)}
    return parsers & table


class PromisedCommands(unittest.TestCase):
    def test_every_command_the_messages_name_really_exists(self):
        """🔴An error message must never lie: the 401 message sends people off to run `scv token`, while `main`'s
        dispatch table used to have only `version` in it (`argparse` throws `invalid choice` directly) -- sending
        someone off to run a command that does not exist is worse than giving no advice at all (he would think he
        installed it wrong).
        ⭐This case used to sit in the repository marked `expectedFailure`, and Task 12 turned it into a real pass =
        the evidence that the subcommand landed.
        ⭐13b: every self-referencing command shown to a person switched to going through `self_cmd` (the source no
          longer has that backtick-quoted style at all, tests/test_90_cli.py::SelfCommands pins this down) => the
          source of what gets named switched to the door's arguments; the nature of the check must never turn into a
          vacuous one: the set the door names must be non-empty, and deleting one entry from the dispatch table must
          make it go red (the two controls below).
        ⚠️The check only looks at "does it recognize this name, does it accept it", never at whether that command is
          actually finished (at Task 12, `pair` / `update` were still placeholders)."""
        missing, door = promised_missing(src_tree(), src_text())
        self.assertLessEqual({"start", "stop", "status", "run", "token", "doctor", "pair", "update"}, door)   # the ruler is not blind
        self.assertEqual(missing, [])
        # deleting one entry from the dispatch table / one from `add_parser` => each goes red on the spot, red on
        # that exact name (never just prove "it went red": what has to be proven is that it is this one)
        for cut in ('"stop": cmd_stop, ', '    sub.add_parser("stop", help="stop the bridge running in the background")' + NL):
            with self.subTest(cut=cut.strip()):
                self.assertEqual(src_text().count(cut), 1)
                cut_src = src_text().replace(cut, "")
                self.assertEqual(promised_missing(ast.parse(cut_src), cut_src)[0], ["stop"])
        bad = src_text() + NL + "def probe(sub):" + NL + "    return self_cmd(sub)" + NL      # the door's argument is not a literal => red
        self.assertEqual(promised_missing(ast.parse(bad), bad)[0], ["<line %d is not a literal>" % bad.count(NL)])
        # 13b review M10: the arguments after the door's name are also checked -- that subcommand's argparse really
        # recognizes it (or else what gets pasted out would be `unrecognized arguments`)
        for tail, want in (('self_cmd("status", "--live")', ["status --live"]), ('self_cmd("doctor", "--liv")', ["doctor --liv"]),
                           ('self_cmd("token", "extra")', ["token extra"]), ('self_cmd("doctor", flag)', ["doctor <not a literal>"]),
                           ('self_cmd("update", "abc1234")', ["update abc1234"]),
                           ('self_cmd("update", "--commit", "abc1234")', []),
                           ('self_cmd("pair", "https://x", "--code", "C")', []), ('self_cmd("doctor", "--live")', [])):
            with self.subTest(tail=tail):
                more = src_text() + NL + "def probe(flag):" + NL + "    return " + tail + NL
                self.assertEqual(promised_missing(ast.parse(more), more)[0], want)

    def test_every_command_fix_hint_and_setup_name_really_exists(self):
        """Task 13 (carry-forward 7/15): the source-code case cannot see the real output at runtime => two things are
        scanned against the real output instead:
        (1) every self-referencing command `scv setup` really prints (all going through `self_cmd` since 13b): record
          which subcommands the door got asked about, that its answer appears verbatim in the output (never computed
          but not printed), and that everything named is really in the dispatch table;
        (2) `fix_hint`'s return value: it goes into the API error body, out onto the network, to the remote dispatcher
          => must never name any scv subcommand (a bare one would not even run), must never carry a local path (a
          path has the username in it, and the door's own output does carry one). Since 13c, it only ever gives the
          CLI's own commands (`claude auth login` / `codex login`).
        ⭐"The ruler is not blind": the setup half can pick out four names; the `fix_hint` half feeds it a synthetic
          line to prove a bare occurrence is recognized."""
        seen, real = [], scv.self_cmd

        def spy(*words):
            out = real(*words)
            seen.append((words, out))
            return out

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(scv, "self_cmd", side_effect=spy), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            scv.cmd_setup(argparse.Namespace(live=False))
        named = {w[0] for w, _o in seen}
        self.assertLessEqual({"start", "token", "doctor", "pair"}, named, out.getvalue())
        self.assertEqual([w for w, o in seen if o not in out.getvalue()], [])       # the door's answer got printed out verbatim
        self.assertEqual(sorted(named - dispatched(src_tree())), [])
        cut = src_text().replace('"pair": cmd_pair, ', "")                           # delete one entry from the dispatch table => it goes red on that one
        self.assertEqual((src_text().count('"pair": cmd_pair, '), sorted(named - dispatched(ast.parse(cut)))), (1, ["pair"]))
        bare = re.compile(r"(?<![A-Za-z0-9_.-])scv(?:[.]py)? ([a-z][a-z0-9-]*)")
        self.assertEqual(bare.findall("then run scv codex-login and scv start; not myscv x; python scv.py run"),
                         ["codex-login", "start", "run"])
        hints = {scv.fix_hint(k, f) for k in scv.HTTP_STATUS for f in ("claude", "codex", "")}
        self.assertEqual(sorted(set(bare.findall(NL.join(hints)))), [])
        said = out.getvalue() + err.getvalue()
        for _w, o in seen:                  # the segments the door hands out already carry `…/scv.py <subcommand>`: strip them out before looking for a bare occurrence
            said = said.replace(o, "")
        self.assertEqual(sorted(bare.findall(said)), [])
        for local in (os.path.abspath(scv.__file__), sys.executable, os.path.expanduser("~")):
            for form in {local, local.replace(os.sep, "/")}:
                self.assertEqual([h for h in hints if form in h], [], form)
        # the source-code side (never calling the door from inside `fix_hint`) is pinned by
        # tests/test_90_cli.py::SelfCommands::test_nothing_that_leaves_the_machine_calls_the_door


class Startup(unittest.TestCase):
    def setUp(self):
        os.environ["FAKE_MODE"] = "ok"

    def test_a_second_bridge_cannot_quietly_bind_the_same_port(self):
        """🔴`SO_REUSEADDR`'s semantics on win32 are not the same as POSIX: with both sides on, the second bridge
        really can bind the same port (measured locally on 2026-09-22, five cases, 📎 NOTES.md::windows-reuseaddr)
        => the two bridges each get half the requests, and neither one reports an error. And `allow_reuse_address = 1`
        is exactly stdlib's default value, which is exactly the "both sides on" case.

        ⚠️This case's shape was measured, never imagined: the first version occupied the port with a bare socket --
          that spot gets rejected by the OS (10013) under both ways of writing it, so it could not tell apart whether
          this knob was on or off at all (run separately: not one case went red). Whatever occupies the port has to
          be a bridge too."""
        first = scv.Bridge(scv.load_config())
        port = first.start_local(0)
        second = scv.Bridge(scv.load_config())
        try:
            with self.assertRaises(scv.BridgeError) as cm:
                second.start_local(port)
            self.assertIn(str(port), cm.exception.raw)      # never make the user look at a bare WinError
            self.assertEqual(cm.exception.klass, "crashed")
        finally:
            second.stop()
            first.stop()

    def test_an_unknown_path_is_a_404_not_a_traceback(self):
        for method, path in (("GET", "/"), ("POST", "/v1/completions")):
            with self.subTest(path=path):
                status, _h, body = helpers.http(method, PORT, path, token=TOKEN,
                                                body={"x": 1} if method == "POST" else None)
                self.assertEqual((status, json.loads(body)["error"]["code"]), (404, "not_found"))

    def test_a_bug_inside_the_handler_becomes_a_502_not_a_dead_connection(self):
        """⚠️For an exception that escapes it, `BaseHTTPRequestHandler` only prints a traceback to stderr: not a
        single character lands on disk, and what the caller gets is a broken connection with no status code at all.
        ⭐Classified as `unknown`, never `crashed`: `crashed ∈ RETRYABLE` => that would amount to asking the caller to
        retry a bug that is bound to reproduce, forever."""
        with mock.patch.object(scv, "normalize_request", side_effect=TypeError("桥自己的 bug")):
            box = {}
            lines = log_lines_during(lambda: box.__setitem__("r", chat({"messages": [U("甲")]})))
        status, _h, body = box["r"]
        err = json.loads(body)["error"]
        self.assertEqual((status, err["type"], err["retryable"]), (502, "unknown", False))
        self.assertIn("桥自己的 bug", err["message"])
        self.assertEqual(len(lines), 1, lines)


class DefaultPortTaken(unittest.TestCase):
    """0.2.1 (Plan 2B walkthrough, 2026-09-27): on the maintainer's desktop the default port was held by another
    program; the first `start` quit on the spot and the installing agent had to edit config.json by itself.
    => when the *default* port is taken, the local API moves to the next free port, config.json is updated and the
    log says so; a port the user chose is never moved (their own client may point at it).
    The default is swapped for a port this test holds: tests never touch the real default port (A12)."""

    def _hold(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        return s.getsockname()[1]

    def _bridge(self, port):
        b = scv.Bridge(dict(scv.load_config(), port=port))
        self.addCleanup(b.stop)
        return b

    def test_the_default_port_taken_moves_to_the_next_free_one(self):
        held = self._hold()
        with mock.patch.dict(scv.DEFAULT_CONFIG, {"port": held}):
            port, moved = self._bridge(held).start_local_or_next()
        self.assertEqual(moved, held)
        self.assertTrue(held < port <= held + scv.PORT_TRIES, port)

    def test_a_port_the_user_chose_is_never_moved(self):
        held = self._hold()
        self.assertNotEqual(held, scv.DEFAULT_CONFIG["port"])
        with self.assertRaises(scv.BridgeError):
            self._bridge(held).start_local_or_next()

    def test_the_default_port_free_is_used_as_is(self):
        """Zero-input control: nothing holds it => no move."""
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
        s.close()
        with mock.patch.dict(scv.DEFAULT_CONFIG, {"port": free}):
            port, moved = self._bridge(free).start_local_or_next()
        self.assertEqual((port, moved), (free, None))

    def test_run_writes_the_moved_port_to_config_and_says_so(self):
        """The wiring `cmd_run` uses: the moved port lands in config.json (so `status`, `token`, `doctor` and the
        next `start` all see it) and one log line names both ports."""
        held = self._hold()
        self.addCleanup(scv.save_config, scv.load_config())    # put back exactly what was there (restoring "the default" dialed the real default port, test_99)
        cfg = dict(scv.load_config(), port=held)
        scv.save_config(cfg)
        with mock.patch.dict(scv.DEFAULT_CONFIG, {"port": held}):
            b = scv.Bridge(cfg)
            self.addCleanup(b.stop)
            lines = log_lines_during(lambda: scv.bind_local(b, cfg))
        now = scv.load_config()["port"]
        self.assertNotEqual(now, held)
        self.assertEqual(b.httpd.server_address[1], now)
        self.assertEqual(len([x for x in lines if str(held) in x and str(now) in x and "config.json" in x]), 1, lines)

    def test_start_waits_on_the_port_the_bridge_reports(self):
        """`scv start` read the port from config.json before spawning; after a move the bridge answers elsewhere =>
        `started()` takes the port from bridge.pid (written by the bridge itself) once the ticket matches."""
        b, port, _tok = helpers.start_bridge()
        self.addCleanup(b.stop)
        scv._atomic_write("bridge.pid", json.dumps({"pid": os.getpid(), "born": "x", "port": port, "ticket": "t-1"}))
        self.addCleanup(lambda: scv.spath("bridge.pid").unlink())
        other = self._hold()
        self.assertIsNotNone(scv.started(other, "t-1"))
        self.assertIsNone(scv.started(other, "t-2"), "a different ticket is still refused")


if __name__ == "__main__":
    unittest.main()
