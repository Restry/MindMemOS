from __future__ import annotations

import os
import tempfile
import unittest

from recall_evaluation import RecallReviewStore, build_recall_snapshot


class RecallEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "reviews.sqlite3")
        self.store = RecallReviewStore(self.path)
        self.points = [
            {
                "id": "search-1",
                "payload": {
                    "query": "Home AI Hub 有哪些约束",
                    "request_submitted_at": "2026-08-08T01:00:00Z",
                    "task_completed_at": "2026-08-08T01:00:01.250Z",
                    "status": "ok",
                    "api_key_uuid": "safe-public-id",
                    "memory_algorithm": "vanilla",
                    "rerank": True,
                    "top_k": 8,
                    "memories": [
                        {"id": "m1", "memory": "业务系统必须通过 Hub。", "memory_type": "fact"},
                        {"id": "m2", "memory": "另一个项目的信息。", "memory_type": "fact"},
                    ],
                },
            }
        ]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_unreviewed_calls_still_appear(self) -> None:
        snapshot = build_recall_snapshot(self.points, self.store)
        self.assertEqual(snapshot["summary"]["calls"], 1)
        self.assertEqual(snapshot["summary"]["reviewed_calls"], 0)
        self.assertEqual(snapshot["records"][0]["latency_ms"], 1250)
        self.assertEqual(snapshot["records"][0]["memory_count"], 2)
        self.assertIsNone(snapshot["records"][0]["review"])
        self.assertTrue(snapshot["read_only_observation"])

    def test_human_review_updates_quality_metrics_only(self) -> None:
        self.store.save(
            {
                "search_record_id": "search-1",
                "verdict": "poor",
                "reason": "返回内容过泛或噪音",
                "memory_reviews": [
                    {"memory_id": "m1", "label": "relevant"},
                    {"memory_id": "m2", "label": "irrelevant"},
                ],
            }
        )
        snapshot = build_recall_snapshot(self.points, self.store)
        self.assertEqual(snapshot["summary"]["technical_success_rate"], 100.0)
        self.assertEqual(snapshot["summary"]["poor_calls"], 1)
        self.assertEqual(snapshot["summary"]["labeled_memories"], 2)
        self.assertIsNotNone(snapshot["summary"]["context_waste_rate"])
        self.assertEqual(self.points[0]["payload"]["memories"][0]["memory"], "业务系统必须通过 Hub。")

    def test_rejects_automatic_or_unknown_labels(self) -> None:
        with self.assertRaises(ValueError):
            self.store.save({"search_record_id": "search-1", "verdict": "auto_optimize"})
        with self.assertRaises(ValueError):
            self.store.save(
                {
                    "search_record_id": "search-1",
                    "verdict": "useful",
                    "memory_reviews": [{"memory_id": "m1", "label": "delete_memory"}],
                }
            )


if __name__ == "__main__":
    unittest.main()
