"""DATABASE SIGNALS tile: rebase to zero without deleting any receipt.

The tile must never be reset by deleting rows. Completed-trade records, the
Incoming LuxAlgo Signals Status table and the execution journals all read the
same receipts, so a DELETE would silently destroy verified trade history.
"""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import app as app_module
from app import app


class FakeCursor:
    """Minimal stand-in for a psycopg dict-row cursor.

    Records every statement so the tests can assert on the SQL actually
    issued, and answers the handful of shapes _signal_count_baseline uses.
    """

    def __init__(self, max_id=572, baseline=None, baseline_at=None):
        self.max_id = max_id
        self.baseline = baseline
        self.baseline_at = baseline_at
        self.statements = []
        self._result = None

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params))
        normalised = " ".join(sql.split())
        if normalised.startswith("CREATE TABLE"):
            self._result = None
        elif normalised.startswith("SELECT baseline_id"):
            self._result = {"baseline_id": self.baseline} if self.baseline is not None else None
        elif "MAX(id)" in normalised:
            self._result = {"watermark": self.max_id}
        elif normalised.startswith("INSERT INTO luxalgo_signal_count_baseline"):
            written = int(params[0])
            if self.baseline is None or written > self.baseline:
                self.baseline = written
                self.baseline_at = datetime.now(timezone.utc)
            self._result = {"baseline_id": self.baseline, "reset_at": self.baseline_at}
        else:  # pragma: no cover - unexpected statement is a test failure
            self._result = None

    def fetchone(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _FakeConnection:
    """Context-manager stand-in for a psycopg connection."""

    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class SignalCountResetTests(unittest.TestCase):
    def test_no_baseline_and_no_env_returns_zero_baseline(self):
        cur = FakeCursor(max_id=572, baseline=None)
        with patch.dict("os.environ", {}, clear=False):
            with patch.dict("os.environ"):
                import os
                os.environ.pop(app_module.SIGNAL_COUNT_RESET_ENV, None)
                self.assertEqual(app_module._signal_count_baseline(cur), 0)

    def test_stored_baseline_is_honoured_without_env(self):
        cur = FakeCursor(max_id=572, baseline=572)
        import os
        with patch.dict("os.environ", {}):
            os.environ.pop(app_module.SIGNAL_COUNT_RESET_ENV, None)
            self.assertEqual(app_module._signal_count_baseline(cur), 572)

    def test_env_seed_advances_baseline_to_current_max(self):
        cur = FakeCursor(max_id=572, baseline=None)
        with patch.dict("os.environ", {app_module.SIGNAL_COUNT_RESET_ENV: "2026-11-19T18:00:00Z"}):
            self.assertEqual(app_module._signal_count_baseline(cur), 572)

    def test_env_seed_never_moves_baseline_backwards(self):
        # A stale env value must not resurrect the 572 already hidden.
        cur = FakeCursor(max_id=572, baseline=572)
        with patch.dict("os.environ", {app_module.SIGNAL_COUNT_RESET_ENV: "2026-11-01T00:00:00Z"}):
            self.assertEqual(app_module._signal_count_baseline(cur), 572)

    def test_env_seed_is_idempotent_across_redeploys(self):
        cur = FakeCursor(max_id=572, baseline=None)
        env = {app_module.SIGNAL_COUNT_RESET_ENV: "2026-11-19T18:00:00Z"}
        with patch.dict("os.environ", env):
            first = app_module._signal_count_baseline(cur)
            second = app_module._signal_count_baseline(cur)
            third = app_module._signal_count_baseline(cur)
        self.assertEqual((first, second, third), (572, 572, 572))

    def test_new_signals_after_reset_are_counted_from_zero(self):
        cur = FakeCursor(max_id=572, baseline=572)
        import os
        with patch.dict("os.environ", {}):
            os.environ.pop(app_module.SIGNAL_COUNT_RESET_ENV, None)
            app_module._signal_count_baseline(cur)
        # A fresh receipt raises the max id; the query must then count it.
        cur.max_id = 573
        cur.execute('SELECT COUNT(*) AS total FROM luxalgo_signal_receipts WHERE id > %s', (572,))
        sql, params = cur.statements[-1]
        self.assertIn("id > %s", sql)
        self.assertEqual(params, (572,))

    def test_reset_never_deletes_receipts(self):
        source = open(app_module.__file__, encoding="utf-8").read()
        normalised = " ".join(source.split())
        for forbidden in ("DELETE FROM luxalgo_signal_receipts",
                          "TRUNCATE luxalgo_signal_receipts",
                          "DROP TABLE luxalgo_signal_receipts"):
            self.assertNotIn(forbidden, normalised, forbidden)

    def test_invalid_env_value_fails_closed(self):
        cur = FakeCursor(max_id=572, baseline=None)
        with patch.dict("os.environ", {app_module.SIGNAL_COUNT_RESET_ENV: "not-a-date"}):
            with self.assertRaises(ValueError):
                app_module._signal_count_baseline(cur)

    def test_env_value_without_timezone_is_rejected(self):
        cur = FakeCursor(max_id=572, baseline=None)
        with patch.dict("os.environ", {app_module.SIGNAL_COUNT_RESET_ENV: "2026-11-19T18:00:00"}):
            with self.assertRaises(ValueError):
                app_module._signal_count_baseline(cur)


class ResetEndpointTests(unittest.TestCase):
    def test_endpoint_is_unavailable_without_a_terminal_password(self):
        # No password configured means the whole terminal is closed, not open.
        client = app.test_client()
        with patch.dict("os.environ", {}, clear=True):
            response = client.post("/terminal/signals/reset-count")
        self.assertEqual(response.status_code, 503)

    def test_endpoint_requires_authenticated_terminal_session(self):
        client = app.test_client()
        with patch.dict("os.environ", {"TERMINAL_PASSWORD": "x" * 32, "TERMINAL_USER": "admin"}):
            response = client.post("/terminal/signals/reset-count")
        self.assertEqual(response.status_code, 401)

    def test_endpoint_refuses_without_valid_csrf(self):
        client = app.test_client()
        with patch.dict("os.environ", {"TERMINAL_PASSWORD": "x" * 32, "TERMINAL_USER": "admin"}):
            with client.session_transaction() as session:
                session["authenticated"] = True
                session["user"] = "admin"
            response = client.post("/terminal/signals/reset-count")
        self.assertEqual(response.status_code, 403)

    def test_endpoint_resets_the_count_when_authorised(self):
        client = app.test_client()
        cur = FakeCursor(max_id=572, baseline=None)
        fake_conn = _FakeConnection(cur)
        with patch.dict("os.environ", {"TERMINAL_PASSWORD": "x" * 32, "TERMINAL_USER": "admin"}):
            with client.session_transaction() as session:
                session["authenticated"] = True
                session["user"] = "admin"
                session["csrf_token"] = "token-abc"
            with patch.object(app_module, "_db_connect", return_value=fake_conn):
                with patch.object(app_module, "_ensure_schema", return_value=None):
                    response = client.post("/terminal/signals/reset-count",
                                           headers={"X-Terminal-CSRF": "token-abc"})
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["signal_count"], 0)
        self.assertEqual(payload["baseline_id"], 572)
        self.assertTrue(payload["receipts_preserved"])
        # The reset must record history as preserved, never delete it.
        issued = " ".join(sql for sql, _ in cur.statements)
        self.assertNotIn("DELETE", issued)
        self.assertNotIn("TRUNCATE", issued)

    def test_tile_reads_the_rebased_count(self):
        from app import PAGE
        self.assertIn("DATABASE SIGNALS", PAGE)
        self.assertIn("{{ count }}", PAGE)


