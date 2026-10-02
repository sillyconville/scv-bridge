# -*- coding: utf-8 -*-
"""The remote leg, against the reference dispatcher."""
import collections
import contextlib
import gc
import http.client
import io
import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.request
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from tests import helpers
from tests.fake_dispatcher import Dispatcher
from tests.test_30_drivers import log_lines_during
import scv

_REAL_HEAD = scv.cli_head
_REAL_AGE = scv.STREAM_MAX_AGE_S
# The class name of a read timeout in a "remote leg dropped" line. ⭐ Judge by the interpreter's own name, never
#   hardcode "TimeoutError": `socket.timeout` is only an alias for `TimeoutError` from 3.10 on; on 3.9 it is called
#   `timeout` (the log says `timeout: timed out`), and MIN_PY is 3.9 (supplementary review 3, M-3).
READ_TIMEOUT_NAME = socket.timeout.__name__


def died_of_read_timeout(line):
    """Whether this "remote leg dropped" line is saying a read timeout (never 500, never a failed connection --
    those two are `HTTPError`/`URLError`)."""
    return (": %s: " % READ_TIMEOUT_NAME) in line


def setUpModule():
    helpers.fresh_home("remote", unittest.addModuleCleanup)
    scv.cli_head = helpers.fake_head


def tearDownModule():
    scv.cli_head = _REAL_HEAD
    scv.STREAM_MAX_AGE_S = _REAL_AGE


def job(jid, text, **kw):
    d = {"job_id": jid, "session": kw.pop("session", None), "model": kw.pop("model", "claude/haiku"),
         "system": "SYS", "messages": [{"role": "user", "content": text}], "opts": kw.pop("opts", {})}
    d.update(kw)
    return d


def rsid(sid):
    """I-3: the key a session pushed over the remote leg (a `job`'s `session`, a `close_session`'s `session`)
    actually lives under inside `Bridge`/`SessionManager` -- namespaced before it ever reaches them, so it can
    never collide with a local client's raw `session`. Every white-box check in this file against
    `close_state`/`_sessions`/`_closed_at` for a remote-origin id goes through this one place."""
    return "remote:" + sid


class Case(unittest.TestCase):
    max_age = 0
    cfg = {}

    def setUp(self):
        os.environ["FAKE_MODE"] = "ok"
        scv.STREAM_MAX_AGE_S = self.max_age
        self.d = Dispatcher()
        url = self.d.start()
        self.b, self.port, self.tok = helpers.start_bridge(remote_url=url, remote_token=self.d.token, **self.cfg)

    def tearDown(self):
        self.b.stop()
        self.d.stop()

    def events(self, jid):
        return [r for r in self.d.results if r["job_id"] == jid]

    def terminal(self, jid):
        return lambda: any(r["event"] in ("done", "error") for r in self.events(jid))

    def rows_of(self, jid):
        """The rows in `jobs.log` for this job. ⭐ Identify by `job_id`, never by "the last row": this module shares
        one SCV_HOME, so rows left by earlier cases (including ones that land on disk only after they wind down)
        can satisfy a "there is a row" check on the spot (supplementary review 2, M-3, measured: the whole class
        runs green, a solo run runs red)."""
        return [r for r in self.b.joblog.tail(99) if r["job_id"] == jid]


class Basics(Case):
    def test_hello_is_a_whitelist(self):
        self.assertTrue(self.b.start_remote())
        self.assertTrue(self.d.wait(lambda: self.d.hellos))
        hello = self.d.hellos[0]
        self.assertEqual(sorted(hello), ["bridge_version", "families", "local_port", "max_concurrent", "models", "os", "protocol", "python"])
        self.assertEqual(hello["local_port"], self.port)
        self.assertEqual(hello["models"], self.b.cat)
        blob = json.dumps(hello)
        for leak in ("someone@example.com", "org-1", os.path.expanduser("~")):
            self.assertNotIn(leak, blob)

    def test_job_runs_and_reports_in_order(self):
        self.b.start_remote()
        self.d.push("job", job("j1", "甲"))
        self.assertTrue(self.d.wait(self.terminal("j1")))
        evs = self.events("j1")
        self.assertEqual([e["seq"] for e in evs], list(range(len(evs))))
        kinds = [e["event"] for e in evs]
        self.assertEqual((kinds[0], kinds[1], kinds[-1]), ("ack", "started", "done"))
        self.assertEqual(evs[-1]["text"], "echo[1]: 甲")
        self.assertEqual("".join(e["text"] for e in evs if e["event"] == "chunk"), "echo[1]: 甲")
        self.assertIn("queued_ms", evs[1])
        self.assertTrue(self.d.wait(lambda: self.rows_of("j1")), "jobs.log has no row for j1")
        self.assertEqual([r["leg"] for r in self.rows_of("j1")], ["remote"])

    def test_same_job_id_twice_runs_once(self):
        self.b.start_remote()
        self.d.push("job", job("dup", "甲"))
        self.assertTrue(self.d.wait(self.terminal("dup")))
        turns_before = len([x for x in helpers.read_fake_log() if x.get("turn")])
        self.d.push("job", job("dup", "甲"))
        self.assertTrue(self.d.wait(lambda: any(e.get("dup") for e in self.events("dup"))))
        # ⚠️ the line below by itself is not enough: the dup ack is visible the moment it goes out (sent on the
        #   outbound channel since 15b, formerly on the stream-reading thread), while "it actually ran again" only
        #   lands in the fake CLI's log once a new CLI process comes up ⇒ counting turns right after getting the ack
        #   makes "blocked" and "acked the dup but reran it anyway" look identical (negative control NC-1b,
        #   measured: the latter runs all green).
        #   ⇒ switch to the event-driven judgment instead: the same job must never see `started` a second time.
        self.assertFalse(self.d.wait(lambda: len([e for e in self.events("dup") if e["event"] == "started"]) > 1,
                                     timeout=6))
        self.assertEqual(len([x for x in helpers.read_fake_log() if x.get("turn")]), turns_before)

    def test_error_carries_class_and_verbatim_text(self):
        os.environ["FAKE_MODE"] = "auth"
        self.b.start_remote()
        self.d.push("job", job("e1", "甲"))
        self.assertTrue(self.d.wait(self.terminal("e1")))
        err = self.events("e1")[-1]["error"]
        self.assertEqual((err["type"], err["message"], err["fix_hint"]),
                         ("auth_required", "Not logged in · Please run /login", "claude auth login"))

    def test_cancel_kills_the_turn(self):
        os.environ["FAKE_MODE"] = "hang"
        self.b.start_remote()
        self.d.push("job", job("c1", "甲"))
        self.assertTrue(self.d.wait(lambda: any(e["event"] == "started" for e in self.events("c1"))))
        self.d.push("cancel", {"job_id": "c1"})
        self.assertTrue(self.d.wait(self.terminal("c1")))
        self.assertEqual(self.events("c1")[-1]["error"]["type"], "cancelled")
        self.assertTrue(self.d.wait(lambda: self.b.sessions.snapshot()["children"] == []))

    def test_close_session(self):
        self.b.start_remote()
        self.d.push("job", job("s1", "甲", session="room-1"))
        self.assertTrue(self.d.wait(self.terminal("s1")))
        self.assertEqual(self.b.sessions.snapshot()["sessions"], 1)
        self.d.push("close_session", {"session": "room-1"})
        self.assertTrue(self.d.wait(lambda: self.b.sessions.snapshot()["sessions"] == 0))

    def test_remote_cannot_name_a_model_the_bridge_never_offered(self):
        self.b.start_remote()
        self.d.push("job", job("m1", "甲", model="claude/haiku & calc"))
        self.assertTrue(self.d.wait(self.terminal("m1")))
        self.assertEqual(self.events("m1")[-1]["error"]["type"], "bad_request")


class Reconnect(Case):
    def test_resumes_after_a_dropped_stream_with_last_event_id(self):
        self.b.start_remote()
        first = self.d.push("job", job("r1", "甲"))
        self.assertTrue(self.d.wait(self.terminal("r1")))
        self.d.drop()
        self.d.push("job", job("r2", "乙"))
        self.assertTrue(self.d.wait(self.terminal("r2"), timeout=15))
        self.assertGreaterEqual(self.d.connects, 2)
        self.assertEqual(self.d.stream_headers[-1].get("last-event-id"), str(first))

    def test_an_id_that_cannot_go_back_as_a_header_is_not_adopted(self):
        """15b fix2 O1 (outside review scope, same shape as D23/I3) + fix3 (review 2, item 6, O-c): `id:` must go
        verbatim into the `Last-Event-ID` header on reconnect ⇒ one carrying a CJK character or an embedded CR
        (http.client cannot encode it), or a 70,000-digit number (the peer replies 431), used to mean the bridge
        could never reconnect to the stream again, with the log only saying "remote leg dropped"; ones with U+0085 or
        U+001F at the ends used to have them stripped by `.strip()` and the rest adopted anyway. Now the entry point
        shapes it by PROTOCOL's "1-20 decimal digits". ⭐ Judgment: it reconnects anyway after dropping (this comes
        first), it carries the last good id (the correct value), and that line is loud. One cell per shape, four
        shapes.
        ⭐ Before dropping, wait for "the bridge finished handling this event": that line has landed on disk, or
        `_last_id` moved (adopted it -- the shape of the broken behavior) (review 2, N-M4: it used to be sleep 0.5s).
        Never judge by `_order` moving forward: the previous-cell event resent after reconnecting also advances it."""
        self.b.start_remote()
        good = self.d.push("job", job("lid-ok", "甲"))
        self.assertTrue(self.d.wait(self.terminal("lid-ok")))
        leg, p = self.b.remote, scv.spath("bridge.log")
        for name, bad in (("cjk", "中1"), ("cr", "1" + chr(13) + "2"), ("long", "1" * 70000),
                          ("ends", chr(0x85) + "99999" + chr(0x1F))):
            with self.subTest(id=name):
                c0, last0, eid = self.d.connects, leg._last_id, self.d._eid + 1
                start = p.stat().st_size if p.exists() else 0
                said = ("has the wrong shape (PROTOCOL wants 1-%d decimal digits, this one has %d characters)" % (scv.SSE_ID_DIGITS, len(bad))).encode("utf-8")
                lines = log_lines_during(lambda: (
                    self.d.push_raw("id: %s%sevent: ping%sdata: {}%s%s" % (bad, chr(10), chr(10), chr(10), chr(10)), eid),
                    self.assertTrue(self.d.wait(lambda: said in (p.read_bytes() if p.exists() else b"")[start:] or leg._last_id != last0),
                                    "the bridge did not handle this event within 10 seconds"),
                    self.d.drop(),
                    self.assertTrue(self.d.wait(lambda: self.d.connects > c0, timeout=10), "did not reconnect to the stream after dropping")))
                self.assertEqual(self.d.stream_headers[-1].get("last-event-id"), str(good))
                self.assertTrue([x for x in lines if said.decode("utf-8") in x], lines[-3:])


class MaxAge(Case):
    max_age = 1

    def test_stream_is_recycled_on_schedule_and_jobs_still_arrive(self):
        self.b.start_remote()
        self.assertTrue(self.d.wait(lambda: self.d.connects >= 3, timeout=8))
        self.d.push("job", job("a1", "甲"))
        self.assertTrue(self.d.wait(self.terminal("a1")))


class NoMaxAge(Case):
    max_age = 0

    def test_zero_means_never_recycle(self):
        self.b.start_remote()
        self.assertTrue(self.d.wait(lambda: self.d.connects == 1))
        self.assertFalse(self.d.wait(lambda: self.d.connects > 1, timeout=2.5))


class TooOld(Case):
    def test_refuses_loudly_and_never_opens_the_stream(self):
        self.d.hello_reply = {"ok": True, "min_supported": "9.9.9", "latest": {"version": "9.9.9", "commit": "x", "sha256": "y"}}
        self.b.start_remote()
        self.assertTrue(self.d.wait(lambda: self.b.remote.describe()["refused"]))
        said = self.b.remote.describe()["refused"]
        self.assertIn("the update subcommand", said)
        self.assertIn("9.9.9", said)
        # 13b: this line goes into /healthz (readable without a token too) ⇒ never carry a local path (the path has
        #   the username in it); the pasteable whole-line command is printed by `cmd_status`
        #   (tests/test_90_cli.py::StatusSaysTooOld)
        for local in (os.path.abspath(scv.__file__), sys.executable):
            self.assertNotIn(local.replace(os.sep, "/"), said)
            self.assertNotIn(local, said)
        self.assertEqual(self.d.connects, 0)


class MinSupportedShape(Case):
    """Task 14 review 3, I-b: the `min_supported` in the hello reply is the dispatcher's own text. It used to be
    spliced into `self.refused` with a plain `str()` ⇒ it went verbatim into bridge.log, `/healthz` (no token
    needed), and `status`'s stdout (`json.dumps` only escapes C0, C1 goes through as-is) -- the shape the `log()`
    door was guarding against, through a different door.
    ⭐ The shape gate sits at the single entry point that receives the reply (`MIN_SUPPORTED_RE`): a string that is
      not 1-3 dot-separated segments of 1-9 digits each ⇒ only the length is reported, the remote leg does not
      connect; downstream (that log line / `/healthz` / `status`) is entirely covered by it (never patch each exit
      separately).
    ⭐ A malformed shape ⇒ `bad_hello`, never `return`: keep asking on the same backoff, and once the dispatcher
      fixes it the leg connects by itself and `refused` clears (review 4, m-f: it used to stay stopped forever until
      someone restarted it).
    ⚠️ the fixture starts with "99.": with the gate removed, it would still be read as "server requires >= 99" and
      return through the `too_old` path ⇒ the test would be red on "it copied the original text", not on being stuck
      unable to connect to the stream.
    ⚠️ control characters are placed before the run of Qs: a bridge.log line is capped at 2048 bytes, so if they were
      placed after 3,000 Qs, removing the gate would still not let them reach that line (review 4, m-d)."""
    BAD = "99." + chr(0x9b) + "2J" + chr(27) + "[31m" + chr(0x2028) + "X" + "Q" * 3000
    RAW = (chr(0x9b), chr(27), chr(0x2028))

    def ask(self, value):
        """Start a remote leg, hand it a reply with `value`, wait for it to be refused and that line to land on disk
        ⇒ `(leg, refused, the raw bytes of that stretch of bridge.log)`.
        ⭐ Read bridge.log by bytes, never with a measure like `splitlines()`: it eats U+2028 as a line break
        (review 4, m-d). The leg is stopped by tearDown's `Bridge.stop`."""
        self.d.hello_reply = {"ok": True, "min_supported": value, "latest": {}}
        p = scv.spath("bridge.log")
        start = p.stat().st_size if p.exists() else 0
        leg = self.b.remote = scv.RemoteLeg(self.b)
        threading.Thread(target=leg.run, daemon=True).start()

        def landed():
            said = leg.describe()["refused"]
            return bool(said) and p.exists() and ("❌ " + said).encode("utf-8") in p.read_bytes()[start:]
        self.assertTrue(self.d.wait(landed), leg.describe())
        return leg, leg.describe()["refused"], p.read_bytes()[start:]

    def test_a_malformed_min_supported_is_reported_by_its_length_only(self):
        leg, said, logged = self.ask(self.BAD)
        _st, _h, body = helpers.http("GET", self.port, "/healthz")
        cfg = scv.load_config()
        cfg["port"] = self.port
        scv.save_config(cfg)
        self.addCleanup(helpers.pin_quiet_port)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = scv.main(["status"])
        shown = json.loads(out.getvalue())["remote"]
        # ⭐ each arm gets its own subTest: if one arm goes red the others are still measured (the mutation harness
        #   needs to see which arm is red)
        with self.subTest(what="refused"):
            self.assertIn("(%d characters" % len(self.BAD), said)                 # correct value: only the length is reported
            self.assertNotIn("QQ", said)
            self.assertEqual(self.d.connects, 0)
        with self.subTest(what="state"):
            # review 4, m-d: never `too_old` -- for that state `status` points to update on stderr, and upgrading
            #   does not fix a shape error on the dispatcher's side
            self.assertEqual((leg.describe()["state"], shown["state"]), ("bad_hello", "bad_hello"))
            self.assertEqual(sorted(helpers.door_lines_missing(err.getvalue(), "update")), sorted(scv.self_cmd("update").splitlines()))
        with self.subTest(what="downstream"):                                   # downstream all gets the entry point's own line (never each copies its own original text)
            self.assertEqual((rc, json.loads(body)["remote"]["refused"], shown["refused"]), (0, said, said))
        for where, raw in (("bridge.log", logged), ("/healthz", body), ("status", out.getvalue().encode("utf-8"))):
            with self.subTest(where=where):
                self.assertNotIn(b"QQ", raw)
                for c in self.RAW:
                    self.assertNotIn(c.encode("utf-8"), raw)

    def test_a_segment_longer_than_nine_digits_is_malformed_too(self):
        """The ruler caps each segment at 9 digits (maintainer's ruling): `"9"*5000` used to match the shape and then
        make `_ver` raise ⇒ forever backing off, never `too_old`; a shorter one that matches the shape instead
        stuffed a truncated repr into that line ⇒ now it is always `bad_hello`, reporting only the length."""
        leg, said, _logged = self.ask("1234567890")
        self.assertEqual(leg.describe()["state"], "bad_hello")
        self.assertIn("(10 characters", said)
        self.assertNotIn("1234567890", said)

    def test_once_the_paired_service_fixes_it_the_leg_connects_by_itself(self):
        """Review 4, m-f: `bad_hello` used to `return` ⇒ a dispatcher release that got the format wrong once (`_ver`
        tolerated it before the fix for `0.2.0-rc1`) stopped every bridge online until someone restarted it.
        ⭐ Now it keeps asking on the backoff: once the reply is fixed, it connects by itself and `refused` clears
        (never "refuse it forever once it has happened once")."""
        leg, _said, _logged = self.ask("0.2.0-rc1")
        self.assertEqual(leg.describe()["state"], "bad_hello")
        self.d.hello_reply = {"ok": True, "min_supported": "0.0.1", "latest": {}}
        self.assertTrue(self.d.wait(lambda: self.d.connects >= 1, timeout=15), leg.describe())
        self.assertEqual(leg.describe()["refused"], "")

    def test_a_latest_that_cannot_be_encoded_is_not_saved_and_the_leg_still_connects(self):
        """Review I3 (a cell of the same shape that D23 missed): the hello reply's `latest.*` carries a lone
        surrogate ⇒ it used to blow up in `_save_latest`'s → `_clip`'s encode step; the log said "remote leg
        dropped", `/healthz` showed `retrying` paired with an empty `refused`, and it could never connect to the
        stream again. ⭐ Judgment: it connects to the stream anyway (this comes first); latest.json's three values
        are all empty strings (the correct value); that line appears exactly once."""
        with contextlib.suppress(FileNotFoundError):
            scv.spath("latest.json").unlink()
        self.d.hello_reply = {"ok": True, "min_supported": "0.0.1",
                              "latest": {"version": "0.1" + chr(0xD800), "commit": "c0ffee", "sha256": "00"}}
        box = {}
        lines = log_lines_during(lambda: (self.b.start_remote(),
                                          box.__setitem__("up", self.d.wait(lambda: self.d.connects >= 1, timeout=10))))
        self.assertTrue(box["up"], "a broken latest in the reply left the remote leg unable to connect to the stream: %s" % lines[-2:])
        self.assertEqual(json.loads(scv.spath("latest.json").read_text(encoding="utf-8")),
                         {"version": "", "commit": "", "sha256": ""})
        self.assertEqual(len([x for x in lines if "the hello reply's latest cannot be encoded as UTF-8" in x]), 1, lines[-3:])

    def test_a_hello_reply_that_is_not_an_object_says_so(self):
        """Maintainer's ruling (fix2 addendum 3, present since BASE): the hello reply is JSON but not an object (a
        list here) ⇒ `.get` used to blow up into an "AttributeError" line. ⭐ Judgment: the log clearly says "the
        hello reply is not a JSON object (it is list)", it backs off as usual (`retrying`); once the reply is fixed
        it connects by itself."""
        self.d.hello_reply = ["not", "an", "object"]
        p = scv.spath("bridge.log")
        start = p.stat().st_size if p.exists() else 0
        self.b.start_remote()
        said = "the hello reply is not a JSON object (it is list)".encode("utf-8")
        # ⚠️ running this one case alone in this module, bridge.log may not exist yet ⇒ check existence first (never
        #   let it go red as FileNotFoundError)
        self.assertTrue(self.d.wait(lambda: p.exists() and said in p.read_bytes()[start:], timeout=10),
                        p.read_bytes()[start:].decode("utf-8", "replace")[-300:] if p.exists() else "(bridge.log does not exist yet)")
        self.assertEqual(self.b.remote.describe()["state"], "retrying")
        self.d.hello_reply = {"ok": True, "min_supported": "0.0.1", "latest": {}}
        self.assertTrue(self.d.wait(lambda: self.d.connects >= 1, timeout=15), self.b.remote.describe())

    def test_the_hello_that_asks_again_still_says_bad_hello_while_it_is_in_flight(self):
        """Task 14 fix5 F5-⑦1 (15b filled in the harness): the next hello sent to ask again also stays `bad_hello`
        while it is in flight (the guard clause at the top of `run()`). The harness used to only ever read the state
        during the wait periods, so removing the guard (`if True:`) still ran all green. ⭐ The dispatcher holds every
        subsequent hello for 1.5 seconds ⇒ the window is guaranteed to have a stretch of "hello in flight"; a
        separate fixture precondition pins down that a hello really did reach the dispatcher inside the window
        (otherwise this is again reading nothing but wait periods)."""
        leg, _said, _logged = self.ask(self.BAD)
        self.d.hello_delay, t0 = 1.5, time.time()
        seen = []
        while time.time() - t0 < 4.5:
            seen.append(leg.describe()["state"])
            time.sleep(0.05)
        self.assertEqual(sorted(set(seen)), ["bad_hello"], "state was changed while the follow-up hello was in flight")
        self.assertTrue([t for t in self.d.hello_times if t > t0], "fixture precondition: the window must really have a hello in flight")

    def test_a_hello_that_fails_on_the_network_clears_the_old_reason(self):
        """F5-⑦2 (maintainer's ruling): if the hello call itself fails on the network while in `bad_hello` ⇒ the state
        becomes `retrying`, and `refused`, which reports "the most recent reason", clears along with it (it used to
        keep the previous shape error hanging around until the next valid reply: `/healthz` would show `retrying`
        paired with an unrelated sentence)."""
        leg, said, _logged = self.ask(self.BAD)
        self.assertTrue(said)                                        # precondition: the earlier line is really there
        self.d.hello_status = 500
        self.assertTrue(self.d.wait(lambda: leg.describe()["state"] == "retrying", timeout=15), leg.describe())
        self.assertEqual(leg.describe()["refused"], "")

    def test_asking_again_backs_off_doubling_up_to_the_cap(self):
        """Review 5, m-g: the `bad_hello` retry loop's "the gaps get longer, capped at `MAX_BACKOFF_S`" used to have
        no test (the mutation K-no-double, which targets it, ran all 122 tests green; in reality it was 25 calls in
        26 seconds, making the "the gaps get longer" line on `/healthz` a lie). ⭐ Two judgments: (1) each ❌ line's own
        "asking again in Ns" values, taken in order, are exactly the backoff sequence (the correct value: with the
        cap turned down to 4 ⇒ 1, 2, 4, 4); (2) the number of hello calls has an upper bound following `dial_bound`'s
        sequence, and the ❌ line count equals the hello count (exactly one line per ask)."""
        with mock.patch.object(scv, "MAX_BACKOFF_S", 4.0):
            self.d.hello_reply = {"ok": True, "min_supported": "x", "latest": {}}
            leg = self.b.remote = scv.RemoteLeg(self.b)
            box = {}

            def run():
                runner = threading.Thread(target=leg.run, daemon=True)
                runner.start()
                self.assertTrue(self.d.wait(lambda: self.d.hellos, timeout=10))
                time.sleep(8.0)                                    # watching the window itself (never waiting for something to land on disk)
                leg.stop()
                box["bound"] = dial_bound(8.0, 0.0)
                # ⭐ wait for the leg to really exit before reading (never the old sleep 0.3): the in-flight call
                #   coming back and its ❌ line landing on disk both happen before `run()` returns (fix1 addendum 2, same shape)
                runner.join(10)
                self.assertFalse(runner.is_alive(), "the leg did not exit after 10 seconds of stopping")
            lines = log_lines_during(run)
        asked = [x for x in lines if "the dispatcher's required minimum version is not recognized" in x]
        waits = [int(x.rsplit("(asking again in ", 1)[1].split("s)")[0]) for x in asked]
        self.assertEqual(waits[:4], [1, 2, 4, 4], asked[-2:])
        self.assertLessEqual(len(self.d.hellos), box["bound"], "asked %d times in 8 seconds (at most %d per the backoff sequence)" % (len(self.d.hellos), box["bound"]))
        self.assertEqual(len(asked), len(self.d.hellos))


