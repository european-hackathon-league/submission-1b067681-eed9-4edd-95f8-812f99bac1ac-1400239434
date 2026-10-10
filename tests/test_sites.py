"""sites.py helpers that need no browser."""
import asyncio
import os
import unittest
from unittest import mock

import sites


class Sites(unittest.TestCase):
    def test_plain_strips_padding(self):
        self.assertEqual(sites.plain("A​B-‍1﻿2 "), "AB-12")

    def test_locked_means_gesperrt_only(self):
        self.assertTrue(sites.is_locked("Bereich Gesperrt"))
        self.assertFalse(sites.is_locked("Ich bin kein Roboter"))   # the caption stays after the check

    def test_unlock_fails_fast_in_a_run(self):
        with mock.patch.dict(os.environ, {"CHALLENGE_RUN_TOKEN": "t"}):
            with self.assertRaises(RuntimeError):
                asyncio.run(sites.pass_robot_check(page=None))


if __name__ == "__main__":
    unittest.main()
