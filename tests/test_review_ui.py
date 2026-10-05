import sqlite3
import unittest
from pathlib import Path
import tempfile

import test_relevance as fixtures
from petrovaradin_archive.review_ui import export_page, import_decisions, review_packet


class ReviewUITests(unittest.TestCase):
    setUp = fixtures.RelevanceTests.setUp
    tearDown = fixtures.RelevanceTests.tearDown
    def human_payload(self):
        packet = review_packet(self.review, self.review.batch()['batch_id'])
        decisions = []
        for a in packet['articles']:
            decisions.append(dict(id=a['id'], label='context', reason='Ljudska potvrda',
                evidence=a['text'], text_hash=a['text_hash'], revision=a['revision'], status=a['status']))
        return dict(format='petrovaradin-human-review-v1', batch_id=packet['batch_id'], decisions=decisions)

    def test_bulk_preview_apply_and_replay(self):
        p = self.human_payload()
        self.assertFalse(import_decisions(self.review,p)['applied'])
        self.assertEqual(self.review.show(1)['status'],'awaiting_review')
        self.assertEqual(import_decisions(self.review,p,True)['count'],2)
        self.assertEqual(self.review.show(1)['status'],'archived')
        self.assertTrue(import_decisions(self.review,p,True)['already_imported'])
        self.assertEqual(len(self.review.show(1)['decisions']),1)

    def test_one_invalid_decision_rolls_back_all(self):
        p = self.human_payload()
        p['decisions'][1]['evidence']='invented'
        with self.assertRaises(ValueError):
            import_decisions(self.review,p,True)
        self.assertEqual(self.review.show(1)['decisions'],[])

    def test_database_failure_rolls_back_earlier_writes(self):
        p = self.human_payload()
        self.db.execute("""CREATE TRIGGER fail_second BEFORE UPDATE ON urls
            WHEN NEW.id=2 BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            import_decisions(self.review,p,True)
        self.assertEqual(self.review.show(1)['status'],'awaiting_review')
        self.assertEqual(self.review.show(1)['decisions'],[])

    def test_stale_revision_and_status_rejected(self):
        p=self.human_payload()
        self.review.decide(1,'irrelevant','Ulica','Petrovaradinska ulica')
        with self.assertRaises(ValueError):
            import_decisions(self.review,p,True)
        self.assertEqual(self.review.show(2)['decisions'],[])

    def test_stale_body_rejected(self):
        p=self.human_payload()
        self.db.execute("UPDATE urls SET article_text='promenjen' WHERE id=1")
        self.db.commit()
        with self.assertRaises(ValueError):
            import_decisions(self.review,p,True)

    def test_malformed_duplicate_and_unknown_ids(self):
        for invalid in (True,999):
            p=self.human_payload();p['decisions'][0]['id']=invalid
            with self.assertRaises(ValueError):import_decisions(self.review,p,True)
        p=self.human_payload();p['decisions'].append(p['decisions'][0])
        with self.assertRaises(ValueError):import_decisions(self.review,p,True)

    def test_html_escapes_untrusted_script_and_no_default_confirmation(self):
        self.db.execute("UPDATE urls SET article_text=? WHERE id=1",('</script><script>alert(1)</script>',))
        self.db.commit()
        batch=self.review.batch()
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)/'review.html'
            export_page(self.review,batch['batch_id'],out)
            html=out.read_text(encoding='utf-8')
            self.assertNotIn('</script><script>alert(1)',html)
            self.assertIn('selected:false',html)
            self.assertIn('textContent',html)
