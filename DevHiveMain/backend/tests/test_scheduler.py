import unittest
from datetime import datetime, timezone

from app.scheduler import due_week, next_run


class SchedulerTimeTest(unittest.TestCase):
    def test_next_run_is_sunday_two_am_india_time(self):
        thursday = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
        self.assertEqual(next_run(thursday).isoformat(), "2026-10-18T02:00:00+05:30")
        self.assertIsNone(due_week(datetime(2026, 10, 17, 20, 29, tzinfo=timezone.utc)))
        self.assertEqual(due_week(datetime(2026, 10, 17, 20, 30, tzinfo=timezone.utc)), "2026-10-18")
        self.assertEqual(next_run(datetime(2026, 10, 17, 20, 30, tzinfo=timezone.utc)).isoformat(),
                         "2026-10-25T02:00:00+05:30")


if __name__ == "__main__":
    unittest.main()
