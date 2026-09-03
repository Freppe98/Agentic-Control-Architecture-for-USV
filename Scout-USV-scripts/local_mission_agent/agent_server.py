"""
Inbound HTTP surface for the Local Agent -- read-only Vehicle Health data
for the operator station.

    GET  /agent/diagnostics       per-component OK/WARNING/FAIL/UNKNOWN
    POST /agent/system_check      PASS/WARN/FAIL/UNKNOWN readiness checklist
    GET  /agent/command_history   rolling record of recent operator command lifecycles
    GET  /agent/decision_timeline rolling record of current_decision changes (Agent page)
    GET  /agent/mission           mission currently stored on the Pixhawk (legacy schema)
    GET  /agent/pixhawk_mission   mission currently stored on the Pixhawk (Pixhawk Mission card schema)
    GET  /agent/mission_operation authoritative record of the most recent MISSION_UPLOAD/MISSION_CLEAR

This is the only inbound listener the Local Agent runs; everything else in
this module is an outbound client (api_client.py) to the vehicle Flask
service and the operator backend. Deliberately stdlib-only (same
http.server.ThreadingHTTPServer approach as mock_operator.py) rather than a
second Flask app, since this process has no other HTTP-serving need.

Started as a daemon thread from local_agent.py's main() -- run() blocks, so
it must never be called on the main thread, which owns the polling loop.

Read-only by construction: every handler only ever calls into diagnostics.py,
mission.py, or pixhawk_mission.py (all GET-only) or command_history.py's
in-memory record of commands the main loop already executed via
_poll_and_execute_commands. No handler here takes a request body that
changes vehicle state, and none can reach a /nav/* or /agent/set_home
endpoint -- there is no code path here that can arm, change mode, set Home,
upload/clear a mission, or otherwise write to Pixhawk. The Operator Backend
is the only thing the frontend/operator UI ever talks to; this process has
no inbound HTTP surface for issuing commands (including SET_HOME, which
reaches the vehicle Flask service exactly the way ARM/RTL/every other
command type does -- queued by the operator backend, polled via
GET /agent/commands, executed by command_handler.py/command_executor.py,
result pushed back via POST /agent/command_result -- see README "Set
Home").
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from diagnostics import build_diagnostics, build_system_check
from mission import build_mission_status
from pixhawk_mission import build_pixhawk_mission_status
import command_history
import mission_operation_status
import transition_log
import replan_api
import mission_execution_api
import experiment_recording_api

# Cap an inbound JSON body so a pathological upload can't exhaust memory before
# planning_package's own MAX_PACKAGE_BYTES check runs.
_MAX_BODY_BYTES = 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        """Parse the request body as JSON. Returns (obj, error_str). A missing
        body is {} (not an error) so a bare DELETE/PATCH still parses."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return None, "invalid Content-Length"
        if length <= 0:
            return {}, None
        if length > _MAX_BODY_BYTES:
            return None, f"request body exceeds {_MAX_BODY_BYTES} bytes"
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8")), None
        except (ValueError, UnicodeDecodeError) as e:
            return None, f"invalid JSON body: {e}"

    def _api_op(self, fn, needs_body=False):
        """Run a replan_api / mission_execution_api operation and send its
        (code, body). Handles body parsing and turns any unexpected exception
        into a 500. Both API surfaces return (http_status, body_dict), so one
        dispatcher serves both."""
        try:
            if needs_body:
                body, err = self._read_json_body()
                if err is not None:
                    self._send_json({"accepted": False,
                                     "error": {"code": "INVALID_REQUEST", "message": err}}, 400)
                    return
                code, out = fn(body)
            else:
                code, out = fn()
            self._send_json(out, code)
        except Exception as e:
            self._send_json({"error": {"code": "INTERNAL", "message": str(e)}}, 500)

    def do_GET(self):
        path = self.path.split("?")[0]
        # ── Replanning read surface (see replan_api.py) ─────────────────────
        if path == "/agent/replan/planning_package":
            return self._api_op(replan_api.get_planning_package)
        if path == "/agent/replan/experiment":
            return self._api_op(replan_api.get_experiment)
        if path == "/agent/replan/config":
            return self._api_op(replan_api.get_config)
        if path == "/agent/replan/status":
            return self._api_op(replan_api.get_status)
        # ── Mission-execution read surface (see mission_execution_api.py) ───
        # Exact-match and above the "/agent/mission" prefix branch below, which
        # would otherwise swallow "/agent/mission_execution/status".
        if path == "/agent/mission_execution/status":
            return self._api_op(mission_execution_api.get_status)
        # Any other /agent/mission_execution/* GET is unknown -- return 404 here
        # so the "/agent/mission" prefix branch below never swallows it (and so
        # GETs on the POST-only start/pause/resume/rearm routes 404 cleanly).
        if path.startswith("/agent/mission_execution/"):
            return self._send_json({"error": "not found"}, 404)
        # ── Experiment-recording read surface (see experiment_recording_api.py).
        # Read-only, best-effort, never affects mission/replan state -- see
        # experiment_recorder.py's fail-open contract.
        if path == "/agent/experiment_recording/status":
            return self._api_op(experiment_recording_api.get_status)
        if path == "/agent/experiment_recording/config":
            return self._api_op(experiment_recording_api.get_config)
        if path == "/agent/experiment_recording/runs":
            return self._api_op(experiment_recording_api.get_runs)
        if path.startswith("/agent/experiment_recording/runs/"):
            run_id = path[len("/agent/experiment_recording/runs/"):]
            return self._api_op(lambda: experiment_recording_api.get_run(run_id))
        if path.startswith("/agent/experiment_recording/"):
            return self._send_json({"error": "not found"}, 404)
        if self.path.startswith("/agent/diagnostics"):
            try:
                self._send_json(build_diagnostics())
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path.startswith("/agent/command_history"):
            try:
                self._send_json({"commands": command_history.get_recent()})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path.startswith("/agent/decision_timeline"):
            # Same rolling data as payload.agent.decision_timeline on every
            # status push (transition_log.py, type=="decision") -- exposed
            # here too so the Agent page can fetch full recent history on
            # load/reconnect without waiting for the next push.
            try:
                self._send_json({"decisions": transition_log.get_recent_by_type("decision")})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path.startswith("/agent/pixhawk_mission"):
            try:
                self._send_json(build_pixhawk_mission_status())
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path.startswith("/agent/mission_operation"):
            # The authoritative persistent record of the most recent
            # MISSION_UPLOAD/MISSION_CLEAR (mission_operation_status.py).
            # Also pushed in every status payload as agent.mission_operation;
            # exposed here too so the terminal counts/hashes/diagnostics can be
            # fetched directly after a comm interruption -- and on the bench,
            # without an operator at all.
            #
            # MUST stay above the "/agent/mission" branch: that one is a prefix
            # of this path and would otherwise swallow it.
            try:
                self._send_json(mission_operation_status.get())
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path.startswith("/agent/mission"):
            try:
                self._send_json(build_mission_status())
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        # The planning-package acceptance surface is exposed under BOTH POST and
        # PUT on the same /agent/replan/planning_package resource (single-slot,
        # idempotent). No route is duplicated -- both verbs dispatch to the one
        # operation in replan_api.
        if path == "/agent/replan/planning_package":
            return self._api_op(replan_api.put_planning_package, needs_body=True)
        if path == "/agent/replan/reset":
            return self._api_op(replan_api.reset, needs_body=True)
        # ── Mission-execution write surface (Local Agent owns each as one
        # transaction; see mission_execution_api.py). Separate from
        # /agent/replan/* on purpose. ─────────────────────────────────────
        if path == "/agent/mission_execution/start":
            return self._api_op(mission_execution_api.start, needs_body=True)
        if path == "/agent/mission_execution/pause":
            return self._api_op(mission_execution_api.pause, needs_body=True)
        if path == "/agent/mission_execution/resume":
            return self._api_op(mission_execution_api.resume, needs_body=True)
        if path == "/agent/mission_execution/rearm":
            return self._api_op(mission_execution_api.rearm, needs_body=True)
        if path == "/agent/mission_execution/stop":
            return self._api_op(mission_execution_api.stop, needs_body=True)
        if path == "/agent/mission_execution/reprove_binding":
            return self._api_op(mission_execution_api.reprove_binding, needs_body=True)
        if path == "/agent/experiment_recording/annotation":
            return self._api_op(experiment_recording_api.post_annotation, needs_body=True)
        if self.path.startswith("/agent/system_check"):
            try:
                self._send_json(build_system_check())
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_PUT(self):
        path = self.path.split("?")[0]
        if path == "/agent/replan/planning_package":
            return self._api_op(replan_api.put_planning_package, needs_body=True)
        if path == "/agent/replan/experiment":
            return self._api_op(replan_api.put_experiment, needs_body=True)
        self._send_json({"error": "not found"}, 404)

    def do_PATCH(self):
        path = self.path.split("?")[0]
        if path == "/agent/replan/config":
            return self._api_op(replan_api.patch_config, needs_body=True)
        if path == "/agent/experiment_recording/config":
            return self._api_op(experiment_recording_api.patch_config, needs_body=True)
        self._send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if path == "/agent/replan/planning_package":
            return self._api_op(replan_api.delete_planning_package)
        if path == "/agent/replan/experiment":
            return self._api_op(replan_api.delete_experiment)
        self._send_json({"error": "not found"}, 404)

    def log_message(self, format, *args):
        pass  # quiet -- local_agent.py's own [LOCAL AGENT] prints are enough


def serve_forever(host: str, port: int) -> None:
    print(f"[LOCAL AGENT] Diagnostics HTTP server listening on {host}:{port} "
          f"(GET /agent/diagnostics, POST /agent/system_check, GET /agent/command_history, "
          f"GET /agent/decision_timeline, GET /agent/mission, GET /agent/pixhawk_mission, "
          f"GET /agent/mission_operation; replanning: GET/POST/PUT/DELETE /agent/replan/planning_package, "
          f"GET/PUT/DELETE /agent/replan/experiment, GET/PATCH /agent/replan/config, "
          f"GET /agent/replan/status, POST /agent/replan/reset; mission execution: "
          f"GET /agent/mission_execution/status, POST /agent/mission_execution/"
          f"{{start,pause,resume,rearm,stop,reprove_binding}}; experiment recording: "
          f"GET/PATCH /agent/experiment_recording/config, GET /agent/experiment_recording/status, "
          f"GET /agent/experiment_recording/runs[/{{run_id}}], "
          f"POST /agent/experiment_recording/annotation)")
    try:
        ThreadingHTTPServer((host, port), Handler).serve_forever()
    except Exception as e:
        print(f"[LOCAL AGENT] Diagnostics HTTP server crashed: {e}", file=sys.stderr)
