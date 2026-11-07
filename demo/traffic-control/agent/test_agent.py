"""python3 -I -m unittest discover -s demo/traffic-control/agent"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent as A  # noqa: E402

KEY = "a" * 40


class FakeHttp:
    def __init__(self):
        self.routes = {}
        self.calls = []

    def __call__(self, method, url, *, body=None, headers=None, timeout=5.0):
        self.calls.append((method, url, body, headers))
        handler = self.routes.get((method, url))
        if handler is None:
            raise A.HttpError("HTTP_404", 404)
        result = handler(body, headers) if callable(handler) else handler
        if isinstance(result, Exception):
            raise result
        return result


def make_db(path, jobs):
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE IF EXISTS cutover_jobs")
    conn.execute("CREATE TABLE cutover_jobs (job_id TEXT, direction TEXT, source TEXT, target TEXT,"
                 " stage TEXT, checkpoint TEXT, updated_at TEXT)")
    for job_id, stage, checkpoint in jobs:
        conn.execute("INSERT INTO cutover_jobs VALUES (?,?,?,?,?,?,?)",
                     (job_id, "a->b", "a", "b", stage, json.dumps(checkpoint), "t"))
    conn.commit()
    conn.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "cutover.sqlite3")
        make_db(self.db, [("done", "COMPLETED", {}), ("closed", "NEEDS_RECOVERY",
                                                       {"manual_resolution": {}})])
        self.http = FakeHttp()
        s = A.Settings(control_url="http://ctl", agent_key=KEY, worker_url="http://w",
                       bot_url="http://b", bot_token="t" * 32, connect_url="http://c",
                       cutover_db=self.db)
        self.t = [0.0]
        self.agent = A.Agent(s, http=self.http, clock=lambda: self.t[0])
        self.worker_status = {"state": "RUNNING", "target": "onprem_return", "rate_per_second": 1.0,
                              "queue_length": 0, "generation_enabled": True, "max_generated": None,
                              "store_counts": {"committed": 5}}
        self.http.routes[("GET", "http://w/status")] = lambda b, h: dict(self.worker_status)
        self.http.routes[("GET", "http://c/connectors")] = []
        self.bot_state = {"generation": "stopped", "pending": 0, "committed": 3, "rate_per_second": 1}
        self.http.routes[("GET", "http://b/control")] = lambda b, h: dict(self.bot_state)

    def tearDown(self):
        self.tmp.cleanup()


class SignatureTest(unittest.TestCase):
    def test_shared_vector_matches_node(self):
        # Same vector as test/server.test.js.
        self.assertEqual(
            A.auth_header(KEY, "agent", "POST", "/agent/report", b'{"x":1}',
                          timestamp=1794000000, nonce="0123456789abcdef0123"),
            "agent.1794000000.0123456789abcdef0123."
            "07e3f2e5208ec2cfce9f47329b4504478b128c9e282e7e5e6af99c952679ef8f")


class SummaryTest(unittest.TestCase):
    def test_budget_mode_is_display_only(self):
        s = A.summarize_worker({"max_generated": 200, "generation_limit": 100, "generated_total": 40,
                                "generation_enabled": True})
        self.assertEqual((s["mode"], s["controllable"], s["generation"]), ("budget", False, None))

    def test_old_worker_is_unsupported(self):
        s = A.summarize_worker({"state": "RUNNING", "max_generated": None})
        self.assertEqual((s["mode"], s["controllable"]), ("unsupported", False))

    def test_bot_without_token_is_not_controllable(self):
        s = A.summarize_bot({"generation": "running"}, token_present=False)
        self.assertFalse(s["controllable"])
        self.assertIn("BOT_CONTROL_TOKEN", s["reason"])


class LockTest(Base):
    def test_unlocked_when_quiet(self):
        self.assertEqual(self.agent.lock(self.agent.worker()), {"locked": False, "reasons": []})

    def test_connectors_open_jobs_switching_and_unknowns_lock(self):
        self.http.routes[("GET", "http://c/connectors")] = ["rf-source"]
        make_db(self.db, [("live", "CATCHING_UP", {})])
        self.worker_status["state"] = "SWITCHING"
        lock = self.agent.lock(self.agent.worker(force=True))
        self.assertTrue(lock["locked"])
        self.assertEqual(len(lock["reasons"]), 3)
        self.http.routes[("GET", "http://c/connectors")] = A.HttpError("URLError")
        self.agent.settings.cutover_db = os.path.join(self.tmp.name, "missing.sqlite3")
        self.worker_status["state"] = "RUNNING"
        lock = self.agent.lock(self.agent.worker(force=True))
        self.assertTrue(lock["locked"], "unknown state must fail closed")

    def test_unresolved_needs_recovery_locks(self):
        make_db(self.db, [("x", "NEEDS_RECOVERY", {})])
        self.assertTrue(self.agent.lock(self.agent.worker())["locked"])


class CommandTest(Base):
    def test_bot_start_sends_bearer_and_enabled(self):
        self.http.routes[("POST", "http://b/control/generation")] = {"generation": "running"}
        out = self.agent.execute({"id": "1", "target": "bot", "action": "start"})
        self.assertEqual(out, {"id": "1", "ok": True})
        method, url, body, headers = self.http.calls[-1]
        self.assertEqual(body, {"enabled": True})
        self.assertEqual(headers["Authorization"], "Bearer " + "t" * 32)

    def test_worker_stop(self):
        self.http.routes[("POST", "http://w/generation")] = {"generation_enabled": False}
        self.assertTrue(self.agent.execute({"id": "2", "target": "worker", "action": "stop"})["ok"])
        self.assertIn(("POST", "http://w/generation", {"enabled": False}, None), self.http.calls)

    def test_refuses_during_cutover_without_calling_bots(self):
        self.http.routes[("GET", "http://c/connectors")] = ["x-sink"]
        out = self.agent.execute({"id": "3", "target": "bot", "action": "stop"})
        self.assertFalse(out["ok"])
        self.assertTrue(out["error"].startswith("CUTOVER_LOCK"))
        self.assertFalse(any(c[0] == "POST" for c in self.http.calls))

    def test_budget_worker_refused(self):
        self.worker_status.update(max_generated=200, generation_limit=100)
        out = self.agent.execute({"id": "4", "target": "worker", "action": "stop"})
        self.assertEqual(out["error"], "WORKER_NOT_CONTROLLABLE:budget")

    def test_invalid_command(self):
        self.assertEqual(self.agent.execute({"id": "5", "target": "db", "action": "drop"})["error"],
                         "INVALID_COMMAND")


class StepTest(Base):
    def test_step_reports_signed_and_returns_results_next_time(self):
        reports = []
        self.http.routes[("POST", "http://ctl/agent/report")] = \
            lambda body, headers: reports.append((body, headers)) or {"ok": True}
        self.http.routes[("GET", "http://ctl/agent/commands")] = \
            {"commands": [{"id": "c1", "target": "bot", "action": "start"}]}
        self.http.routes[("POST", "http://b/control/generation")] = {}
        self.agent.step()
        self.http.routes[("GET", "http://ctl/agent/commands")] = {"commands": []}
        self.agent.step()
        first, second = reports
        self.assertTrue(first[1]["X-Demo-Auth"].startswith("agent."))
        self.assertEqual(first[0]["results"], [])
        self.assertEqual(second[0]["results"], [{"id": "c1", "ok": True}])

    def test_worker_status_is_rate_limited(self):
        self.agent.worker()
        self.agent.worker()
        self.t[0] = 9.9
        self.agent.worker()
        self.assertEqual(sum(1 for c in self.http.calls if c[1] == "http://w/status"), 1)
        self.t[0] = 10.1
        self.agent.worker()
        self.assertEqual(sum(1 for c in self.http.calls if c[1] == "http://w/status"), 2)


if __name__ == "__main__":
    unittest.main()
