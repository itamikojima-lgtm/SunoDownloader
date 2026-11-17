"""Temporary, session-isolated downloads. Run with a single Uvicorn worker."""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

BASE = Path(__file__).resolve().parent.parent
TTL = 15 * 60
MAX_JOBS = 8
MAX_RUNNING = 1
TIMEOUT = 600
COOKIE = 'suno_session'
SECRET = secrets.token_bytes(32)
SESSION_SECONDS = 86400
ERRORS = {
    'ACCESS_BLOCKED': 'Suno側で取得が拒否されました。公式サイトで利用可能な状態を確認してください。',
    'RIGHTS_DENIED': 'この音源の取得が許可されませんでした。',
    'NO_SONG_ID': '楽曲を特定できません。共有URLを確認してください。',
    'NOT_FOUND': '楽曲が見つかりません。共有URLを確認してください。',
    'NETWORK_ERROR': 'Sunoへの通信に失敗しました。時間を置いて再実行してください。',
    'FFMPEG_MISSING': 'サーバーにFFmpegがありません。管理者に確認してください。',
    'FFPROBE_MISSING': 'サーバーにFFprobeがありません。管理者に確認してください。',
}


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str = Field(min_length=1, max_length=2048)
    format: str = 'mp3'


def validate_url(value: str) -> str:
    import re
    value = value.strip()
    try:
        p = urlsplit(value)
        if p.scheme != 'https' or p.hostname not in {'suno.com', 'www.suno.com'} or p.username or p.password or p.port not in (None, 443) or p.fragment:
            raise ValueError()
        if not (re.fullmatch(r'/s/[A-Za-z0-9_-]{4,64}/?', p.path) or re.fullmatch(r'/song/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/?', p.path)):
            raise ValueError()
    except ValueError:
        raise HTTPException(422, 'Sunoの楽曲共有URL（/s/ または /song/）を入力してください。')
    # Tracking queries are not needed by the pipeline.
    return f'https://{p.hostname}{p.path.rstrip("/")}'


def new_session() -> str:
    payload = f'{int(time.time())}.{secrets.token_hex(24)}'
    return payload + '.' + hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()


