"""Operator-side client for Scout's Local Agent replanning API (`/agent/replan/*`, port 8090).

WHY THIS MODULE EXISTS, AND WHAT IT DELIBERATELY IS NOT
------------------------------------------------------
Scout's Local Agent is the AUTHORITATIVE owner of every piece of replanning state — the
FSM, the energy decision, the mission revision, the single-slot planning-package store, the
experiment injection and the runtime config. This module is a thin, honest proxy for the
Operator Station: it forwards explicit supervisory operations and returns Scout's own body
verbatim. It computes no energy margin, runs no FSM, keeps no shadow copy of the decision,
and NEVER fabricates a success. Everything a page renders about replanning is either Scout's
word or a clearly-marked "unknown / unavailable".

THE OUTCOME MODEL (the load-bearing part; see task Sections 1, 11, 12)
--------------------------------------------------------------------
A supervisory WRITE can end in one of three epistemic states, and conflating them is the
exact failure this station must not have:

  accepted  — Scout returned a definite success (2xx). It stored / applied the operation.
  rejected  — Scout returned a definite refusal (a 4xx it authored: validation, a 409
              TRANSACTION_ACTIVE, a hash mismatch). The operation did NOT take effect, and
              Scout told us why. Its error code is preserved for the UI.
  unknown   — we never received Scout's verdict (an HTTP timeout, a dropped connection, or
              an ambiguous 5xx). The operation MAY have taken effect — Scout's stores are
              idempotent by design — so we must NOT declare failure. The caller reconciles
              later with a GET (compare mission id / hash) rather than blindly retrying.

A read that fails is `reachable:false` / `unavailable` — never a fabricated inactive/active
state. A route the Scout does not implement (404) is `supported:false` — an older Scout, not
an error, and never a fabricated safe default for a missing safety field.

ONE MOCKING SURFACE: this module does its own HTTP through the module-level `requests`, so a
test swaps `scout_replan.requests` (mirroring how the existing suite swaps `main.requests`).

SHARED TRANSPORT: `read()` and `write()` below are the Local Agent (port 8090) transport for
the WHOLE station, not only for replanning — `scout_mission_execution.py` calls them for
Scout's mission-execution lifecycle routes. They take a `subsystem` label purely so an error
string names the right API ("replanning" / "mission-execution"); the outcome model, the
timeouts and the mocking surface stay singular, so the two subsystems can never drift apart
on what "unknown" means.
"""
from __future__ import annotations

import requests

# Bounded, distinct connect/read timeouts (seconds). A supervisory op must never hang the
# operator UI; when Scout is slow we return `unknown`/`unavailable` and let a later GET
# reconcile. Writes get a longer read budget than reads because a package PUT can be large.
#
# THESE ARE THE BUDGET FOR A SHORT SUPERVISORY CALL, and they stay that way. A route whose Scout
# side is a LONG BOUNDED TRANSACTION (mission-execution Start: verified LOITER → Home → package
# sync → verified AUTO → progression confirmation, tens of seconds by contract) must not be
# covered by raising THIS number — that would spend the same budget on every config PATCH,
# package PUT and status read on the link. Such a route passes its OWN `read_timeout` to write()
# instead; see scout_mission_execution.START_READ_TIMEOUT for the one that does.
CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 8.0
WRITE_READ_TIMEOUT = 12.0

# Normalized outcome vocabulary — the single set of words the whole station reasons with.
OUTCOME_ACCEPTED = "accepted"
OUTCOME_REJECTED = "rejected"
OUTCOME_UNKNOWN = "unknown"
OUTCOME_UNAVAILABLE = "unavailable"
OUTCOME_UNSUPPORTED = "unsupported"

# Scout body keys that may carry a machine error code, in priority order. Scout's replanning
# codes (PLANNING_PACKAGE_MISSING, TRANSACTION_ACTIVE, HASH_MISMATCH, …) arrive under one of
# these; whichever is present is preserved so the UI shows Scout's reason, not a guess.
_ERROR_CODE_KEYS = ("error_code", "code", "error", "reason", "detail")


def _extract_error_code(body):
    """Scout's machine error code from a response body, or None. Strings only — a nested
    object under `detail` is not a code, so it is skipped rather than stringified into noise."""
    if not isinstance(body, dict):
        return None
    for k in _ERROR_CODE_KEYS:
        v = body.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _parse_body(resp):
    """Scout's JSON body, or {} when there is none / it is not JSON. Never raises."""
    try:
        if resp.content:
            data = resp.json()
            return data if isinstance(data, dict) else {"value": data}
    except Exception:
        pass
    return {}


def _base_result(operation, base):
    """The stable skeleton every normalized result shares, so callers never branch on a
    missing key. `reachable`/`supported` default optimistic and are narrowed on failure."""
    return {
        "operation": operation,
        "base": base,
        "reachable": True,
        "supported": True,
        "ok": False,
        "outcome": None,
        "http_status": None,
        "scout": None,
        "scout_error_code": None,
        "error": None,
    }


def read(operation, base, path, *, subsystem="replanning"):
    """A GET proxy. A transport failure is `unavailable` (reachable:false) — never fabricated
    state. A 404 is `unsupported` (older Scout). A 2xx carries Scout's body verbatim."""
    out = _base_result(operation, base)
    url = base.rstrip("/") + path
    try:
        resp = requests.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    except requests.RequestException as exc:
        out.update(reachable=False, outcome=OUTCOME_UNAVAILABLE,
                   error=f"Scout {subsystem} API unreachable: {exc}")
        return out
    out["http_status"] = resp.status_code
    body = _parse_body(resp)
    out["scout"] = body
    if resp.status_code == 404:
        out.update(supported=False, outcome=OUTCOME_UNSUPPORTED,
                   error=f"Scout does not implement this {subsystem} route")
        return out
    if 200 <= resp.status_code < 300:
        out.update(ok=True, outcome=OUTCOME_ACCEPTED)
        return out
    out.update(outcome=OUTCOME_UNAVAILABLE, scout_error_code=_extract_error_code(body),
               error=f"Scout returned HTTP {resp.status_code}")
    return out


