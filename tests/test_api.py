import time
import unittest

from app.harness import runner
from app.server import create_app
from tests.helpers import DBTestCase


class ApiTest(DBTestCase):
    def setUp(self):
        super().setUp()
        self.c = create_app().test_client()

    def _wait(self, tid, until):
        for _ in range(100):
            runner.wait(tid, 1)
            t = self.c.get(f"/api/tasks/{tid}").get_json()
            if t["status"] in until:
                return t
            time.sleep(0.05)
        self.fail(f"timeout, status={t['status']}")

    def test_full_flow_with_hitl_and_rewind(self):
        r = self.c.post("/api/tasks", json={"topic": "API 测试", "graph": "demo"})
        self.assertEqual(r.status_code, 201)
        tid = r.get_json()["id"]
        t = self._wait(tid, {"waiting_human"})
        hid = [h for h in t["hitl"] if h["status"] == "open"][0]["id"]

        self.assertEqual(self.c.post(f"/api/tasks/{tid}/resume").status_code, 400)
        self.c.post(f"/api/hitl/{hid}", json={"response": {"keep": ["独立复现 D"]}})
        t = self._wait(tid, {"done"})
        self.assertEqual(t["state"]["result"]["items"], ["独立复现 D"])

        trace = self.c.get(f"/api/tasks/{tid}/trace").get_json()
        self.assertTrue(any(s["tool_calls"] for s in trace))
        last = trace[-1]["id"]
        self.assertEqual(self.c.get(f"/api/tasks/{tid}/trace?after={last}").get_json(), [])

        # 回退到审阅节点：再次暂停等待人工，检索结果复用
        self.c.post(f"/api/tasks/{tid}/rewind", json={"node": "review", "invalidate": ["kept"]})
        t = self._wait(tid, {"waiting_human"})
        self.assertEqual(len([h for h in t["hitl"] if h["status"] == "open"]), 1)

    def test_validation(self):
        self.assertEqual(self.c.post("/api/tasks", json={"topic": ""}).status_code, 400)
        self.assertEqual(self.c.post("/api/tasks", json={"topic": "x", "graph": "nope"}).status_code, 400)
        self.assertEqual(self.c.get("/api/tasks/none").status_code, 404)


if __name__ == "__main__":
    unittest.main()
