"""
Canonical timestamp normalization for every operator command timestamp
field (expires_at/issued_at/created_at/claimed_at) -- the single place
that decides how epoch seconds (int/float) and ISO-8601 strings (with an
explicit offset or a trailing 'Z') become a comparable epoch float.

This exists because the real Operator Backend sends expires_at as an
ISO-8601 string (e.g. "2026-07-17T09:52:58.498992+00:00"), while
command_handler.py's own tests and mock_operator.py have always used
time.time()-style floats. Comparing a raw string against time.time()
directly raises "'>' not supported between instances of 'float' and
'str'" -- see command_handler._expired's previous implementation. Every
comparison against "now" must go through normalize_timestamp() first, no
exceptions.
"""
from datetime import datetime, timezone


class InvalidTimestamp(ValueError):
    """
    Raised when a command timestamp field can't be normalized to epoch
    seconds. Callers (command_handler.process_command) must catch this and
    turn it into one terminal rejected/failed command_result -- it must
    never escape as a bare TypeError/ValueError into the outer emergency
    exception handler in local_agent.py (that handler exists to catch
    truly unexpected bugs, not routine bad input from the operator side).
    """


def normalize_timestamp(value, field_name: str = "timestamp") -> float:
    """
    Normalize one incoming timestamp value to epoch seconds (float, UTC).

    Accepts:
      - int/float epoch seconds (bool is rejected even though it's
        technically an int subclass -- True/False are never a meaningful
        timestamp and silently accepting one would just hide a bug)
      - ISO-8601 strings with an explicit UTC offset, e.g.
        "2026-07-17T09:52:58.498992+00:00"
      - ISO-8601 strings ending in 'Z', treated as +00:00
      - ISO-8601 strings with no timezone at all ("naive") -- explicitly
        interpreted as UTC (tzinfo=timezone.utc is set outright) rather
        than left to whatever timezone the interpreter happens to be
        running in. This process must never call datetime.now() or
        fromtimestamp() without an explicit tz for this reason -- the
        Raspberry Pi's local timezone must never silently leak into a
        command validation decision.

    Raises InvalidTimestamp for anything else: an unparseable string, or
    any type other than int/float/str. Callers are responsible for
    deciding what a missing (None) field means -- this function is only
    ever called with a value that is already known to be present.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise InvalidTimestamp(
            f"{field_name}: unsupported type {type(value).__name__} ({value!r})"
        )

    if isinstance(value, (int, float)):
        return float(value)

    raw = value.strip()
    iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as e:
        raise InvalidTimestamp(f"{field_name}: unparseable timestamp {value!r} ({e})") from e

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.timestamp()


COMMAND_TIMESTAMP_FIELDS = ("expires_at", "issued_at", "created_at", "claimed_at")


def normalize_command_timestamps(command: dict, fields=COMMAND_TIMESTAMP_FIELDS) -> dict:
    """
    Normalize every present timestamp field on an operator command dict to
    epoch seconds, without mutating the raw command -- the raw command
    dict is left exactly as received (for command_history/diagnostics),
    and callers use this returned dict for any comparison against "now".

    A field that's absent or explicitly None is left out of the result --
    that's not an error, since not every command carries every field (a
    minimal test command may have no created_at/claimed_at at all). A
    field that IS present but unparseable raises InvalidTimestamp naming
    that exact field, so the caller's rejection reason is specific.
    """
    normalized = {}
    for field in fields:
        raw_value = command.get(field)
        if raw_value is None:
            continue
        normalized[field] = normalize_timestamp(raw_value, field)
    return normalized