def write(operation, base, path, method, json_body=None, *, subsystem="replanning",
          conflict_error="Scout refused: a replanning transaction is active",
          read_timeout=None):
    """A PUT/PATCH/POST/DELETE proxy with the three-state outcome model. A transport failure
    (timeout / dropped connection) or an ambiguous 5xx is `unknown` — the write MAY have
    landed (Scout's stores are idempotent), so we never call it a failure; a later GET
    reconciles. A definite 4xx is `rejected`, with Scout's error code preserved (409
    TRANSACTION_ACTIVE included, flagged so the UI does not read it as a network fault).

    `read_timeout` overrides WRITE_READ_TIMEOUT for THIS call only, and exists for exactly one
    reason: a Scout route whose bounded transaction legitimately runs longer than a short
    supervisory write. Giving up before Scout's OWN bound expires manufactures an `unknown` for
    an operation that was going to succeed — which is what the operator then reads as a failed
    Start. The CONNECT timeout is deliberately NOT overridable: a Scout that will not accept a
    TCP connection in 3 s is unreachable, however long its transactions take."""
    out = _base_result(operation, base)
    url = base.rstrip("/") + path
    read_budget = WRITE_READ_TIMEOUT if read_timeout is None else float(read_timeout)
    try:
        resp = requests.request(method, url, json=json_body,
                                timeout=(CONNECT_TIMEOUT, read_budget))
    except requests.RequestException as exc:
        # No verdict from Scout. UNKNOWN, never a definite failure — reconcile with a GET.
        out.update(reachable=False, outcome=OUTCOME_UNKNOWN,
                   error=f"No response from Scout — outcome unknown, reconcile with a read: {exc}")
        return out
    out["http_status"] = resp.status_code
    body = _parse_body(resp)
    out["scout"] = body
    out["scout_error_code"] = _extract_error_code(body)
    if resp.status_code == 404:
        out.update(supported=False, outcome=OUTCOME_UNSUPPORTED,
                   error=f"Scout does not implement this {subsystem} route")
        return out
    if 200 <= resp.status_code < 300:
        out.update(ok=True, outcome=OUTCOME_ACCEPTED)
        return out
    if resp.status_code == 409:
        # Deliberate refusal — a definite reject the UI must present as such (a precondition /
        # lifecycle / arbitration conflict), never as a generic network failure.
        out.update(outcome=OUTCOME_REJECTED, transaction_active=True, error=conflict_error)
        return out
    if 400 <= resp.status_code < 500:
        out.update(outcome=OUTCOME_REJECTED,
                   error=f"Scout rejected the operation (HTTP {resp.status_code})")
        return out
    # 5xx — Scout errored, but whether it applied the write first is ambiguous. Treat as
    # UNKNOWN so the caller reconciles rather than assuming failure.
    out.update(outcome=OUTCOME_UNKNOWN,
               error=f"Scout server error (HTTP {resp.status_code}) — outcome unknown")
    return out


# Historic private names, kept so the replanning call sites below read unchanged.
_read, _write = read, write


# ── Planning package (single-slot, idempotent PUT; idempotent DELETE) ─────────────────
def get_planning_package(base):
    return _read("planning_package.get", base, "/agent/replan/planning_package")


def put_planning_package(base, package):
    return _write("planning_package.put", base, "/agent/replan/planning_package", "PUT", package)


def post_planning_package(base, package):
    """POST the replan-planning-package-v1 package to Scout's single package slot.

    Same route, same single slot, same idempotency as the PUT above — Scout accepts both
    verbs. The operator sync uses POST because that is the verb Scout's v1 receiving side
    documents as the submission entry point; a Scout that has not been rebuilt with the v1
    receiver 404s it, which surfaces as `unsupported` (an older Scout) rather than a
    fabricated success. There is deliberately NO automatic fall back to the PUT verb: the
    older PUT handler validates the OLD package shape, so retrying there would trade an
    honest "not supported" for a confusing schema rejection."""
    return _write("planning_package.post", base, "/agent/replan/planning_package", "POST", package)


def delete_planning_package(base):
    return _write("planning_package.delete", base, "/agent/replan/planning_package", "DELETE")


# ── Experiment injection (single explicit PUT; idempotent DELETE) ──────────────────────
def get_experiment(base):
    return _read("experiment.get", base, "/agent/replan/experiment")


def put_experiment(base, overrides):
    return _write("experiment.put", base, "/agent/replan/experiment", "PUT", overrides)


def delete_experiment(base):
    return _write("experiment.delete", base, "/agent/replan/experiment", "DELETE")


# ── Runtime configuration (GET resolved values + sources; PATCH override) ──────────────
def get_config(base):
    return _read("config.get", base, "/agent/replan/config")


def patch_config(base, patch):
    return _write("config.patch", base, "/agent/replan/config", "PATCH", patch)


# ── Replanning status (canonical status object) ───────────────────────────────────────
def get_status(base):
    return _read("status.get", base, "/agent/replan/status")


# ── Controller reset / rearm (rejected while a transaction is active) ──────────────────
def post_reset(base):
    return _write("reset.post", base, "/agent/replan/reset", "POST")
