# -*- coding: utf-8 -*-
"""Reference dispatcher: the other half of PROTOCOL.md. Plan 2's server side is implemented to match its behavior."""
import json
import os
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NL = chr(10)


class Dispatcher:
    def __init__(self, token="tok-GOOD"):
        self.token = token
        self.events = []                 # [(id, event, data)]
        self.results, self.hellos, self.stream_headers = [], [], []
        self.connects = 0
        self.hello_reply = {"ok": True, "min_supported": "0.0.1",
                            "latest": {"version": "0.1.0", "commit": "c0ffee", "sha256": "00"}}
        self.raw_lines = []              # raw SSE bytes push_raw() stuffed in, ahead of events
        self.fail_result = None          # f(body)->bool: return 500 for this one callback (makes "could not deliver")
        self.flooded = 0                 # bytes push_flood really wrote out (once the bridge gives up, this side can't write anymore)
        self.hangup = False              # True = closes the stream right after 200 (the most common shape of an overloaded / half-dead server); "rst" = a raw RST right after 200 (never a FIN first)
        self.stream_status = 0           # not 0 = /bridge/stream returns this status (hello still works as usual): only the stream endpoint is down
        self.stream_delay = 0.0          # wait this long before returning any stream response head (200 or stream_status): a slow-to-respond origin (overload / CDN, etc.)
        self.stream_times = []           # the arrival time of each /bridge/stream hit (used to measure "how long between two dial-ins")
        self.hello_delay = 0.0           # wait this long before replying to hello (makes the "the retry hello is in flight" window)
        self.hello_status = 0            # not 0 = /bridge/hello returns this status (makes "hello itself failed on the network")
        self.hello_times = []            # the arrival time of each /bridge/hello hit
        self.hold_result = None          # f(body)->bool: hold this callback and never answer it, until release() (or hold_max seconds): "result stuck, stream fine"
        self.hold_max = 60.0
        self.result_log = []             # [(arrival time, body)]: the timestamped version of results (used to measure "how long until it takes effect")
        self.result_raw = None           # not None = /bridge/result returns 200 plus these raw bytes as-is (never JSON): what shape the dispatcher's receipt body is in is none of the bridge's business
        self._released = threading.Event()
        self.tick = ""                   # non-empty = push one event of this name the moment each pipe connects (with a new id => the bridge's `_last_id` keeps moving forward on every one)
        self.then = ""                   # "silent" = after pushing, write not a single byte more (not even a keepalive, let the bridge hit its own read timeout)
        self._eid = 0                    # push() / push_raw() share one incrementing number (never give them each their own:
        #                                  Last-Event-ID is compared by size, and two separate counters would clobber each other)
        self._gen = 0                    # drop() bumps this; any stream already running sees it and gives up
        self._lock = threading.Lock()
        self.httpd = None

    def push(self, event, data):
        with self._lock:
            self._eid += 1
            self.events.append((self._eid, event, data))
            return self._eid

    def push_raw(self, text, eid=0):
        """Stuff a piece of raw text into the stream (never through the events path): this is how malformed events and
        oversized events get made. `eid` is not decoration: the dispatcher resends raw events the same way, by
        `Last-Event-ID` (that is what PROTOCOL says) => if the bridge refuses to skip an event it already gave up on,
        the resend becomes an infinite loop, and this harness has to be able to catch that."""
        with self._lock:
            self._eid = max(self._eid, eid)
            self.raw_lines.append((eid, text))

    def push_flood(self, nbytes, eid):
        """A pipe that never emits a newline: `nbytes` bytes written out in chunks, never accumulated into one big
        object in this process. 🔴Accumulating it into one big object grows memory on the dispatcher's own side —
          that is exactly "the way the failure was staged injects its own signal", and the number you measure has
          nothing to do with the bound under test (that was the first version's mistake: both arms grew by 48 MiB)."""
        with self._lock:
            self._eid = max(self._eid, eid)
            self.raw_lines.append((eid, ("FLOOD", nbytes)))

    def drop(self):
        self._gen += 1

    def release(self):
        """Release every callback `hold_result` is holding (any that arrive afterward are no longer held either)."""
        self._released.set()

    def wait(self, pred, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.05)
        return False

    def start(self):
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def _reply(self, status, obj):
                data = json.dumps(obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authed(self):
                return self.headers.get("Authorization") == "Bearer " + me.token

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
                if self.path == "/bridge/pair":
                    return self._reply(200, {"token": me.token}) if body.get("code") == "GOOD" else self._reply(403, {"error": "bad code"})
                if not self._authed():
                    return self._reply(401, {"error": "bad token"})
                if self.path == "/bridge/hello":
                    me.hellos.append(body)
                    me.hello_times.append(time.time())
                    time.sleep(me.hello_delay)
                    if me.hello_status:
                        return self._reply(me.hello_status, {"error": "hello down"})
                    return self._reply(200, me.hello_reply)
                if self.path == "/bridge/result":
                    me.results.append(body)
                    me.result_log.append((time.time(), body))
                    if me.hold_result is not None and me.hold_result(body):
                        me._released.wait(me.hold_max)      # ⭐really held: the bridge's socket for this call just sits there waiting for a reply
                        try:
                            return self._reply(200, {"ok": True})
                        except OSError:
                            return                          # the bridge already timed out and left: can't reply, never mind (never dump a traceback to stderr)
                    if me.fail_result is not None and me.fail_result(body):
                        return self._reply(500, {"error": "nope"})
                    if me.result_raw is not None:
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(me.result_raw)))
                        self.end_headers()
                        try:
                            return self.wfile.write(me.result_raw)
                        except OSError:
                            return                          # the bridge closes after reading the status, never reads the body (a big body ends up with the other side gone mid-write): never dump a traceback to stderr
                    return self._reply(200, {"ok": True})
                self._reply(404, {})

            def do_GET(self):
                if self.path != "/bridge/stream" or not self._authed():
                    return self._reply(401, {})
                me.connects += 1
                me.stream_times.append(time.time())
                me.stream_headers.append({k.lower(): v for k, v in self.headers.items()})   # header-name case varies by client, normalize to lower
                sent, gen, raw_sent = int(self.headers.get("Last-Event-ID") or 0), me._gen, 0
                time.sleep(me.stream_delay)
                if me.stream_status:
                    return self._reply(me.stream_status, {"error": "stream down"})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if me.hangup == "rst":
                    self.wfile.flush()
                    # SO_LINGER(on, 0 seconds) then close => the kernel sends RST, never a FIN; socketserver's later
                    # shutdown/close hitting an already-closed socket gets swallowed by socketserver itself
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("HH" if os.name == "nt" else "ii", 1, 0))
                    self.connection.close()
                    return
                if me.hangup:
                    return
                if me.tick:
                    me.push(me.tick, {})
                if me.then == "silent":
                    # push everything up to this moment, then go silent: makes the pipe "connected, made progress,
                    # then ended with a read timeout"
                    for eid, event, data in list(me.events):
                        if eid > sent:
                            self.wfile.write(("id: %d%sevent: %s%sdata: %s%s%s" % (
                                eid, NL, event, NL, json.dumps(data, ensure_ascii=False), NL, NL)).encode("utf-8"))
                    self.wfile.flush()
                    while gen == me._gen:
                        time.sleep(0.1)
                    return
                try:
                    while gen == me._gen:
                        while raw_sent < len(me.raw_lines):
                            reid, text = me.raw_lines[raw_sent]
                            raw_sent += 1
                            if not (reid > sent or not reid):
                                continue
                            if isinstance(text, tuple):        # push_flood: pour it in chunks, never a newline
                                self.wfile.write(("id: %d%sevent: job%sdata: " % (reid, NL, NL)).encode("utf-8"))
                                chunk, left = b"z" * 65536, text[1]
                                while left > 0:
                                    n = min(left, 65536)
                                    self.wfile.write(chunk[:n])
                                    me.flooded += n
                                    left -= n
                                self.wfile.write(NL.encode("utf-8") + NL.encode("utf-8"))
                            else:
                                self.wfile.write(text.encode("utf-8"))
                        for eid, event, data in list(me.events):
                            if eid > sent:
                                self.wfile.write(("id: %d%sevent: %s%sdata: %s%s%s" % (
                                    eid, NL, event, NL, json.dumps(data, ensure_ascii=False), NL, NL)).encode("utf-8"))
                                sent = eid
                        self.wfile.write((": keepalive" + NL + NL).encode("utf-8"))
                        self.wfile.flush()
                        time.sleep(0.1)
                except OSError:
                    return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def stop(self):
        self.drop()
        self.release()
        self.httpd.shutdown()
        self.httpd.server_close()
