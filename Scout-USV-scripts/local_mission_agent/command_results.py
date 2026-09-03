"""
Persisted authoritative terminal result per operator command_id -- the
actual command_result payload, keyed by command_id, not just a marker that
the id was seen (see command_log.py for that narrower purpose). This is
what lets a redelivered command_id resend the exact original result
instead of re-executing the vehicle Flask endpoint or fabricating a fresh
generic "duplicate" rejection on every poll (see command_handler.py).
Persisted to disk so this survives a Local Agent restart mid-mission, same
as command_log.py.

The first result stored for a given command_id always wins -- store_result
is a no-op if one is already present, so a later call (a retried duplicate
path, a defensive re-store, ...) can never clobber the authoritative
terminal result already recorded. Entries are removed only once the
operator backend has successfully acknowledged receipt (clear_result), not
on any fixed expiry -- this process has no way to know a redelivery won't
still arrive after a long comm outage, so the resend must stay available
until acked.
"""
import json
import threading

import config

# Serializes read-modify-write of COMMAND_RESULTS_FILE against the background
# upload worker thread -- same reason as command_log.py's lock.
_lock = threading.Lock()


def _read_all() -> dict:
    try:
        with open(config.COMMAND_RESULTS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_all(results: dict) -> None:
    with open(config.COMMAND_RESULTS_FILE, "w") as f:
        json.dump(results, f)


def get_stored_result(command_id: str):
    with _lock:
        return _read_all().get(command_id)


def store_result(command_id: str, result_payload: dict) -> None:
    if not command_id:
        return
    with _lock:
        results = _read_all()
        if command_id in results:
            return
        results[command_id] = result_payload
        # config.MAX_TRACKED_COMMAND_IDS read dynamically (see command_log.py's
        # module docstring) so a test can lower the bound via config without this
        # module having snapshotted the value at import time.
        max_tracked = config.MAX_TRACKED_COMMAND_IDS
        if len(results) > max_tracked:
            # dict preserves insertion order (Python 3.7+): drop the oldest
            # entries first, same bounding policy as command_log.py.
            for old_id in list(results.keys())[: len(results) - max_tracked]:
                del results[old_id]
        _write_all(results)


def clear_result(command_id: str) -> None:
    if not command_id:
        return
    with _lock:
        results = _read_all()
        if command_id in results:
            del results[command_id]
            _write_all(results)
