import unittest


class SyntheticFailureTest(unittest.TestCase):
    def test_failure_is_reported(self):
        print("合成失败输出")
        self.fail("synthetic failure")
