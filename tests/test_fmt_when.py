"""
Timestamps are stored in UTC and displayed local.

The bug this guards against: the UI sliced the stored ISO string to 16
characters, which threw away the "Z" and printed UTC digits as though they
were local time -- a scan run at 16:52 PDT displayed as 23:52, seven hours in
the future, with no timezone shown to reveal the discrepancy.

Pinned across a DST boundary because hardcoding "PST" is wrong from roughly
March to November, which is most of the year.
"""
from __future__ import annotations

import os
import time
import unittest

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cartographer.cli import fmt_when

UNIX_TZ = hasattr(time, "tzset")


class FmtWhenTest(unittest.TestCase):
    def setUp(self):
        self._tz = os.environ.get("TZ")

    def tearDown(self):
        if UNIX_TZ:
            if self._tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = self._tz
            time.tzset()

    def _in_tz(self, tz, iso):
        os.environ["TZ"] = tz
        time.tzset()
        return fmt_when(iso)

    @unittest.skipUnless(UNIX_TZ, "TZ manipulation needs tzset()")
    def test_utc_is_converted_to_local_not_printed_raw(self):
        # 23:52 UTC is 16:52 in Pacific daylight time. The old code printed
        # "2026-09-08 23:52" here, which is the failure being guarded.
        got = self._in_tz("America/Los_Angeles", "2026-09-08T23:52:00Z")
        self.assertTrue(got.startswith("2026-09-08 16:52"), got)
        self.assertNotIn("23:52", got)

    @unittest.skipUnless(UNIX_TZ, "TZ manipulation needs tzset()")
    def test_zone_label_follows_dst_rather_than_being_hardcoded(self):
        summer = self._in_tz("America/Los_Angeles", "2026-09-08T23:52:00Z")
        winter = self._in_tz("America/Los_Angeles", "2026-01-15T23:52:00Z")
        self.assertIn("PDT", summer)
        self.assertIn("PST", winter)
        # Same instant-of-day in UTC, different local hour across the boundary.
        self.assertIn("16:52", summer)
        self.assertIn("15:52", winter)

    @unittest.skipUnless(UNIX_TZ, "TZ manipulation needs tzset()")
    def test_other_zones(self):
        self.assertTrue(
            self._in_tz("America/New_York", "2026-09-08T23:52:00Z")
            .startswith("2026-09-08 19:52"))
        self.assertTrue(
            self._in_tz("UTC", "2026-09-08T23:52:00Z")
            .startswith("2026-09-08 23:52"))

    def test_non_timestamps_pass_through_untouched(self):
        for value in ("unknown", "?", "", None):
            self.assertEqual(fmt_when(value), value or "unknown")
        # A malformed value is shown as-is rather than crashing the status line.
        self.assertEqual(fmt_when("garbage"), "garbage")


if __name__ == "__main__":
    unittest.main()
