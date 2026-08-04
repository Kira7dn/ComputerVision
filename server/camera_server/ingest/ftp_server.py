#!/usr/bin/env python3
"""Standalone FTP receiver for recordings uploaded directly by Dahua XVR."""

import argparse
import logging
import os
from pathlib import Path

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from camera_server.archive.queue import UploadQueue
from camera_server.config import load_settings

HOST = '0.0.0.0'
PORT = 2121
PASSIVE_PORTS = range(30000, 30010)
SETTINGS = load_settings(require_dahua=False)
VIDEO_DIR = SETTINGS.video_dir
QUEUE_DB = SETTINGS.queue_db
class ReceiverHandler(FTPHandler):
    def on_file_received(self, file):
        path = Path(file)
        self.upload_queue.enqueue(path)
        logging.info('Dahua upload complete: %s (%d bytes)', path, path.stat().st_size)

    def on_incomplete_file_received(self, file):
        path = Path(file)
        if path.exists():
            path.unlink()
        logging.error('Incomplete upload removed: %s', path)


def main():
    parser = argparse.ArgumentParser(description='Standalone Dahua FTP recording receiver')
    parser.add_argument('--host', default=HOST)
    parser.add_argument('--port', type=int, default=PORT)
    parser.add_argument('--user', default=os.environ.get('DAHUA_FTP_USER', 'dahua'))
    parser.add_argument('--password', default=None, help='FTP password; required when DAHUA_FTP_PASSWORD is unset')
    parser.add_argument('--public-host', default='192.168.100.108')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(message)s')
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    authorizer = DummyAuthorizer()
    ftp_password = args.password or os.environ.get('DAHUA_FTP_PASSWORD')
    if not ftp_password:
        parser.error('provide --password or DAHUA_FTP_PASSWORD')
    authorizer.add_user(args.user, ftp_password, str(VIDEO_DIR), perm='elradfmwMT')
    ReceiverHandler.authorizer = authorizer
    ReceiverHandler.passive_ports = PASSIVE_PORTS
    ReceiverHandler.banner = 'Letron Dahua FTP receiver'
    ReceiverHandler.upload_queue = UploadQueue(QUEUE_DB, VIDEO_DIR)
    server = FTPServer((args.host, args.port), ReceiverHandler)

    logging.info('FTP receiver ready at %s:%d; storage=%s', args.host, args.port, VIDEO_DIR)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.close_all()


if __name__ == '__main__':
    main()
