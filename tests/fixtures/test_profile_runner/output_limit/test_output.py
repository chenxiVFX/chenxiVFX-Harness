import unittest


class SyntheticOutputTest(unittest.TestCase):
    def test_bounded_output(self):
        print("合成输出开始")
        print("x" * 50_000)
        self.assertTrue(True)
