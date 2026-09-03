# Outbound buffer — terminal command results unknown to the Operator

**Status: the Operator-side contract this review asked for now exists.**
Sections 1–3 are the original read-only review (retained: the reasoning is what
justifies the contract). **§4 has been superseded — see §4 and §6 for the
implemented cross-system contract as of Operator commit `6a9214b`.**

It records (1) how the Local Agent's outbound buffer behaves when the Operator
cannot match a terminal `command_result`, and (2) the protocol signal that had
to come *from the Operator* before any drop policy was safe.

Scope is the outbound path only: `command_result` messages the Local Agent
pushes to `POST /agent/command_result`. Inbound command polling is unaffected —
the Operator backend owns that queue (see `api_client.get_pending_commands`).

---

## 1. What "rejected as unknown" means here

A terminal `command_result` is delivered by `local_agent._deliver_command_result`
→ `api_client.send_to_operator("/agent/command_result", …)`. The Operator can
answer three ways:

| Operator response | `send_to_operator` behaviour | Classified as |
|---|---|---|
| 2xx | returns `{ok: True}` | delivered |
| **4xx** (e.g. "unknown command_id") | logs body, raises `RuntimeError(... protocol rejection ...)` | **rejection** |
| 5xx / timeout / conn refused / DNS | raises `RuntimeError(No operator reachable ...)` | unreachable |

