# Scout companion-v2 payload captures

This directory is where **real, off-the-wire `POST /agent/status` bodies from Scout's actual
companion-reporting implementation** go, once they exist. It is empty today — Scout has not
implemented `companion-v2` yet (see `../../COMPANION_CONTRACT.md`'s status line) — and that is
an expected, honest state, not a bug.

## Why this directory is separate from `scripts/companion_fixture.py`

`scripts/companion_fixture.py` and `tests/test_companion_telemetry.py` are **operator-authored**:
they encode this station's own reading of the contract. A payload built by either one proves the
operator parses what *we* think Scout should send — it proves nothing about what Scout's actual
`session`/`generation` counter persistence, clock handling, or hazard/replan reporting really
produce. Only a capture of Scout's own real output establishes that.

## What to put here

One file per capture, `*.json`, each either:

- a single JSON object — one captured envelope, exactly the shape
  `tests/fixtures/scout-status-live.json` already uses (that file is the established
  "captured off the wire" convention this extends to `companion-v2` — see
  `tests/test_vehicle_telemetry.py`'s module docstring for how it was captured), or
- a JSON array of envelope objects — an ordered **sequence** capture (e.g. assignment -> a
  hazard reported -> Scout accepts it -> replanning verified, or a companion-subsystem restart),
  replayed to the operator in file order.

Capture the envelope **exactly as Scout composed it** — no hand-editing, no filling in fields
the operator "probably wants." A field Scout's real code omits should stay omitted; that is
itself part of what this capture is meant to reveal.

See `SCOUT_INTEGRATION_HANDOFF.md`'s **"Handing back a compatibility capture"** section for the
full instructions this is the target of, and `scripts/scout_payload_replay.py` for how these
files get replayed through the real operator endpoint in an isolated runtime directory.

## Running the replay once files exist

```
python scripts/scout_payload_replay.py
python -m unittest tests.test_scout_companion_compat -v
```

Neither command touches `runtime_data/` or any live vehicle state — both force an isolated,
temporary runtime directory before `main.py` is ever imported.