class LocalRateLimit(Case):
    cfg = {"remote_jobs_per_hour": 1}

    def test_second_remote_job_is_refused_by_the_bridge_itself(self):
        """⚠️ l1 must claim that slot first: the two job threads are started concurrently, and without waiting, which
        one hits `allow_remote` first is up to the scheduler ⇒ this case would go red intermittently on "the one
        refused is l1".
        `started` is the signal that "the slot has already been claimed" (`allow_remote` comes before
        `sessions.run`)."""
        self.b.start_remote()
        self.d.push("job", job("l1", "甲"))
        self.assertTrue(self.d.wait(lambda: any(e["event"] == "started" for e in self.events("l1"))))
        self.d.push("job", job("l2", "乙"))
        self.assertTrue(self.d.wait(self.terminal("l2")))
        self.assertEqual(self.events("l2")[-1]["error"]["type"], "local_rate_limit")


class LocalRateLimitZero(Case):
    """M-2: `remote_jobs_per_hour: 0` used to be read as `0 or 600` (Python folds a falsy 0 into the fallback the
    same way it folds a missing key) ⇒ the one local knob B30 gives him to pause remote work silently let 600 jobs
    an hour through instead. `0` configured on purpose must mean zero, never "not configured"."""
    cfg = {"remote_jobs_per_hour": 0}

    def test_a_configured_zero_refuses_the_very_first_remote_job(self):
        self.b.start_remote()
        self.d.push("job", job("z1", "甲"))
        self.assertTrue(self.d.wait(self.terminal("z1")))
        self.assertEqual(self.events("z1")[-1]["error"]["type"], "local_rate_limit")


class EntryGates(Case):
    """🔴 the same rule, two submission paths: the row of entry gates in the local leg's `normalize_request()`
    (length/range/content shape) must be the same set on the remote leg -- the two paths are not equivalent, and a
    bug missing from one side only shows up on the other side. ⭐ So what this test checks is "the remote leg also
    returns 400", never "it did not crash"."""

    BAD = (("session too long", {"session": "s" * 200}),
           ("model too long", {"model": "m" * 200}),
           ("timeout big enough to occupy a slot forever", {"opts": {"timeout": 10 ** 9}}),
           ("timeout is not a number", {"opts": {"timeout": "三十秒"}}),
           ("effort is not recognized", {"opts": {"effort": "turbo"}}),
           ("content is not text", {"messages": [{"role": "user", "content": 7}]}))

    def test_every_entry_gate_also_guards_the_remote_leg(self):
        self.b.start_remote()
        for i, (why, over) in enumerate(self.BAD):
            with self.subTest(why=why):
                jid = "g%d" % i
                self.d.push("job", job(jid, "甲", **over))
                self.assertTrue(self.d.wait(self.terminal(jid)), why)
                last = self.events(jid)[-1]
                # ⭐ what is pinned down is the category, never just "there is an error": without that gate, what flies
                #   out is a bare ValueError, or an unknown caught and reported as "the bridge itself hit a bug" ⇒
                #   judging only "there is an error" would still run that version all green.
                self.assertEqual(last["error"]["type"], "bad_request", (why, last))

    def test_a_job_id_too_long_is_dropped_before_anything_is_allocated(self):
        """`job_id` never reaches `remote_request()` (it is needed to ack first) ⇒ its own gate is pinned separately.
        The judgment is "not a single delivery, and no CLI was started": echoing that value back is itself one way
        to send it into bridge.log."""
        self.b.start_remote()
        self.d.push("job", job("x" * 500, "甲"))
        self.d.push("job", job("ok1", "甲"))
        self.assertTrue(self.d.wait(self.terminal("ok1")))
        self.assertEqual([r for r in self.d.results if len(r["job_id"]) > 128], [])

    def push_escaped(self, body):
        """The dispatcher writes a lone surrogate as a JSON escape (`ensure_ascii`) ⇒ the bridge's `json.loads` gets
        back a lone surrogate. Never `push()`: the reference dispatcher itself cannot encode it as UTF-8. The id
        takes the next number (the dispatcher only resends raw events with an id bigger than what it has already
        sent)."""
        eid = self.d._eid + 1
        self.d.push_raw("id: %d%sevent: job%sdata: %s%s%s" % (eid, chr(10), chr(10), json.dumps(body, ensure_ascii=True),
                                                            chr(10), chr(10)), eid)

    def test_a_job_id_carrying_a_lone_surrogate_is_dropped_like_a_bad_one(self):
        """D23 (Task 14 review 4, "measured while we were at it"): the ack used to fail to encode as UTF-8 ⇒ `_post`
        treated it as a network failure and tried all 3 times, the log said "delivering result failed" (a lie), and
        the dispatcher got nothing at all. ⭐ Now it gets the same treatment as "job_id is invalid" at the `_take` entry
        point: dropped, one loud line (never echoed back). Judgment: that line appears exactly once (the correct
        value), and that lie never appears at all."""
        self.b.start_remote()
        lines = log_lines_during(lambda: (self.push_escaped(job("D" + chr(0xD800) + "x", "甲")), self.d.push("job", job("sur-fence", "甲")),
                                          self.assertTrue(self.d.wait(self.terminal("sur-fence")))))
        self.assertEqual(len([x for x in lines if "dropping a job: job_id is invalid (str, 3 characters)" in x]), 1, lines[-4:])
        self.assertEqual([x for x in lines if "delivering result failed" in x], [])

    def test_a_lone_surrogate_anywhere_else_in_the_job_is_a_bad_request(self):
        """D23, "count the same shape first" (measured before the fix): session ⇒ blew up into `unknown` ("the bridge
        itself hit a bug") while hashing; system/body ⇒ blew up into a retryable `crashed` while writing the file or
        stdin ("could not write the working directory" and "stdin is already closed" are both lies, and the former
        even handed the local working directory's absolute path back to the dispatcher); model/effort were already
        repr'd first, so they were already bad_request. ⭐ Now the entry point the two legs share (`normalize_request`)
        rejects it in one place ⇒ every cell is `bad_request`, and the original words say it is a lone surrogate.
        The local leg's half: tests/test_70_local_api.py::Numbers."""
        s = chr(0xD800)
        self.b.start_remote()
        for field, over in (("session", {"session": "s" + s}), ("system", {"system": "SYS" + s}),
                            ("content", {"messages": [{"role": "user", "content": "甲" + s}]})):
            with self.subTest(field=field):
                jid = "sur-" + field
                self.push_escaped(job(jid, "甲", **over))
                self.assertTrue(self.d.wait(self.terminal(jid), timeout=30))
                err = self.events(jid)[-1]["error"]
                self.assertEqual((err["type"], "lone surrogate" in err["message"]), ("bad_request", True), err)

    def test_a_body_that_cannot_be_encoded_is_not_called_a_network_failure(self):
        """The other half of D23: `_post` used to treat an encoding error (`UnicodeEncodeError` is a `ValueError`) as
        a network failure and try all 3 times, with the log saying "delivering failed". Once the entry point rejects
        it, the dispatcher's own strings never reach here, but the CLI's original words can still carry one (its own
        escapes inside JSON) ⇒ this line still has to stay true: never retry, say "cannot be encoded as UTF-8", and
        never dial even once."""
        leg = self.b.remote = scv.RemoteLeg(self.b)
        body = {"job_id": "j", "seq": 3, "event": "chunk", "text": "a" + chr(0xD800)}
        lines = log_lines_during(lambda: self.assertRaises(UnicodeEncodeError, leg._post, "result", body))
        self.assertEqual(len([x for x in lines if "result was never sent: the content has a character that cannot be encoded as UTF-8" in x]), 1, lines)
        self.assertEqual([x for x in lines if "delivering result failed" in x], [])
        self.assertEqual(self.d.results, [])

    def test_one_bad_request_lands_exactly_one_line(self):
        """🔴 the accounting: `bad_request` raised by `SessionManager` deliberately does not go through `_fail()`
        ⇒ that line on disk can only be added by the error exit point. If the remote leg called `error_body()`
        itself, every bad_request on this leg would land zero lines.
        ⭐ Paired with a zero-input control (a good request lands no line at all): without it, this cannot be told apart
        from "it writes one line for every single call"."""
        self.b.start_remote()

        def push_and_wait(jid, **over):
            self.d.push("job", job(jid, "甲", **over))
            self.assertTrue(self.d.wait(self.terminal(jid)))

        bad = log_lines_during(lambda: push_and_wait("acct1", model="claude/nope"))
        self.assertEqual([x for x in bad if "bad_request" in x].__len__(), 1, bad)
        good = log_lines_during(lambda: push_and_wait("acct2"))
        self.assertEqual([x for x in good if "bad_request" in x], [])


