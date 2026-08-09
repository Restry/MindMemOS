from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from recall_evaluation import RecallReviewStore, build_recall_snapshot
from recall_judge import JUDGE_VERSION, JudgeEndpoint, RecallJudge, normalize_judgment


class RecallJudgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RecallReviewStore(os.path.join(self.tmp.name, "reviews.sqlite3"))
        self.points = [
            {
                "id": "r1",
                "payload": {
                    "query": "项目使用什么数据库？",
                    "request_submitted_at": "2026-08-08T01:00:00Z",
                    "task_completed_at": "2026-08-08T01:00:01Z",
                    "status": "ok",
                    "memories": [
                        {"id": "m1", "memory": "项目生产环境使用 PostgreSQL。"},
                        {"id": "m2", "memory": "用户喜欢拿铁。"},
                    ],
                },
            }
        ]
        self.endpoint = JudgeEndpoint("https://example.test/v1", "secret", "hub-cloud/gpt-4.1")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _normalizer(self, points, store):
        return build_recall_snapshot(points, store, limit=100)

    def test_normalize_enforces_score_label_contract(self) -> None:
        value = normalize_judgment(
            {"score": 82, "label": "irrelevant", "confidence": 1.4, "reason": "直接回答"}
        )
        self.assertEqual(value["label"], "relevant")
        self.assertEqual(value["confidence"], 1.0)

    @patch("recall_judge.judge_one")
    def test_scores_each_memory_once_and_skips_current_results(self, mocked) -> None:
        mocked.side_effect = [
            {"score": 92, "label": "relevant", "confidence": 0.95, "reason": "直接回答数据库类型"},
            {"score": 3, "label": "irrelevant", "confidence": 0.99, "reason": "与数据库无关"},
        ]
        judge = RecallJudge(
            store=self.store,
            endpoint_loader=lambda: self.endpoint,
            point_loader=lambda: self.points,
            normalizer=self._normalizer,
            interval_seconds=1800,
        )
        first = judge.run_once()
        second = judge.run_once()
        self.assertEqual(first["scored"], 2)
        self.assertEqual(second["scored"], 0)
        self.assertEqual(second["skipped"], 2)
        self.assertEqual(mocked.call_count, 2)
        snapshot = build_recall_snapshot(self.points, self.store)
        self.assertEqual(snapshot["summary"]["ai_scored_memories"], 2)
        self.assertEqual(snapshot["records"][0]["ai_score"], 47.5)

    @patch("recall_judge.judge_one")
    def test_content_change_is_rescored(self, mocked) -> None:
        mocked.return_value = {
            "score": 80,
            "label": "relevant",
            "confidence": 0.8,
            "reason": "相关",
        }
        judge = RecallJudge(
            store=self.store,
            endpoint_loader=lambda: self.endpoint,
            point_loader=lambda: self.points,
            normalizer=self._normalizer,
        )
        judge.run_once()
        self.points[0]["payload"]["memories"][0]["memory"] += " 主版本为 16。"
        result = judge.run_once()
        self.assertEqual(result["scored"], 1)
        self.assertEqual(result["skipped"], 1)

    def test_ai_reviews_never_touch_human_labels(self) -> None:
        self.store.save_ai_review(
            search_record_id="r1",
            memory_id="m1",
            query="q",
            content="c",
            judge_version=JUDGE_VERSION,
            model=self.endpoint.model,
            score=77,
            label="relevant",
            confidence=0.8,
            reason="相关",
        )
        calls, memories = self.store.load(["r1"])
        self.assertEqual(calls, {})
        self.assertEqual(memories, {})


if __name__ == "__main__":
    unittest.main()
