import unittest
from camera_server.adas.pipeline import LatestFrameSlot, RollingLatency


class LatestFrameSlotTest(unittest.TestCase):
    def test_replaces_unconsumed_frame(self):
        slot = LatestFrameSlot()
        slot.put('old', 1.0)
        slot.put('new', 2.0)
        sequence, frame, received_at, source_pts_ms, source_capture_at = slot.get(0)
        self.assertEqual(2, sequence)
        self.assertEqual('new', frame)
        self.assertEqual(2.0, received_at)
        self.assertIsNone(source_pts_ms)
        self.assertIsNone(source_capture_at)
        self.assertEqual(1, slot.replaced)


class RollingLatencyTest(unittest.TestCase):
    def test_reports_percentiles(self):
        latency = RollingLatency()
        for value in range(1, 101):
            latency.add(value)
        snapshot = latency.snapshot()
        self.assertEqual(100, snapshot['count'])
        self.assertEqual(50.0, snapshot['p50_ms'])
        self.assertEqual(95.0, snapshot['p95_ms'])
        self.assertEqual(99.0, snapshot['p99_ms'])


if __name__ == '__main__':
    unittest.main()
