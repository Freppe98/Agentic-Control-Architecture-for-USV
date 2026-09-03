"""
Rolling read-only record of recent operator command lifecycles -- backs
GET /agent/command_history (agent_server.py) so the operator station can see
recent command outcomes without needing to have been listening when the
command_result was pushed. command_log.py (dedup by command_id, unbounded
growth prevented via MAX_TRACKED_COMMAND_IDS) is a different concern: it only
ever stores bare command_ids to reject a redelivery, not what happened.
"""
from collections import deque

MAX_HISTORY = 50

_history = deque(maxlen=MAX_HISTORY)


def record(entry: dict) -> None:
    _history.append(entry)


def get_recent(n: int = MAX_HISTORY) -> list:
    return list(_history)[-n:]
