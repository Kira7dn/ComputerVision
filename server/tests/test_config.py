import os
import unittest
from pathlib import Path
from unittest.mock import patch

from camera_server.config import load_settings


class SettingsTest(unittest.TestCase):
    def test_requires_dahua_password(self):
        with patch.dict(os.environ, {"DAHUA_PASSWORD": ""}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "DAHUA_PASSWORD"):
                load_settings()

    def test_runtime_paths_are_derived_from_runtime_dir(self):
        with patch.dict(os.environ, {"CAMERA_RUNTIME_DIR": "C:/camera-runtime", "DAHUA_PASSWORD": "secret"}, clear=False):
            settings = load_settings()
        self.assertEqual(Path("C:/camera-runtime").resolve(), settings.runtime_dir)
        self.assertEqual(settings.runtime_dir / "uploads" / "videos", settings.video_dir)
        self.assertEqual(settings.runtime_dir / "queue" / "cloud_queue.sqlite3", settings.queue_db)


if __name__ == "__main__":
    unittest.main()
