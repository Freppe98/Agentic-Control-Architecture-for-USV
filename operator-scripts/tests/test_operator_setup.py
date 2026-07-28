"""Regression tests for the Windows install/bootstrap workflow.

These lock in the *contract* of install_operator.ps1 / run_operator_backend.ps1 and the
supporting manifests (requirements.txt, package.json/-lock, .env.example, .gitignore) so
a future edit can't silently break the "fresh clone -> two commands -> running station"
promise. Two of them actually execute PowerShell (syntax validity + the run script's
missing-venv guard); those skip cleanly when no PowerShell interpreter is on PATH (e.g.
a non-Windows CI runner). The rest are content/static checks and run everywhere.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

OP_DIR = Path(__file__).resolve().parent.parent
INSTALL_PS1 = OP_DIR / "install_operator.ps1"
RUN_PS1 = OP_DIR / "run_operator_backend.ps1"
START_HIDDEN_PS1 = OP_DIR / "start_operator_hidden.ps1"
STOP_HIDDEN_PS1 = OP_DIR / "stop_operator_hidden.ps1"
REQUIREMENTS = OP_DIR / "requirements.txt"
ENV_EXAMPLE = OP_DIR / ".env.example"
GITIGNORE = OP_DIR / ".gitignore"
PACKAGE_JSON = OP_DIR / "package.json"
PACKAGE_LOCK = OP_DIR / "package-lock.json"

POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def _run_powershell(command, cwd=None, env=None, timeout=60):
    """Run a PowerShell -Command and return (returncode, stdout, stderr)."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


class PowerShellSyntaxTests(unittest.TestCase):
    """The scripts must parse — a syntax error would fail only at run time on a fresh box."""

    @unittest.skipUnless(POWERSHELL, "no PowerShell interpreter on PATH")
    def test_scripts_parse_without_errors(self):
        parse = (
            "$errs = $null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            "$env:PS_TARGET, [ref]$null, [ref]$errs) | Out-Null; "
            "if ($errs) { $errs | ForEach-Object { $_.Message }; exit 1 } else { exit 0 }"
        )
        for script in (INSTALL_PS1, RUN_PS1, START_HIDDEN_PS1, STOP_HIDDEN_PS1):
            with self.subTest(script=script.name):
                rc, out, err = _run_powershell(parse, env={"PS_TARGET": str(script)})
                self.assertEqual(rc, 0, f"{script.name} has PowerShell parse errors:\n{out}\n{err}")


class RunScriptBehaviourTests(unittest.TestCase):
    """The run script must refuse to launch without a venv and point at the installer."""

    @unittest.skipUnless(POWERSHELL, "no PowerShell interpreter on PATH")
    def test_missing_venv_is_rejected_clearly(self):
        # Copy just the run script into an isolated dir with NO .venv, so running it hits
        # the guard and exits before it would ever start uvicorn. Nothing is installed.
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "run_operator_backend.ps1"
            shutil.copyfile(RUN_PS1, dest)
            rc, out, err = _run_powershell(
                f"& '{dest}'", cwd=tmp, timeout=60
            )
            combined = out + err
            self.assertEqual(rc, 1, f"expected non-zero exit, got {rc}. Output:\n{combined}")
            self.assertIn("install_operator.ps1", combined)
            self.assertRegex(combined, r"(?i)virtual environment not found")


