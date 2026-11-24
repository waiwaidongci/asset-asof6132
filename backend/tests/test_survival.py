import unittest
from datetime import date

from src.domain import survival
from src.domain.analysis import analyze, validate_rows
from src.domain.demo import demo_rows


def rows_for(events):
    return [
        {"event_id": event_id, "asset_id": asset_id, "asset_name": name,
         "event_date": day, "sequence": seq, "event_type": kind}
        for event_id, asset_id, name, day, seq, kind in events
    ]


class SurvivalTests(unittest.TestCase):
    def setUp(self):
        self.params = {"start_date": "2026-09-01", "as_of_date": "2026-11-20",
                       "horizon_days": 7, "min_sample": 1}

    def test_spells_and_censoring(self):
        rows = rows_for([
            ("E1", "A", "电钻", "2026-09-01", 1, "accept"),
            ("E2", "A", None, "2026-09-10", 1, "check-out"),
            ("E3", "A", None, "2026-09-15", 1, "check-in"),
            ("E4", "A", None, "2026-09-20", 1, "check-out"),
        ])
        events = survival._ordered_events(rows)
        spells = survival.build_spells(events)
        self.assertEqual(len(spells), 2)
        closed, open_spell = spells
        self.assertEqual(closed.end_reason, "check-in")
        self.assertEqual((closed.end_date - closed.loan_date).days, 5)
        self.assertEqual(open_spell.end_reason, "censor")
        self.assertIsNone(open_spell.end_event_id)

    def test_duplicate_event_counted_once(self):
        rows = rows_for([
            ("E1", "A", "电钻", "2026-09-01", 1, "accept"),
            ("E2", "A", None, "2026-09-10", 1, "check-out"),
            ("E3", "A", None, "2026-09-15", 1, "check-in"),
        ])
        rows.append(dict(rows[2]))
        events = validate_rows(rows)
        self.assertEqual(len(events), 3)
        spells = survival.build_spells(survival._ordered_events(rows))
        self.assertEqual(len(spells), 1)

    def test_probability_between_zero_and_one(self):
        rows = rows_for([
            ("E1", "A", "电钻", "2026-09-01", 1, "accept"),
            ("E2", "A", None, "2026-09-02", 1, "check-out"),
            ("E3", "A", None, "2026-09-04", 1, "check-in"),
            ("E4", "B", "梯子", "2026-09-01", 1, "accept"),
            ("E5", "B", None, "2026-09-03", 1, "check-out"),
            ("E6", "B", None, "2026-09-06", 1, "check-in"),
            ("E7", "C", "夹具", "2026-09-01", 1, "accept"),
            ("E8", "C", None, "2026-09-05", 1, "check-out"),
        ])
        result = survival.backtest(rows, self.params)
        outstanding = result["tables"][0]["rows"]
        self.assertTrue(outstanding)
        for row in outstanding:
            if row["return_prob_7d_pct"] is not None:
                self.assertGreaterEqual(row["return_prob_7d_pct"], 0)
                self.assertLessEqual(row["return_prob_7d_pct"], 100)

    def test_returned_outcome_uses_later_checkin(self):
        rows = rows_for([
            ("E1", "A", "电钻", "2026-09-01", 1, "accept"),
            ("E2", "A", None, "2026-09-25", 1, "check-out"),
            ("E3", "A", None, "2026-10-03", 1, "check-in"),
        ])
        result = survival.backtest(rows, self.params)
        september = [r for r in result["tables"][0]["rows"] if r["eval_date"] == "2026-09-30"]
        self.assertEqual(len(september), 1)
        row = september[0]
        self.assertEqual(row["elapsed_days"], 5)
        self.assertEqual(row["actual_outcome"], "已归还")
        self.assertEqual(row["outcome_event_id"], "E3")
        self.assertEqual(row["observation_status"], "答案可判")

    def test_not_returned_when_window_full(self):
        rows = rows_for([
            ("E1", "A", "电钻", "2026-09-01", 1, "accept"),
            ("E2", "A", None, "2026-09-25", 1, "check-out"),
            ("E3", "A", None, "2026-10-20", 1, "check-in"),
        ])
        result = survival.backtest(rows, self.params)
        september = [r for r in result["tables"][0]["rows"] if r["eval_date"] == "2026-09-30"][0]
        self.assertEqual(september["actual_outcome"], "未归还")
        self.assertIsNone(september["outcome_event_id"])

    def test_pending_when_window_not_covered(self):
        rows = rows_for([
            ("E1", "A", "电钻", "2026-09-01", 1, "accept"),
            ("E2", "A", None, "2026-11-18", 1, "check-out"),
        ])
        result = survival.backtest(rows, self.params)
        november = [r for r in result["tables"][0]["rows"] if r["eval_date"] == "2026-11-20"][0]
        self.assertEqual(november["actual_outcome"], "待观察")
        self.assertEqual(november["observation_status"], "待观察")

    def test_late_arrival_becomes_answer_on_rerun(self):
        before = rows_for([
            ("E1", "A", "电钻", "2026-09-01", 1, "accept"),
            ("E2", "A", None, "2026-09-25", 1, "check-out"),
        ])
        first = survival.backtest(before, self.params)
        pending = [r for r in first["tables"][0]["rows"] if r["eval_date"] == "2026-09-30"][0]
        self.assertEqual(pending["actual_outcome"], "待观察")
        after = before + rows_for([
            ("E3", "A", None, "2026-10-02", 1, "check-in"),
        ])
        rerun = survival.backtest(after, self.params)
        answered = [r for r in rerun["tables"][0]["rows"] if r["eval_date"] == "2026-09-30"][0]
        self.assertEqual(answered["actual_outcome"], "已归还")
        self.assertEqual(answered["outcome_event_id"], "E3")

    def test_estimate_ignores_future_events(self):
        base = rows_for([
            ("E1", "A", "电钻", "2026-09-01", 1, "accept"),
            ("E2", "A", None, "2026-09-10", 1, "check-out"),
            ("E3", "A", None, "2026-09-12", 1, "check-in"),
        ])
        with_future = base + rows_for([
            ("E4", "A", None, "2026-10-25", 1, "check-out"),
            ("E5", "A", None, "2026-10-26", 1, "check-in"),
        ])
        params = dict(self.params, as_of_date="2026-09-30")
        table = survival.life_table(survival._ordered_events(with_future), date(2026, 9, 30), 7)
        self.assertEqual(table["spell_count"], 1)
        self.assertEqual(table["max_duration_days"], 2)

    def test_min_sample_suppresses_probability(self):
        rows = rows_for([
            ("E1", "A", "电钻", "2026-09-01", 1, "accept"),
            ("E2", "A", None, "2026-09-25", 1, "check-out"),
        ])
        params = dict(self.params, min_sample=5)
        result = survival.backtest(rows, params)
        row = result["tables"][0]["rows"][0]
        self.assertIsNone(row["return_prob_7d_pct"])
        self.assertIn("风险集", row["probability_note"])

    def test_invalid_horizon_rejected(self):
        with self.assertRaises(ValueError):
            survival.backtest(demo_rows(), {**self.params, "horizon_days": 0})
        with self.assertRaises(ValueError):
            survival.backtest(demo_rows(), {**self.params, "min_sample": 0})

    def test_concurrent_backfill_uses_launch_time_snapshot(self):
        # 第一次运行发起时的批次（晚到归还尚未补录）
        snapshot_at_launch = rows_for([
            ("E1", "A", "电钻", "2026-09-01", 1, "accept"),
            ("E2", "A", None, "2026-09-25", 1, "check-out"),
        ])
        first = survival.backtest(snapshot_at_launch, self.params)
        # 之后补录的新批次包含晚到归还；旧运行结果保持「待观察」，新运行才给答案
        later_batch = snapshot_at_launch + rows_for([
            ("E3", "A", None, "2026-10-02", 1, "check-in"),
        ])
        rerun = survival.backtest(later_batch, self.params)
        old_row = [r for r in first["tables"][0]["rows"] if r["eval_date"] == "2026-09-30"][0]
        new_row = [r for r in rerun["tables"][0]["rows"] if r["eval_date"] == "2026-09-30"][0]
        self.assertEqual(old_row["actual_outcome"], "待观察")
        self.assertEqual(new_row["actual_outcome"], "已归还")
        self.assertEqual(new_row["outcome_event_id"], "E3")

    def test_demo_analyze_includes_backtest_tables(self):
        result = analyze(demo_rows(), {"start_date": "2026-09-01", "as_of_date": "2026-11-20"})
        titles = [table["title"] for table in result["tables"]]
        self.assertIn("逐月未还工具归还概率回测", titles)
        self.assertIn("回测月度汇总", titles)
        self.assertTrue(any(title.startswith("生命表") for title in titles))
        backtest = result["backtest"]
        self.assertEqual(backtest["horizon_days"], 7)
        outstanding = next(table for table in result["tables"]
                           if table["title"] == "逐月未还工具归还概率回测")
        eval_dates = {row["eval_date"] for row in outstanding["rows"]}
        self.assertEqual(eval_dates, {"2026-09-30", "2026-10-31", "2026-11-20"})

    def test_demo_known_outcomes(self):
        result = analyze(demo_rows(), {"start_date": "2026-09-01", "as_of_date": "2026-11-20"})
        rows = result["tables"][3]["rows"]
        sep = [r for r in rows if r["eval_date"] == "2026-09-30"]
        self.assertEqual([r["asset_id"] for r in sep], ["GQ-01"])
        self.assertEqual(sep[0]["actual_outcome"], "已归还")
        oct_row = [r for r in rows if r["eval_date"] == "2026-10-31"][0]
        self.assertEqual(oct_row["asset_id"], "GQ-03")
        self.assertEqual(oct_row["actual_outcome"], "未归还")
        self.assertEqual(oct_row["elapsed_days"], 0)
        nov = {r["asset_id"]: r for r in rows if r["eval_date"] == "2026-11-20"}
        self.assertEqual(set(nov), {"GQ-01", "GQ-04"})
        self.assertEqual(nov["GQ-04"]["actual_outcome"], "待观察")
        self.assertEqual(nov["GQ-01"]["actual_outcome"], "已归还")
        self.assertEqual(nov["GQ-01"]["outcome_date"], "2026-11-21")


if __name__ == "__main__":
    unittest.main()
