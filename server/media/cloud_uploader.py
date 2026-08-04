#!/usr/bin/env python3
"""Durably upload completed Dahua FTP recordings from edge spool to S3."""

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import shutil
import time
from pathlib import Path

from cloud_queue import UploadQueue

SERVER_ROOT = Path(__file__).resolve().parent.parent
SPOOL_ROOT = SERVER_ROOT / 'uploads' / 'videos'
QUEUE_DB = SERVER_ROOT / 'uploads' / 'cloud_queue.sqlite3'
TEST_QUEUE_DB = SERVER_ROOT / 'uploads' / 'cloud_queue.test.sqlite3'
VIDEO_EXTENSIONS = {'.dav', '.mp4', '.mov', '.avi', '.mkv', '.ts', '.265', '.h265'}


class LocalObjectStore:
    """Filesystem object store used only for deterministic integration tests."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, source, object_key, checksum):
        target = (self.root / object_key).resolve()
        if self.root != target and self.root not in target.parents:
            raise ValueError('object key escapes local object store')
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and sha256_file(target) == checksum:
            return checksum
        temporary = target.with_suffix(target.suffix + '.uploading')
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
        return checksum


class S3ObjectStore:
    def __init__(self, bucket, region=None):
        import boto3
        from botocore.exceptions import ClientError
        from boto3.s3.transfer import TransferConfig

        self.bucket = bucket
        self.client = boto3.client('s3', region_name=region)
        self.client_error = ClientError
        self.transfer_config = TransferConfig(
            multipart_threshold=100 * 1024 * 1024,
            multipart_chunksize=16 * 1024 * 1024,
            max_concurrency=2,
            use_threads=True,
        )

    def put(self, source, object_key, checksum):
        try:
            existing = self.client.head_object(Bucket=self.bucket, Key=object_key)
            if existing.get('Metadata', {}).get('sha256') == checksum:
                return existing.get('ETag', '').strip('"')
        except self.client_error as exc:
            if exc.response.get('ResponseMetadata', {}).get('HTTPStatusCode') != 404:
                raise
        content_type = mimetypes.guess_type(source.name)[0] or 'application/octet-stream'
        self.client.upload_file(
            str(source), self.bucket, object_key,
            ExtraArgs={
                'ContentType': content_type,
                'Metadata': {'sha256': checksum, 'source': 'dahua-edge-spool'},
            },
            Config=self.transfer_config,
        )
        uploaded = self.client.head_object(Bucket=self.bucket, Key=object_key)
        if uploaded.get('Metadata', {}).get('sha256') != checksum:
            raise RuntimeError('S3 checksum metadata verification failed')
        return uploaded.get('ETag', '').strip('"')


class CloudUploader:
    def __init__(self, queue, object_store, prefix):
        self.queue = queue
        self.object_store = object_store
        self.prefix = prefix.strip('/')

    def process_one(self):
        item = self.queue.claim()
        if not item:
            return False
        source = self.queue.spool_root / item['relative_path']
        try:
            stat = source.stat()
            if stat.st_size != item['size'] or stat.st_mtime_ns != item['mtime_ns']:
                self.queue.enqueue(source)
                raise RuntimeError('source changed after it was queued')
            checksum = sha256_file(source)
            object_key = '/'.join(part for part in (self.prefix, item['relative_path']) if part)
            etag = self.object_store.put(source, object_key, checksum)
            self.queue.complete(item['id'], object_key, etag, checksum)
            logging.info('Cloud upload complete: %s -> %s', source, object_key)
        except Exception as exc:
            self.queue.fail(item['id'], exc, item['attempts'] + 1)
            logging.error('Cloud upload failed: %s: %s', source, exc)
        return True


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description='Dahua edge spool cloud uploader')
    parser.add_argument('--backend', choices=('local-test', 's3'), required=True)
    parser.add_argument('--bucket')
    parser.add_argument('--region')
    parser.add_argument('--prefix', default='dahua-history')
    parser.add_argument('--local-target', default=str(SERVER_ROOT / 'uploads' / 'cloud-test'))
    parser.add_argument('--queue-db')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--poll-seconds', type=float, default=5)
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(message)s')
    queue_path = Path(args.queue_db) if args.queue_db else (
        TEST_QUEUE_DB if args.backend == 'local-test' else QUEUE_DB
    )
    queue = UploadQueue(queue_path, SPOOL_ROOT)
    queue.recover_interrupted()
    queue.reconcile(VIDEO_EXTENSIONS)
    if args.status:
        print(json.dumps(queue.snapshot(), ensure_ascii=False, indent=2))
        return
    if args.backend == 's3':
        if not args.bucket:
            parser.error('--bucket is required for the s3 backend')
        store = S3ObjectStore(args.bucket, args.region)
    else:
        store = LocalObjectStore(args.local_target)
    uploader = CloudUploader(queue, store, args.prefix)

    while True:
        worked = uploader.process_one()
        if args.once and not worked:
            break
        if not worked:
            time.sleep(args.poll_seconds)


if __name__ == '__main__':
    main()