class InstallerContractTests(unittest.TestCase):
    """Static guarantees about install_operator.ps1's behaviour."""

    @classmethod
    def setUpClass(cls):
        cls.text = INSTALL_PS1.read_text(encoding="utf-8")

    def test_has_shortcut_switches(self):
        # -CreateShortcut and -SkipShortcut must be present for scriptable control.
        self.assertRegex(self.text, r"\[switch\]\s*\$CreateShortcut")
        self.assertRegex(self.text, r"\[switch\]\s*\$SkipShortcut")

    def test_interactive_shortcut_prompt_with_safety(self):
        # The shortcut step must have an interactive prompt (Read-Host) wrapped in
        # try/catch so non-interactive/CI invocations don't hang.
        self.assertIn("Read-Host", self.text)
        self.assertIn("try", self.text)
        self.assertIn("catch", self.text)

    def test_uses_psscriptroot(self):
        self.assertIn("$PSScriptRoot", self.text)

    def test_uses_venv_python_for_python_ops(self):
        # All Python work must go through the venv interpreter, never a bare `python`.
        self.assertIn(r".venv", self.text)
        self.assertIn(r"Scripts\python.exe", self.text)
        self.assertIn("$VenvPython -m pip install", self.text)
        self.assertIn("$VenvPython -m unittest", self.text)

    def test_checks_every_prerequisite(self):
        # git/node/npm are probed directly; python is probed through a python/py loop
        # (`$cand`), so assert the loop + version parse rather than a literal name.
        for tool in ("git", "node", "npm"):
            with self.subTest(tool=tool):
                self.assertRegex(self.text, rf"Get-Command {tool}\b")
        self.assertRegex(self.text, r"@\('python',\s*'py'\)")
        self.assertRegex(self.text, r"Get-Command \$cand\b")

    def test_verifies_python_and_node_versions(self):
        self.assertIn("MinPythonMinor", self.text)
        self.assertIn("MinNodeMajor", self.text)

    def test_creates_venv_only_when_missing(self):
        # Guarded creation = idempotent; reuse an existing valid venv.
        self.assertRegex(self.text, r"if\s*\(Test-Path\s+\$VenvPython\)")
        self.assertIn("-m venv", self.text)

    def test_npm_ci_gated_on_lockfile(self):
        self.assertRegex(self.text, r"if\s*\(Test-Path\s+\$Lockfile\)")
        self.assertIn("npm ci", self.text)

    def test_env_copied_only_when_absent(self):
        # Must never clobber an existing .env: the copy sits in the else-branch of a
        # Test-Path $EnvFile check.
        self.assertRegex(self.text, r"if\s*\(Test-Path\s+\$EnvFile\)")
        self.assertIn("Copy-Item", self.text)
        # The .env branch must be reached only after confirming .env is absent.
        env_idx = self.text.index("Test-Path $EnvFile")
        copy_idx = self.text.index("Copy-Item")
        self.assertLess(env_idx, copy_idx)

    def test_missing_requirements_fails_clearly(self):
        self.assertRegex(self.text, r"if\s*\(-not\s*\(Test-Path\s+\$Requirements\)\)")
        self.assertRegex(self.text, r"Fail\b.*requirements\.txt", )

    def test_bounded_smoke_check(self):
        # A timeout so a hang can't wedge the installer, and a positive success token.
        self.assertIn("Wait-Job", self.text)
        self.assertIn("-Timeout", self.text)
        self.assertIn("IMPORT-OK", self.text)

    def test_has_skiptests_switch(self):
        self.assertRegex(self.text, r"\[switch\]\s*\$SkipTests")

    def test_fails_with_nonzero_exit(self):
        self.assertRegex(self.text, r"function\s+Fail")
        self.assertIn("exit 1", self.text)

    def test_prints_launch_command(self):
        self.assertIn("run_operator_backend.ps1", self.text)


class RunScriptContractTests(unittest.TestCase):
    """Static guarantees about run_operator_backend.ps1."""

    @classmethod
    def setUpClass(cls):
        cls.text = RUN_PS1.read_text(encoding="utf-8")

    def test_uses_venv_python(self):
        self.assertIn(r".venv\Scripts\python.exe", self.text)
        self.assertRegex(self.text, r"&\s+\$VenvPython\s+-m\s+uvicorn\s+main:app")

    def test_guards_missing_venv(self):
        self.assertRegex(self.text, r"if\s*\(-not\s*\(Test-Path\s+\$VenvPython\)\)")
        self.assertIn("install_operator.ps1", self.text)
        self.assertIn("exit 1", self.text)

    def test_preserves_binding_port_and_interface_listing(self):
        # Default 0.0.0.0:8210 and the multi-interface address print must survive.
        self.assertIn("8210", self.text)
        self.assertIn("0.0.0.0", self.text)
        self.assertIn("Get-NetIPAddress", self.text)
        self.assertIn("--no-access-log", self.text)


