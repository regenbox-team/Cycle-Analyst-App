from __future__ import annotations
import os
from flask import Blueprint, Response, request, abort
from app.config import PMTILES_PATH


def _send_file_range(path: str):
    if not os.path.exists(path) or not os.path.isfile(path):
        abort(404)

    file_size = os.path.getsize(path)
    range_header = request.headers.get('Range', None)
    start = 0
    end = file_size - 1

    if range_header:
        try:
            # Expecting formats like: bytes=START-END or bytes=START-
            units, range_spec = range_header.split('=')
            if units.strip().lower() != 'bytes':
                abort(416)
            start_str, end_str = range_spec.split('-')
            if start_str:
                start = int(start_str)
            if end_str:
                end = int(end_str)
            if start > end or start >= file_size:
                abort(416)
        except Exception:
            abort(416)

    chunk_size = 1024 * 256

    def generate():
        with open(path, 'rb') as f:
            f.seek(start)
            bytes_left = end - start + 1
            while bytes_left > 0:
                read_size = min(chunk_size, bytes_left)
                data = f.read(read_size)
                if not data:
                    break
                bytes_left -= len(data)
                yield data

    status = 206 if range_header else 200
    headers = {
        'Content-Type': 'application/vnd.pmtiles',
        'Accept-Ranges': 'bytes',
        'Content-Length': str(end - start + 1),
    }
    if status == 206:
        headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'

    # Add mild caching to avoid re-requests for same ranges
    headers['Cache-Control'] = 'public, max-age=3600'

    return Response(generate(), status=status, headers=headers)


def basemap():
    # Serve the configured PMTiles file under a fixed URL
    return _send_file_range(PMTILES_PATH)


def create_blueprint():
    bp = Blueprint('tiles', __name__)
    bp.add_url_rule('/tiles/basemap.pmtiles', view_func=basemap)
    return bp


def register(app):
    app.add_url_rule('/tiles/basemap.pmtiles', view_func=basemap)