def valid_session(token: str) -> bool:
    try:
        stamp, nonce, signature = token.split('.')
        if len(nonce) != 48 or not 0 <= time.time() - int(stamp) <= SESSION_SECONDS:
            return False
        expected = hmac.new(SECRET, f'{stamp}.{nonce}'.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False


def csrf(token: str) -> str:
    return hmac.new(SECRET, ('csrf:' + token).encode(), hashlib.sha256).hexdigest()


def owner(request: Request, write=False) -> str:
    token = request.cookies.get(COOKIE, '')
    if not valid_session(token):
        raise HTTPException(401, 'セッションが切れました。画面を再読み込みしてください。')
    if write and not hmac.compare_digest(request.headers.get('x-csrf-token', ''), csrf(token)):
        raise HTTPException(403, '操作を確認できません。画面を再読み込みしてください。')
    return token


@dataclass
class Job:
    id: str
    owner: str
    directory: Path
    format: str
    status: str = 'queued'
    stage: str = 'queued'
    progress: float | None = None
    title: str | None = None
    filename: str | None = None
    size_bytes: int | None = None
    error: str | None = None
    expires_at: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    process: subprocess.Popen | None = None
    downloads: int = 0

    def public(self):
        return {key: getattr(self, key) for key in ('id', 'status', 'stage', 'progress', 'title', 'format', 'filename', 'size_bytes', 'error', 'expires_at')} | {'download_url': f'/api/jobs/{self.id}/download' if self.status == 'completed' else None}


def kill(proc):
    if proc.poll() is not None:
        return
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
        proc.kill()


class Manager:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix='suno_web_'))
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        self.closed = False

    def cleanup(self):
        with self.lock:
            for key, job in list(self.jobs.items()):
                if job.expires_at and job.expires_at <= time.time() and not job.downloads:
                    shutil.rmtree(job.directory, ignore_errors=True)
                    del self.jobs[key]

    def create(self, token, url, fmt):
        with self.lock:
            self.cleanup()
            active = [j for j in self.jobs.values() if j.status in {'queued', 'running'}]
            if active or len(self.jobs) >= MAX_JOBS:
                raise HTTPException(429, '現在処理中、または一時保存枠がいっぱいです。少し待って再実行してください。')
            if shutil.disk_usage(self.root).free < 1024 * 1024 * 1024:
                raise HTTPException(507, 'サーバーの空き容量が不足しています。時間を置いて再実行してください。')
            job_id = secrets.token_urlsafe(24)
            directory = self.root / job_id
            directory.mkdir()
            job = Job(job_id, token, directory, fmt)
            self.jobs[job_id] = job
            threading.Thread(target=self.run, args=(job, url), daemon=True).start()
            return job.public()

    def get(self, job_id, token):
        with self.lock:
            self.cleanup()
            job = self.jobs.get(job_id)
            if not job or not hmac.compare_digest(job.owner, token):
                raise HTTPException(404, '処理結果が見つかりません。期限切れ・再起動の場合は再実行してください。')
            return job

    def run(self, job, url):
        proc = None
        deadline = time.monotonic() + TIMEOUT
        result = None
        try:
            with self.lock:
                job.status = 'running'
                job.stage = 'resolve'
            proc = subprocess.Popen([sys.executable, '-m', 'backend.worker'], cwd=BASE,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, encoding='utf-8', start_new_session=os.name != 'nt')
            with self.lock:
                job.process = proc
            proc.stdin.write(json.dumps({'url': url, 'directory': str(job.directory), 'format': job.format}) + '\n')
            proc.stdin.close()
            # Reading runs separately so a stalled upstream request remains cancellable.
            events: list[dict] = []
            def read():
                for line in proc.stdout:
                    if len(line) < 8192:
                        try:
                            payload = json.loads(line)
                            with self.lock:
                                events.append(payload)
                        except ValueError:
                            pass
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            while proc.poll() is None:
                if job.cancel.is_set() or self.closed:
                    kill(proc)
                    break
                if time.monotonic() > deadline:
                    kill(proc)
                    raise TimeoutError()
                total = sum(p.stat().st_size for p in job.directory.rglob('*') if p.is_file())
                if total > 512 * 1024 * 1024:
                    kill(proc)
                    raise OSError('size limit')
                result = self.events(job, events) or result
                time.sleep(.1)
            proc.wait(timeout=10)
            reader.join(timeout=2)
            result = self.events(job, events) or result
            with self.lock:
                if job.cancel.is_set() or self.closed:
                    job.status = 'cancelled'
                elif result and result.get('status') == 'COMPLETED':
                    filename = result.get('filename')
                    path = job.directory / (filename or '')
                    if not filename or Path(filename).name != filename or not path.is_file() or path.stat().st_size > 256 * 1024 * 1024:
                        raise ValueError('invalid output')
                    job.filename = filename
                    job.size_bytes = path.stat().st_size
                    job.status = 'completed'
                    job.stage = 'complete'
                    job.progress = 1
                else:
                    code = (result or {}).get('status', 'FAILED')
                    job.status = 'failed'
                    job.error = ERRORS.get(code, '音源を準備できませんでした。共有URLとSuno側の利用状況を確認してください。')
        except Exception:
            with self.lock:
                job.status = 'cancelled' if job.cancel.is_set() else 'failed'
                job.error = None if job.status == 'cancelled' else '処理が完了しませんでした。時間を置いて再実行してください。'
        finally:
            if proc:
                kill(proc)
            with self.lock:
                job.process = None
                job.expires_at = time.time() + TTL
                if job.status != 'completed':
                    shutil.rmtree(job.directory, ignore_errors=True)

    def events(self, job, events):
        result = None
        with self.lock:
            while events:
                e = events.pop(0)
                if 'title' in e:
                    job.title = str(e['title'])[:200]
                if 'stage' in e:
                    stage = e['stage']
                    job.stage = {'validate_source': 'decrypt', 'validate_output': 'verify', 'commit': 'verify'}.get(stage, stage)
                    job.progress = e.get('progress')
                if 'result' in e:
                    result = e['result']
        return result

    def stop(self):
        self.closed = True
        with self.lock:
            for job in self.jobs.values():
                job.cancel.set()
                if job.process:
                    kill(job.process)
        shutil.rmtree(self.root, ignore_errors=True)


