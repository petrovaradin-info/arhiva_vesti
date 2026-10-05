import json
import sqlite3
import unittest

from petrovaradin_archive.relevance import RelevanceReview


class RelevanceTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT,
          site_id TEXT, article_title TEXT, article_text TEXT, download_status TEXT,
          review_status TEXT, access_status TEXT, requires_manual_review INTEGER)""")
        self.db.executemany("INSERT INTO urls VALUES (?,?,?,?,?,?,?,?,?)", [
            (1, "https://example.org/1", "a", "Struja", "Petrovaradinska ulica u Mitrovici.",
             "awaiting_review", "pending", "hidden", 1),
            (2, "https://example.org/2", "b", "Istorija", "Jevreji su ziveli u Petrovaradinu.",
             "awaiting_review", "pending", "hidden", 1)])
        self.db.commit()
        self.review = RelevanceReview(self.db)

    def tearDown(self):
        self.db.close()

    def payload(self):
        batch = self.review.batch()
        return {"batch_id": batch["batch_id"], "predictions": [
            {"id": 1, "label": "irrelevant", "reason": "Ulica u drugom gradu",
             "evidence": "Petrovaradinska ulica u Mitrovici."}]}

    def test_prediction_does_not_approve(self):
        self.review.import_predictions(self.payload(), "test-model")
        self.assertEqual(self.review.show(1)["status"], "awaiting_review")
        self.assertEqual(self.review.show(1)["decisions"], [])

    def test_invalid_evidence_and_ids_are_atomic(self):
        for alteration in ({"evidence": "invented"}, {"id": 99}):
            payload = self.payload()
            invalid = dict(payload["predictions"][0], **alteration)
            payload["predictions"].append(invalid)
            with self.assertRaises(ValueError):
                self.review.import_predictions(payload, "test")
            self.assertEqual(self.db.execute("SELECT count(*) FROM relevance_predictions").fetchone()[0], 0)

    def test_stale_text_rejected(self):
        payload = self.payload()
        self.db.execute("UPDATE urls SET article_text='changed' WHERE id=1")
        self.db.commit()
        with self.assertRaises(ValueError):
            self.review.import_predictions(payload, "test")

    def test_decision_memory_and_correction(self):
        self.review.decide(1, "irrelevant", "Drugi grad", "Petrovaradinska ulica")
        self.assertEqual(self.review.show(1)["status"], "rejected")
        batch = self.review.batch()
        self.assertEqual([a["id"] for a in batch["articles"]], [2])
        self.assertEqual(batch["confirmed_examples"][0]["label"], "irrelevant")
        self.review.decide(1, "uncertain", "Ponovo proveriti", "Petrovaradinska ulica")
        self.assertEqual(self.review.show(1)["status"], "awaiting_review")
        self.assertEqual(len(self.review.show(1)["decisions"]), 2)
        self.assertEqual(self.review.batch()["confirmed_examples"], [])

    def test_context_approval_and_bad_decision_rollback(self):
        with self.assertRaises(ValueError):
            self.review.decide(2, "context", "Istorija", "invented")
        self.assertEqual(self.review.show(2)["decisions"], [])
        self.review.decide(2, "context", "Istorijski znacaj", "u Petrovaradinu")
        self.assertEqual(self.review.show(2)["status"], "archived")
        self.assertEqual(self.review.show(2)["decisions"][0]["previous_status"], "awaiting_review")

    def test_batch_balances_sources_and_retains_full_text(self):
        batch = self.review.batch(2)
        self.assertEqual([a["id"] for a in batch["articles"]], [1, 2])
        self.assertEqual(batch["articles"][1]["text"], "Jevreji su ziveli u Petrovaradinu.")
        self.assertEqual(batch["batch_id"], self.review.batch(2)["batch_id"])


if __name__ == "__main__":
    unittest.main()
