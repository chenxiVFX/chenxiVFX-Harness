import os
import unittest


class SyntheticUserTest(unittest.TestCase):
    def test_unicode_redaction_control_and_clean_environment(self):
        print("合成测试通过")
        print("api_key=synthetic-secret-value")
        print("terminal=\x1b[2J")
        print("parent_marker=" + os.environ.get("CVFX_SYNTHETIC_PARENT_MARKER", "missing"))
        print("secret_marker=" + os.environ.get("CVFX_SYNTHETIC_ENV_SECRET", "missing"))
        self.assertEqual(2 + 2, 4)
