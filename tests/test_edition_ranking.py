from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import test_edition_digests as digest_tests
from edition_helpers import EditionCase
NOW=digest_tests.NOW
from newsroom.edition import ranking,model,digests,store
from newsroom.agent_control import set_enabled,AgentDisabled


class RankingTests(EditionCase):
    post=digest_tests.DigestTests.post
    sender=digest_tests.DigestTests.sender
    def setUp(self):
        super().setUp();self.config['digests']={'enabled':True}
        self.db.executescript(ranking.SCHEMA+digests.SCHEMA)
        digests.state(self.db,'digest_v2_activation',(NOW-timedelta(days=1)).isoformat());self.db.commit()

    def response(self,posts):
        levels=[('HIGH','sanctions_rf'),('MEDIUM','event'),('ORDINARY','commentary')]
        return {'ranks':[{'post_id':p['post_id'],'level':levels[i%3][0],
                         'criterion':levels[i%3][1],'reason':'Причина ранга.',
                         'quote':ranking.text(p['text'])} for i,p in enumerate(posts)]},{'response_id':'test'}

    def test_rank_sort_retains_all_posts_and_caches_explanation(self):
        for name in ['Обычный комментарий','Ивент','Санкции РФ']:self.post(name)
        with patch.object(model,'ask',side_effect=lambda role,schema,instruction,data,settings:self.response(data['posts'])) as ask:
            self.assertEqual(ranking.classify(self.db,self.config,now=NOW),3)
            ranking.classify(self.db,self.config,now=NOW)
        ask.assert_called_once()
        selected=digests.published(self.db,self.config,NOW-timedelta(days=1),NOW)
        self.assertEqual([p['importance']['level'] for p in selected],['HIGH','MEDIUM','ORDINARY'])
        self.assertEqual(len(selected),3)
        self.assertTrue(all(p['importance']['reason']=='Причина ранга.' for p in selected))

    def test_bad_neighbour_does_not_erase_valid_rank_or_block_digest(self):
        self.post('Санкции РФ');self.post('Другой пост')
        def answer(role,schema,instruction,data,settings):
            result,receipt=self.response(data['posts']);result['ranks'][1]['quote']='Выдуманная цитата';return result,receipt
        with patch.object(model,'ask',side_effect=answer):ranking.classify(self.db,self.config,now=NOW)
        self.assertEqual(self.db.execute("SELECT count(*) FROM edition_post_ranks WHERE status='RANKED'").fetchone()[0],1)
        self.assertEqual(len(digests.published(self.db,self.config,NOW-timedelta(days=1),NOW)),2)
        digests.tick(self.db,self.config,now=NOW,send=self.sender())
        self.assertEqual(self.db.execute('SELECT state FROM edition_digest_runs').fetchone()[0],'SENT')

    def test_failure_is_unranked_without_changing_publication_or_call_loop(self):
        self.post('Новость')
        with patch.object(model,'ask',side_effect=TimeoutError()) as ask:
            ranking.classify(self.db,self.config,now=NOW)
            ranking.classify(self.db,self.config,now=NOW+timedelta(minutes=1))
        ask.assert_called_once()
        self.assertEqual(self.db.execute('SELECT status FROM posts').fetchone()[0],'PUBLISHED')
        selected=digests.published(self.db,self.config,NOW-timedelta(days=1),NOW)
        self.assertIsNone(selected[0]['importance'])

    def test_edit_invalidates_rank_and_tracked_people_are_passed(self):
        pid=self.post('Новость')
        self.config['ai']['_keyword_prefilter']={'people':['Отслеживаемое лицо']}
        def answer(role,schema,instruction,data,settings):
            self.assertEqual(data['tracked_people'],['Отслеживаемое лицо']);return self.response(data['posts'])
        with patch.object(model,'ask',side_effect=answer):ranking.classify(self.db,self.config,now=NOW)
        self.db.execute('UPDATE posts SET text=? WHERE post_id=?',('Новый факт',pid));self.db.commit()
        selected=digests.published(self.db,self.config,NOW-timedelta(days=1),NOW)
        self.assertIsNone(selected[0]['importance'])
        with patch.object(model,'ask',side_effect=answer) as ask:ranking.classify(self.db,self.config,now=NOW)
        ask.assert_called_once()

    def test_owner_stop_prevents_ranking(self):
        self.post('Новость');set_enabled(self.config,False)
        with patch.object(model,'ask') as ask:
            with self.assertRaises(AgentDisabled):ranking.classify(self.db,self.config,now=NOW)
        ask.assert_not_called()
