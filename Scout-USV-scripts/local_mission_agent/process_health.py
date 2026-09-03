"""
This process's own CPU usage -- distinct from the vehicle Flask host's
cpu/memory/storage (services/health_service.py on that side, which measures
the whole machine, not this specific process). No psutil, per repo
convention -- reads /proc/<pid>/stat directly, same manual-/proc approach
health_service.py already uses for RAM/temperature.

cpu_percent() needs two samples to produce a rate: the first call after
this process starts has no prior sample to diff against and returns None
(same "UNKNOWN until we have a baseline" convention as
runtime_status.seconds_since_alive()), exactly reflecting that nothing real
can be measured yet rather than reporting a fabricated 0%.
"""
import os
import time

_CLK_TCK = os.sysconf("SC_CLK_TCK")

_last_sample = None  # (wall_time, process_cpu_seconds)


def _process_cpu_seconds() -> "float | None":
    try:
        with open(f"/proc/{os.getpid()}/stat") as f:
            fields = f.read().split()
        utime, stime = int(fields[13]), int(fields[14])
        return (utime + stime) / _CLK_TCK
    except Exception:
        return None


def cpu_percent() -> "float | None":
    """Percent of one core consumed by this process since the previous call."""
    global _last_sample

    now = time.time()
    cpu_seconds = _process_cpu_seconds()
    if cpu_seconds is None:
        return None

    if _last_sample is None:
        _last_sample = (now, cpu_seconds)
        return None

    prev_wall, prev_cpu = _last_sample
    _last_sample = (now, cpu_seconds)
    wall_delta = now - prev_wall
    if wall_delta <= 0:
        return None
    return round(100.0 * (cpu_seconds - prev_cpu) / wall_delta, 1)
