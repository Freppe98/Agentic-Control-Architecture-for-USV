# Scout quick-move-v1 payload captures

This directory is where **real, off-the-wire `POST /agent/status` bodies from Scout's actual
quick-move implementation** go — the files Scout's `quick_move_fixture.py --export-dir
scout_quick_move_captures` produces.

## Status: the real captures are NOT here yet

Scout's own `QUICK_MOVE_HANDOFF.md` (checkpoint `a102f21a`) says the exported captures live in
`scout_quick_move_captures/` **in the Scout repository, gitignored**, and asks for them to be
handed to this directory. They have not been. What is here instead is a **reconstruction**:

| File | What it is |
|---|---|
| `RECONSTRUCTED-from-scout-handoff.json` | The eight scenarios **transcribed from Scout's handoff prose**, not captured from Scout's code. |

**A reconstruction is weaker evidence than a capture, and the difference is the whole point of
this directory.** It proves the operator handles the contract *as Scout documented it*. It
cannot prove the operator handles what Scout's publisher *actually emits* — exact field
spellings, which optional fields are really omitted, how `updated_at` is really stamped, what a
capability-only heartbeat really looks like on the wire. Those are precisely the things a
written description smooths over, and precisely what has bitten this integration before (see
`COMPANION_CONTRACT.md` on `battery_remaining=-1`, and Scout's own mode/completion coherence
bug, which a prose description of the contract would never have revealed).

**Replace this file with the real captures when they arrive.** The replay tooling and the
compatibility test both pick up whatever `*.json` is here, so nothing else has to change.

## What to put here

One file per capture, `*.json`, each either a single JSON object (one captured envelope) or a
JSON array of envelopes (an ordered **sequence** capture, replayed in file order). Scout's
eight fixture scenarios are naturally one file each.

Capture the envelope **exactly as Scout composed it** — no hand-editing, no filling in fields
the operator "probably wants". A field Scout's real code omits must stay omitted; that is
itself part of what the capture exists to reveal.

Scout's fixture names its vehicle with a `-fixture` suffix (`usv-2-fixture`) and suffixes the
display name `"(SIMULATED QUICK MOVE FIXTURE)"`. **Leave both exactly as captured** — see
"Correlated command ids" below for how the replay handles the identity, and
`../../../QUICK_MOVE_HANDOFF.md` §12 for why the suffix matters.

## Correlated command ids

A quick move is **correlated**: `payload.quick_move.active.command_id` names an operator
command, and the operator deliberately refuses a status for an id it never issued
(`unknown_command_id`), so a delayed or foreign report can never put a target on the map.

A capture recorded on Scout carries ids from *Scout's* run. Replaying it bare therefore proves
only that the block **parses** — every status is correctly refused. To exercise the applied
path, the replay mints a real operator command per captured id and rewrites that id in memory:

```
python scripts/scout_payload_replay.py tests/fixtures/scout_quick_move_payloads/ --correlate-commands
```

The capture files on disk are **never modified** — a capture is evidence, and evidence the
harness edits in place is not evidence any more. Every rewrite is printed.

## Running the replay and the compatibility test

```
python scripts/scout_payload_replay.py tests/fixtures/scout_quick_move_payloads/ --correlate-commands
python -m unittest tests.test_scout_quick_move_compat -v
```

Neither touches `runtime_data/` or any live vehicle state — both force an isolated, temporary
runtime directory before `main.py` is ever imported. Neither sends anything to a vehicle: these
replay **Scout -> operator reporting**, which is the opposite direction from a command.
