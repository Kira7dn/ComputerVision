import unittest

from camera_server.adas.pipeline import LatestFrameSlot, RollingLatency
from camera_server.archive.queue import UploadQueue
from camera_server.archive.worker import UploadWorker
from camera_server.media.manager import NetSdkHlsManager


class PackageBoundaryTest(unittest.TestCase):
    def test_runtime_boundaries_are_importable(self):
        self.assertIsNotNone(LatestFrameSlot)
        self.assertIsNotNone(RollingLatency)
        self.assertIsNotNone(UploadQueue)
        self.assertIsNotNone(UploadWorker)
        self.assertIsNotNone(NetSdkHlsManager)


if __name__ == "__main__":
    unittest.main()
