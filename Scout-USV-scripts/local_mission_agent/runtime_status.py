"""
Thread-safe snapshot of the Local Agent's own main-loop liveness.

The diagnostics HTTP server (agent_server.py) runs on its own thread and
must be able to answer "is the Local Agent alive" honestly -- the HTTP
thread responding to a request only proves the *listener* is alive, not
that main()'s polling loop hasn't stalled (e.g. wedged on a slow/hanging
call). main() calls mark_alive() once per iteration; the HTTP handler
reads it via seconds_since_alive() without needing to touch main()'s other
state directly.
"""
import threading
import time

_lock = threading.Lock()
_last_alive_ts = None


def mark_alive() -> None:
    global _last_alive_ts
    with _lock:
        _last_alive_ts = time.time()


def seconds_since_alive():
    """None if the main loop has never completed an iteration yet."""
    with _lock:
        if _last_alive_ts is None:
            return None
        return time.time() - _last_alive_ts
