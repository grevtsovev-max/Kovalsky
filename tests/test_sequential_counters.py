import unittest
from newsroom.cabinet_pipeline import sequential_progress, PATH_KEYS
class SequentialCounterTests(unittest.TestCase):
    def test_missing_admission_does_not_claim_later_stages(self):
        raw=dict.fromkeys(PATH_KEYS,True);raw['first_filter']=False
        result=sequential_progress(raw)
        self.assertTrue(raw['published'])
        self.assertTrue(result['received'])
        self.assertFalse(any(result[k] for k in PATH_KEYS[1:]))
    def test_gap_is_not_filled_from_publication(self):
        raw=dict.fromkeys(PATH_KEYS,True);raw['checked']=False
        result=sequential_progress(raw)
        self.assertTrue(result['drafted'])
        self.assertFalse(result['published'])
    def test_complete_chain_is_preserved(self):
        raw=dict.fromkeys(PATH_KEYS,True)
        self.assertEqual(sequential_progress(raw),raw)
