import unittest
import tempfile
from pathlib import Path
from newsroom.db import connect
from newsroom.archive_memory import migrate,covered

class ArchiveMemoryTests(unittest.TestCase):
    def test_publication_coverage_does_not_create_fake_primary_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=connect(str(Path(tmp)/'db'))
            try:
                db.execute("INSERT INTO stories(story_id,canonical_topic,headline,first_seen_at,last_updated_at) VALUES(1,'crypto','crypto','2026','2026')")
                text='Компания объявила запуск криптосервиса в октябре.'
                db.execute("INSERT INTO posts(story_id,text,status,created_at,version,post_hash) VALUES(1,?,'PUBLISHED','2026',1,'hash')",(text,));db.commit()
                result={'claims':[{'subject':'Компания','predicate':'срок запуска','scope':'криптосервис RU','value':'октябрь','claim_type':'CLAIM','post_quote':text}]}
                self.assertEqual(migrate(db,{'model':'test'},lambda *args:result)['extracted'],1)
                self.assertTrue(covered(db,'Компания','срок запуска','криптосервис RU','октябрь','CLAIM'))
                self.assertEqual(db.execute('SELECT count(*) FROM story_facts').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT count(*) FROM source_snapshots').fetchone()[0],0)
                self.assertEqual(migrate(db,{'model':'test'},lambda *args:result)['skipped'],1)
            finally:db.close()

    def test_bad_archive_quote_is_rejected_without_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=connect(str(Path(tmp)/'db'))
            try:
                db.execute("INSERT INTO stories(story_id,canonical_topic,headline,first_seen_at,last_updated_at) VALUES(1,'crypto','crypto','2026','2026')")
                db.execute("INSERT INTO posts(story_id,text,status,created_at,version,post_hash) VALUES(1,'Прежняя публикация','PUBLISHED','2026',1,'hash')");db.commit()
                result={'claims':[{'subject':'A','predicate':'B','scope':'C','value':'D','claim_type':'FACT','post_quote':'Вымышленная цитата отсутствует в опубликованном тексте.'}]}
                self.assertEqual(migrate(db,{},lambda *args:result)['failed'],1)
                self.assertEqual(db.execute('SELECT count(*) FROM publication_coverage').fetchone()[0],0)
            finally:db.close()
