import json
from config import BUFFER_FILE, MAX_BUFFERED_MESSAGES


def _command_result_key(message):
    """command_id if `message` is a command_result, else None. Used to
    dedupe repeated buffering of the same command_id's result (see
    buffer_message) -- distinct from every other message type, which has
    no natural identity to dedupe on and is buffered as-is."""
    if not isinstance(message, dict) or message.get("message_type") != "command_result":
        return None
    return (message.get("payload") or {}).get("command_id")


def buffer_message(message):
    """
    Append-then-trim rather than a plain append: bounds agent_buffer.jsonl
    at MAX_BUFFERED_MESSAGES so a message type that can never succeed
    against the currently configured operator endpoint (a route mismatch,
    e.g. command_result 405ing forever) can't grow the file without bound --
    see config.MAX_BUFFERED_MESSAGES. Oldest entries are dropped first.

    A command_result carries an identity (command_id) that command_handler.py
    now always resends unchanged on redelivery (see command_results.py), so
    repeated polls of the same still-undelivered command_id must never
    append additional lines here -- any existing buffered entry for that
    command_id is replaced in place (same content, since it's the same
    stored authoritative result) rather than duplicated.
    """
    messages = read_buffered_messages()
    dedup_key = _command_result_key(message)
    if dedup_key is not None:
        messages = [m for m in messages if _command_result_key(m) != dedup_key]
    messages.append(message)
    if len(messages) > MAX_BUFFERED_MESSAGES:
        messages = messages[-MAX_BUFFERED_MESSAGES:]
    with open(BUFFER_FILE, "w") as f:
        for m in messages:
            f.write(json.dumps(m) + "\n")


def read_buffered_messages():
    try:
        with open(BUFFER_FILE, "r") as f:
            lines = [line for line in f if line.strip()]
    except FileNotFoundError:
        return []

    messages = []
    for line in lines:
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return messages


def flush_buffer(send_fn):
    """
    Attempt to deliver every buffered message, in order, via send_fn(message).
    Nothing is discarded for being old -- a buffered message is the historical
    record of what the vehicle was doing while disconnected, and it's up to
    the operator backend (which owns arrival-age-based comm/event history) to
    decide how to treat an old timestamp. The caller is responsible for
    sending a fresh live status *after* this flush completes, so a replayed
    old message can never look like current state (see local_agent.py).
    Messages that still fail to send are kept, in order, for next time.
    """
    messages = read_buffered_messages()
    if not messages:
        return {"sent": 0, "remaining": 0}

    remaining = []
    sent = 0
    for message in messages:
        try:
            send_fn(message)
            sent += 1
        except Exception:
            remaining.append(message)

    with open(BUFFER_FILE, "w") as f:
        for message in remaining:
            f.write(json.dumps(message) + "\n")

    return {"sent": sent, "remaining": len(remaining)}