class RequirementsTests(unittest.TestCase):
    """requirements.txt must be an exactly-pinned, reviewed list of the direct deps."""

    def test_direct_deps_present_and_exactly_pinned(self):
        lines = [
            ln.strip()
            for ln in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        pins = {}
        for ln in lines:
            m = re.match(r"^([A-Za-z0-9_.\-]+)==([0-9][^\s#]*)$", ln)
            self.assertIsNotNone(m, f"requirement not exactly pinned (pkg==version): {ln!r}")
            pins[m.group(1).lower()] = m.group(2)
        for pkg in ("fastapi", "uvicorn", "requests", "shapely", "pyproj", "numpy", "httpx"):
            self.assertIn(pkg, pins, f"{pkg} missing from requirements.txt")

    def test_no_transitive_noise(self):
        # Guard against someone pasting `pip freeze`: these transitive deps must not be
        # listed as direct requirements.
        text = REQUIREMENTS.read_text(encoding="utf-8").lower()
        for noise in ("starlette==", "pydantic==", "anyio==", "click=="):
            self.assertNotIn(noise, text, f"unexpected transitive pin: {noise}")


class EnvExampleTests(unittest.TestCase):
    def test_documents_configurable_values_without_secrets(self):
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("OPERATOR_BACKEND_PORT", text)
        self.assertIn("OPERATOR_BACKEND_HOST", text)
        # No secret *assignments* (KEY=value). The words may appear in prose ("no secrets
        # here"), so only flag an actual `password=...` / `token=...` style line.
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key = stripped.split("=", 1)[0].strip().lower()
            for secret in ("password", "secret", "token", "api_key", "apikey"):
                self.assertNotIn(secret, key, f"looks like a secret assignment: {line!r}")


class GitignoreTests(unittest.TestCase):
    def test_required_entries_present(self):
        entries = {
            ln.strip()
            for ln in GITIGNORE.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        }
        for required in (".venv/", "node_modules/", ".env", "__pycache__/", "*.pyc", ".pytest_cache/", ".operator.pid", "logs/"):
            self.assertIn(required, entries, f"{required} missing from .gitignore")


class PackageLockTests(unittest.TestCase):
    def test_package_and_lock_agree(self):
        pkg = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
        lock = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
        self.assertEqual(pkg.get("name"), lock.get("name"))
        # npm ci requires lockfileVersion >= 2 (the `packages` map).
        self.assertGreaterEqual(lock.get("lockfileVersion", 0), 2)


class VenvImportTests(unittest.TestCase):
    """When this suite is run by the venv interpreter, the whole backend must import.

    This is the in-process form of the installer's smoke check: it proves the created
    environment is complete (core + geometry stack) rather than merely present.
    """

    def test_backend_and_geometry_stack_import(self):
        import importlib

        for mod in ("main", "mission_contract", "planning", "shapely", "pyproj", "numpy",
                    "fastapi", "uvicorn", "requests", "httpx"):
            with self.subTest(module=mod):
                importlib.import_module(mod)


class HiddenLauncherScriptTests(unittest.TestCase):
    """Static guarantees about start_operator_hidden.ps1 and stop_operator_hidden.ps1."""

    def test_start_hidden_script_has_required_functions(self):
        text = START_HIDDEN_PS1.read_text(encoding="utf-8")
        # Must have helpers for MessageBox and server-ready tests.
        self.assertIn("Show-MessageBox", text)
        self.assertIn("Test-ServerReady", text)
        # Must guard the venv.
        self.assertIn(".venv\\Scripts\\python.exe", text)
        # Must use .operator.pid for the PID file.
        self.assertIn(".operator.pid", text)

    def test_stop_hidden_script_has_required_functions(self):
        text = STOP_HIDDEN_PS1.read_text(encoding="utf-8")
        # Must have MessageBox helper.
        self.assertIn("Show-MessageBox", text)
        # Must read and kill using the PID file.
        self.assertIn(".operator.pid", text)
        self.assertIn("taskkill", text)


if __name__ == "__main__":
    unittest.main()