class ResetHandlerScriptTests(unittest.TestCase):
    """The reset button is useless if its script block cannot parse.

    PAGE is a normal (non-raw) Python string, so a bare \\n inside a JS string
    literal is emitted as a REAL newline. That is a fatal JavaScript syntax
    error, and it silently kills every handler in the same <script> block --
    the button then does nothing at all when clicked. This happened once; the
    tests below make sure it cannot happen again unnoticed.
    """

    def _served_page(self):
        """The page as the browser receives it, with Jinja placeholders filled.

        PAGE still contains {{ ... }} expressions; those are not JavaScript, so
        they must be rendered before a JS parser can judge the block.
        """
        import re
        from app import PAGE
        # Substitute every Jinja expression with a harmless JS literal.
        return re.sub(r"\{\{.*?\}\}", "0", PAGE, flags=re.S)

    def test_every_script_block_parses_as_javascript(self):
        """Authoritative check: hand each block to a real JS parser.

        A regex cannot decide this reliably -- it cannot tell a string
        terminator from an apostrophe in a comment. node --check can, and it
        is exactly what the browser does before running the block.
        """
        import re
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed; cannot parse-check JS")

        page = self._served_page()
        blocks = re.findall(r"<script\b[^>]*>(.*?)</script>", page, re.S)
        self.assertTrue(blocks, "no script blocks found")

        failures = []
        for index, block in enumerate(blocks):
            if not block.strip():
                continue
            with tempfile.NamedTemporaryFile("w", suffix=".js",
                                             delete=False, encoding="utf-8") as fh:
                fh.write(block)
                path = fh.name
            try:
                result = subprocess.run([node, "--check", path],
                                        capture_output=True, text=True)
                if result.returncode != 0:
                    failures.append((index, result.stderr.strip().splitlines()[:3]))
            finally:
                import os
                os.unlink(path)

        self.assertEqual(
            failures, [],
            "script block(s) do not parse as JavaScript: %r" % (failures,)
        )

    def test_reset_handler_is_present_and_escapes_its_newlines(self):
        page = self._served_page()
        self.assertIn("getElementById('resetSignalCount')", page)
        self.assertIn("addEventListener('click'", page)
        # The confirm text must carry the two-character escape, not a newline.
        self.assertIn("count to 0?\\n\\nStored signals", page)
        self.assertNotIn("count to 0?\n\nStored signals", page)

    def test_reset_handler_reports_failures_visibly(self):
        page = self._served_page()
        # Every failure path must write to the status element, never fail mute.
        self.assertIn("Reset FAILED", page)
        self.assertIn("No valid session token", page)
        self.assertIn("Reset outcome UNKNOWN", page)
        self.assertIn("resetSignalCountStatus", page)


if __name__ == "__main__":
    unittest.main()
