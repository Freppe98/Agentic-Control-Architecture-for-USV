"""
Minimal mock operator backend for exercising the Local Agent's command path
end-to-end before the real operator backend implements /agent/commands and
/agent/command_result. Dev/test tool only -- not a production service.

Usage:
    python3 mock_operator.py [port]        # default port 8200

    # queue a command for the Scout to pick up on its next poll
    curl -s localhost:8200/test/queue -X POST -H 'Content-Type: application/json' \\
        -d '{"command_type": "SET_MODE_HOLD"}'

    # see command results the Local Agent has posted back
    curl -s localhost:8200/test/results

Point a real Local Agent at this mock with:
    OPERATOR_URLS=http://127.0.0.1:8200 ./run_local_agent.sh
"""
import json
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_pending = []   # commands not yet delivered to the Local Agent
_results = []   # command_result messages received from the Local Agent


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length))

    def do_GET(self):
        if self.path.startswith("/agent/status"):
            self._send_json({"status": "ok"})
        elif self.path.startswith("/agent/commands"):
            global _pending
            due, _pending = _pending, []  # deliver-once
            self._send_json({"commands": due})
        elif self.path == "/test/results":
            self._send_json({"results": _results})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/agent/status":
            self._read_json()
            self._send_json({"ok": True})
        elif self.path == "/agent/command_result":
            msg = self._read_json()
            _results.append(msg)
            print("[MOCK OPERATOR] command_result:", json.dumps(msg))
            self._send_json({"ok": True})
        elif self.path == "/test/queue":
            body = self._read_json()
            command = {
                "command_id": body.get("command_id") or str(uuid.uuid4()),
                "usv_id": body.get("usv_id", "usv-2"),
                "command_type": body["command_type"],
                "issued_at": time.time(),
                "expires_at": body.get("expires_at", time.time() + 60),
                "params": body.get("params", {}),
                "requested_by": body.get("requested_by", "test"),
            }
            _pending.append(command)
            print("[MOCK OPERATOR] queued:", command)
            self._send_json({"queued": command})
        else:
            self._send_json({"error": "not found"}, 404)

    def log_message(self, format, *args):
        pass  # quiet -- explicit prints above are enough


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8200
    print(f"[MOCK OPERATOR] listening on :{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