The 4xx branch ([api_client.py:147-153](api_client.py#L147-L153)) is the
"unknown" case: the Operator *was reached* and refused the payload — a wrong
route, an unrecognised `command_id`, a schema mismatch. Critically, **from the
vehicle side a 4xx is indistinguishable between "transient, will be accepted
later" and "permanent, will never be accepted."** The HTTP status alone does
not carry that intent.

---

## 2. Current behaviour (traced, not assumed)

1. `_deliver_command_result` catches the raised error and calls
   `buffer_message(result_message)` ([local_agent.py:65-67](local_agent.py#L65-L67)).
   The authoritative per-`command_id` result also remains in
   `command_results.json` — `clear_result` is only reached on a *successful*
   send, so a rejected result is retained in **both** places.
2. `buffer.buffer_message` appends to `agent_buffer.jsonl`, **de-duplicated by
   `command_id`** ([buffer.py:30-39](buffer.py#L30-L39)): re-buffering the same
   result replaces its line in place rather than adding a new one. So a single
   rejected result cannot grow the file.
3. The file is bounded at `config.MAX_BUFFERED_MESSAGES` (500), dropping the
   **oldest** entries first if the cap is exceeded across *different* messages.
4. On every loop iteration where `comm_state == CONNECTED` and the buffer is
   non-empty, `flush_buffer(_send_buffered)` retries the whole backlog
   ([local_agent.py:488-494](local_agent.py#L488-L494)). This fires even without
   a comm-down→up edge, precisely to catch results stuck on a route/"unknown"
   rejection.
5. Each retry hits the same 4xx, re-raises, and the message is kept in
   `remaining` — i.e. **retried indefinitely**, on every iteration, forever, as
   long as the Operator keeps returning 4xx.

### Net assessment

- **Safe against unbounded growth** — de-dup by `command_id` + the 500-message
  cap mean a permanently-rejected result cannot blow up the file. ✅
- **Never loses a result to the rejection itself** — nothing drops a
  `command_result` because it was refused; it is only ever evicted if 500
  *newer* messages push it out (a very long, busy outage). This preserves the
  at-least-once guarantee: the vehicle never silently forgets an operation it
  performed. ✅
- **Wastes work on a genuinely-permanent rejection** — a `command_id` the
  Operator will *never* accept (e.g. it was purged from the Operator's command
  table) is retried every iteration for the life of the process, and its
  `command_results.json` entry is retained forever. This is the only real cost,
  and it is bounded/cheap (one POST per iteration per stuck id). ⚠️

### Observed on the bench (2026-07-20, read-only)

`bench_preflight.py`'s `outbound_buffer` check surfaced live evidence of exactly
this state — retained, not dropped:

- `agent_buffer.jsonl`: 1 buffered `command_result` (`cb7bb60f-…` SET_HOME, `executed`)
- `command_results.json`: 4 retained ids — `authority-gate-local-agent-1`,
  `authority-gate-operator-1`, `src-provenance-1`, `cb7bb60f-…`

Several of these are synthetic/test-shaped ids that a live Operator would reject
as unknown — a faithful example of the reject-forever path above.

---

## 3. Why the Agent cannot decide to drop on its own

Dropping a rejected `command_result` is a decision about *intent* the HTTP 4xx
does not encode. The Agent has only two locally-available signals, and neither
is sufficient:

- **Status code** — 4xx conflates "bad right now" (Operator mid-deploy, stale
  route, transient schema skew) with "bad forever" (id purged). Dropping on any
  4xx would discard results during a recoverable Operator-side blip, breaking
  at-least-once exactly when it matters.
- **Age / retry count** — "rejected N times" or "older than T" is a proxy, not
  proof. A long comm/Operator outage looks identical to a permanent rejection
  until the Operator returns. A TTL here would silently lose the outcome in the
  precise long-outage case the buffer exists for (same reasoning as
  `mission_operation_status`' deliberate no-TTL retention).

So a safe drop policy needs a signal the Operator must *emit*, not one the Agent
can infer.

---

## 4. Protocol assumption needed from Operator — SUPERSEDED, see §6

> **Superseded by Operator commit `6a9214b`.** This section is kept because it
> states the requirement the Operator ultimately satisfied. One prediction here
> was wrong in a way that matters: it assumed the terminal signal would arrive
> as a field on a **4xx rejection** body. The Operator instead delivers it on a
> **HTTP 200** — see §6.

**Assumption adopted at the time (no code change):** *every* 4xx rejection of a
`command_result` is **transient and retryable**, and **nothing is ever dropped
for being rejected.** A result leaves the buffer on exactly one event: a
successful (2xx) delivery that the Operator acknowledges, which triggers
`clear_result`. This is what the code already does, and it is the conservative,
at-least-once-preserving default. Keep it until the contract below exists.

**The one thing the Operator must provide to enable safe dropping:** an
**explicit terminal-disposition acknowledgement** on the
`/agent/command_result` response that distinguishes the two intents the status
code cannot:

| Operator disposition | Meaning | Agent action |
|---|---|---|
| `ACCEPTED` (2xx) | result recorded | `clear_result` — stop retaining (already implemented) |
| `RETRY` (4xx/5xx, transient) | not recorded yet, resend later | keep buffered, keep retrying (current default for *all* rejections) |
| `TERMINAL_UNPROCESSABLE` (explicit) | Operator will *never* accept this `command_id`; it is safe for the vehicle to stop trying | **only then** drop from `agent_buffer.jsonl` **and** `command_results.json` |

The essential property: **`TERMINAL_UNPROCESSABLE` must be an affirmative
statement by the Operator that it has permanently and deliberately abandoned
this `command_id`** — not merely the absence of the id, not a generic 400, and
not something the Agent infers from repetition or age. Concretely it should be
a machine-readable field in the rejection body, e.g.
`{"disposition": "terminal_unprocessable", "command_id": "...", "reason": "..."}`,
so `send_to_operator` can classify it as a *third* outcome alongside the
delivered / rejected / unreachable it already separates.

Until the Operator emits that field, the correct behaviour is the current one:
**retain and retry, drop nothing.** That is the assumption this bench run
proceeds under.

---

## 5. What was deliberately NOT done

- No buffered data deleted or drained (`agent_buffer.jsonl`,
  `command_results.json` untouched).
- No change to `buffer.py`, `api_client.send_to_operator`, `command_results.py`,
  or the flush loop — this is a review, and the drop path is intentionally
  blocked on the Operator-side contract above.
- `bench_preflight.py` only *reports* buffer contents (read-only); it has no
  code path that clears, acks, or resends them.

---

## 6. Implemented cross-system contract (Operator `6a9214b`)

### 6.1 What the Operator now emits

`POST /agent/command_result` with a **present but unknown** `command_id` no
longer 4xx-rejects. It answers **HTTP 200**:

```json
{"ok": true, "found": false, "applied": false,
 "orphaned": true, "error": "unknown command id"}
```

Semantics: the result was **not applied** to any current Operator command; it
was **archived as an orphaned historical audit record**; the acknowledgement is
**terminal**; Scout must **stop retrying**.

### 6.2 The correction to §4's prediction

§4 expected this signal on a 4xx and concluded the Agent would need a *third*
outcome alongside delivered/rejected/unreachable. Because the Operator chose
**200**, it lands on the existing **delivered** path instead — so both removals
this contract requires were *already happening* before any code was written:

| Store | Mechanism | Already correct? |
|---|---|---|
| `agent_buffer.jsonl` | `send_fn` returns without raising → `flush_buffer` omits it from `remaining` ([buffer.py:75-84](buffer.py#L75-L84)) | ✅ yes |
| `command_results.json` | `clear_result(command_id)` on the no-exception path of both `_deliver_command_result` and `_send_buffered` | ✅ yes |

The residual gap was **not retention — it was truth**: an orphan ack was
indistinguishable from a real apply in logs and in code. A vehicle operation
the Operator has no live command for was passing as a normal delivery.

### 6.3 What was implemented

`api_client.classify_command_result_ack()` classifies a successful send into
`ACK_APPLIED` / `ACK_TERMINAL_ORPHAN` / `ACK_ACCEPTED`;
`local_agent._note_command_result_ack()` applies it on both delivery paths and
logs an orphan distinctly. **Retention behaviour is deliberately unchanged.**

| Operator response | Disposition | Buffer | `command_results.json` |
|---|---|---|---|
| 2xx, `applied: true` | `ACK_APPLIED` | dropped (sent) | cleared |
| 2xx, `orphaned: true` **and** `found: false` | `ACK_TERMINAL_ORPHAN` | dropped (sent) | cleared |
| 2xx, any other body | `ACK_ACCEPTED` | dropped (sent) | cleared |
| **4xx** (malformed, unknown route, auth) | — raises | **kept** | **retained** |
| **5xx / timeout / conn refused / DNS** | — raises | **kept** | **retained** |

Invariants this rests on:

1. **`orphaned` is never `applied`.** The orphan test runs *first*, so a
   contradictory body carrying both flags can never report as applied.
2. **Terminal orphan is only ever read from an affirmative statement.** Both
   flags are required and compared with `is` against literal booleans. A body
   omitting them, carrying half the pair (`{"found": false}` alone), using
   strings, or not a JSON object is `ACK_ACCEPTED` — never inferred to be an
   orphan.
3. **Known-command semantics unchanged.** A 2xx has always meant "stop
   retaining" (§4); an unrecognised 2xx body still clears, as an ordinary
   accepted delivery. It is not *silently* cleared — it is classified as
   `ACK_ACCEPTED`, not as something the Operator abandoned. Declining to
   narrow this preserves compatibility with an Operator build that answers a
   bare `{"ok": true}`, which under a stricter rule would be retained and
   retried forever.
4. **Nothing is dropped for age or retry count.** §3's reasoning stands
   unchanged; only an affirmative Operator disposition ends retention.
5. **Only `command_result` messages consult the disposition.** A buffered
   status message flushes exactly as before.

Covered by `test_orphan_acknowledgement.py` (15 tests), including duplicate
orphan acks (harmless — `clear_result` on a missing id is a no-op) and a
50-iteration retry loop proving a retryable failure is never dropped.

### 6.4 Residual: non-JSON 2xx bodies — RESOLVED

**Was:** `send_to_operator` built its return with a bare `r.json()`, so a **2xx
with an empty (204) or non-JSON body** raised `ValueError` out of the *success*
path. The caller could not distinguish that from a real send failure: it
buffered an **actually-delivered** result and retried it every iteration,
forever. The `command_results.json` entry was retained alongside it.

**Now:** the success path builds its body through `api_client._success_body()`,
which falls back to an explicitly-marked, **length-bounded** representation
instead of raising:

```json
{"body_format": "non_json", "text": "<first 200 chars, or empty string>"}
```

The bound (`api_client.MAX_NON_JSON_BODY_CHARS`) applies to both the returned
metadata and the log line, so a 2xx that is accidentally an HTML error page or
a proxy banner cannot dump an unbounded body into either.

The governing rule is now stated once and applied uniformly: **every successful
HTTP 2xx is a terminal acknowledgement — the status, not the body, is what says
"stop retaining."**

| Operator response | Disposition | Buffer | `command_results.json` |
|---|---|---|---|
| 2xx, `applied: true` | `ACK_APPLIED` | dropped (sent) | cleared |
| 2xx, `orphaned: true` **and** `found: false` | `ACK_TERMINAL_ORPHAN` | dropped (sent) | cleared |
| 2xx, any other JSON body | `ACK_ACCEPTED` | dropped (sent) | cleared |
| **2xx, empty or non-JSON body** | **`ACK_ACCEPTED`** | **dropped (sent)** | **cleared** |
| 4xx / 5xx / timeout / conn refused / DNS | — raises | kept | retained |

`classify_command_result_ack` is unchanged and backward compatible: the
`non_json` marker carries neither flag, so it lands on the existing
`ACK_ACCEPTED` path. **Disposition is never read out of response text** — a 2xx
body of the literal string `"applied"` or `"orphaned"` classifies as
`ACK_ACCEPTED`, because only an affirmative JSON statement can make a delivery
APPLIED or TERMINAL_ORPHAN. §6.3's invariants 1–5 all still hold; retryable
failures are untouched.

Covered by `test_non_json_acknowledgement.py` (15 tests): applied/orphan JSON
still classify and clear both stores, 204-empty and 200-plain-text clear both
stores, text is bounded in the return value and the log, 400/500/timeout/
connection failure still retain in both stores, and status-message delivery is
unaffected.
