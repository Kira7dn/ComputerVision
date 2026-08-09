import tempfile
import time
import unittest
from pathlib import Path

from camera_server.archive.queue import UploadQueue
from camera_server.archive.worker import (
    LocalObjectStore,
    ObjectStore,
    UploadWorker,
    sha256_file,
)


class FailOnceStore:
    def __init__(self, delegate: ObjectStore) -> None:
        self.delegate = delegate
        self.calls = 0

    def put(self, source: Path, key: str, checksum: str) -> str:
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError('simulated WAN outage')
        return self.delegate.put(source, key, checksum)


class UploadWorkerIntegrationTest(unittest.TestCase):
    def test_retry_restart_and_idempotency(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spool = root / 'spool'
            object_store = root / 'objects'
            spool.mkdir()
            source = spool / 'xvr' / '2026-08-02' / 'XVR_ch6_test.dav'
            source.parent.mkdir(parents=True)
            source.write_bytes(b'DHAV' + b'video-payload' * 1024)

            queue = UploadQueue(root / 'queue.sqlite3', spool)
            queue.enqueue(source)
            store = FailOnceStore(LocalObjectStore(object_store))
            worker = UploadWorker(queue, store, 'vehicle/VH-TEST')

            self.assertTrue(worker.process_one())
            failed = queue.snapshot()[0]
            self.assertEqual('failed', failed['status'])
            self.assertTrue(source.is_file())

            time.sleep(2.1)
            self.assertTrue(worker.process_one())
            uploaded = queue.snapshot()[0]
            self.assertEqual('uploaded', uploaded['status'])
            target = object_store / uploaded['object_key']
            self.assertEqual(sha256_file(source), sha256_file(target))

            restarted_queue = UploadQueue(root / 'queue.sqlite3', spool)
            restarted_queue.recover_interrupted()
            restarted_queue.reconcile({'.dav'})
            restarted = UploadWorker(restarted_queue, store, 'vehicle/VH-TEST')
            self.assertFalse(restarted.process_one())
            self.assertEqual(2, store.calls)


if __name__ == '__main__':
    unittest.main()
