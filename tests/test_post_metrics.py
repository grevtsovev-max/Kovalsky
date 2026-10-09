import unittest
from newsroom.post_metrics import aggregate

class PostMetricsTests(unittest.TestCase):
    def event(self, response='r1', seconds=10, usd=.002):
        return ('draft', {'receipt':{'response_id':response,'elapsed_seconds':seconds,'pricing':{'total_usd':usd}}})
    def test_sum_and_duplicate_receipt(self):
        first=self.event()
        result=aggregate([first,first,('review_response',self.event('r2',20,.003)[1])])
        self.assertEqual(result['calls'],2)
        self.assertEqual(result['api_seconds'],30)
        self.assertAlmostEqual(result['usd'],.005)
    def test_unknown_cost_is_not_zero(self):
        result=aggregate([self.event(usd=None)])
        self.assertEqual(result['calls'],1)
        self.assertIsNone(result['usd'])
        self.assertIsNone(aggregate([])['calls'])
    def test_failed_review_does_not_claim_complete_total(self):
        result=aggregate([self.event(),('review_metrics',{'error':True})])
        self.assertIsNone(result['usd'])
        self.assertIsNone(result['api_seconds'])
    def test_planning_is_excluded(self):
        self.assertIsNone(aggregate([('planning',self.event()[1])])['calls'])