class Leak(Case):
    def test_a_cli_version_that_is_really_an_error_message_never_ships(self):
        """🔴 B24's whitelist controls the key names, not the values: when `detect()` cannot probe out a version, it
        stores the OS's own words straight into that same `version` field, and `TimeoutExpired`'s original words
        carry the executable's full path (= the username).
        ⭐ This is pinned down the positive-control way: first set that field to a shape it can really take, then check
        whether it is in the hello -- never "run one and see it looks clean" (the default stub's version is clean to
        begin with, so that way tests everything green)."""
        home = os.path.expanduser("~")
        self.b.found["claude"]["version"] = ("Command '['%s/AppData/claude.cmd', '--version']' "
                                             "timed out after 10 seconds" % home)
        self.b.start_remote()
        self.assertTrue(self.d.wait(lambda: self.d.hellos))
        blob = json.dumps(self.d.hellos[0], ensure_ascii=False)
        self.assertNotIn(home, blob)
        self.assertNotIn("claude.cmd", blob)
        self.assertEqual([f["cli_version"] for f in self.d.hellos[0]["families"] if f["family"] == "claude"], [""])

    def test_latest_json_keeps_only_three_clipped_keys(self):
        """The server's word on "the latest version" lands on the local disk ⇒ a whitelist plus truncation; never let
        the peer write as much as it likes."""
        self.d.hello_reply = {"ok": True, "min_supported": "0.0.1",
                              "latest": {"version": "9" * 500, "commit": "c0ffee", "sha256": "00",
                                         "surprise": "x" * 4000}}
        # ⚠️ this module shares one SCV_HOME ⇒ latest.json (`0.1.0`) written by another case may already be on disk.
        #   The old wait condition, "the file exists and has no surprise", is satisfied by that old file on the spot
        #   ⇒ what gets read is someone else's (measured hitting this on the 6th full run). This race was invisible
        #   when the previous round only asserted `≤128` -- only pinning down the correct value exposed it. ⇒ delete
        #   it first, then wait for the copy written by this run.
        with contextlib.suppress(FileNotFoundError):
            scv.spath("latest.json").unlink()
        self.b.start_remote()
        # only wait for "the file to appear": having deleted it, whatever appears is what this run wrote. Never wait
        #   for "the content to be right": that way, when broken it goes red as a timeout, never red on the judgment
        self.assertTrue(self.d.wait(lambda: scv.spath("latest.json").exists() and self.d.hellos))
        self.assertNotIn("surprise", scv.spath("latest.json").read_text(encoding="utf-8"))
        got = json.loads(scv.spath("latest.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(got), ["commit", "sha256", "version"])
        # ⭐ an upper bound is not enough: "every value written as an empty string" is also ≤128 (2026-09-23,
        #   `assertLessEqual` general audit) ⇒ pin down the correct value: the truncated cell is the result of
        #   `_clip`, and the two cells that were not over stay untouched to the letter.
        self.assertEqual(got, {"version": scv._clip("9" * 500, 128), "commit": "c0ffee", "sha256": "00"})
        self.assertLessEqual(len(got["version"]), 128)
        self.assertTrue(got["version"].startswith("9" * 32), got["version"])


class NoLocalPathsLeave(Case):
    """15c (15b ⑧-6(a)): a local path in a failure's original words (with the username in it) must never leave over
    the network through the remote leg -- caught at the remote leg's single error exit point (`RemoteLeg._report`'s
    `error` field), replaced by shape (`_no_local_paths`, maintainer's ⑦3); the local leg still gives the original
    words as before (it answers to this machine's owner, who needs the path to fix it).
    ⭐ Two real failures: an unwritable working directory (claude, the OS's own words carry the full path), codex
    dying during the handshake (the real CLI's config-warning line at the end of stderr carries the home
    directory)."""

    SUFFIX = "(%d local paths in the original words were not sent; the original is in bridge.log on this machine, which keeps only the first 2048 bytes of each line)"

    def leaks(self, text):
        home = str(Path.home())
        return [x for x in (os.environ["SCV_HOME"], home, home.replace(chr(92), "/"), Path.home().name) if x in text]

    def unwritable_workdir(self):
        """A real OS failure (never mock the write): put a directory named system.txt inside the session directory
        first ⇒ `write_text` raises OSError on the spot, and the original words carry the full path."""
        real = scv.SessionManager._workdir

        def poisoned(mgr, key):
            p = real(mgr, key)
            (p / "system.txt").mkdir()
            return p
        return mock.patch.object(scv.SessionManager, "_workdir", poisoned)

    def test_an_unwritable_workdir_reaches_the_dispatcher_without_the_path(self):
        self.b.start_remote()
        with self.unwritable_workdir():
            self.d.push("job", job("np1", "甲"))
            self.assertTrue(self.d.wait(self.terminal("np1")))
        err = self.events("np1")[-1]["error"]
        self.assertEqual(err["type"], "crashed")
        self.assertEqual(self.leaks(err["message"]), [], err["message"])
        self.assertEqual(err["message"].count("<local path>"), 2, err["message"])      # ⭐ correct value: one spot for the working directory, one inside the OS's own words
        self.assertTrue(err["message"].startswith("could not write claude's working directory <local path> (system.txt/isolation.json): "), err["message"])
        self.assertTrue(err["message"].endswith(self.SUFFIX % 2), err["message"])

    def test_the_local_leg_still_gets_the_whole_text(self):
        """The local leg (the other side of the zero-input control): the same failure, the original words still
        carry the path (it answers to the local owner); never replaced by a substitute word."""
        with self.unwritable_workdir():
            st, _h, body = helpers.http("POST", self.port, "/v1/chat/completions", token=self.tok,
                                        body={"model": "claude/haiku", "messages": [{"role": "user", "content": "甲"}]})
        msg = json.loads(body)["error"]["message"]
        self.assertEqual(st, 502, msg)
        self.assertIn(os.environ["SCV_HOME"], msg)
        self.assertNotIn("<local path>", msg)

    def test_codex_dying_during_the_handshake_does_not_ship_the_home_in_its_stderr(self):
        os.environ["FAKE_MODE"] = "home_stderr_exit"
        self.b.start_remote()
        self.d.push("job", job("np2", "甲", model="codex/gpt-6-luna"))
        self.assertTrue(self.d.wait(self.terminal("np2")))
        msg = self.events("np2")[-1]["error"]["message"]
        self.assertEqual(self.leaks(msg), [], msg)
        self.assertIn("user (<local path>): `features.rmcp_client` is ignored.", msg)      # B21: the original words outside the path stay untouched to the letter
        self.assertTrue(msg.endswith(self.SUFFIX % 1), msg)

    def test_urls_and_ordinary_slashes_pass_untouched(self):
        """Positive control (never a false hit): a URL, `5/5`, a fullwidth slash, `/tmp` with only one segment, and
        this bridge's own endpoint path (`resolve_model`'s "see /v1/models" line -- turning that into `<local path>`
        would be a lying hint) all pass through unchanged; since none were replaced, never append that suffix."""
        text = ("unexpected status 401 Unauthorized: url: https://api.openai.com/v1/responses, cf-ray: a4-SIN; "
                "Reconnecting... 5/5 and/or system.txt／isolation.json in /tmp；这座桥没有报过这个模型：'x'（看 /v1/models）")
        self.assertEqual(scv._no_local_paths(text), text)

    def test_the_shapes_it_does_take(self):
        """The shape half, pinned cell by cell (⭐ the correct value): a backslash path, a backslash doubled up inside a
        repr, a drive letter plus a forward slash, `file://`, a POSIX absolute path of two or more segments, a home
        directory with a space in it (the whole home directory is replaced by `~` as one piece, never cut in half by
        the space and leaking the back half)."""
        bs = chr(92)
        with mock.patch.object(scv, "user_home", lambda: Path("C:/Users/John Smith")):
            got = scv._no_local_paths("a C:%sUsers%sJohn Smith%s.scv%swork b 'C:%sx%sy' c:/Temp/z file:///C:/q.txt /home/al/.scv "
                                      "~/.codex/config.toml: done" % (bs, bs, bs, bs, bs * 2, bs * 2))
        self.assertEqual(got, "a <local path> b '<local path>' <local path> <local path> <local path> <local path>: done" + self.SUFFIX % 6)
        self.assertNotIn("Smith", got)

    BS = chr(92)
    # 15c review M2/M3's table, cell by cell (home directory, input, expected (never that trailing suffix), count):
    #   the miss side (mixed separators, a Git Bash/WSL header, stuck right after `=`/`:`/a CJK character, a
    #   passed-through prefix with `..`) + counting (the home directory replaced by `~` counts as one too, a path
    #   with a space in it counts as one, a bare `~` never counts) + the home directory matched on segment
    #   boundaries.
    CELLS = [
        ("C:/Users/John Smith", "C:/Users" + BS + "John Smith/.scv", "<local path>", 1),
        ("C:/Users/John Smith", "/c/Users/John Smith/x", "<local path>", 1),
        ("C:/Users/John Smith", "/mnt/c/Users/John Smith/x", "<local path>", 1),
        ("C:/Users/John Smith", "cfg=D:/data/jsmith-secret/x", "cfg=<local path>", 1),
        ("C:/Users/John Smith", "error:D:/proj/jsmith/x", "error:<local path>", 1),
        ("C:/Users/John Smith", "目录D:/proj/jsmith/x写不了", "目录<local path>", 1),
        ("C:/Users/John Smith", "path=C:/Users/John Smith/x", "path=<local path>", 1),
        ("C:/Users/John Smith", "home=C:" + BS + "Users" + BS + "John Smith", "home=<local path>", 1),
        ("C:/Users/John Smith", "retry in ~ 5s", "retry in ~ 5s", 0),
        ("C:/Users/John Smith", "'C:" + BS + "Program Files" + BS + "Codex" + BS + "codex.exe'", "'<local path>'", 1),
        ("C:/Users/John Smith", BS * 2 + "fileserver" + BS + "share" + BS + "John Smith" + BS + "x", "<local path>", 1),
        ("C:/Users/John Smith", "/bridge/../Users/jsmith/x and /v1/../Users/jsmith/x", "<local path> and <local path>", 2),
        ("C:/Users/John Smith", "https://host.example/C:/Users/John Smith/x", "https://host.example/~/x", 1),
        ("C:/Users/John Smith", "error:https://api.openai.com/v1/x cp C:" + BS + "a D:" + BS + "b",
         "error:https://api.openai.com/v1/x cp <local path> <local path>", 2),
        ("/home/al", "open:/srv/alproj/x", "open:<local path>", 1),
        ("/home/al", "/home/al/My Stuff/scv/work", "<local path>", 1),
        ("/home/al", "/srv/John Doe/x", "<local path>", 1),
        ("/home/al", "/home/alice/x", "<local path>", 1),
        ("/root", "https://example.com/rootkit/x https://example.com/root/x", "https://example.com/rootkit/x https://example.com/root/x", 0),
        # fix2 (review N-M1): "a backslash anywhere in the word" is kept as a fallback -- a relative backslash path,
        #   a UNC/drive-letter path stuck right after another character
        ("C:/Users/John Smith", "open .." + BS + "jsmith" + BS + "secret.txt failed", "open <local path> failed", 1),
        ("C:/Users/John Smith", "open data" + BS + "jsmith" + BS + "x", "open <local path>", 1),
        ("C:/Users/John Smith", "x|" + BS * 2 + "fileserver" + BS + "share" + BS + "jsmith" + BS + "x", "<local path>", 1),
        ("C:/Users/John Smith", "x|D:" + BS + "proj" + BS + "jsmith" + BS + "x", "x|<local path>", 1),
        # fix2 (outside review scope 4): a private-use-area placeholder never counts as "after a CJK character"; a
        #   home directory (replaced by `~`) sitting inside a URL, and a drive-letter path stuck right after it, each
        #   count as one spot
        ("C:/Users/John Smith", "a=https://h/C:/Users/John Smith/x=D:/q/r", "a=https://h/~/x=<local path>", 2),
    ]

    def test_the_review_table_cell_by_cell(self):
        for home, given, want, n in self.CELLS:
            with self.subTest(given=given), mock.patch.object(scv, "user_home", lambda h=home: Path(h)):
                self.assertEqual(scv._no_local_paths(given), want + (self.SUFFIX % n if n else ""))

    def test_a_long_word_costs_linear_time(self):
        """fix2 (review N-M2): candidate starting positions are scanned out in one pass, each position judged in
        constant time -- it used to slice character by character and then `.lower()`, taking 4.9 seconds for 50,000
        CJK characters and 14.7 seconds for `a:` x 25000 (quadratic). ⭐ The judgment is an absolute ceiling, never a
        ratio (a millisecond-scale ratio is noise on a busy machine): the linear implementation takes only tens of
        milliseconds for these cells, a margin of more than twenty times over the 1-second ceiling; never pass by
        "capping the scan length" -- that would let the excess stretch through untouched, and the last line of the
        cell below pins it down and still catches it."""
        with mock.patch.object(scv, "user_home", lambda: Path("C:/Users/John Smith")):
            for name, text in (("50,000 CJK characters", "中" * 50000), ("a: × 25000", "a:" * 25000), ("a= × 25000", "a=" * 25000),
                               ("= alternating with CJK", "=中" * 25000), ("x x 200,000", "x" * 200000)):
                with self.subTest(name=name):
                    t0 = time.perf_counter()
                    self.assertEqual(scv._no_local_paths(text), text)
                    self.assertLess(time.perf_counter() - t0, 1.0)
            tail = "中" * 50000 + "D:/proj/jsmith/x"
            self.assertEqual(scv._no_local_paths(tail), "中" * 50000 + "<local path>" + self.SUFFIX % 1)

    def test_a_home_that_cannot_be_read_only_skips_that_one_step(self):
        """15c review M4: getting the home directory raising any exception at all (3.9's POSIX `Path.home()` raises
        `KeyError`) should only mean "that one substitution step for the home directory is skipped" -- it used to
        catch only `RuntimeError`, letting other families propagate all the way out and get swallowed in `finish`
        ⇒ this job has no terminal state at all. One unit cell plus one end-to-end cell."""
        for boom in (KeyError("HOME"), OSError("nope"), RuntimeError("no home")):
            with self.subTest(boom=type(boom).__name__), mock.patch.object(scv, "user_home", side_effect=boom):
                self.assertEqual(scv._no_local_paths("open C:" + self.BS + "x" + self.BS + "y"), "open <local path>" + self.SUFFIX % 1)
        self.b.start_remote()
        with self.unwritable_workdir(), mock.patch.object(scv, "user_home", side_effect=KeyError("HOME")):
            self.d.push("job", job("np3", "甲"))
            self.assertTrue(self.d.wait(self.terminal("np3")), "no terminal state")
        self.assertEqual(self.events("np3")[-1]["error"]["type"], "crashed")


class Robust(Case):
    def test_a_junk_event_does_not_kill_the_leg(self):
        """🔴 one bad event must never take the whole leg down with it: it is a daemon thread, so propagating out
        only leaves a traceback on stderr, and then this bridge silently never gets another job again -- while the
        local leg is still alive, and from the outside it looks "just fine".
        ⭐ The judgment is "the good job after the bad event still runs to completion", never "no exception was
        raised"."""
        self.d.push_raw("id: 900" + chr(10) + "event: job" + chr(10) + "data: {这不是 JSON" + chr(10) + chr(10), 900)
        self.d.push_raw("id: 901" + chr(10) + "event: job" + chr(10) + "data: [1,2,3]" + chr(10) + chr(10), 901)
        self.d.push_raw("id: 902" + chr(10) + "event: 没听说过" + chr(10) + "data: {}" + chr(10) + chr(10), 902)
        self.b.start_remote()
        self.d.push("job", job("after", "甲"))
        self.assertTrue(self.d.wait(self.terminal("after"), timeout=20))
        self.assertEqual(self.events("after")[-1]["event"], "done")

    def test_an_event_made_of_many_lines_is_capped_too(self):
        """The stream-reading side has two caps. ⭐ Each one gets pinned down by its own judgment, never both cases
        staring at the same symptom (measured 2026-09-22: when only this one case built its event from "one
        oversized line", removing `readline(N)`'s cap entirely still ran it all green -- the cumulative cap covered
        for the symptom on its behalf, the two guardrails masking each other):
          - this case = the cumulative one (an event built up from many short lines, so `readline(N)` never fires
            even once);
          - the next case = the single-line one (one line that never ends, so the cumulative cap never even gets a
            turn before it would return).
        Judgment: the event over the limit gets not a single delivery, and the ordinary job right after it still runs
        to completion (having dropped and reconnected by itself)."""
        n = scv.STREAM_EVENT_MAX // 100 + 20
        many = "".join("data: " + "y" * 93 + chr(10) for _ in range(n))
        self.d.push_raw("id: 800" + chr(10) + "event: job" + chr(10) + many + chr(10), 800)
        self.b.start_remote()
        self.d.push("job", job("small", "甲"))
        self.assertTrue(self.d.wait(self.terminal("small"), timeout=25))
        self.assertEqual([r for r in self.d.results if r["job_id"] == "huge"], [])
        self.assertGreaterEqual(self.d.connects, 2)      # dropped, then came back by itself

    def test_one_line_that_never_ends_cannot_eat_the_memory(self):
        """`readline()` with no cap means one pipe on the other end that never sends a newline can eat this bridge's
        memory whole, and the cumulative cap never even gets a turn before `readline` would return. ⭐ So this case's
        judgment is memory, never "that event got dropped" -- both guardrails can make the latter true.
        ⭐ The judgment is how many bytes the dispatcher really pumped in (once the bridge bails, its end stops being
        writable), never measuring RSS afterward: the first two versions both measured this wrong -- (1) first
        building the 48 MiB into a string on the dispatcher's side, which grows its own memory (the way the break was
        introduced itself injects a signal, so both arms grow); (2) after switching to pumping it in chunks, measuring
        RSS afterward runs both arms green (that huge blob is freed right after bailing, so RSS cannot show the
        peak). The number of bytes pumped in is deterministic, immediate, and cheap."""
        self.d.push_flood(48 * 1024 * 1024, 700)
        self.b.start_remote()
        self.d.push("job", job("tiny", "甲"))
        self.assertTrue(self.d.wait(self.terminal("tiny"), timeout=60))
        # the cap is 256 KiB plus a stretch of TCP buffer (the kernel accepts a few hundred KB on the peer's behalf
        #   first) ⇒ 8 MiB is a loose but decisive bound
        self.assertLess(self.d.flooded, 8 * 1024 * 1024,
                        "the bridge read a whole 48 MiB line in ⇒ the single-line cap did not take effect (%d bytes pumped in)" % self.d.flooded)

    def test_a_result_reply_that_is_not_json_still_counts_as_delivered(self):
        """Maintainer's ruling (fix2 addendum 2, present since BASE): the bridge must never read `/bridge/result`'s
        reply body; a 2xx counts as delivered. `_post` used to run `json.loads` on it, so a dispatcher replying 200
        plus a non-JSON body used to make this call count as failed and retry 3 times; if an ack "fails" like that
        the job gets lost (the dispatcher actually did receive the ack). ⭐ Judgment: exactly one ack, this job starts
        as usual, terminal state `done`."""
        self.delivered_despite(b"OK (not json)", "rr1")

    def test_a_result_reply_bigger_than_the_body_cap_still_counts_as_delivered(self):
        """Review 2, N-M2 (probe `resultraw big`): the reply body used to be read in just the same way (capped at
        8 MiB) ⇒ a 9 MiB body made this call count as failed, the ack retried 3 times, and this job got lost on the
        bridge's side, while the dispatcher received 3 acks (the same silent bug addendum ② was meant to fix, under a
        different trigger condition). ⭐ Now that call reads not a single byte. Same judgment as above."""
        self.delivered_despite(b"x" * (9 * 1024 * 1024), "rr2")

    def delivered_despite(self, raw, jid):
        self.d.result_raw = raw
        self.b.start_remote()
        self.d.push("job", job(jid, "甲"))
        # wait for either "a terminal state" or "the ack has already retried to its 3rd try" (the shape before the
        #   fix): waiting only for the former would go red as a timeout when broken, never red on the judgment
        self.assertTrue(self.d.wait(lambda: self.terminal(jid)() or [e["event"] for e in self.events(jid)].count("ack") >= 3,
                                    timeout=20))
        evs = [e["event"] for e in self.events(jid)]
        self.assertEqual((evs.count("ack"), evs[-1]), (1, "done"), evs)

    def test_a_terminal_event_that_cannot_be_delivered_does_not_become_an_error(self):
        """🔴 the outcome and whether it could be delivered are two separate axes: a job that ran to completion gets
        rewritten as `error` because delivery broke, which both lies to the dispatcher and gets the category on that
        `jobs.log` line wrong (which is exactly what accounting reads later).
        ⭐ The judgment lands on `jobs.log`'s `klass` -- the other side cannot tell the difference (it never received it
        to begin with)."""
        self.d.fail_result = lambda body: body.get("event") == "done"
        self.b.start_remote()
        self.d.push("job", job("nd1", "甲"))
        self.assertTrue(self.d.wait(lambda: any(r["event"] == "done" for r in self.events("nd1")), timeout=20))
        self.assertTrue(self.d.wait(lambda: self.b.joblog.tail(1) and self.b.joblog.tail(1)[0]["job_id"] == "nd1",
                                    timeout=120))
        self.assertEqual(self.b.joblog.tail(1)[0]["klass"], "ok")

    def test_a_chunk_that_cannot_be_delivered_does_not_kill_the_session(self):
        """🔴 `on_delta` is called from inside `driver.turn()` ⇒ if a chunk-delivery failure propagates out, it goes
        straight through `_run_session` (which kills the CLI if it has not finished answering: this used to be
        `except BridgeError`, and is now `finally` + `answered`): it kills the long-lived CLI, reports a call that is
        answering normally as `unknown` (and `unknown` is never in RETRYABLE), gets `jobs.log` wrong too, and the
        next turn has to rebuild from scratch (the genuinely expensive cell) -- exactly the thing this same piece of
        code claims to forbid, only shifted to the other half.
        ⭐ Three judgments in parallel: the terminal state is `done` / `jobs.log`'s `klass` is still `ok` / that session
        is still alive.
        ⚠️ the fixture makes the CLI sleep 1 second first before emitting text: without arranging it this way, the
          two deltas would land within `RESULT_FLUSH_S` of each other, the one and only flush would fall outside
          `turn()`, and whether it propagates or not would never touch the driver layer ⇒ it would run green even
          unfixed."""
        os.environ["FAKE_MODE"], os.environ["FAKE_DELAY"] = "slow_first", "1"
        self.addCleanup(os.environ.pop, "FAKE_DELAY", None)
        self.d.fail_result = lambda body: body.get("event") == "chunk"
        self.b.start_remote()
        self.d.push("job", job("ck1", "甲", session="room-ck"))
        self.assertTrue(self.d.wait(self.terminal("ck1"), timeout=90))
        self.assertEqual(self.events("ck1")[-1]["event"], "done")
        self.assertTrue(self.d.wait(lambda: self.b.joblog.tail(1) and self.b.joblog.tail(1)[0]["job_id"] == "ck1",
                                    timeout=30))
        self.assertEqual(self.b.joblog.tail(1)[0]["klass"], "ok")
        self.assertEqual(self.b.sessions.counts()["sessions"], 1)

    def test_a_session_closed_while_a_turn_is_in_flight_is_cancelled_not_crashed(self):
        """🔴 the `note_close`/`note_closed` bookkeeping is never something the local leg alone needs: without it,
        if the dispatcher sends `close_session` while a turn is in flight, what comes back for that turn is
        `crashed` + `retryable=True` + original words pointing at a CLI crash that never actually happened --
        `_closed_midflight()` exists to fix exactly this lie."""
        os.environ["FAKE_MODE"] = "trickle"
        self.b.start_remote()
        self.d.push("job", job("mf1", "甲", session="room-mf"))
        self.assertTrue(self.d.wait(lambda: any(e["event"] == "chunk" for e in self.events("mf1")), timeout=30))
        self.d.push("close_session", {"session": "room-mf"})
        self.assertTrue(self.d.wait(self.terminal("mf1"), timeout=60))
        err = self.events("mf1")[-1]["error"]
        self.assertEqual(err["type"], "cancelled", err)
        self.assertFalse(err["retryable"])
        # ⭐ the original-words half (review M-1: the category was right but the words were wrong, and it used to
        #   assert only the category): the two legs share the same rewritten sentence, ⇒ it must never point at the
        #   local leg's endpoint -- on this leg, what closed it was the dispatcher's own `close_session` push.
        self.assertIn("this session was closed", err["message"])
        self.assertNotIn("/v1/", err["message"])


class ChunkFailureFamilies(Case):
    """🔴 review C-A: a chunk-delivery raising an exception that is not an OSError (`http.client.BadStatusLine`, one
    of the `HTTPException` family -- exactly what an unhealthy proxy replying with a bad status line raises) used to
    result in no terminal state at all, `jobs.log` getting it wrong, and the long-lived session answering the
    previous turn's question from then on (a silent wrong answer, with all three downstream tables running green).
    The I-9 round only fixed the `OSError` family.
    ⭐ The gate follows the shape of the bug: after a chunk-delivery raises any family of exception, (1) the terminal
      state is exactly one and is `done` (2) `jobs.log`'s klass is `ok` (3) the same session's next question is
      answered with the next question. Three families: `OSError` (500, in `Robust`) / `HTTPException` (raised at the
      dialing layer -- `OpenerDirector.open`, via `scv._open`; taking `_post`'s own path) / an unfamiliar exception
      (raised at the `_post` layer, taking `post()`'s wrapper)."""

    def run_family(self, jid, sid, patcher):
        os.environ["FAKE_MODE"], os.environ["FAKE_DELAY"] = "slow_first", "1"
        self.addCleanup(os.environ.pop, "FAKE_DELAY", None)
        self.b.start_remote()
        with patcher:
            self.d.push("job", job(jid, "甲", session=sid))
            self.assertTrue(self.d.wait(self.terminal(jid), timeout=60))
        terminals = [e["event"] for e in self.events(jid) if e["event"] in ("done", "error")]
        self.assertEqual(terminals, ["done"], "the terminal state must be exactly one and be done")
        self.assertTrue(self.d.wait(lambda: self.b.joblog.tail(1) and self.b.joblog.tail(1)[0]["job_id"] == jid,
                                    timeout=30))
        self.assertEqual(self.b.joblog.tail(1)[0]["klass"], "ok")
        first = self.events(jid)[-1]["text"]
        nxt = {"job_id": jid + "-next", "session": sid, "model": "claude/haiku", "system": "SYS", "opts": {},
               "messages": [{"role": "user", "content": "甲"}, {"role": "assistant", "content": first},
                            {"role": "user", "content": "乙"}]}
        self.d.push("job", nxt)
        self.assertTrue(self.d.wait(self.terminal(jid + "-next"), timeout=60))
        text = self.events(jid + "-next")[-1].get("text", "")
        self.assertTrue(text.endswith("乙") or self.events(jid + "-next")[-1].get("rebuilt"),
                        "the next answer is not to the next question: %r" % text)

    def test_an_http_exception_on_a_chunk(self):
        """⚠️ the injection point follows the production path (Task 13 fix1): dialing now goes through the opener
        `scv._open` builds (with `_Redirects`), no longer through `urllib.request.urlopen` ⇒ inject on
        `OpenerDirector.open` (both `urlopen` and any `build_opener()` go through it). Leaving it on `urlopen`
        would let not a single call be injected here, `hits` staying 0 forever -- red, but the harness is what is
        red."""
        real, hits = urllib.request.OpenerDirector.open, []

        def fake(opener, req, *a, **kw):
            if getattr(req, "data", None) and json.loads(req.data).get("event") == "chunk":
                hits.append(1)
                raise http.client.BadStatusLine("garbage")
            return real(opener, req, *a, **kw)

        lines = log_lines_during(lambda: self.run_family("hx1", "room-hx", mock.patch.object(
            urllib.request.OpenerDirector, "open", autospec=True, side_effect=fake)))
        # ⭐ `_post`'s own judgment at that layer (never let `post()`'s wrapper cover for it): this family too must
        #   retry all 3 times, and must be loud too
        self.assertEqual(len(hits), 3, "HTTPException was not treated by `_post` as a retryable delivery failure")
        self.assertTrue([x for x in lines if "delivering result failed (tried 3 times)" in x and "BadStatusLine" in x], lines[-5:])

    def test_a_stranger_exception_on_a_chunk(self):
        real = scv.RemoteLeg._post

        def fake(leg, key, body, tries=3):
            if body.get("event") == "chunk":
                raise KeyError("stranger exception")
            return real(leg, key, body, tries)

        lines = log_lines_during(lambda: self.run_family("sx1", "room-sx", mock.patch.object(
            scv.RemoteLeg, "_post", autospec=True, side_effect=fake)))
        self.assertTrue([x for x in lines if "an error we do not recognize" in x and "KeyError" in x], lines[-5:])

    def test_a_last_chunk_whose_error_cannot_even_be_printed_does_not_eat_the_terminal(self):
        """🔴 supplementary review 2, M-2: `finish()` used to wrap only the last flush in `suppress(BridgeError)`,
        relying on `post()`'s contract of "only raises BridgeError" -- which is not a structural guarantee: an
        exception whose `__str__` itself blows up makes `post()`'s wrapper body blow up on its own, and what
        propagates out is not a BridgeError ⇒ 0 terminal states, yet `jobs.log` records ok (measured with review's
        `rv2_finalflush.py`).
        ⭐ The fixture turns `RESULT_FLUSH_S` up to one hour: not a single flush happens inside the turn, so the only
          thing that can break is that last flush inside `finish` (never rely on the timing of "the body lands within
          0.3 seconds": if the CLI starts up a little slower, that call falls inside the turn instead and takes the
          root path).
        ⭐ Judgment: the terminal state is exactly one and is done / `jobs.log` is ok / one line was loud. What is
          waited on is that `jobs.log` line (always written inside `finally`, and written after the terminal state),
          never the terminal state: when broken the terminal state never comes at all, which would go red as a
          timeout, never red on the judgment."""
        class Unprintable(Exception):
            def __str__(self):
                raise RuntimeError("cannot even get the original words out")

        real, tries = scv.RemoteLeg._post, []

        def fake(leg, key, body, n=3):
            if body.get("event") == "chunk":
                tries.append(1)
                raise Unprintable()
            return real(leg, key, body, n)

        self.b.start_remote()
        with mock.patch.object(scv, "RESULT_FLUSH_S", 3600), \
                mock.patch.object(scv.RemoteLeg, "_post", autospec=True, side_effect=fake):
            lines = log_lines_during(lambda: (self.d.push("job", job("uf1", "甲")),
                                              self.assertTrue(self.d.wait(lambda: self.rows_of("uf1"), timeout=60))))
        terms = [e["event"] for e in self.events("uf1") if e["event"] in ("done", "error")]
        self.assertEqual(terms, ["done"], "the terminal state must be exactly one and be done")
        self.assertEqual([r["klass"] for r in self.rows_of("uf1")], ["ok"])
        self.assertEqual(len(tries), 1, "fixture precondition: chunk was sent exactly once, and inside finish")
        # what propagates out is the RuntimeError raised inside `__str__` (`Unprintable` sits in its `__context__`)
        self.assertTrue([x for x in lines if "the remote leg's final state errored out" in x and "RuntimeError" in x], lines[-5:])

    def test_a_started_that_cannot_be_delivered_says_so(self):
        """Another consumer of `post()`'s wrapper: when the `started` call raises a stranger exception, the terminal
        state's original words must say "delivering failed", never "the bridge itself hit a bug: KeyError" (the case
        outside review scope: "the outcome and whether it could be delivered are two separate axes")."""
        real = scv.RemoteLeg._post

        def fake(leg, key, body, tries=3):
            if body.get("event") == "started":
                raise KeyError("stranger exception")
            return real(leg, key, body, tries)

        self.b.start_remote()
        with mock.patch.object(scv.RemoteLeg, "_post", autospec=True, side_effect=fake):
            self.d.push("job", job("st1", "甲"))
            self.assertTrue(self.d.wait(self.terminal("st1"), timeout=60))
        err = self.events("st1")[-1]["error"]
        self.assertIn("delivering to the dispatcher failed", err["message"])


class InFlightCap(Case):
    cfg = {"max_concurrent": 1}

    def test_more_jobs_than_the_cap_are_refused_instead_of_spawning_threads(self):
        """🔴 one thread per job, and events are pushed in from the network side ⇒ with no cap, the peer pushing
        10,000 at once means 10,000 threads on this machine (each one also waking up every 0.25s inside `_acquire`).
        The concurrency slots manage "how many run at the same time", never "how many can be pushed in at once".
        ⭐ The judgment is "the one refused gets `local_rate_limit`" plus "the threads did not explode"."""
        os.environ["FAKE_MODE"] = "hang"
        self.b.start_remote()
        cap = self.b.remote.max_inflight
        self.assertEqual(cap, 4)                       # max(4, 4×max_concurrent=4)
        for i in range(cap + 3):
            self.d.push("job", job("f%d" % i, "甲"))
        self.assertTrue(self.d.wait(lambda: any(
            r["event"] == "error" and r["error"]["type"] == "local_rate_limit" for r in self.d.results), timeout=30))
        refused = [r["job_id"] for r in self.d.results
                   if r["event"] == "error" and r["error"]["type"] == "local_rate_limit"]
        self.assertGreaterEqual(len(refused), 1)
        # 🔴 the one refused also gets recorded in `_seen`: PROTOCOL has the dispatcher dedupe by `seq`, and `seq`
        #   counts from 0 for each job ⇒ if the same id ran a second sequence, the peer would drop `ack`/`started` as
        #   duplicates (while `local_rate_limit` is retryable, and this path is the one being encouraged). ⇒ one id
        #   is only ever allowed one sequence.
        #   ⚠️ the judgment lands on the behavior below (resending ⇒ only a dup comes back), never on looking at the
        #   internal `_seen` table: looking at the table, a broken form like "it got recorded, but resending still
        #   runs a second sequence anyway" would still run green.
        # ⭐ pin down that the actionable instruction is really in the message (so the dispatcher, following it, does
        #   not fall into the hole above)
        msg = [r["error"]["message"] for r in self.d.results
               if r["event"] == "error" and r["error"]["type"] == "local_rate_limit"][0]
        self.assertIn("a new job_id", msg)
        # ⭐ the behavior half (never look only at the internal `_seen` table): resend with the same id ⇒ PROTOCOL says
        #   only one `dup` ack comes back. The judgment is "this id has only one sequence starting from 0" -- with
        #   two sequences, the peer deduping by seq would drop things.
        again = refused[0]
        self.d.push("job", job(again, "甲"))
        # wait for either "the dup arrived" or "a second sequence started": waiting only for the former would go red
        #   as a timeout here when broken, never red on the judgment
        self.assertTrue(self.d.wait(lambda: any(r.get("dup") for r in self.events(again))
                                    or sum(r["seq"] == 0 for r in self.events(again)) > 1, timeout=30))
        evs = self.events(again)
        self.assertEqual([(e["seq"], e["event"]) for e in evs if e["seq"] == 0], [(0, "ack")], evs)
        self.assertEqual([e["seq"] for e in evs if e.get("dup")], [-1])

    def test_the_cap_is_not_what_normally_happens(self):
        """Zero-input control: take away "pushing a bunch at once" and run through it again -- not a single
        `local_rate_limit` is allowed."""
        self.b.start_remote()
        self.d.push("job", job("solo", "甲"))
        self.assertTrue(self.d.wait(self.terminal("solo")))
        self.assertEqual([r for r in self.d.results
                          if r["event"] == "error" and r["error"]["type"] == "local_rate_limit"], [])


def outbox_threads(leg):
    """How many threads this leg's outbound channel currently has alive (identified by the thread's target: on 3.9
    the thread name does not carry the target's name)."""
    return [t for t in threading.enumerate() if getattr(getattr(t, "_target", None), "__self__", None) is leg._outbox]


class SendChannel(Case):
    """🔴 Task 11 review 1, M-6: ack (and the error that follows right behind it on the refused path) used to be sent
    synchronously on the stream-reading thread ⇒ while the dispatcher's `/bridge/result` was hung and `/bridge/stream`
    was still open, that pipe read not a single byte, and a cancel queued behind it had to wait out that one
    delivery's worst-case duration (measured before the fix, against production constants: about 92.5 seconds; the
    refused path's ack + error, two items, about 182 seconds -- see task-15b-report). It is now handed off to a
    dedicated sending thread plus a bounded queue (`scv._Outbox`).
    ⭐ The dispatcher is really hung (`hold_result`: that call's socket just keeps waiting to read the response),
      never mock `_post`.
    ⚠️ scale: the harness hangs it for 20 seconds (< production's 30-second delivery timeout ⇒ before the fix, that
      call would succeed at the 20th second and the cancel would take effect right after), and it judges "takes
      effect within 8 seconds"; in production it is minutes-scale, and what is being proven here is the ordering
      (the cancel is no longer queued behind the hung delivery), never some specific number of seconds."""
    cfg = {"max_concurrent": 1}     # ⇒ max_inflight = 4, the outbound channel's queue bound = 16

    def test_a_hung_result_endpoint_does_not_hold_up_a_cancel(self):
        """(1) in-flight A: the cancel takes effect on the spot (never queued behind B's hung ack) (2) B, still
        queued in the channel (its ack is hung): its cancel must not be lost either -- the moment B's ack is
        released and picked up, it is cancelled on the spot (the "cancel that arrived early" table, 15b fix1 I1) ⇒
        not a single turn ran (never `started`)."""
        os.environ["FAKE_MODE"] = "hang"
        self.b.start_remote()
        mark = len(helpers.read_fake_log())
        self.d.push("job", job("m6-a", "甲", system="SYS-m6-a"))
        # 🔴 before turning the stub, wait for A's CLI to really start up (the moment the fake CLI starts it reads
        #   `FAKE_MODE` and records this line right after) -- never just wait for `started`: that is sent the moment
        #   the slot is claimed, before the CLI starts ⇒ if the bridge's thread is a few milliseconds slow, A would
        #   start with `ok` and finish answering before the cancel ⇒ `KeyError: 'error'` (hit once during a fix3 full
        #   run, reproduced with a positive control)
        self.assertTrue(self.d.wait(lambda: any("SYS-m6-a" in str(x.get("system")) for x in helpers.fake_log_since(mark))),
                        "fixture precondition: A's CLI came up")
        os.environ["FAKE_MODE"] = "ok"                      # if B is not cancelled, it will finish answering normally (⇒ red on "done", never a timeout)
        self.d.hold_result, self.d.hold_max = (lambda body: body.get("job_id") == "m6-b"), 20.0
        self.d.push("job", job("m6-b", "乙"))
        self.assertTrue(self.d.wait(lambda: self.events("m6-b")), "fixture precondition: B's ack really reached the dispatcher and is really hung")
        self.d.push("cancel", {"job_id": "m6-b"})
        t0 = time.time()
        self.d.push("cancel", {"job_id": "m6-a"})
        self.assertTrue(self.d.wait(self.terminal("m6-a"), timeout=8),
                        "the in-flight one's cancel did not take effect within 8 seconds: it was queued behind the hung ack (already waited %.1f seconds)" % (time.time() - t0))
        self.assertEqual(self.events("m6-a")[-1]["error"]["type"], "cancelled")
        self.d.release()
        self.assertTrue(self.d.wait(self.terminal("m6-b"), timeout=20))
        self.assertEqual([(e["event"], (e.get("error") or {}).get("type")) for e in self.events("m6-b")],
                         [("ack", None), ("error", "cancelled")], "the cancel for the one queued in the channel was lost, or it started up and ran a turn")

    def queued_cancel(self, mode):
        """The shape of review I1 (probe `qcancel`): A's and C's acks are hung, B's is not; push A, B, C in order,
        then cancel B. When the cancel arrives B is still queued in the channel, with C sitting between B and this
        cancel ⇒ this cancel used to be queued at the tail, having to wait for C's ack to be sent item by item (hang:
        takes 16 seconds to cancel; ok: B already finished running, the cancel falls flat).
        ⭐ The judgment (the correct value): B's delivery is exactly ack + `cancelled`, not a single CLI turn ran, and
          by the time the terminal state arrives C's ack has not yet tried all 3 times.
        ⚠️ scale: the delivery timeout is turned down to 1 second (30 seconds in production: C's one item alone would
          be about 91 seconds), and what is proven is the ordering, never some specific number of seconds."""
        os.environ["FAKE_MODE"] = mode
        with mock.patch.object(scv, "POST_TIMEOUT_S", 1.0):
            self.d.hold_result, self.d.hold_max = (lambda body: body.get("event") == "ack" and body.get("job_id") in ("qa", "qc")), 60.0
            self.b.start_remote()
            leg = self.b.remote
            turns = len([x for x in helpers.read_fake_log() if x.get("turn")])
            self.d.push("job", job("qa", "甲"))
            self.assertTrue(self.d.wait(lambda: self.events("qa")), "fixture precondition: A's ack is really hung")
            self.d.push("job", job("qb", "qb-乙"))
            self.d.push("job", job("qc", "丙"))
            self.assertTrue(self.d.wait(lambda: len(leg._outbox._q.queue) == 2), "fixture precondition: both B and C are queued in the channel")
            self.assertNotIn("qb", leg._cancels)
            self.d.push("cancel", {"job_id": "qb"})
            # wait for either "a terminal state" or "it started up": waiting only for the former, if the cancel is
            #   lost, would leave B never finishing in hang mode, going red as a timeout, never red on the judgment
            self.assertTrue(self.d.wait(lambda: self.terminal("qb")() or any(e["event"] == "started" for e in self.events("qb")),
                                        timeout=30))
            got = [(e["event"], (e.get("error") or {}).get("type")) for e in self.events("qb")]
            t_b = ([t for t, r in self.d.result_log if r["job_id"] == "qb" and r["event"] in ("done", "error")] or [time.time()])[0]
            c_acks = len([t for t, r in self.d.result_log if r["job_id"] == "qc" and r["event"] == "ack" and t <= t_b])
            leg.stop()                                    # ⭐ stop first, then release: never let the hung A/C start up after being released and run into the next test's window
            self.d.release()
        self.assertEqual(got, [("ack", None), ("error", "cancelled")], "B's cancel did not take effect the moment it was picked up")
        self.assertLess(c_acks, 3, "B's terminal state was queued behind C's 3 acks")
        self.assertEqual([x for x in helpers.read_fake_log()[turns:] if x.get("turn") and x.get("text") == "qb-乙"], [])

    def test_a_cancel_for_a_job_the_bridge_never_saw_does_nothing(self):
        """fix1 addendum (maintainer's ruling): "a cancel that arrived early" only recognizes an id that has already
        been queued in the channel. Push cancel X, then push job X (an abnormal order: the dispatch stream is
        ordered) ⇒ job X still runs to completion (`done`) -- honoring it would mean silently cancelling a job no one
        asked to cancel. ⭐ Also pins down that two tables get cleaned up properly (never a cancel left as a leftover
        row). The negative control = the mutation N-wide (removing the "already queued in the channel" half of the
        condition)."""
        self.b.start_remote()
        self.assertTrue(self.d.wait(lambda: self.d.connects >= 1))
        self.d.push("cancel", {"job_id": "early-x"})
        self.d.push("job", job("early-x", "甲"))
        self.assertTrue(self.d.wait(self.terminal("early-x")))
        self.assertEqual([e["event"] for e in self.events("early-x")][-1], "done", self.events("early-x"))
        self.assertEqual((self.b.remote._early, self.b.remote._queued), (set(), {}))

    def test_a_cancel_for_a_queued_job_is_not_held_up_by_the_acks_queued_behind_it(self):
        self.queued_cancel("hang")

    def test_a_cancel_for_a_queued_job_is_not_lost_when_the_result_channel_is_only_slow(self):
        self.queued_cancel("ok")

    def test_a_duplicate_that_arrives_while_the_originals_ack_is_failing_runs_as_the_job(self):
        """Review M3 (a concurrency cell newly introduced by 15b): a second copy arrives while the original's ack is
        still hung. The original's ack fails (never recorded as "seen") ⇒ the second copy still runs as a new job,
        getting an ordinary `seq=0` ack (never a lying dup: that job was never actually delivered). The mutation
        R-dup-seen-first (recording "seen" before sending the ack) goes red on this case.
        ⚠️ scale: the delivery timeout is turned down to 1 second, and the original's ack times out on all 3 tries
          first (about 4.5 seconds)."""
        held = [0]

        def hold(body):
            if body.get("job_id") == "dx" and body.get("event") == "ack" and held[0] < 3:
                held[0] += 1
                return True
            return False

        with mock.patch.object(scv, "POST_TIMEOUT_S", 1.0):
            self.d.hold_result, self.d.hold_max = hold, 60.0
            self.b.start_remote()
            turns = len([x for x in helpers.read_fake_log() if x.get("turn")])
            self.d.push("job", job("dx", "dx-甲"))
            self.assertTrue(self.d.wait(lambda: self.events("dx")), "fixture precondition: the original's ack is really hung")
            self.d.push("job", job("dx", "dx-甲"))
            # wait for either "a terminal state" or "a dup": waiting only for the former would go red as a timeout
            #   when broken, never red on the judgment
            self.assertTrue(self.d.wait(lambda: self.terminal("dx")() or any(e.get("dup") for e in self.events("dx")), timeout=30))
            self.b.remote.stop()
            self.d.release()
        self.assertEqual([e for e in self.events("dx") if e.get("dup")], [], "the original's ack never got delivered, yet the second copy got a dup ack")
        self.assertEqual(self.events("dx")[-1]["event"], "done")
        self.assertEqual(self.b.remote._queued, {}, "the bookkeeping for the failed-ack copy's \"queued\" state was not cleared")
        # ⭐ count by this job's own body text (never count the whole window: the fake CLI's log is shared by the
        #   whole module, and a late turn from an earlier test can mix in)
        self.assertEqual(len([x for x in helpers.read_fake_log()[turns:] if x.get("turn") and x.get("text") == "dx-甲"]), 1)

    def test_a_duplicate_that_arrives_while_the_originals_ack_succeeds_gets_a_dup_ack(self):
        """Zero-input control (the same shape, but the original's ack succeeds after being hung for 2 seconds): the
        second copy gets a `seq=-1` dup ack and never runs again; the CLI runs exactly one turn."""
        self.d.hold_result, self.d.hold_max = (lambda body: body.get("job_id") == "dy" and body.get("event") == "ack"
                                               and not body.get("dup")), 2.0
        self.b.start_remote()
        turns = len([x for x in helpers.read_fake_log() if x.get("turn")])
        self.d.push("job", job("dy", "dy-甲"))
        self.assertTrue(self.d.wait(lambda: self.events("dy")))
        self.d.push("job", job("dy", "dy-甲"))
        self.assertTrue(self.d.wait(lambda: self.terminal("dy")() and any(e.get("dup") for e in self.events("dy")), timeout=30))
        self.assertEqual([e["seq"] for e in self.events("dy") if e.get("dup")], [-1])
        # the moment the dup ack reaches the dispatcher is when that entry's bookkeeping is cleared inside `_admit`'s
        #   shell ⇒ wait for it (never judge on the spot: that would race the channel thread)
        self.assertTrue(self.d.wait(lambda: self.b.remote._queued == {}, timeout=5),
                        "the bookkeeping for the duplicate's \"queued\" state was not cleared (a row is left for every id that got a dup, the table only ever grows): %r" % self.b.remote._queued)
        self.assertEqual(len([x for x in helpers.read_fake_log()[turns:] if x.get("turn") and x.get("text") == "dy-甲"]), 1)

    def test_a_close_while_the_job_is_starting_its_cli_leaves_no_session(self):
        """Maintainer's ruling (fix2 addendum 1, present since BASE): a job is starting its CLI right at the moment
        close arrives, and the new session is not yet registered ⇒ it used to be unreachable, so that turn ran to
        completion and the long-lived process was left behind. ⭐ Now, right after registering, it checks again by
        event sequence number ⇒ killed on the spot, terminal state `cancelled`. Fixture: the fake codex, once
        started, deliberately takes 1 second before answering the handshake (the driver's `__init__` keeps waiting,
        the session is not yet registered); push close right after `started` arrives (the slot is claimed, about to
        start the CLI) ⇒ close is guaranteed to land inside that 1 second.
        Judgment: terminal state `cancelled`, the session table has none of it, the registry has no leftover
        process."""
        os.environ["FAKE_START_DELAY"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_START_DELAY", None)
        self.b.start_remote()
        self.d.push("job", job("st-cli", "甲", session="room-st", model="codex/gpt-6-luna"))
        self.assertTrue(self.d.wait(lambda: any(e["event"] == "started" for e in self.events("st-cli"))))
        self.d.push("close_session", {"session": "room-st"})
        self.assertTrue(self.d.wait(self.terminal("st-cli"), timeout=30))
        self.assertTrue(self.d.wait(lambda: self.b.close_state(rsid("room-st")) == "closed", timeout=30))
        last = self.events("st-cli")[-1]
        self.assertEqual((last["event"], (last.get("error") or {}).get("type")), ("error", "cancelled"), last)
        self.assertNotIn(rsid("room-st"), self.b.sessions._sessions, "close landed inside the CLI-starting stretch, and the session was still left behind")
        self.assertTrue(self.d.wait(lambda: self.b.sessions.snapshot()["children"] == [], timeout=30), "the process that was started was not reaped")

    def test_a_close_while_the_job_waits_for_a_slot_starts_no_cli(self):
        """Review 2, O-g: close lands while the job is waiting for a slot / waiting for the turn lock ⇒ a CLI used to
        get started anyway (codex starting a session even connects to the server once), and only the check right
        after registering would kill it. ⭐ Now there is a check before `make_driver` too. Fixture: `max_concurrent=1`,
        JA (trickle, never winds down on its own) occupying the slot; push close while J1 is queued for the slot,
        wait for it to finish closing, then cancel JA to free the slot. Judgment: J1's terminal state is `cancelled`;
        the fake CLI's log has no startup for J1 at all (identified by J1's own unique system)."""
        self.b.start_remote()
        mark = len(helpers.read_fake_log())
        os.environ["FAKE_MODE"] = "trickle"
        self.d.push("job", job("og-a", "甲"))
        self.assertTrue(self.d.wait(lambda: any(e["event"] == "chunk" for e in self.events("og-a")), timeout=30))
        os.environ["FAKE_MODE"] = "ok"
        self.d.push("job", job("og-1", "乙", session="room-og", system="SYS-og-1"))
        self.assertTrue(self.d.wait(lambda: self.b.sessions.counts()["queued"] == 1), "fixture precondition: J1 is queued for the slot")
        self.d.push("close_session", {"session": "room-og"})
        self.assertTrue(self.d.wait(lambda: self.b.close_state(rsid("room-og")) == "closed"))
        self.d.push("cancel", {"job_id": "og-a"})
        self.assertTrue(self.d.wait(self.terminal("og-1"), timeout=30))
        last = self.events("og-1")[-1]
        self.assertEqual((last["event"], (last.get("error") or {}).get("type")), ("error", "cancelled"), last)
        self.assertEqual([x for x in helpers.fake_log_since(mark) if "SYS-og-1" in str(x.get("system"))], [],
                         "a CLI was still started for it after close")

    def test_a_close_that_overtakes_a_queued_job_cancels_it_instead_of_rebuilding(self):
        """Review I2 (probe `close`): the same session's `close_session`, handled on the spot on the stream-reading
        thread, can get ahead of a job still queued in the channel ⇒ that job used to rebuild from scratch: either a
        retryable fake `crashed` (its original words carrying the local path) or rebuilding the session that was
        already closed (before the fix: 4/5 + 1/5). ⭐ Now the judgment of "the session was closed while it was
        queued" is made by event sequence number on the stream-reading thread (fix1 used arrival time, fix2 in
        review N-I2 switched to the sequence number) ⇒ terminal state `cancelled`, and the session table has none of
        it. ⭐ Each job's ack is hung for 0.5 seconds: this makes the cell "it is still queued by the time close
        arrives" deterministic (it often happens even without hanging on the loopback, but that is a gamble). Run
        three rounds."""
        self.b.start_remote()
        for i in range(3):
            sid, j0, j1 = "cls-%d" % i, "cls0-%d" % i, "cls1-%d" % i
            with self.subTest(round=i):
                self.d.hold_result = None
                self.d.push("job", job(j0, "甲", session=sid))
                self.assertTrue(self.d.wait(self.terminal(j0)))
                ans = self.events(j0)[-1]["text"]
                self.d.hold_result, self.d.hold_max = (lambda body, j=j1: body.get("job_id") == j and body.get("event") == "ack"), 0.5
                msgs = [{"role": "user", "content": "甲"}, {"role": "assistant", "content": ans}, {"role": "user", "content": "乙"}]
                self.d.push("job", job(j1, "乙", session=sid, messages=msgs))
                self.d.push("close_session", {"session": sid})
                self.assertTrue(self.d.wait(self.terminal(j1), timeout=30))
                self.assertTrue(self.d.wait(lambda: self.b.close_state(rsid(sid)) == "closed", timeout=30))
                last = self.events(j1)[-1]
                self.assertEqual((last["event"], (last.get("error") or {}).get("type")), ("error", "cancelled"), last)
                self.assertNotIn(rsid(sid), self.b.sessions._sessions, "the closed session was rebuilt anyway")

    def dup_then_cancel(self, drop_second):
        """The two shapes of review N-I1. ⭐ Judgment (the correct value): DX's delivery is a handful of acks (the hung
        ones count too, since the dispatcher received them) plus one `cancelled`, never `started`."""
        evs = [(e["event"], (e.get("error") or {}).get("type")) for e in self.events("DX")]
        self.assertEqual([x for x in evs if x[0] != "ack"], [("error", "cancelled")], evs)

    def test_a_cancel_survives_a_sibling_copy_that_was_not_taken(self):
        """Review N-I1 (probe `dupcancel`): push job DX, job DX, cancel DX in order; the original's ack times out on
        all 3 tries first ⇒ the original was never picked up, and the second copy is treated as a new job. The one
        never picked up used to void the "cancel that arrived early" on its way out ⇒ the second copy ran anyway,
        the cancel falling flat in silence (this is exactly the shape of the dispatcher resending per the protocol
        and also cancelling).
        ⚠️ scale: the delivery timeout is turned down to 1 second. In ok mode: if the cancel falls flat, DX will
          finish answering (red on `done`, never a timeout)."""
        held = [0]

        def hold(body):
            if body.get("job_id") == "DX" and body.get("event") == "ack" and held[0] < 3:
                held[0] += 1
                return True
            return False

        with mock.patch.object(scv, "POST_TIMEOUT_S", 1.0):
            self.d.hold_result, self.d.hold_max = hold, 60.0
            self.b.start_remote()
            leg = self.b.remote
            self.d.push("job", job("DX", "甲"))
            self.assertTrue(self.d.wait(lambda: self.events("DX")), "fixture precondition: the original's ack is really hung")
            self.d.push("job", job("DX", "甲"))
            self.assertTrue(self.d.wait(lambda: leg._queued.get("DX") == 2), "fixture precondition: the same id has two copies queued")
            self.d.push("cancel", {"job_id": "DX"})
            self.assertTrue(self.d.wait(lambda: "DX" in leg._early), "fixture precondition: the cancel was recorded")
            self.assertTrue(self.d.wait(lambda: self.terminal("DX")(), timeout=20))
            leg.stop()
            self.d.release()
        self.dup_then_cancel(False)

    def test_a_cancel_survives_a_sibling_copy_dropped_by_a_full_queue(self):
        """Review N-I1 (probe `fullcancel`): F0's hung ack blocks the channel; push X, F1..F14 (15 items queued),
        cancel X, F15 (queue full), then push X again (dropped) ⇒ the copy that got dropped used to void the "cancel
        that arrived early" on its way out too, and X ran anyway. ⭐ Judgment same as above: once X is released, it is
        ack + `cancelled`."""
        self.d.hold_result, self.d.hold_max = (lambda body: body.get("event") == "ack" and body.get("job_id") == "F0"), 30.0
        self.b.start_remote()
        leg = self.b.remote
        self.d.push("job", job("F0", "甲"))
        self.assertTrue(self.d.wait(lambda: self.events("F0")))
        self.d.push("job", job("DX", "乙"))
        for i in range(1, 15):
            self.d.push("job", job("F%d" % i, "甲"))
        self.assertTrue(self.d.wait(lambda: len(leg._outbox._q.queue) == 15))
        self.d.push("cancel", {"job_id": "DX"})
        self.d.push("job", job("F15", "甲"))
        self.assertTrue(self.d.wait(lambda: len(leg._outbox._q.queue) == 16))
        self.d.push("job", job("DX", "乙"))
        self.assertTrue(self.d.wait(lambda: leg._full >= 1), "fixture precondition: the second DX hit the full queue and was dropped")
        self.d.release()
        self.assertTrue(self.d.wait(lambda: self.terminal("DX")(), timeout=20))
        self.dup_then_cancel(True)

    def run_pair(self, rounds, close_first):
        """Review N-I2: the same session's close and job pushed back-to-back (gap 0, often written out in the same
        batch) ⇒ returns [(terminal state, category, rebuilt, whether that session is still alive)]."""
        out = []
        for i in range(rounds):
            sid, j0, j1 = "pr-%s-%d" % (close_first, i), "pr0-%s-%d" % (close_first, i), "pr1-%s-%d" % (close_first, i)
            self.d.hold_result = None
            self.d.push("job", job(j0, "甲", session=sid))
            self.assertTrue(self.d.wait(self.terminal(j0)))
            msgs = [{"role": "user", "content": "甲"}, {"role": "assistant", "content": self.events(j0)[-1]["text"]},
                    {"role": "user", "content": "乙"}]
            if not close_first:   # job first: its ack is hung for 0.3 seconds ⇒ by the time close arrives it is guaranteed to still be queued (never gambling on the same write batch)
                self.d.hold_result, self.d.hold_max = (lambda body, j=j1: body.get("job_id") == j and body.get("event") == "ack"), 0.3
                self.d.push("job", job(j1, "乙", session=sid, messages=msgs))
            self.d.push("close_session", {"session": sid})
            if close_first:
                self.d.push("job", job(j1, "乙", session=sid, messages=msgs))
            self.assertTrue(self.d.wait(self.terminal(j1), timeout=30))
            self.assertTrue(self.d.wait(lambda: self.b.close_state(rsid(sid)) == "closed", timeout=30))
            last = self.events(j1)[-1]
            out.append((last["event"], (last.get("error") or {}).get("type"), bool(last.get("rebuilt")),
                        rsid(sid) in self.b.sessions._sessions))
            if rsid(sid) in self.b.sessions._sessions:
                self.b.sessions.close_session(rsid(sid))
        return out

    def test_close_then_job_back_to_back_is_a_rebuild_every_time(self):
        """Review N-I2: "close first, then ask" used to be judged by wall-clock ordering, so close and job landing on
        the same clock tick (win32 ticks about every 1 ms) got judged as "closed while queued" ⇒ a non-retryable fake
        `cancelled` (8 out of 20 rounds on this machine). ⭐ Now it is judged by event sequence number on the
        stream-reading thread: every one of 20 rounds is `done` + `rebuilt`, session alive."""
        self.b.start_remote()
        self.assertEqual(set(self.run_pair(20, True)), {("done", None, True, True)})

    def test_job_then_close_back_to_back_is_cancelled_every_time(self):
        """The reverse order (review I2's cell; the re-review points out it too used to pass on wall-clock ties ⇒
        never just swap `>=` for `>`): every one of 20 rounds is `cancelled`, and the session table has none of
        it."""
        self.b.start_remote()
        self.assertEqual(set(self.run_pair(20, False)), {("error", "cancelled", False, False)})

    def test_a_second_close_also_takes_the_session_a_job_rebuilt_in_between(self):
        """Review M-b (probe `close2`): close#1 → J1 (same session, rebuilt from scratch) → close#2, with close#1
        still closing (the old process deliberately takes 1.5 seconds to wind down) ⇒ close#2 used to get dropped
        entirely as "already closing", and the session J1 rebuilt stayed alive even after the dispatcher had closed
        it twice. ⭐ Now the second call also picks up the entry then in the table and refreshes the record to point to
        it, and that closing thread does one more pass after finishing what it has in hand (never starting another
        thread). Two cells: whether J1 has already finished answering by the time close#2 arrives, or is still
        answering (trickle) ⇒ either way that session is gone; the one still answering gets terminal state
        `cancelled` (closed while in flight)."""
        self.b.start_remote()
        for mode in ("ok", "trickle"):
            with self.subTest(j1=mode):
                sid = "c2-" + mode
                os.environ["FAKE_MODE"], os.environ["FAKE_EXIT_DELAY"] = "ok", "1.5"
                self.addCleanup(os.environ.pop, "FAKE_EXIT_DELAY", None)
                self.d.push("job", job(sid + "-0", "甲", session=sid))
                self.assertTrue(self.d.wait(self.terminal(sid + "-0")))
                os.environ.pop("FAKE_EXIT_DELAY")
                os.environ["FAKE_MODE"] = mode
                msgs = [{"role": "user", "content": "甲"}, {"role": "assistant", "content": self.events(sid + "-0")[-1]["text"]},
                        {"role": "user", "content": "乙"}]
                self.d.push("close_session", {"session": sid})
                self.d.push("job", job(sid + "-1", "乙", session=sid, messages=msgs))
                self.assertTrue(self.d.wait(lambda: (self.terminal(sid + "-1")() if mode == "ok" else
                                                     any(e["event"] == "chunk" for e in self.events(sid + "-1"))), timeout=30))
                self.assertEqual(self.b.close_state(rsid(sid)), "closing", "fixture precondition: close#1 is still closing when close#2 arrives")
                self.d.push("close_session", {"session": sid})
                self.assertTrue(self.d.wait(self.terminal(sid + "-1"), timeout=30), "J1 had no terminal state within 30 seconds (close#2 did not close it: trickle never winds down on its own)")
                self.assertTrue(self.d.wait(lambda: self.b.close_state(rsid(sid)) == "closed", timeout=30), "did not finish closing within 30 seconds")
                last = self.events(sid + "-1")[-1]
                self.assertEqual((rsid(sid) in self.b.sessions._sessions, getattr(self.b.sessions, "_detached", {})),
                                 (False, {}), "the dispatcher closed it twice, and the session J1 rebuilt is still alive")
                self.assertEqual((last["event"], (last.get("error") or {}).get("type")),
                                 ("done", None) if mode == "ok" else ("error", "cancelled"), last)
        os.environ["FAKE_MODE"] = "ok"

    def test_a_full_queue_drops_loudly_and_counts_what_it_dropped(self):
        """A full queue means dropping -- ⭐ but it must be loud, never spam the log: a stretch of fullness is loud only
        for the first item, and once there is room again it is loud about the total in one line. ⭐ The judgment is the
        correct value: the extra 2 pushed get not a single delivery between them; the log has the "channel is full"
        line exactly once, and the total-count line says 2. Also pins down the resource shape: one leg's outbound
        channel has only one thread."""
        self.b.start_remote()
        leg = self.b.remote
        cap = leg._outbox._q.maxsize
        self.assertEqual(cap, 4 * leg.max_inflight)
        self.d.hold_result, self.d.hold_max = (lambda body: body.get("event") == "ack"), 30.0
        self.d.push("job", job("q-0", "甲"))
        self.assertTrue(self.d.wait(lambda: self.events("q-0")), "fixture precondition: the first one's ack is really hung (the channel thread is stuck on it)")

        def burst():
            for i in range(1, cap + 3):
                self.d.push("job", job("q-%d" % i, "甲"))
            self.assertTrue(self.d.wait(lambda: len(leg._outbox._q.queue) == cap, timeout=10), "the queue never filled up")
            # ⭐ wait for the extra 2 pushes to really get dropped before reading the log (never the old sleep 0.5): the
            #   line is loud when the 1st is dropped, and by the time `_full` reaches 2 it is guaranteed to have
            #   already landed on disk (fix1 addendum 2, same shape)
            self.assertTrue(self.d.wait(lambda: leg._full == 2, timeout=10), "the extra 2 pushes were not dropped (_full=%d)" % leg._full)
        lines = log_lines_during(burst)
        self.assertEqual(len(outbox_threads(leg)), 1, "the outbound channel has more than one thread")
        self.assertEqual(len([x for x in lines if "the send channel is full" in x]), 1, lines[-3:])
        self.d.release()
        dropped = ["q-%d" % (cap + 1), "q-%d" % (cap + 2)]
        lines = log_lines_during(lambda: (self.d.push("job", job("q-fence", "甲")),
                                          self.assertTrue(self.d.wait(self.terminal("q-fence"), timeout=30))))
        self.assertEqual([x for x in lines if "the send channel has room again" in x and "2 item(s) were dropped in total" in x].__len__(), 1, lines[-3:])
        self.assertEqual([r for r in self.d.results if r["job_id"] in dropped], [])
        self.assertTrue(self.events("q-%d" % cap), "zero-input control: the last one that made it into the queue still got its delivery")

    def test_stop_neither_retries_nor_leaves_the_send_thread_behind(self):
        """Stopping the bridge ⇒ (1) whatever is queued in the channel is never processed (loud about how many were
        dropped) (2) whatever is currently being processed never tries the next call (review 5, outside scope, ⑤:
        `_post`, once woken up by the stop flag, used to keep dialing anyway, up to `POST_TIMEOUT_S` per call)
        (3) the sending thread exits within one delivery's timeout. ⭐ The delivery timeout is turned down to 1 second
        (30 seconds in production: same relationship, different scale); the judgment is how many more calls the
        dispatcher received after the bridge stopped (the correct value: 0) plus whether the thread is still
        there."""
        with mock.patch.object(scv, "POST_TIMEOUT_S", 1.0):
            self.b.start_remote()
            leg = self.b.remote
            self.d.hold_result, self.d.hold_max = (lambda body: body.get("event") == "ack"), 30.0
            for i in range(3):
                self.d.push("job", job("st-%d" % i, "甲"))
            self.assertTrue(self.d.wait(lambda: self.events("st-0") and len(leg._outbox._q.queue) == 2),
                            "fixture precondition: the first one's ack is hung, and the other two are queued in the channel")
            before = len(self.d.results)
            lines = log_lines_during(lambda: (leg.stop(), time.sleep(3.0)))
        self.assertEqual(self.d.results[before:], [], "the dispatcher received more deliveries after the bridge stopped")
        self.assertEqual(outbox_threads(leg), [], "the sending thread was still there 3 seconds after stopping the bridge (delivery timeout 1 second)")
        self.assertEqual(len([x for x in lines if "2 item(s) in the outbound channel were never done" in x]), 1, lines[-3:])

    def test_a_stopped_channel_is_not_reported_as_full(self):
        """Review M4(b): `put` used to have one False cover two different things ⇒ handing over one more item after
        stopping the bridge got logged as "channel is full (the dispatcher's /bridge/result is slow or hung)".
        ⭐ Now they are reported separately: stopped is reported as stopped, and never counted into "how many were
        dropped while full"."""
        self.b.start_remote()
        leg = self.b.remote
        leg._outbox.stop()
        lines = log_lines_during(lambda: leg._hand("一条 job", lambda: None))
        self.assertEqual((len([x for x in lines if "the bridge is stopping, the send channel is no longer accepting" in x]), [x for x in lines if "is full" in x], leg._full),
                         (1, [], 0), lines)

    def test_a_job_whose_ack_lands_after_stop_is_not_started(self):
        """Caught by the resource census (task-15b-probe.py census): the bridge is stopped while an ack is in flight,
        and the response arrives afterward ⇒ it used to still claim a slot, start a thread, start a CLI -- while
        `Bridge.stop()`'s `close_all` already ran, leaving the process it started with no one to reap it. ⭐ Judgment:
        it never `started`, the fake CLI ran not a single turn, all the slots came back, and one line was loud."""
        self.b.start_remote()
        leg = self.b.remote
        self.d.hold_result, self.d.hold_max = (lambda body: body.get("event") == "ack"), 30.0
        self.d.push("job", job("late-0", "甲"))
        self.assertTrue(self.d.wait(lambda: self.events("late-0")), "fixture precondition: the ack is really hung")
        turns = len([x for x in helpers.read_fake_log() if x.get("turn")])

        def stop_then_release():
            leg.stop()
            self.d.release()
            self.assertTrue(self.d.wait(lambda: outbox_threads(leg) == [], timeout=10), "the sending thread did not exit after being released")
            time.sleep(1.0)
        lines = log_lines_during(stop_then_release)
        self.assertEqual([e["event"] for e in self.events("late-0")], ["ack"])
        self.assertEqual(len([x for x in helpers.read_fake_log() if x.get("turn")]), turns)
        self.assertEqual(len([x for x in lines if "the bridge stopped right after" in x]), 1, lines[-3:])
        got = 0
        while leg._inflight.acquire(blocking=False):
            got += 1
        self.assertEqual(got, leg.max_inflight, "the slots were not returned")


class Cleanup(Case):
    """🔴 the gate is built to the shape of the bug: the judgment is "after any family of exception inside `_job`,
    the slot returns to its original value, `jobs.log` gains exactly one row, and the terminal state is attempted
    exactly once", never "returns 400 when `session` is a list".
    ⚠️ "the terminal state is exactly one" was written as a promise in the previous round, but was not actually
      guaranteed by the structure (if the handler raises again it is 0, review M-A); now the terminal state has been
      moved into the same `finally`, pinned down by `test_a_handler_that_throws_again_…`. ⚠️ it is still only
      attempted once: if delivery fails, it fails (`post` has already been loud about it), never read this as "the
      dispatcher is guaranteed to receive it".

    The shape of that bug is structural: the wind-down used to be split off the same `try` statement's `finally`
    into a second `try` ⇒ anything raised again inside either `except` handler propagates straight out of `_job`,
    and the second `try` never even starts ⇒ the slot is never returned, no terminal state is sent, `jobs.log` gets
    zero rows, and `bridge.log` has not a single word (only a traceback on stderr). With the default `max_inflight=16`
    ⇒ 16 malformed jobs are enough to permanently mute this leg for good, and the reason given externally is the lie
    "already at the in-flight cap". ⇒ pinning down only the `session` cell would let the next new raise point tear
    it apart all over again."""

    cfg = {"max_concurrent": 1}     # ⇒ max_inflight=4, exhausting the slots is visible

    def free_slots(self):
        leg, got = self.b.remote, 0
        while leg._inflight.acquire(blocking=False):
            got += 1
        for _ in range(got):
            leg._inflight.release()
        return got

    # (name, how it breaks, expected category) ⭐ one call for each of four families: an unhashable input value (the
    #   C-1 cell) / an ordinary bad_request / a failure on the CLI side / a bug in the bridge itself (inject an
    #   unrecognized exception).
    # 🔴 the C-1 cell must be a non-empty unhashable value (`["x"]`), never `[]`: `closed_during()` has
    #   `get(sid) if sid else None`, and an empty list is falsy, so it never even reaches the dict lookup ⇒ using
    #   `[]` would still run this case green with the fix removed (negative control measured 2026-09-23: removing
    #   the "only for a value that passed the gate" spot ⇒ both cases run green). The fixture happened to route
    #   around the bug.
    WAYS = (("session is unhashable (C-1)", {"session": ["x"]}, "bad_request", None),
            ("the model was never reported", {"model": "nope/nope"}, "bad_request", None),
            ("the CLI says it is not logged in", {}, "auth_required", "auth"),
            ("the bridge's own bug", {}, "unknown", None))

    def test_any_failure_still_returns_the_slot(self):
        self.b.start_remote()
        self.assertTrue(self.d.wait(lambda: self.d.connects >= 1))
        before = self.free_slots()
        self.assertEqual(before, 4)
        for i, (why, over, want, mode) in enumerate(self.WAYS):
            with self.subTest(why=why):
                os.environ["FAKE_MODE"] = mode or "ok"
                jid = "cl%d" % i
                ctx = (mock.patch.object(scv, "remote_request", side_effect=KeyError("injected bug"))
                       if want == "unknown" else contextlib.nullcontext())
                with ctx:
                    self.d.push("job", job(jid, "甲", **over))
                    self.assertTrue(self.d.wait(self.terminal(jid), timeout=30), why)
                self.assertEqual(self.events(jid)[-1]["error"]["type"], want)
                # ⭐ count by `job_id`, never by "total row count + 1": a late row from an earlier case would be
                #   mistaken for this one (supplementary review 2, M-3, general audit), and once the table fills up
                #   at 99 rows `tail(99)` stops growing, so "+1" would never be reached (a false red).
                self.assertTrue(self.d.wait(lambda: self.rows_of(jid), timeout=10), "jobs.log is missing a row ⇒ the wind-down stretch did not run")
                self.assertEqual(len(self.rows_of(jid)), 1, self.rows_of(jid))
                self.assertEqual(self.free_slots(), before, "the slot was not returned ⇒ a few more of these and this leg goes mute")

    def test_a_handler_that_throws_again_still_sends_one_terminal_with_the_real_reason(self):
        """Review M-A: when the handler itself raises again (here injecting `closed_during` raising `RuntimeError`),
        the terminal state used to be 0, and `jobs.log`'s klass stayed at its initial value `ok` (recording a
        failure as a success). ⭐ Three judgments: the terminal state is exactly one / it states the real reason (the
        CLI is not logged in, never covered up by the second exception) / `jobs.log` records the same thing.
        ⚠️ since 15b fix1, `_job` also asks `closed_during` once before starting the CLI ("the session was closed
        while queued") ⇒ the injection only hits the second call (the one inside the handler): if it raised on the
        first call, the CLI would never start, the real reason would never appear, and this case would be testing
        something else entirely."""
        os.environ["FAKE_MODE"] = "auth"
        self.b.start_remote()
        with mock.patch.object(self.b, "closed_during", side_effect=[False, RuntimeError("injected: the rewrite step blew up again")]):
            self.d.push("job", job("ma1", "甲"))
            self.assertTrue(self.d.wait(self.terminal("ma1"), timeout=30))
            self.assertTrue(self.d.wait(lambda: self.b.joblog.tail(1) and self.b.joblog.tail(1)[0]["job_id"] == "ma1",
                                        timeout=30))
        terms = [e for e in self.events("ma1") if e["event"] in ("done", "error")]
        self.assertEqual([e["error"]["type"] for e in terms], ["auth_required"])
        self.assertEqual(self.b.joblog.tail(1)[0]["klass"], "auth_required")

    def test_one_bad_job_does_not_poison_the_next_good_one(self):
        """The zero-input-control half: the ordinary job after the bad ones must still run to completion, never
        receive a lying local_rate_limit. ⚠️ push `max_inflight` bad ones (never just one) -- leaking one slot and
        leaking all four look identical if only one is checked."""
        self.b.start_remote()
        for i in range(4):
            self.d.push("job", job("poison%d" % i, "甲", session=["x"]))   # never use `[]`, the reason is in WAYS
            self.assertTrue(self.d.wait(self.terminal("poison%d" % i), timeout=30))
        self.d.push("job", job("after-poison", "甲"))
        self.assertTrue(self.d.wait(self.terminal("after-poison"), timeout=30))
        self.assertEqual(self.events("after-poison")[-1]["event"], "done")


def dial_bound(window, min_s):
    """Computed by the backoff sequence: on consecutive bad redials, the k-th gap is approximately
    `min_s + 2^(k-1)` (capped at `MAX_BACKOFF_S`) ⇒ at most how many connections (including the first) within
    `window` seconds counting from the first connection, plus a margin of 1.
    ⭐ The judgment follows the sequence, never an arbitrary rule of thumb like "fewer than 3 per second": review
      measured that removing exponential backoff entirely and keeping only the `MIN_REDIAL_S` wait (once every 2
      seconds) still ran both hangup/stuck cases green, while PROTOCOL promises the dispatcher that the gaps get
      sparser and sparser (M-B)."""
    t, n, b = 0.0, 1, 1.0
    while True:
        t += min_s + b
        b = min(b * 2, scv.MAX_BACKOFF_S)
        if t > window:
            return n + 1
        n += 1


def dial_floor(window, life, gap=1.0):
    """The opposite of `dial_bound`: when every pipe is good (backoff always returns to 1), each round = living
    `life` seconds + waiting `gap` seconds ⇒ at least how many connections (including the first) within `window`
    seconds counting from the first connection. Each round gets a margin of 0.25s (hello/dialing/log-writing
    overhead), minus a margin of 1.
    ⭐ This is a lower bound, never an upper bound: the symptom of that regression is "a good pipe backed off like a
      bad one" = connecting too few times, which an upper bound could never catch.
    ⭐ `gap` is not the same on the two paths (never borrow one path's number for the other: borrowing too generously
      would loosen the lower bound until it can no longer tell "fixed" apart from "removed"): ending on an
      exception ⇒ the outer `except` waits out `backoff` first (1, once a good pipe has been reset) before hello ⇒
      `gap=1.0` (the default); returning normally ⇒ `_pace` only tops up whatever is left of `MIN_REDIAL_S` ⇒ a pipe
      that lived past 1 second gets `gap=0`."""
    return int(window // (life + gap + 0.25))


class PacingRulers(unittest.TestCase):
    def test_the_read_timeout_ruler_is_not_blind(self):
        """`died_of_read_timeout` is the ruler behind two of Pacing's fixture preconditions ("every one of them ended
        on a read timeout") ⇒ both ends need verifying: if it were always true, those preconditions would be vacuous.
        The positive case builds a real read-timeout exception in the exact format of `run()`'s own words (never
        hardcode the class name: that would share the same source as the judgment); the negative cases are the two
        other ways of failing that get written into the same line: 500 (`HTTPError`), and a failed connection
        (`URLError`, whose own words also contain "timed out")."""
        a, b = socket.socketpair()
        try:
            a.settimeout(0.01)
            with self.assertRaises(OSError) as got:
                a.recv(1)
        finally:
            a.close()
            b.close()
        real = "⚠️ remote leg dropped, reconnecting in 1s: %s: %s" % (type(got.exception).__name__, got.exception)
        self.assertTrue(died_of_read_timeout(real), real)
        self.assertFalse(died_of_read_timeout("⚠️ remote leg dropped, reconnecting in 1s: HTTPError: HTTP Error 500: Internal Server Error"))
        self.assertFalse(died_of_read_timeout("⚠️ remote leg dropped, reconnecting in 1s: URLError: <urlopen error timed out>"))


class Pacing(Case):
    """🔴 `_stream_once()`'s three paths that return normally (EOF/reached its lifespan/dropped an over-limit event)
    used to redial on the spot; the path where `/bridge/stream` replies non-2xx (the exception path) used to have
    backoff reset by every round's successful hello.
    ⭐ The judgment is "how many connections the dispatcher saw during this stretch", never "that bad event got
      dropped" -- the latter had been running green all along, and that is exactly what let this hole slip by. The
      upper bound is computed by `dial_bound()` following the backoff sequence. 📎 NOTES.md::redial-pacing"""

    WINDOW = 12.0

    def watch(self, patch_min=None, window=None, until=None):
        """Start the bridge -> wait for the first connection -> watch for `window` seconds (default WINDOW), or, with
        `until`, until that condition holds (`window` is then only the ceiling). Returns (the log lines from this
        stretch, the connection count within the window (including the first))."""
        box = {}

        def run():
            self.b.start_remote()
            self.assertTrue(self.d.wait(lambda: self.d.connects >= 1, timeout=10))
            c0 = self.d.connects
            if until is None:
                time.sleep(window or self.WINDOW)
            else:
                self.d.wait(until, timeout=window or self.WINDOW)
            box["n"] = self.d.connects - c0 + 1

        if patch_min is None:
            lines = log_lines_during(run)
        else:
            with mock.patch.object(scv, "MIN_REDIAL_S", patch_min):
                lines = log_lines_during(run)
        return lines, box["n"]

    def test_a_dispatcher_that_hangs_up_is_not_a_hot_loop(self):
        """The dispatcher "closes the stream right after 200" -- this needs no malice, an overloaded or half-dead
        server behaves exactly this way. Measured without throttling: 398.5 times/second (3,188 times in 8 seconds)
        = hammering the peer and any proxy in between as if it were a DDoS source."""
        self.d.hangup = True
        lines, n = self.watch()
        bound = dial_bound(self.WINDOW, scv.MIN_REDIAL_S)
        self.assertLessEqual(n, bound, "within %.0f seconds, connected %d times (at most %d per the backoff sequence)" % (self.WINDOW, n, bound))
        self.assertGreaterEqual(n, 2, "fixture precondition: the window must have redialed at least once, otherwise the line above is vacuous")
        # ⭐ the false-alarm side (never fold this into the case above): this cell has no over-limit event at all ⇒
        #   never let it be blamed on the `id:` ordering -- that would be a diagnosis pointing at the wrong place,
        #   sending the dispatcher's people to comb through a stretch of frame-format code that is not the problem
        self.assertEqual([x for x in lines if "id:" in x and "data:" in x], [], lines[-3:])
        self.assertEqual(len([x for x in lines if "disconnected within a second" in x]), 1, lines[-3:])

    def test_a_stuck_stream_backs_off_and_says_why(self):
        """`id:` comes after `data:`, plus one over-limit event ⇒ we can never skip past it.
        ⭐ Two judgments together: never spin idle (per the sequence's upper bound) plus can state the cause (that
          log line). Neither one alone is enough: pinning down only the log would let it spin at 45 Hz while being
          loud; pinning down only the frequency would let it lock up silently."""
        big = "z" * (scv.STREAM_EVENT_MAX + 4096)
        self.d.push_raw("event: job" + chr(10) + "data: " + big + chr(10) + "id: 600" + chr(10) + chr(10), 0)
        lines, n = self.watch()
        bound = dial_bound(self.WINDOW, scv.MIN_REDIAL_S)
        self.assertLessEqual(n, bound, "within %.0f seconds, connected %d times (at most %d per the backoff sequence)" % (self.WINDOW, n, bound))
        self.assertEqual(len([x for x in lines if "id:" in x and "data:" in x]), 1, lines[-3:])

    def test_a_slow_stuck_stream_is_still_caught(self):
        """🔴 the livelock judgment must never rely on "this pipe lived a short time": on a real network, reading a
        full 256 KiB can take longer than `MIN_REDIAL_S`, in which case the judgment "short and made no progress"
        would never hold even once ⇒ it would stay locked at one call per second forever, never be loud about the
        cause, and no job after it could ever get in. ⭐ Here `MIN_REDIAL_S` is turned down to 0 to simulate "every
        pipe lives long enough" (never actually build a slow network for this: that would depend on how fast the
        machine is, which is a gamble; review reproduced the same cell separately with a dispatcher on a genuinely
        slow network)."""
        big = "z" * (scv.STREAM_EVENT_MAX + 4096)
        self.d.push_raw("event: job" + chr(10) + "data: " + big + chr(10) + "id: 600" + chr(10) + chr(10), 0)
        lines, n = self.watch(patch_min=0.0)
        self.assertEqual(len([x for x in lines if "id:" in x and "data:" in x]), 1, lines[-3:])
        bound = dial_bound(self.WINDOW, 0.0)
        self.assertLessEqual(n, bound, "within %.0f seconds, connected %d times (at most %d per the backoff sequence)" % (self.WINDOW, n, bound))

    def test_a_stream_endpoint_that_errors_while_hello_works_backs_off_too(self):
        """🔴 review I-A: `/bridge/stream` replies 500 while hello keeps working as usual ⇒ every round used to
        succeed at hello first and reset backoff back to 1 ⇒ one hello + one stream + one log line every second,
        never backing off (measured 39 times in 40 seconds). A whole fleet of bridges hammering like this at once =
        a mass surge. ⭐ Judgment: within the window, the total of hello + stream calls has an upper bound per the
        sequence, plus the log can state it is a 500."""
        self.d.stream_status = 500
        box = {}

        def run():
            self.b.start_remote()
            self.assertTrue(self.d.wait(lambda: self.d.connects >= 1, timeout=10))
            h0, c0 = len(self.d.hellos), self.d.connects
            time.sleep(self.WINDOW)
            box["n"] = (len(self.d.hellos) - h0) + (self.d.connects - c0) + 2

        lines = log_lines_during(run)
        bound = 2 * dial_bound(self.WINDOW, 0.0)
        self.assertLessEqual(box["n"], bound, "within %.0f seconds, hello + stream totaled %d times (at most %d per the backoff sequence)"
                             % (self.WINDOW, box["n"], bound))
        why = [x for x in lines if "remote leg dropped" in x]
        self.assertTrue(why and all("500" in x for x in why), lines[-3:])

    LIFE = 0.5   # the three cases below turn the read timeout down to this: a pipe's round of "connect -> push one -> silence -> read timeout" takes only half a second

    def test_a_pipe_that_made_progress_and_then_died_of_an_exception_is_not_backed_off(self):
        """🔴 supplementary review 2, I-1 (a regression the previous round introduced on its own): resetting to zero
        only went through `_pace()`, and `_pace()` is only called when `_stream_once()` returns normally ⇒ a pipe
        that genuinely connected, received an event, and finally ended on an exception (a read timeout, an RST, an
        IncompleteRead -- the most common ways a real network fails) still had its backoff double all the way up to
        60 seconds (review measured 6 times in 40 seconds; before the fix it came back at a constant 1 second, 16
        times).
        ⭐ The judgment is a lower bound (`dial_floor`): the symptom is "connected too few times", which an upper bound
          can never catch. A separate fixture precondition pins down that every one of them really did end on an
          exception (every "remote leg dropped" line in the log is a read timeout), otherwise this case is testing
          the normal-return path instead."""
        self.d.tick, self.d.then = "ping", "silent"
        with mock.patch.object(scv, "STREAM_READ_TIMEOUT_S", self.LIFE):
            lines, n = self.watch(window=8.0)
        floor = dial_floor(8.0, self.LIFE)
        self.assertGreaterEqual(n, floor, "connected only %d times in 8 seconds (every pipe made progress, should be at least %d) ⇒ a good pipe was backed off like a bad one"
                                % (n, floor))
        why = [x for x in lines if "remote leg dropped" in x]
        self.assertTrue(len(why) >= 2 and all(died_of_read_timeout(x) for x in why), lines[-3:])
        # ⭐ while we are at it, this also serves as a positive control for the `no_progress` ruler: every pipe in this
        #   case made progress ⇒ it must say "yes" (otherwise the preconditions of the next two cases are vacuous)
        self.assertFalse(self.no_progress(), self.d.stream_headers[-2:])

    def test_a_pipe_that_died_of_an_exception_without_progress_is_still_backed_off(self):
        """Zero-input control (the other side of the previous case): also ends on a read timeout, but received not a
        single event and never lived even 1 second ⇒ still backs off. Without this case, the previous one cannot be
        told apart from "any exception clears backoff to zero" -- which would turn the stream-500 cell back into one
        call per second."""
        self.d.then = "silent"
        with mock.patch.object(scv, "STREAM_READ_TIMEOUT_S", self.LIFE):
            lines, n = self.watch(window=8.0)
        bound = dial_bound(8.0, self.LIFE)
        self.assertLessEqual(n, bound, "connected %d times in 8 seconds (at most %d per the backoff sequence)" % (n, bound))
        self.assertGreaterEqual(n, 2, "fixture precondition: the window must have redialed at least once, otherwise the line above is vacuous")

    def test_a_slow_error_is_not_a_connection(self):
        """🔴 "lived past 1 second" must never count one that never even connected: the dispatcher replies 500
        slowly (the shape of an overloaded origin, or a CDN timing out waiting on its origin), so every pipe "lives
        past" 1 second counting from the dial, and judging purely by lifespan would call every one of them good,
        with backoff always returning to 1 (`_sick`'s `opened`). ⭐ Same judgment as
        `test_a_stream_endpoint_that_errors_…`: an upper bound per the sequence; counting only stream calls
        (the hello half is already pinned down by another case)."""
        self.d.stream_status, self.d.stream_delay = 500, 1.05
        lines, n = self.watch(window=14.0)
        bound = dial_bound(14.0, self.d.stream_delay)
        self.assertLessEqual(n, bound, "dialed stream %d times in 14 seconds (at most %d per the backoff sequence)" % (n, bound))
        self.assertGreaterEqual(n, 2, "fixture precondition: the window must have redialed at least once")
        why = [x for x in lines if "remote leg dropped" in x]
        self.assertTrue(why and all("500" in x for x in why), lines[-3:])

    SLOW_HEAD = 1.05   # the two cases below: the dispatcher waits this long before replying with the 200 head
                       #   (> `MIN_REDIAL_S` ⇒ counting from the dial, every pipe would have "lived past" it)

    def slow_head_gaps(self):
        """Start the bridge, watch until the 4th stream (at most 30 seconds) ⇒ (log lines, the first three gaps, each with `SLOW_HEAD` seconds
        subtracted for the head reply and rounded to whole seconds).
        ⭐ This judges backoff itself: each round = waiting for the head reply + waiting out backoff (the
          millisecond-scale dial/hello overhead is rounded away). Measured on the dispatcher's side
          (`stream_times`)."""
        # until the 4th stream, never a fixed window: the 4th dial comes ~10.2 s in (1.05 + 1, 2, 4), and a fixed
        #   11 s window lost it on a slow CI runner (the first CI run saw gaps [1, 2])
        lines, _n = self.watch(window=30.0, until=lambda: len(self.d.stream_times) >= 4)
        t = self.d.stream_times
        return lines, [round(b - a - self.SLOW_HEAD) for a, b in zip(t, t[1:])][:3]

    def test_a_dispatcher_that_answers_200_slowly_then_hangs_up_is_backed_off(self):
        """🔴 review 3, M-2: "lived past 1 second" used to be counted from the dial ⇒ when the dispatcher replies 200
        slowly and then closes the stream right away, every pipe "lived past" it and never backed off (measured
        before the fix: the gap held constant at about 1.23 seconds, 12 times in 14 seconds, not a single loud line).
        Now it is counted from getting the 2xx.
        ⭐ Assert the doubling gaps themselves (the correct value: 1, 2, 4 seconds), never just "it is not 1.2 seconds";
          also pin down that line stating the cause is loud exactly once (the 3rd time).
        ⚠️ scale: in the harness the head reply is only 1.05 seconds slow, and the backoff sequence starts at 1
          second (the same set of constants as production); what is examined is the first three rounds, with the cap
          (60 seconds) never falling inside the window."""
        self.d.stream_delay, self.d.hangup = self.SLOW_HEAD, True
        lines, gaps = self.slow_head_gaps()
        self.assertEqual(gaps, [1, 2, 4], "after subtracting the %.2f-second head reply, the gaps should be 1, 2, 4 seconds (backoff doubling)" % self.SLOW_HEAD)
        self.assertEqual(len([x for x in lines if "disconnected within a second" in x]), 1, lines[-3:])

    def test_a_dispatcher_that_answers_200_slowly_then_resets_is_backed_off_every_time(self):
        """The same dispatcher, changed to sending an RST right after the head (the exception path). Before the fix,
        it sometimes backed off and sometimes did not (measured gaps mixing 2.29/3.28 seconds: if the RST beat
        urllib to finishing reading the head ⇒ "getting a 2xx" was never counted, so it backed off; if it came after
        ⇒ counted as "lived past" 1 second from the dial, cleared to zero) -- a race. Counting from getting the 2xx
        makes both orderings judged as "dropped right after connecting" ⇒ the race is gone, and this pins it down:
        the gaps are again 1, 2, 4 seconds. Fixture precondition: every one of them ended on an exception (at least
        3 "remote leg dropped" lines, never a read timeout -- if the RST arrives before urllib finishes reading the
        head it is a `URLError`, and after, a `ConnectionResetError`; both count, and the class name is never
        pinned)."""
        self.d.stream_delay, self.d.hangup = self.SLOW_HEAD, "rst"
        lines, gaps = self.slow_head_gaps()
        self.assertEqual(gaps, [1, 2, 4], "after subtracting the %.2f-second head reply, the gaps should be 1, 2, 4 seconds (backoff doubling)" % self.SLOW_HEAD)
        why = [x for x in lines if "remote leg dropped" in x]
        self.assertTrue(len(why) >= 3 and not any(died_of_read_timeout(x) for x in why), lines[-3:])

    IDLE_LIFE = 1.2   # the two cases below: slightly bigger than `MIN_REDIAL_S` ⇒ judging this idle pipe "good" is
                      #   left with only the half that says "lived past 1 second from getting the 2xx" (on the
                      #   loopback, dial to head reply takes only a few milliseconds, so the margin stays about 0.2
                      #   seconds)

    def no_progress(self):
        """The fixture precondition "not a single pipe made progress": the bridge never once dialed with a
        `Last-Event-ID` (it never had an id in hand).
        ⭐ Judge by the bridge's own behavior, never by what the dispatcher pushed: what judges "made progress" is the
          bridge's own `_last_id`."""
        return [h for h in self.d.stream_headers if "last-event-id" in h] == []

    def test_an_idle_pipe_that_outlived_a_second_and_then_timed_out_is_not_backed_off(self):
        """🔴 supplementary review 3, I-1 (the exception path): an idle bridge receives only keepalives, `_last_id`
        never moves; once its pipe is silently dropped by a NAT or proxy, it ends on a read timeout -- the most
        common shape on a real network, and the only thing judging it "good" is `_sick`'s "lived past
        `MIN_REDIAL_S` from getting the 2xx" half. Removing that half (= "looking only at whether it made progress",
        the exact thing `_sick`'s own docstring names as forbidden) ⇒ every pipe backs off (review probe: 2.5 -> 3.5
        -> 5.5 -> 9.5 seconds; in the worst case an idle bridge would not get a job for 60 seconds), while this whole
        file, test_80, used to run all 47 cases green.
        ⭐ The judgment is a lower bound (`dial_floor`, exception path `gap=1`). Two fixture preconditions (otherwise
          this tests a different path): every pipe ends on a read timeout, and not a single one made progress. Never
          set `tick`: with even one event, judging it good becomes the "made progress" half (pinned down by the
          case above)."""
        self.d.then = "silent"
        with mock.patch.object(scv, "STREAM_READ_TIMEOUT_S", self.IDLE_LIFE):
            lines, n = self.watch(window=14.0)
        floor = dial_floor(14.0, self.IDLE_LIFE)
        self.assertGreaterEqual(n, floor, "connected only %d times in 14 seconds (idle, but every pipe lived past 1 second, should be at least %d) "
                                "⇒ a healthy idle pipe was backed off like a bad one" % (n, floor))
        why = [x for x in lines if "remote leg dropped" in x]
        self.assertTrue(len(why) >= 2 and all(died_of_read_timeout(x) for x in why), lines[-3:])
        self.assertTrue(self.no_progress(), self.d.stream_headers[-2:])

    def test_an_idle_pipe_recycled_on_schedule_is_neither_backed_off_nor_accused(self):
        """🔴 supplementary review 3, I-1 (the normal-return path): an idle bridge proactively switches pipes once
        `STREAM_MAX_AGE_S` is reached ⇒ no progress, returns normally, and the only thing judging it "good" is again
        the "lived past 1 second" half. Removing it ⇒ every pipe backs off (review probe: 4.0 -> 5.0 -> 7.0 -> 11.0
        seconds), and it also wrongly gets loud with "connected and disconnected within a second, 3 times in a row
        ... the dispatcher is unhealthy".
        ⭐ Two judgments together: a lower bound (`dial_floor`, `gap=0` for a normal return) plus not a single one of
          that wrongful accusation is allowed.
        ⭐ The dispatcher pushes nothing at all ⇒ this is exactly an idle pipe that only sends keepalives (`Dispatcher`'s
          default shape, one every 0.1 seconds).
        Two fixture preconditions: not a single one ends on an exception (otherwise this is testing the previous
          case's path), and not a single one made progress."""
        with mock.patch.object(scv, "STREAM_MAX_AGE_S", self.IDLE_LIFE):
            lines, n = self.watch(window=12.0)
        floor = dial_floor(12.0, self.IDLE_LIFE, gap=0.0)
        self.assertGreaterEqual(n, floor, "connected only %d times in 12 seconds (idle, each one switched only on reaching its lifespan, should be at least %d) "
                                "⇒ a healthy idle pipe was backed off like a bad one" % (n, floor))
        self.assertEqual([x for x in lines if "disconnected within a second" in x], [])
        self.assertEqual([x for x in lines if "remote leg dropped" in x], [], "fixture precondition: every pipe must have returned normally (reached its lifespan)")
        self.assertTrue(self.no_progress(), self.d.stream_headers[-2:])

    def test_a_healthy_stream_is_neither_throttled_nor_accused(self):
        """Zero-input control, needing both ends: (1) an over-limit event with `id:` in front ⇒ can still push
        `_last_id` forward ⇒ not a single line about the cause is allowed to be loud; (2) the ordinary job right
        after it still runs to completion (never turn throttling into "so slow that nothing can get in")."""
        big = "z" * (scv.STREAM_EVENT_MAX + 4096)
        self.d.push_raw("id: 600" + chr(10) + "event: job" + chr(10) + "data: " + big + chr(10) + chr(10), 600)
        self.d.push("job", job("healthy", "甲"))
        lines = log_lines_during(lambda: (self.b.start_remote(),
                                          self.assertTrue(self.d.wait(self.terminal("healthy"), timeout=25))))
        self.assertEqual([x for x in lines if "id:" in x and "data:" in x], [])
        self.assertEqual(self.events("healthy")[-1]["event"], "done")


class CloseGate(Case):
    """I-3: the `two-legs-one-entry-gate` rule used to cover only `job`, and `close_session` passed through not a
    single gate."""

    def test_close_session_goes_through_the_same_two_gates(self):
        """(1) length/type (`_name`) (2) "don't open a second close while the first is still closing" (`close_state`).
        ⭐ The judgment is the row count of `Bridge._closed_at`: that table is kept for `CLOSE_MEMORY_S` (about 1
          hour), keyed by whatever the network hands it -- measured that without these two gates, pushing 300 of
          them means +300 rows, with keys totaling 1.2 MB, a path neither `MAX_SESSIONS` nor `_inflight` can
          control."""
        self.b.start_remote()
        self.assertTrue(self.d.wait(lambda: self.d.connects >= 1))
        for i in range(30):
            self.d.push("close_session", {"session": "x" * 4008})       # too long ⇒ not a single one is allowed into the table
            self.d.push("close_session", {"session": 7})                # wrong type
            self.d.push("close_session", {"session": ""})               # empty
        self.d.push("job", job("cg", "甲"))                              # use it as the fence for "everything above has been processed"
        self.assertTrue(self.d.wait(self.terminal("cg"), timeout=30))
        self.assertEqual(list(self.b._closed_at), [])

    def test_a_second_close_while_the_first_is_still_closing_starts_no_second_close(self):
        """The test for gate (2) itself (⚠️ the previous case only feeds it an invalid id, so gate (2) never even
        comes into play there ⇒ it would still run green with it removed).
        Judgment: a second close arrives while the first is still closing ⇒ never start a second closing thread (or
          the peer pushing one after another would keep adding more) -- that shape would let the second one finish
          closing quickly and its state become `closed`; ⭐ but the closing record must be refreshed to point to this
          call (15b fix2, M-b, maintainer's ruling): a job that arrives between the two calls must be judged against
          the second one, and that thread does one more pass after finishing what it has in hand. (Before 15b, what
          was pinned here was "the timestamp must never be reset"; M-b reversed it: never resetting it would let a
          job arriving between the two calls rebuild the session that was already closed.)
        ⚠️ the fence used is the ack of the job that comes right after: the stream-reading thread finishes handling
          the preceding `close_session` before handing this job off to the sending channel (15b, M-6; it used to be
          `_take` sending the ack synchronously on the stream-reading thread) ⇒ seeing it means the preceding call
          has already been processed. Never wait for the job's terminal state: that would mean waiting out a whole
          turn, by which time the first call might already have finished closing."""
        os.environ["FAKE_MODE"] = "trickle"
        self.b.start_remote()
        self.d.push("job", job("dd1", "甲", session="room-dd"))
        self.assertTrue(self.d.wait(lambda: any(e["event"] == "chunk" for e in self.events("dd1")), timeout=30))
        self.d.push("close_session", {"session": "room-dd"})
        self.assertTrue(self.d.wait(lambda: self.b.close_state(rsid("room-dd")) == "closing"))
        at0 = self.b._closed_at[rsid("room-dd")]["at"]
        time.sleep(0.05)                                   # make "it was refreshed" definitely observable (clock granularity)
        self.d.push("close_session", {"session": "room-dd"})
        self.d.push("job", job("dd-fence", "乙"))
        self.assertTrue(self.d.wait(lambda: any(e["event"] == "ack" for e in self.events("dd-fence")), timeout=30))
        # ⭐ with that dedup removed, the second call would finish closing quickly on its own (the session was already
        #   picked off by the first call) ⇒ the state becomes closed ⇒ red on this line
        self.assertEqual(self.b.close_state(rsid("room-dd")), "closing", "the second call started another closing thread (if the next line also does not match, check first whether the first call finished closing before the fence)")
        self.assertGreater(self.b._closed_at[rsid("room-dd")]["at"], at0, "the second call did not refresh the closing record to point to itself (M-b)")
        self.assertTrue(self.d.wait(self.terminal("dd1"), timeout=60))

    def test_a_remote_close_never_touches_a_local_session_with_the_same_string_id(self):
        """I-3 retires the scenario this case used to probe (review 2, N-M1, `crossleg`): the same session id used
        across both legs, a local close still winding down when a remote job with that same string rebuilt a
        session, and the dispatcher's own `close_session` then got dropped as "already closing," picking off the
        wrong instance -- the long-lived process the remote job rebuilt stayed alive until the bridge stopped, with
        nobody left to close it. `RemoteLeg` now namespaces every session id before it reaches
        `Bridge`/`SessionManager`, so a local `session` and a remote `session` that happen to be the same string
        are two entirely different keys ⇒ that whole class of bug is unreachable by construction: a remote
        `close_session` can never even find the local one to race against."""
        sid = "xl-1"
        status, _h, _body = helpers.http("POST", self.port, "/v1/chat/completions", token=self.tok, body={
            "model": "claude/haiku", "session": sid,
            "messages": [{"role": "system", "content": "LOCAL-SYS"}, {"role": "user", "content": "甲"}]})
        self.assertEqual(status, 200)
        self.assertIn(sid, self.b.sessions._sessions)
        self.b.start_remote()
        self.assertTrue(self.d.wait(lambda: self.d.connects >= 1))
        self.d.push("job", job("xl-remote", "乙", session=sid))
        self.assertTrue(self.d.wait(self.terminal("xl-remote"), timeout=30))
        self.assertIsNone(self.events("xl-remote")[-1].get("rebuilt"))
        self.assertIn(sid, self.b.sessions._sessions)          # the local session, untouched by the remote job
        self.assertIn(rsid(sid), self.b.sessions._sessions)    # a distinct remote session under the same string
        self.d.push("close_session", {"session": sid})
        self.assertTrue(self.d.wait(lambda: rsid(sid) not in self.b.sessions._sessions, timeout=30))
        self.assertIn(sid, self.b.sessions._sessions, "a remote close_session for a shared string must never reach the local session")
        self.b.sessions.close_session(sid)

    def test_a_legitimate_close_still_works(self):
        """Zero-input control: a legitimate close must still be able to close it (never build the gate so it lets
        nothing through)."""
        self.b.start_remote()
        self.d.push("job", job("cg2", "甲", session="room-cg"))
        self.assertTrue(self.d.wait(self.terminal("cg2")))
        self.d.push("close_session", {"session": "room-cg"})
        self.assertTrue(self.d.wait(lambda: self.b.sessions.counts()["sessions"] == 0))
        self.assertEqual(list(self.b._closed_at), [rsid("room-cg")])


class SessionNamespace(Case):
    """I-3: the review's own probe -- a remote job, then a local call, then another remote job, all with the
    same string `session` id -- must have neither leg rebuild the other. Before the fix, the two shared one
    key: the local call's system prompt does not match the remote job's fingerprint ⇒ each side's turn would
    rebuild (and kill) the other's process every time."""

    def test_a_shared_id_never_rebuilds_either_leg(self):
        self.b.start_remote()
        sid = "S"
        self.d.push("job", job("ns1", "甲", session=sid))
        self.assertTrue(self.d.wait(self.terminal("ns1")))
        self.assertIsNone(self.events("ns1")[-1].get("rebuilt"))
        ans1 = self.events("ns1")[-1]["text"]
        # the local call is this string's first appearance under the *local* key ⇒ a fresh session there, one
        # message is enough; it must never disturb the *remote* session under "remote:" + sid.
        status, _h, body = helpers.http("POST", self.port, "/v1/chat/completions", token=self.tok, body={
            "model": "claude/haiku", "session": sid,
            "messages": [{"role": "system", "content": "LOCAL-SYS"}, {"role": "user", "content": "乙"}]})
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(body)["scv"]["rebuilt"])
        # continuing the *remote* session needs its own prior turn in the history (never a bare new message: a
        # persistent session's fingerprint is checked against the full prefix) ⇒ this only proves isolation if it
        # comes back without a rebuild despite the local call sitting in between.
        msgs = [{"role": "user", "content": "甲"}, {"role": "assistant", "content": ans1}, {"role": "user", "content": "丙"}]
        self.d.push("job", job("ns2", "丙", session=sid, messages=msgs))
        self.assertTrue(self.d.wait(self.terminal("ns2")))
        self.assertIsNone(self.events("ns2")[-1].get("rebuilt"))
        self.assertIn(sid, self.b.sessions._sessions)
        self.assertIn(rsid(sid), self.b.sessions._sessions)


class FailedPostCloses(Case):
    """I-4: the true independent variable behind that `ResourceWarning: unclosed` is the path where a delivery gets
    a non-2xx reply (never the request count): the `HTTPError` `urllib` raises is itself a response object carrying
    a connection, and dropping it as if it were an ordinary exception leaves no one to close it. ⭐ The judgment is the
    `ResourceWarning` count on that one path, never a total across the full run (a full run's total gets muddied by
    other cases' time slices -- review is exactly where it was seen, inside the time slice of the stream-cap case)."""

    def test_a_refused_post_does_not_leave_a_socket_behind(self):
        """⚠️ this must go through the production path (`_job` -> `post()` -> `_post`), never calling `_post`
        directly and catching the exception: measured 2026-09-23 that calling it directly runs green even with
        `e.close()` removed -- on that path, the moment `HTTPError` exits `except` it gets reference-counted away on
        the spot, with `IOBase`'s teardown closing the socket first. On the production path it is wrapped inside
        `BridgeError.__context__`, forming a reference cycle with `post()`'s stack frame that has to wait for `gc` to
        collect it, with no guarantee on the collection order ⇒ if the socket gets collected first, this line goes
        loud.
        ⭐ So the window needs one `gc.collect()`, and the window has to cover the entire job."""
        self.d.fail_result = lambda body: body.get("event") == "done"
        with warnings.catch_warnings(record=True) as got:
            warnings.simplefilter("always")
            with contextlib.redirect_stderr(io.StringIO()):
                self.b.start_remote()
                self.d.push("job", job("rw", "甲"))
                self.assertTrue(self.d.wait(lambda: self.b.joblog.tail(1)
                                            and self.b.joblog.tail(1)[0]["job_id"] == "rw", timeout=30))
                gc.collect()
        self.assertEqual([e["event"] for e in self.events("rw")][-1], "done",
                         "fixture precondition: the terminal-state call must really have hit a 500")
        self.assertEqual([str(w.message) for w in got if issubclass(w.category, ResourceWarning)], [])


class Unpaired(unittest.TestCase):
    def test_no_pairing_means_no_outbound_at_all(self):
        b, _p, _t = helpers.start_bridge(remote_url="", remote_token="")
        try:
            self.assertFalse(b.start_remote())
            self.assertIsNone(b.remote)
        finally:
            b.stop()

    def test_a_plaintext_remote_url_is_refused_before_a_single_byte_goes_out(self):
        """🔴 §① has a line saying "the remote address must start with `HTTPS`", and before this round of work, not a
        single place in the whole file was actually enforcing it. What went out in plaintext was
        `Authorization: Bearer <remote_token>`, the hello's own report of "which CLIs are installed on this
        machine", and every answer's text.
        ⭐ The judgment is that not a single dial happened, never "it returned False" -- an implementation that "sends
          hello first and decides afterward" would still return False.
        ⚠️ the judgment is a dialing probe, never "the dispatcher never received any bytes": the first version used a
          hostname that could not be connected to, so no bytes would get through whether the gate was there or not
          (measured with review: removing the gate and deleting the first assertion still ran that case green) --
          the way the break was introduced made the judgment vacuously true. Switching to an unreachable address is
          only to avoid actually dialing out; what is judged is whether `_request` was called."""
        # ⭐ cells 2 and 3 are review M-F: loopback used to be judged by the string prefix `http://127.0.0.1` ⇒ a remote
        #   name whose prefix happened to match (`127.0.0.1.<remote-domain>`) got let through anyway, with
        #   `start_remote()` returning True. Now it is judged by the parsed hostname.
        # ⭐ cells 4 and 5 are supplementary review 2, M-6: `urlsplit` cuts the hostname at the first colon while
        #   `http.client` cuts at the last colon ⇒ this side reads it as 127.0.0.1 while that side dials
        #   `127.0.0.1:<remote-domain>`; for the one with an embedded newline, `urlsplit` strips the newline first.
        # ⭐ cell 6: `localhost` can be rewritten by hosts/DNS ⇒ refused; and the original words must be able to say
        #   "plaintext is only allowed for a loopback IP" (it used to only say "must start with https", leaving
        #   people thinking loopback was disallowed too).
        for plain in ("http://a-remote-host.example:9", "http://127.0.0.1.a-remote-host.example:9",
                      "http://127.0.0.1@a-remote-host.example:9", "http://127.0.0.1:a-remote-host.example:443/x",
                      "http://127.0.0.1:9" + chr(10) + ".a-remote-host.example", "http://localhost:9"):
            with self.subTest(url=plain), mock.patch.object(scv.RemoteLeg, "_request", autospec=True) as dial:
                b, _p, _t = helpers.start_bridge(remote_url=plain, remote_token="tok-x")
                try:
                    box = {}
                    said = log_lines_during(lambda: box.__setitem__("started", b.start_remote()))
                    started = box["started"]
                    # ⚠️ give the thread a real chance to dial before judging "what if the gate is gone": winding
                    #   down right away might catch it before it even reaches `_request`, blocked by `stop()` ⇒ the
                    #   judgment would be vacuously true again (still green even when broken)
                    end = time.time() + 2
                    while time.time() < end and not dial.called:
                        time.sleep(0.05)
                    # ⭐ the judgment comes first: with the gate removed, this must go red on "it dialed", never on "it
                    #   returned True"
                    self.assertEqual(dial.call_args_list, [])
                    self.assertFalse(started)
                    self.assertIsNone(b.remote)
                    self.assertEqual(len([x for x in said if "the remote leg is not starting" in x and "only allowed on a loopback IP" in x]), 1, said)
                finally:
                    b.stop()

    def test_the_loopback_exception_is_what_lets_the_harness_work(self):
        """Zero-input control: the same path, switched to a loopback `http://`, must be able to start -- otherwise
        the case above cannot be told apart from "it lets nothing through at all" (and this whole test module runs
        on loopback http, which would then all be false green)."""
        d = Dispatcher()
        url = d.start()
        b, _p, _t = helpers.start_bridge(remote_url=url, remote_token=d.token)
        try:
            self.assertTrue(b.start_remote())
            self.assertTrue(d.wait(lambda: d.hellos))
        finally:
            b.stop()
            d.stop()


def serve(answer):
    """A small server on the loopback; every request is handed to `answer(handler)` to answer for itself. Returns
    `(base URL, server)`."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_GET(self):
            answer(self)

        def do_POST(self):
            answer(self)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % srv.server_address[1], srv


class Redirects(unittest.TestCase):
    """Review M-8: urllib's default redirect handler follows a 3xx the dispatcher replies with (including an
    https->http downgrade), and does so carrying `Authorization: Bearer <remote_token>` verbatim (checked in this
    machine's 3.12 source: it only strips content-length/content-type, and carries it across hosts too). The fix:
    a request carrying a token never follows a redirect, no matter what (`scv.redirect_refused`, installed on every
    call via `_open`, taken by both `_post` and `_stream_once`).
    ⭐ The judgment: the redirect target's side (B) receives not a single request at all -- not merely "the request it
      received had no Authorization": the former is stricter, and is what is wanted here.
    ⭐ Both A and B are loopback plaintext: the judgment "carrying a token means never following" has nothing to do
      with the scheme, so plaintext loopback is enough to prove it; the other half, "one without a token is blocked
      only for an https downgrade", is proven on its own terms in
      tests/test_95_setup_pair_update.py::Redirects (building a real https dispatcher needs a certificate, which
      stdlib cannot produce)."""

    TOKEN = "tok-SECRET-M8"

    def setUp(self):
        os.environ["FAKE_MODE"] = "ok"
        self.seen_b, self.hits_a = [], collections.Counter()

        def b(h):
            self.seen_b.append((h.command, h.path, dict(h.headers)))
            h.send_response(200)
            h.send_header("Content-Length", "2")
            h.end_headers()
            h.wfile.write(b"{}")

        self.b_url, b_srv = serve(b)
        self.addCleanup(self.halt, b_srv)

    @staticmethod
    def halt(srv):
        srv.shutdown()
        srv.server_close()

    def start(self, plan):
        """`plan(path) -> (status code, which path on B to jump to, or None, reply body or None)`."""
        def a(h):
            if h.command == "POST":
                h.rfile.read(int(h.headers.get("Content-Length") or 0))
            path = h.path.split("?")[0]
            self.hits_a[path] += 1
            status, to, body = plan(path)
            data = b"" if body is None else json.dumps(body).encode("utf-8")
            h.send_response(status)
            if to:
                h.send_header("Location", self.b_url + to)
            h.send_header("Content-Length", str(len(data)))
            h.end_headers()
            h.wfile.write(data)

        a_url, a_srv = serve(a)
        self.addCleanup(self.halt, a_srv)
        self.bridge, _p, _t = helpers.start_bridge(remote_url=a_url, remote_token=self.TOKEN)
        self.addCleanup(self.bridge.stop)

    def run_until(self, pred, timeout=15):
        """Start the remote leg, wait for `pred()`, and also for both cases' asserted "never follows a redirect"
        line to have landed on disk before winding down (15b fix1 addendum 2).
        🔴 it used to wait only for `pred()` (the dispatcher receiving the Nth call): the hello case's line is only
          written after the 3rd try fails (`_post` is only loud once it has tried all of them), and winding down
          used to race ahead of it ⇒ about 2 out of 30 false reds (HEAD is the same as 9acee5e). Never patch it with
          a sleep. If it never lands on disk (the redirect was followed, so that line never appears), wait out the
          full `timeout` and go red on the judgment."""
        p = scv.spath("bridge.log")
        start = p.stat().st_size if p.exists() else 0

        def landed():
            return p.exists() and "never follow redirects".encode("utf-8") in p.read_bytes()[start:]

        def go():
            self.assertTrue(self.bridge.start_remote())
            end = time.time() + timeout
            while time.time() < end and not (pred() and landed()):
                time.sleep(0.05)
        return log_lines_during(go)

    def test_a_redirected_hello_sends_the_token_nowhere(self):
        """hello is a POST: when urllib follows a 302, it rewrites the resend as a GET -- the request body is lost,
        but `Authorization` stays."""
        self.start(lambda path: (302, "/stolen-hello", None) if path == "/bridge/hello" else (404, None, {}))
        lines = self.run_until(lambda: self.hits_a["/bridge/hello"] >= 3)
        self.assertEqual(self.seen_b, [])
        self.assertTrue([x for x in lines if "never follow redirects" in x], lines[-5:])

    def test_a_redirected_stream_sends_the_token_nowhere(self):
        """stream is a GET: urllib follows all of 301/302/303/307/308."""
        def plan(path):
            if path == "/bridge/hello":
                return 200, None, {"ok": True, "min_supported": "0.0.1", "latest": {}}
            return (307, "/stolen-stream", None) if path == "/bridge/stream" else (404, None, {})

        self.start(plan)
        lines = self.run_until(lambda: self.hits_a["/bridge/stream"] >= 2)
        self.assertEqual(self.seen_b, [])
        self.assertTrue([x for x in lines if "never follow redirects" in x], lines[-5:])


class KeepAwakeLeg(Case):
    """Every job on the remote leg is counted in KeepAwake: +1 when it starts, -1 when it ends with the end time
    written down (a job the local rate limit refuses has to balance too).
    ⭐Waits for the job's `jobs.log` row, never for its final state: the final state is sent inside the same
      `finally` *before* the slot/KeepAwake/bookkeeping lines, so right after `done` the count may still be 1."""

    def test_a_job_is_counted_and_released(self):
        seen = []
        orig_begin = self.b.awake.begin
        self.b.awake.begin = lambda: (seen.append(self.b.awake._busy), orig_begin())
        self.b.start_remote()
        self.d.push("job", job("ka1", "甲"))
        self.assertTrue(self.d.wait(lambda: self.rows_of("ka1")), "jobs.log has no row for ka1")
        self.assertEqual(seen, [0], "the job was counted when it started")
        self.assertEqual(self.b.awake._busy, 0, "balanced once it ended")
        self.assertIsNotNone(self.b.awake._last)
        self.assertTrue(self.b.awake.wanted(), "it just ended: inside the tail")


class KeepAwakeLegRateLimited(Case):
    cfg = {"remote_jobs_per_hour": 0}           # the local rate limit: every job is refused

    def test_a_rate_limited_job_still_balances(self):
        self.b.start_remote()
        self.d.push("job", job("ka2", "甲"))
        self.assertTrue(self.d.wait(lambda: self.rows_of("ka2")), "jobs.log has no row for ka2")
        self.assertEqual(self.events("ka2")[-1]["error"]["type"], "local_rate_limit")
        self.assertEqual(self.b.awake._busy, 0)
        self.assertIsNotNone(self.b.awake._last, "the refused job went through begin/end too")


class SleepWake(Case):
    """0.3.0 (spec B35–B38): the remote leg only runs while the bridge is awake. Woken = the same remote leg as before
    (B37); back to sleep once it has been quiet for `idle_sleep_s` (the clock counts from the later of waking and the
    last job's end); ⛔never while a job is queued or running. The in-process `Bridge` never dials by itself — "a
    started bridge is asleep" on the real start path is pinned in tests/test_90_cli.py::Lifecycle."""
    cfg = {"idle_sleep_s": 600}

    def test_healthz_says_asleep_then_awake_and_when_it_sleeps(self):
        self.assertEqual(self.b.health()["remote"], {"state": "asleep"})
        self.assertEqual((self.d.hellos, self.d.connects), ([], 0))
        self.assertEqual(self.b.wake("test"), "woke")
        self.assertTrue(self.d.wait(lambda: self.d.connects >= 1), "woke, but the remote leg never dialed")
        rem = self.b.health()["remote"]
        self.assertIn(rem["state"], ("idle", "hello", "streaming"), rem)
        self.assertTrue(590 <= rem["sleeps_in_s"] <= 600, rem)

    def test_a_second_wake_starts_nothing_new_and_restarts_the_clock(self):
        self.assertEqual(self.b.wake("test"), "woke")
        leg = self.b.remote
        self.b.woke_at -= 500
        self.assertEqual(self.b.wake("again"), "awake")
        self.assertIs(self.b.remote, leg, "a second wake started a second remote leg (P22)")
        self.assertGreater(self.b.sleeps_in(), 590, "someone just asked for it: the ten minutes start over")

    def test_quiet_for_idle_sleep_s_then_asleep_and_silent(self):
        self.assertEqual(self.b.wake("test"), "woke")
        self.assertTrue(self.d.wait(lambda: self.d.connects >= 1))
        old = self.b.remote
        self.assertFalse(self.b.idle_tick(now=self.b.woke_at + 599))
        self.assertTrue(self.b.idle_tick(now=self.b.woke_at + 601))
        self.assertIsNone(self.b.remote)
        self.assertTrue(old._stop.is_set(), "the old remote leg was never told to stop")
        self.assertEqual(self.b.health()["remote"], {"state": "asleep"})
        time.sleep(1.0)                                  # the old leg's open stream ends at its next line (the dispatcher's keepalive)
        n, h = self.d.connects, len(self.d.hellos)
        time.sleep(1.5)
        self.assertEqual((self.d.connects, len(self.d.hellos)), (n, h), "asleep, yet it still dials")

    def test_the_clock_counts_from_the_last_job(self):
        self.assertEqual(self.b.wake("test"), "woke")
        self.d.push("job", job("sw-last", "甲"))
        self.assertTrue(self.d.wait(lambda: self.rows_of("sw-last")), "jobs.log has no row for sw-last")
        last = self.b.awake._last
        self.b.woke_at = last - 300                      # woke five minutes before the job: the job's end must win
        self.assertFalse(self.b.idle_tick(now=last + 599), "went to sleep counting from waking, not from the last job")
        self.assertTrue(self.b.idle_tick(now=last + 601))

    def test_never_while_a_job_is_queued_or_running(self):
        self.assertEqual(self.b.wake("test"), "woke")
        far = time.time() + 10 ** 6
        self.b.awake.begin()
        try:
            self.assertFalse(self.b.idle_tick(now=far), "went to sleep with a job running")
        finally:
            self.b.awake.end()
        with self.b.remote._early_lock:
            self.b.remote._queued["sw-q"] = 1
        try:
            self.assertFalse(self.b.idle_tick(now=far), "went to sleep with a job waiting for its ack")
        finally:
            with self.b.remote._early_lock:
                self.b.remote._queued.pop("sw-q", None)
        self.assertTrue(self.b.idle_tick(now=far), "control: nothing left ⇒ it does go to sleep")

    def test_after_sleep_a_wake_runs_a_job_exactly_once(self):
        """Review Focus ①②: asleep → woken again right away (the old leg may still be winding down) ⇒ a new remote
        leg; a job pushed now is acked and answered exactly once."""
        self.assertEqual(self.b.wake("test"), "woke")
        self.assertTrue(self.d.wait(lambda: self.d.connects >= 1))
        old = self.b.remote
        self.assertTrue(self.b.idle_tick(now=time.time() + 10 ** 6))
        self.assertEqual(self.b.wake("again"), "woke", "asleep again, so this is a real wake")
        self.assertIsNot(self.b.remote, old)
        self.d.push("job", job("sw-once", "甲"))
        self.assertTrue(self.d.wait(self.terminal("sw-once")))
        time.sleep(1.0)
        kinds = [e["event"] for e in self.events("sw-once")]
        self.assertEqual((kinds.count("ack"), kinds.count("done")), (1, 1), kinds)

    def test_not_paired_and_refused(self):
        b, _p, _t = helpers.start_bridge(remote_url="", remote_token="")
        self.addCleanup(b.stop)
        self.assertEqual(b.health()["remote"], {"state": "off"})
        self.assertEqual((b.wake("test"), b.remote), ("not_paired", None))
        b2, _p2, _t2 = helpers.start_bridge(remote_url="http://example.com", remote_token="x")
        self.addCleanup(b2.stop)
        self.assertEqual((b2.wake("test"), b2.remote), ("cannot_dial", None))


class NeverGoesBack(Case):
    """`idle_sleep_s: 0` ⇒ once woken it stays awake until the bridge stops (the knob for someone who wants 0.2.x's
    always-on)."""
    cfg = {"idle_sleep_s": 0}

    def test_zero_never_sleeps(self):
        self.assertEqual(self.b.wake("test"), "woke")
        self.assertIsNone(self.b.sleeps_in())
        self.assertFalse(self.b.idle_tick(now=time.time() + 10 ** 6))
        self.assertIsNotNone(self.b.remote)


if __name__ == "__main__":
    unittest.main()
