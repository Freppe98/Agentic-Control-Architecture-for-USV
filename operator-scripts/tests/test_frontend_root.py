"""Backend tests for the frontend mount: the classic static/ dashboard has been
retired, and the modern operator/ dashboard (served at /app) is now the only
supported UI. GET / redirects there instead of serving a page of its own.

Run from operator-scripts/:  python -m unittest tests.test_frontend_root  (no pytest).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class TestRootRedirectsToModernDashboard(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_root_redirects_to_app_with_307(self):
        r = self.client.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 307)
        self.assertEqual(r.headers["location"], "/app/")

    def test_app_serves_modern_dashboard_html(self):
        r = self.client.get("/app/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])
        self.assertIn("USV Fleet Command", r.text)

    def test_root_following_redirect_lands_on_modern_dashboard(self):
        r = self.client.get("/", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("USV Fleet Command", r.text)


class TestClassicFrontendNoLongerServed(unittest.TestCase):
    """The classic dashboard's own identifying strings must not appear anywhere
    the frontend is now served from — proof the old UI is gone, not just hidden."""

    def setUp(self):
        self.client = TestClient(main.app)

    def test_classic_identifiers_absent_from_app_dashboard(self):
        r = self.client.get("/app/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Fleet library", r.text)
        self.assertNotIn("Fleet Library", r.text)
        self.assertNotIn("Mission Actions", r.text)

    def test_classic_static_files_are_gone(self):
        r = self.client.get("/index.html")
        self.assertNotEqual(r.status_code, 200)
        r = self.client.get("/app.js")
        self.assertNotEqual(r.status_code, 200)


class TestBackendApiRoutesRemainAvailable(unittest.TestCase):
    """Removing the classic UI must not touch the backend API surface."""

    def setUp(self):
        self.client = TestClient(main.app)

    def test_fleet_status_still_available(self):
        r = self.client.get("/api/fleet/status")
        self.assertEqual(r.status_code, 200)

    def test_environment_still_available(self):
        r = self.client.get("/api/environment")
        self.assertEqual(r.status_code, 200)

    def test_commands_capabilities_still_available(self):
        r = self.client.get("/api/commands/capabilities")
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
