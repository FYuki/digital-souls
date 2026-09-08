"""専用test ProfileのWhisper中継。実POSTのPCMはメモリ内で照合し、数値だけ残す。"""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
import wave

import httpx
import numpy as np
from pcm_boundary_alignment import align_pcm, match_pcm_edges

ROOT = Path(__file__).resolve().parents[2]
MAX_PCM_BYTES = 3_200_000
MAX_ROWS = 1024


class WhisperPcmObserver:
    def __init__(self, output: Path, *, port: int = 50023,
                 upstream: str = 'http://127.0.0.1:50022',
                 transport: httpx.BaseTransport | None = None):
        self.output = output
        self.port = port
        self.client = httpx.Client(base_url=upstream, timeout=55, trust_env=False, transport=transport)
        self.lock = threading.Lock()
        self.selection = None
        self.rows = []
        self.active = 0
        self.overflow = False
        self.server = None
        self.thread = None
        self.stream = None

    def select(self, value: dict) -> None:
        if (set(value) != {'fixture_sha256', 'trial_ordinal', 'phase'}
                or type(value['trial_ordinal']) is not int or not 1 <= value['trial_ordinal'] <= 105
                or value['phase'] not in ('initial', 'labeled')):
            raise ValueError('invalid fixture selection')
        fixture = self._fixture(value['fixture_sha256'])
        with self.lock:
            if self.active:
                raise ValueError('transcription still active')
            self.selection = {**value, **fixture}

    def _fixture(self, digest: str) -> dict:
        root = ROOT / 'frontend/playwright/fixtures'
        metadata = json.loads((root / 'speech.metadata.json').read_text())
        if digest == metadata['audio_sha256']:
            path, start, end = root / 'speech.wav', metadata['speech_start_sample'], metadata['speech_end_sample']
        else:
            manifest = json.loads((root / 'voice-quality-v2/manifest.json').read_text())
            matches = [row for row in manifest['trials'] if row['audio_sha256'] == digest]
            if len(matches) != 1:
                raise ValueError('unknown or ambiguous fixture')
            row = matches[0]
            path = ROOT / 'frontend/test-results/vad-quality/fixtures-v2' / (row['id'] + '.wav')
            start, end = row['speech_intervals'][0]['start_sample'], row['speech_intervals'][-1]['end_sample']
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('fixture hash mismatch')
        with wave.open(str(path)) as source:
            if source.getframerate() != 48000 or source.getnchannels() != 1 or source.getsampwidth() != 2:
                raise ValueError('fixture format invalid')
            raw = source.readframes(source.getnframes())
        reference = np.frombuffer(raw, dtype='<i2')[::3].tobytes()
        return {'reference': reference, 'start': start // 3, 'end': (end + 2) // 3}

    def transcribe(self, body: bytes) -> tuple[int, bytes, str]:
        if not body or len(body) % 2 or len(body) > MAX_PCM_BYTES:
            raise ValueError('invalid PCM request')
        with self.lock:
            selected = self.selection
            self.active += 1
            index = len(self.rows) + 1
            row = {'request_ordinal': index, 'trial_ordinal': None if selected is None else selected['trial_ordinal'],
                   'phase': None if selected is None else selected['phase'],
                   'fixture_sha256': None if selected is None else selected['fixture_sha256'],
                   'started_ns': time.monotonic_ns(), 'completed_ns': None,
                   'input_sample_count': len(body) // 2, 'preparation_silence': body == bytes(3200),
                   'upstream_status': None, 'alignment': None, 'edge_alignment': None, 'reason': None}
            if len(self.rows) >= MAX_ROWS:
                self.overflow = True
                self.active -= 1
                raise ValueError('observer capacity exceeded')
            self.rows.append(row)
        try:
            # 同じbytesを変更せず転送する。応答本文は透過転送だけに使い、記録しない。
            response = self.client.post('/v1/transcriptions', content=body,
                                        headers={'Content-Type': 'application/octet-stream'})
            row['upstream_status'] = response.status_code
            if row['preparation_silence']:
                row['reason'] = 'preparation_silence'
            elif selected is None:
                row['reason'] = 'fixture_not_selected'
            else:
                try:
                    row['alignment'] = align_pcm(selected['reference'], body,
                        speech_start_sample=selected['start'], speech_end_sample=selected['end'])
                    row['edge_alignment'] = match_pcm_edges(selected['reference'], body,
                        speech_start_sample=selected['start'], speech_end_sample=selected['end'])
                except ValueError:
                    row['reason'] = 'alignment_failed'
            return response.status_code, response.content, response.headers.get('content-type', 'application/json')
        except httpx.HTTPError:
            row['reason'] = 'upstream_request_failed'
            return 502, b'{"error":"upstream_unavailable"}', 'application/json'
        finally:
            with self.lock:
                row['completed_ns'] = time.monotonic_ns()
                self.active -= 1
                if self.stream is not None:
                    self.stream.write(json.dumps(row, allow_nan=False) + '\n')
                    self.stream.flush()

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps({'scope': 'actual_whisper_input_pcm_diagnostic',
                'rows': self.rows, 'active_requests': self.active, 'overflow': self.overflow}))

    def __enter__(self):
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body, content_type='application/json'):
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == '/observations':
                    self.reply(200, json.dumps(owner.snapshot()).encode())
                elif self.path == '/health/ready':
                    try:
                        response = owner.client.get('/health/ready')
                        self.reply(response.status_code, response.content, response.headers.get('content-type', 'application/json'))
                    except httpx.HTTPError:
                        self.reply(502, b'{}')
                else:
                    self.reply(404, b'{}')

            def do_POST(self):
                try:
                    limit = 1024 if self.path == '/fixture' else MAX_PCM_BYTES
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= limit or self.headers.get('Transfer-Encoding') is not None:
                        raise ValueError('invalid body size')
                    self.connection.settimeout(60)
                    body = self.rfile.read(size)
                    if len(body) != size:
                        raise ValueError('incomplete body')
                    if self.path == '/fixture':
                        owner.select(json.loads(body))
                        self.reply(200, b'{}')
                    elif self.path == '/v1/transcriptions':
                        self.reply(*owner.transcribe(body))
                    else:
                        self.reply(404, b'{}')
                except (ValueError, TypeError, KeyError, OSError):
                    self.reply(400, b'{"error":"observer_request_invalid"}')
        try:
            self.stream = self.output.open('x', encoding='utf-8')
            self.server = ThreadingHTTPServer(('127.0.0.1', self.port), Handler)
            # 所有requestを終了まで待ち、他のWhisper serviceには終了要求を出さない。
            self.server.daemon_threads = False
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.server is not None:
            if self.thread is not None and self.thread.is_alive():
                self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
        if self.stream is not None:
            self.stream.close()
        self.client.close()

    def __exit__(self, *_):
        self.close()
