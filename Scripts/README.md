# Scripts/ — early prototype (not the final system)

This directory is an early exploratory prototype of Local Mission Agent logic (a
lightweight FSM template, MAVLink/UDP adapters, a status-collector/reporting-policy
sketch). It predates and is **not** the final deployed Local Mission Agent.

- The final Scout Local Mission Agent lives in the separate **AqualityONE** vehicle
  codebase, deployed on the Raspberry Pi 5 aboard the USV — not in this repository.
- `operator-scripts/` is the current, final Operator Station implementation (backend +
  frontend). Nothing in `operator-scripts/` imports from this directory.

Kept only as design history; not run, tested, or maintained as part of the submitted
system.