def create_app(manager=None):
    manager = manager or Manager()
    @asynccontextmanager
    async def lifespan(app):
        async def sweep():
            while True:
                await asyncio.sleep(30)
                manager.cleanup()
        task = asyncio.create_task(sweep())
        yield
        task.cancel()
        manager.stop()
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.manager = manager

    @app.middleware('http')
    async def guard(request, call_next):
        if request.url.path.startswith('/api/'):
            if request.headers.get('sec-fetch-site') == 'cross-site':
                return JSONResponse({'detail': '外部サイトからの操作はできません。'}, 403)
            origin = request.headers.get('origin')
            expected = (os.getenv('PUBLIC_ORIGIN') or os.getenv('RENDER_EXTERNAL_URL') or '').rstrip('/') or None
            if origin and origin != (expected or str(request.base_url).rstrip('/')):
                return JSONResponse({'detail': '許可されていない接続元です。'}, 403)
            try:
                length = int(request.headers.get('content-length', '0') or '0')
            except ValueError:
                return JSONResponse({'detail': '入力の長さが不正です。'}, 400)
            if length < 0:
                return JSONResponse({'detail': '入力の長さが不正です。'}, 400)
            if length > 4096:
                return JSONResponse({'detail': '入力が大きすぎます。'}, 413)
            if request.method == 'POST':
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 4096:
                        return JSONResponse({'detail': '入力が大きすぎます。'}, 413)
                request._body = bytes(body)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.get('/api/health')
    def health():
        return {'status': 'ok', 'ffmpeg': bool(shutil.which('ffmpeg')), 'ffprobe': bool(shutil.which('ffprobe'))}

    @app.get('/api/session')
    def session(request: Request, response: Response):
        token = request.cookies.get(COOKIE, '')
        if not valid_session(token):
            token = new_session()
        response.set_cookie(COOKIE, token, httponly=True, samesite='strict', secure=os.getenv('COOKIE_SECURE') == '1', max_age=SESSION_SECONDS)
        return {'csrf': csrf(token)}

    @app.post('/api/jobs', status_code=202)
    def create(data: Input, request: Request):
        token = owner(request, True)
        if data.format not in {'mp3', 'm4a', 'wav'}:
            raise HTTPException(422, '対応形式はMP3・M4A・WAVです。')
        url = validate_url(data.url)
        if not shutil.which('ffprobe') or not shutil.which('ffmpeg'):
            raise HTTPException(503, 'サーバーの音声処理ツールが準備されていません。')
        return manager.create(token, url, data.format)

    @app.get('/api/jobs/{job_id}')
    def status(job_id: str, request: Request):
        return manager.get(job_id, owner(request)).public()

    @app.post('/api/jobs/{job_id}/cancel')
    def cancel(job_id: str, request: Request):
        job = manager.get(job_id, owner(request, True))
        with manager.lock:
            if job.status in {'queued', 'running'}:
                job.cancel.set()
            return job.public()

    @app.get('/api/jobs/{job_id}/download')
    def download(job_id: str, request: Request):
        from starlette.background import BackgroundTask
        with manager.lock:
            job = manager.get(job_id, owner(request))
            if job.status != 'completed' or not job.filename:
                raise HTTPException(409, '保存ファイルはまだ準備できていません。')
            job.downloads += 1
        def release():
            with manager.lock:
                job.downloads -= 1
        return FileResponse(job.directory / job.filename, filename=job.filename,
                            media_type={'mp3': 'audio/mpeg', 'm4a': 'audio/mp4', 'wav': 'audio/wav'}[job.format],
                            background=BackgroundTask(release))

    dist = BASE / 'frontend' / 'dist'
    if dist.is_dir():
        app.mount('/', StaticFiles(directory=dist, html=True), name='frontend')
    return app


app = create_app()
