#!/usr/bin/env bash
# Quick status check for the Scout Local Mission Agent:
# process, vehicle Flask endpoint, operator reachability, buffer count.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LOCAL_FLASK_URL=$(python3 -c "import config; print(config.LOCAL_FLASK_URL)")
BUFFER_FILE=$(python3 -c "import config; print(config.BUFFER_FILE)")
OPERATOR_URLS_SOURCE=$(python3 -c "import config; print(config.OPERATOR_URLS_SOURCE)")
mapfile -t OPERATOR_URLS < <(python3 -c "import config; print('\n'.join(config.OPERATOR_URLS))")
LOCAL_AGENT_HTTP_HOST=$(python3 -c "import config; print(config.LOCAL_AGENT_HTTP_HOST)")
LOCAL_AGENT_HTTP_PORT=$(python3 -c "import config; print(config.LOCAL_AGENT_HTTP_PORT)")

echo "== Local Agent process =="
pid=$(pgrep -f "[l]ocal_agent.py" | head -1 || true)
if [ -n "$pid" ]; then
    echo "  running (pid $pid)"
else
    echo "  NOT running"
fi

echo
echo "== Vehicle Flask (${LOCAL_FLASK_URL}/agent/state) =="
if resp=$(curl -sf -m 3 "${LOCAL_FLASK_URL}/agent/state"); then
    echo "  reachable"
    python3 -c "
import json, sys
d = json.loads(sys.argv[1])
tel = d.get('telemetry', {})
pix = d.get('health', {}).get('pixhawk', {})
mav = d.get('mavlink', {})
print('  comm_state (agent):', d.get('agent', {}).get('current_communication_state'))
print('  telemetry lat/lng:', tel.get('lat'), tel.get('lng'), ' mode:', tel.get('mode_name'))
print('  pixhawk.connected:', pix.get('connected'), ' gps_fix:', pix.get('gps_fix'))
print('  mavlink_connected:', mav.get('mavlink_connected'), ' heartbeat_age_s:', mav.get('heartbeat_age_s'),
      ' msg_rate_hz:', mav.get('mavlink_msg_rate_hz'))
" "$resp" 2>/dev/null || echo "  (could not parse response as JSON)"
else
    echo "  UNREACHABLE"
fi

echo
echo "== Control authority (${LOCAL_FLASK_URL}/agent/control_authority) =="
if resp=$(curl -sf -m 2 "${LOCAL_FLASK_URL}/agent/control_authority"); then
    echo "  $resp"
else
    echo "  UNREACHABLE (vehicle Flask service not reachable)"
fi

echo
echo "== Operator reachability (source: ${OPERATOR_URLS_SOURCE}) =="
if [ "${#OPERATOR_URLS[@]}" -eq 0 ]; then
    echo "  none configured"
else
    for url in "${OPERATOR_URLS[@]}"; do
        [ -z "$url" ] && continue
        if curl -sf -m 2 -o /dev/null "${url}/agent/status"; then
            echo "  $url -> reachable"
        else
            echo "  $url -> unreachable"
        fi
    done
fi

echo
echo "== Diagnostics HTTP server (${LOCAL_AGENT_HTTP_HOST}:${LOCAL_AGENT_HTTP_PORT}) =="
DIAG_URL="http://127.0.0.1:${LOCAL_AGENT_HTTP_PORT}"
if resp=$(curl -sf -m 3 "${DIAG_URL}/agent/diagnostics"); then
    echo "  reachable"
    python3 -c "
import json, sys
d = json.loads(sys.argv[1])
for k, v in d.items():
    if isinstance(v, dict) and 'status' in v:
        print(f'  {k}: {v[\"status\"]}')
" "$resp" 2>/dev/null || echo "  (could not parse response as JSON)"
else
    echo "  UNREACHABLE"
fi

echo
echo "== Recent transitions (${DIAG_URL}, via a live status if the agent were sending one) =="
echo "  (transitions/decision_reason ship on payload.transitions/payload.agent -- see README \"Richer status payload\")"

echo
echo "== Command history (${DIAG_URL}/agent/command_history) =="
if resp=$(curl -sf -m 3 "${DIAG_URL}/agent/command_history"); then
    python3 -c "
import json, sys
d = json.loads(sys.argv[1])
commands = d.get('commands', [])
print(f'  {len(commands)} recent command(s)')
for c in commands[-5:]:
    print(f\"    {c.get('command_type')} ({c.get('command_id')}): {c.get('status')} -- {c.get('reason')}\")
" "$resp" 2>/dev/null || echo "  (could not parse response as JSON)"
else
    echo "  UNREACHABLE"
fi

echo
echo "== Buffer =="
if [ -f "$BUFFER_FILE" ]; then
    count=$(wc -l < "$BUFFER_FILE" | tr -d ' ')
    echo "  $BUFFER_FILE: $count buffered message(s)"
else
    echo "  $BUFFER_FILE: not present (0 buffered)"
fi
