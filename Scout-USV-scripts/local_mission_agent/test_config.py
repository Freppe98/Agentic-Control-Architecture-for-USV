"""
Tests for the operator-endpoint configuration (config.py).

These pin the current Operator-backend deployment: after the desktop URL
change and the addition of the aquality laptop as an operator station, all
three configured stations answer on port 8210 (the port the laptop backend
moved to from the historical 8200). They also pin that ./run_local_agent.sh
loads the built-in DEFAULT_OPERATOR_URLS (it sets no OPERATOR_URLS override
and no local_config.py is committed). Run directly:

    python3 test_config.py
"""
import os
import re
import unittest

import config

_HERE = os.path.dirname(os.path.abspath(__file__))
_RUN_SCRIPT = os.path.join(_HERE, "run_local_agent.sh")

DESKTOP = "http://10.0.0.23:8210"          # Fredrik desktop
LAPTOP = "http://10.0.0.24:8210"           # Fredrik laptop
AQUALITY_LAPTOP = "http://10.0.0.25:8210"  # aquality laptop

EXPECTED_DEFAULTS = [DESKTOP, LAPTOP, AQUALITY_LAPTOP]


def _port_for_host(urls, host):
    for u in urls:
        m = re.match(r"https?://([^:/]+):(\d+)", u)
        if m and m.group(1) == host:
            return int(m.group(2))
    return None


class TestOperatorEndpoints(unittest.TestCase):
    def test_all_stations_resolve_to_8210(self):
        # After the backend move, every configured operator station answers on
        # 8210 -- desktop, laptop, and the aquality laptop alike.
        for host in ("10.0.0.23", "10.0.0.24", "10.0.0.25"):
            self.assertEqual(_port_for_host(config.DEFAULT_OPERATOR_URLS, host), 8210)

    def test_no_station_points_at_stale_8200(self):
        # The historical 8200 port is dead for every station -- no entry may map
        # any known operator host to it.
        for u in config.DEFAULT_OPERATOR_URLS:
            for host in ("10.0.0.23", "10.0.0.24", "10.0.0.25"):
                self.assertNotIn(f"{host}:8200", u)

    def test_default_list_is_exactly_the_three_stations(self):
        self.assertEqual(list(config.DEFAULT_OPERATOR_URLS), EXPECTED_DEFAULTS)

    def test_run_script_loads_default_config(self):
        # ./run_local_agent.sh must not inject an OPERATOR_URLS override; with
        # no env var and no local_config.py, resolution falls through to
        # DEFAULT_OPERATOR_URLS -- the source this change edits.
        with open(_RUN_SCRIPT) as f:
            script = f.read()
        self.assertNotIn("OPERATOR_URLS", script)
        self.assertIn("local_agent.py", script)

    def test_resolved_urls_match_defaults_when_unoverridden(self):
        # When neither the env var nor local_config.py is present (the state
        # ./run_local_agent.sh runs in on the Scout), the resolver returns the
        # defaults. Re-resolve freshly rather than reading config.OPERATOR_URLS:
        # other test modules mutate that global at import time (e.g.
        # test_control_authority clears it), so under `unittest discover` the
        # live global is not a reliable witness.
        if not os.environ.get("OPERATOR_URLS") and not _committed_modules():
            urls, source = config._load_operator_urls()
            self.assertEqual(source, "default (config.DEFAULT_OPERATOR_URLS)")
            self.assertEqual(list(urls), EXPECTED_DEFAULTS)


def _committed_modules():
    # local_config.py is gitignored; treat its presence as a machine-local
    # override that legitimately changes OPERATOR_URLS_SOURCE.
    return [f[:-3] for f in os.listdir(_HERE) if f == "local_config.py"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
