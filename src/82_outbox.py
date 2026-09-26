# ━━ The remote leg's outbound channel (M-6): one thread + one bounded queue, doing one thing at a time in the
#   order it came in. ⭐A self-contained block: its own class, its own lock, never a mutable global shared across
#   blocks; it does not recognize a job, only "one thing" (a callable) ⇒ that is its own piece: `src/82_outbox.py`.
#   📎 NOTES.md::ack-on-stream-thread
class _Outbox:
    """The part that has to wait on the network after a job comes in (the ack, the error that follows a
    rejection right away, the ack for a dup) is handed to this, never waited on the receive-stream thread: that
    thread waiting even once means the worst-case time of one delivery (about 91 seconds), and during that whole
    stretch the SSE pipe reads not a single byte, so every cancel/close_session queued behind it is stuck too.
    ⭐one thread (started only the first time there is work), a queue with an upper bound: when it will not fit,
      `put` returns why (`"full"`/`"stopped"`, never one False covering two different things), and it is up to the
      caller to complain.
    ⭐`stop()`: stop accepting; nothing still queued gets done (returns how many were dropped); whatever is being
      worked on right now finishes and then the thread exits — waiting at most one result-post's timeout
      (`_post` watches the stop-the-bridge flag)."""

    def __init__(self, cap: int):
        self._q, self._lock, self._stopped, self.thread = queue.Queue(cap), threading.Lock(), False, None
        self.cap = cap

    def put(self, fn, *args) -> str:
        with self._lock:
            if self._stopped:
                return "stopped"
            try:
                self._q.put_nowait((fn, args))
            except queue.Full:
                return "full"
            if self.thread is None:
                self.thread = threading.Thread(target=self._loop, daemon=True)
                self.thread.start()
            return ""

    def _loop(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            try:
                item[0](*item[1])
            except Exception as e:      # ⭐one item blowing up must never take the whole channel down with it: this is a daemon thread, and letting it fly out means this leg can never ack again (without a sound)
                log("❌ error handling a remote event, dropping this one: %s: %s" % (type(e).__name__, repr(str(e))[:128]))

    def stop(self) -> int:
        with self._lock:
            self._stopped, left = True, 0
            while True:
                try:
                    self._q.get_nowait()        # ⚠️never check `empty()` first and then get: the channel thread could take the last item at just that moment ⇒ this raises Empty
                except queue.Empty:
                    break
                left += 1
            if self.thread is not None:
                self._q.put_nowait(None)        # just emptied ⇒ there is definitely room; wakes the thread stuck on `get()`
        return left


