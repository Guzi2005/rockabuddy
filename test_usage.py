import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from unittest.mock import patch
import providers
from codex_usage import normalize_limits
from app import merge_results
from panel import reset_text


class UsageTests(unittest.TestCase):
    def test_two_windows_have_independent_reset_times(self):
        data = normalize_limits({"rateLimitsByLimitId": {"codex": {
            "primary": {"usedPercent": 21, "windowDurationMins": 300, "resetsAt": 1000},
            "secondary": {"usedPercent": 92, "windowDurationMins": 10080, "resetsAt": 2000}}}})
        self.assertEqual(data["remaining"], 8)
        self.assertEqual([w["label"] for w in data["windows"]], ["5 小时", "本周"])
        self.assertEqual([w["resets_at"] for w in data["windows"]], [1000, 2000])

    def test_unknown_window_is_not_zero(self):
        with self.assertRaises(RuntimeError):
            normalize_limits({"rateLimits": {"primary": None}})

    def test_other_buckets_are_preserved_not_averaged(self):
        data = normalize_limits({"rateLimitsByLimitId": {
            "codex": {"primary": {"usedPercent": 10, "windowDurationMins": 300}},
            "special": {"primary": {"usedPercent": 95, "windowDurationMins": 60}}}})
        self.assertEqual(len(data["windows"]), 2)
        self.assertEqual(data["remaining"], 90)

    @patch("providers.get_api_key", return_value="test-not-a-secret")
    @patch("providers._get_json")
    def test_siliconflow_total_balance_and_missing(self, request, key):
        request.return_value = {"data": {"balance": "0.88", "chargeBalance": "88", "totalBalance": "88.88"}}
        result = providers.fetch_siliconflow({})
        self.assertEqual(result["remaining"], 88.88)
        self.assertEqual(result["unit"], "¥")
        self.assertIsNone(result["total"])
        request.return_value = {"data": {}}
        self.assertFalse(providers.fetch_siliconflow({})["ok"])
        request.return_value = {"data": {"totalBalance": "0"}}
        self.assertEqual(providers.fetch_siliconflow({})["remaining"], 0)

    def test_failure_retains_timestamp_and_marks_stale(self):
        old = {"codex": {"ok": True, "remaining": 80, "fetched_at": 123}}
        failure = {"codex": {"ok": False, "error": "offline", "fetched_at": 456}}
        current = merge_results(old, failure)["codex"]
        self.assertTrue(current["stale"])
        self.assertEqual(current["remaining"], 80)
        self.assertEqual(current["fetched_at"], 123)
        self.assertEqual(current["last_attempt_at"], 456)

    def test_reset_countdown_does_not_claim_reset(self):
        self.assertIn("等待同步", reset_text(1000, now=1001))
        self.assertIn("暂不可用", reset_text(None))
        self.assertIn("1小时", reset_text(4600, now=1000))


class ZcodeTests(unittest.TestCase):
    """fetch_zcode 读本地会话库: 只计今日已完成请求, 预算换算剩余百分比。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "db.sqlite")
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE model_usage (id INTEGER PRIMARY KEY, model_id TEXT,"
                    " status TEXT, started_at INTEGER, input_tokens INTEGER,"
                    " output_tokens INTEGER, computed_total_tokens INTEGER)")
        now_ms = int(time.time() * 1000)
        rows = [
            ("GLM-5.3", "completed", now_ms - 3600 * 1000, 1000, 500, 1500),
            ("GLM-5.3", "completed", now_ms - 60 * 1000, 2000, 1000, 3000),
            ("GLM-5.3-Flash", "completed", now_ms - 30 * 1000, 100, 100, 200),
            ("GLM-5.3", "completed", now_ms - 26 * 3600 * 1000, 99999, 99999, 199998),
            ("GLM-5.3-Flash", "error", now_ms - 1000, 500, 500, 1000),
        ]
        con.executemany("INSERT INTO model_usage (model_id, status, started_at,"
                        " input_tokens, output_tokens, computed_total_tokens)"
                        " VALUES (?,?,?,?,?,?)", rows)
        con.commit()
        con.close()
        self._old_env = os.environ.get("ZCODE_DB")
        os.environ["ZCODE_DB"] = self.db
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._old_env is None:
            os.environ.pop("ZCODE_DB", None)
        else:
            os.environ["ZCODE_DB"] = self._old_env

    def test_without_budget_counts_today_completed_only(self):
        data = providers.fetch_zcode({})
        self.assertTrue(data["ok"])
        self.assertAlmostEqual(data["remaining"], (1500 + 3000 + 200) / 1e4)
        self.assertIsNone(data["total"])
        self.assertEqual(data["unit"], "万tok")
        self.assertNotIn("windows", data)
        self.assertIn("GLM-5.3", data["note"])

    def test_budget_derives_daily_window(self):
        data = providers.fetch_zcode({"daily_budget_tokens": 100000})
        self.assertEqual(data["total"], 10.0)
        self.assertAlmostEqual(data["remaining"], (100000 - 4700) / 1e4, delta=0.1)
        window = data["windows"][0]
        self.assertAlmostEqual(window["remaining_percent"], 95.3)
        midnight = datetime.combine(datetime.now().date(),
                                    datetime.min.time()).timestamp()
        self.assertEqual(window["resets_at"], midnight + 86400)

    def test_missing_db_is_not_ok(self):
        os.environ["ZCODE_DB"] = os.path.join(self.tmp.name, "nope.sqlite")
        data = providers.fetch_zcode({})
        self.assertFalse(data["ok"])
        self.assertIn("未找到 ZCode", data["error"])

    def test_dispatched_by_fetch_one(self):
        data = providers.fetch_one({"id": "zcode", "type": "zcode"})
        self.assertTrue(data["ok"])
        self.assertEqual(data["source"], "ZCode 本地会话库")
        self.assertIn("fetched_at", data)


if __name__ == "__main__":
    unittest.main()
