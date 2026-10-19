import ctypes
import os
import unittest


class WindowsJobMembershipTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows Job Object check")
    def test_user_code_starts_inside_assigned_job(self):
        is_in_job = ctypes.c_int(0)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.IsProcessInJob.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
        ]
        kernel32.IsProcessInJob.restype = ctypes.c_int
        ok = kernel32.IsProcessInJob(
            kernel32.GetCurrentProcess(), None, ctypes.byref(is_in_job)
        )
        self.assertTrue(ok)
        self.assertEqual(is_in_job.value, 1)
