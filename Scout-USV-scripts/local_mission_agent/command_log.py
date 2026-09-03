"""
Tracks operator command_ids already processed by this Local Agent, so a
command re-delivered by the operator backend is never executed twice.
Persisted to survive a Local Agent restart mid-mission.

Runtime file path and bound are read from `config` *dynamically* on every
call (config.COMMAND_LOG_FILE / config.MAX_TRACKED_COMMAND_IDS), never bound
by value at import time. A `from config import COMMAND_LOG_FILE` would
snapshot whatever the path happened to be the first time this module was
imported, so a test (or a second test module in the same interpreter) that
later repoints config.COMMAND_LOG_FILE at a temp file would be silently
ignored here and this module would keep writing the real command_log.jsonl
-- exactly the runtime/test state bleed that leaked test command_ids into the
committed command_results.json. Referencing config.<NAME> per call keeps the
one authoritative value in config and lets any caller override it safely.
"""
import threading

import config

# Serializes read-modify-write of COMMAND_LOG_FILE. The Local Agent now runs a
# bounded background worker (mission_upload_worker.py) alongside the main loop,
# so a mark_processed() on the main thread (e.g. a "busy" MISSION_UPLOAD
# rejection) can overlap the worker thread's own mark_processed() -- without
# this lock that read-modify-write could interleave and drop an id.
_lock = threading.Lock()


def _read_ids():
    try:
        with open(config.COMMAND_LOG_FILE, "r") as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        return []


def is_duplicate(command_id: str) -> bool:
    with _lock:
        return command_id in _read_ids()


def mark_processed(command_id: str) -> None:
    with _lock:
        ids = _read_ids()
        ids.append(command_id)
        ids = ids[-config.MAX_TRACKED_COMMAND_IDS:]
        with open(config.COMMAND_LOG_FILE, "w") as f:
            for cid in ids:
                f.write(cid + "\n")
