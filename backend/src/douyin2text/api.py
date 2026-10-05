"""v1 LAN gateway with durable queue, idempotency and opaque playback capabilities."""
import asyncio
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import shutil
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

from .artifacts import DATA, safe_path, snapshot, public_manifest, valid_files, digest
from .pipeline import CONFIG, process, LoginRequired, ConfirmationRequired
from .safe_network import validate_url
from .providers.registry import asr_provider

DB = DATA / 'state' / 'v1.sqlite3'
WORKER_LOCK = DATA / 'state' / 'worker.lock'
TOKEN = (DATA / 'state' / 'api-token').read_text().strip()
ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
STOPPED = ('succeeded', 'failed', 'partial_failed', 'waiting_login', 'waiting_confirmation')
logger = logging.getLogger('media_service_v1')

@contextmanager
def connect():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()
    DB.chmod(0o600)

def fail(status, code, message, retryable=False):
    raise HTTPException(status, {'code': code, 'message': message, 'retryable': retryable})

def auth(request: Request):
    value = request.headers.get('authorization', '')
    if value.startswith('Bearer ') and hmac.compare_digest(value[7:], TOKEN):
        return
    fail(401, 'AUTH_REQUIRED', 'Authentication required')

def initialize():
    with connect() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, url TEXT NOT NULL, request_json TEXT NOT NULL, request_hash TEXT NOT NULL, config_hash TEXT NOT NULL, status TEXT NOT NULL, media_id TEXT, artifact_id TEXT, error TEXT, created REAL NOT NULL, updated REAL NOT NULL)')
        c.execute('CREATE INDEX IF NOT EXISTS jobs_request ON jobs(request_hash,config_hash,created)')
        c.execute('CREATE TABLE IF NOT EXISTS idempotency (key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, job_id TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS playback (token_hash TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, kind TEXT NOT NULL, expires REAL NOT NULL)')
        # A single OS file lock protects both workers and migrations. Verified completed
        # stages are reused by the pipeline rather than discarded after interruption.
        c.execute("UPDATE jobs SET status='queued', error=NULL, updated=? WHERE status IN ('downloading','extracting','transcribing')", (time.time(),))

def import_legacy_jobs():
    """One-time import preserves IDs without changing the legacy DB or original files."""
    legacy_db = DATA / 'state' / 'jobs.sqlite3'
    if not legacy_db.exists():
        return
    with connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY)')
        if c.execute("SELECT 1 FROM migrations WHERE name='legacy-jobs'").fetchone():
            return
    legacy = sqlite3.connect('file:' + str(legacy_db) + '?mode=ro', uri=True)
    legacy.row_factory = sqlite3.Row
    try:
        rows = legacy.execute('SELECT * FROM jobs').fetchall()
    finally:
        legacy.close()
    if any(r['status'] not in STOPPED for r in rows):
        raise RuntimeError('Legacy service still processing; wait before migration')
    imported = []
    for row in rows:
        j = dict(row)
        canonical = json.dumps({'url': j['url'], 'options': {'language': 'zh', 'max_height': 1080}}, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        request_hash = hashlib.sha256(canonical.encode()).hexdigest()
        aid = None
        state = j['status']
        error = None
        if state == 'succeeded':
            path = DATA / 'metadata' / (j['media_id'] + '.manifest.json')
            m = json.loads(safe_path(str(path.relative_to(DATA))).read_text())
            aid, artifact = snapshot(m)
            language = {'english': 'en', 'chinese': 'zh'}.get(artifact['asr']['language'], artifact['asr']['language'])
            if language != 'zh' and artifact.get('translation', {}).get('target_language') != 'zh':
                state = 'waiting_confirmation'
                error = json.dumps({'code': 'SOURCE_LANGUAGE_MISMATCH', 'message': 'Chinese delivery not yet available; original ASR retained', 'retryable': False})
        else:
            error = json.dumps({'code': 'LEGACY_PROCESSING_FAILED', 'message': 'Earlier attempt failed; original files retained', 'retryable': False})
        imported.append((j['id'], j['url'], canonical, request_hash, CONFIG.get('output_fingerprint', CONFIG['asr_fingerprint']) + str(CONFIG.get('allow_source_language', False)), state, j.get('media_id'), aid, error, j['created'], j['updated']))
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.executemany('INSERT OR IGNORE INTO jobs(id,url,request_json,request_hash,config_hash,status,media_id,artifact_id,error,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)', imported)
        c.execute("INSERT OR IGNORE INTO migrations VALUES('legacy-jobs')")

def get(jid):
    if not ID.fullmatch(jid):
        fail(404, 'NOT_FOUND', 'Job not found')
    with connect() as c:
        row = c.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
    if not row:
        fail(404, 'NOT_FOUND', 'Job not found')
    return dict(row)

def update(jid, status, **fields):
    allowed = {'media_id', 'artifact_id', 'error'}
    fields = {k: v for k, v in fields.items() if k in allowed}
    with connect() as c:
        c.execute('UPDATE jobs SET status=?,updated=?' + ''.join(',' + k + '=?' for k in fields) + ' WHERE id=?', [status, time.time(), *fields.values(), jid])

def load_artifact(aid):
    if not aid or not ID.fullmatch(aid):
        fail(404, 'NOT_FOUND', 'Media not found')
    path = DATA / 'metadata' / 'v1' / (aid + '.json')
    try:
        return json.loads(safe_path(str(path.relative_to(DATA))).read_text())
    except (OSError, ValueError):
        fail(404, 'NOT_FOUND', 'Media not found')

async def worker():
    while True:
        with connect() as c:
            row = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        if not row:
            await asyncio.sleep(.5)
            continue
        j = dict(row)
        try:
            result = await process(j, lambda s, **kw: update(j['id'], s, **kw))
            aid, artifact = await asyncio.to_thread(snapshot, result)
            language = {'english': 'en', 'chinese': 'zh'}.get(artifact['asr']['language'], artifact['asr']['language'])
            translation = artifact.get('translation', {})
            if language != 'zh' and translation.get('target_language') != 'zh' and not CONFIG.get('allow_source_language', False):
                update(j['id'], 'waiting_confirmation', media_id=result['media_id'], artifact_id=aid,
                       error=json.dumps({'code': 'SOURCE_LANGUAGE_MISMATCH', 'message': 'Original audio is not Chinese; original transcription retained. Chinese translation requires an explicit delivery decision.', 'retryable': False}))
            else:
                update(j['id'], 'succeeded', media_id=result['media_id'], artifact_id=aid, error=None)
        except asyncio.CancelledError:
            raise
        except (LoginRequired, ConfirmationRequired) as exc:
            state = 'waiting_login' if isinstance(exc, LoginRequired) else 'waiting_confirmation'
            update(j['id'], state, error=json.dumps({'code': state.upper(), 'message': str(exc), 'retryable': False}))
        except Exception as exc:
            # Do not persist remote responses, exception URLs, cookies or headers.
            state = 'partial_failed' if get(j['id']).get('media_id') else 'failed'
            update(j['id'], state, error=json.dumps({'code': 'PROCESSING_FAILED', 'message': 'Processing failed; original files retained. See sanitized server diagnostics.', 'retryable': False}))
            logger.error('Job %s failed (%s)', j['id'], type(exc).__name__)

@asynccontextmanager
async def lifespan(app):
    lock = WORKER_LOCK.open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    initialize()
    import_legacy_jobs()
    task = asyncio.create_task(worker())
    app.state.worker = task
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        lock.close()

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

@app.exception_handler(StarletteHTTPException)
async def handle_http(request, exc):
    error = exc.detail if isinstance(exc.detail, dict) else {'code': 'HTTP_ERROR', 'message': 'Request rejected', 'retryable': False}
    return JSONResponse({'api_version': '1', 'error': error}, status_code=exc.status_code, headers=exc.headers)

@app.exception_handler(RequestValidationError)
async def handle_validation(request, exc):
    return JSONResponse({'api_version': '1', 'error': {'code': 'INVALID_INPUT', 'message': 'Invalid request body', 'retryable': False}}, status_code=400)

@app.exception_handler(Exception)
async def handle_unexpected(request, exc):
    logger.error('Unhandled request failure (%s)', type(exc).__name__)
    return JSONResponse({'api_version': '1', 'error': {'code': 'INTERNAL_ERROR', 'message': 'Internal service error', 'retryable': True}}, status_code=500)

@app.middleware('http')
async def request_limits(request, call_next):
    if request.method == 'POST':
        try:
            size = int(request.headers.get('content-length', '0'))
        except ValueError:
            size = 16385
        if size > 16384:
            return JSONResponse({'api_version': '1', 'error': {'code': 'REQUEST_TOO_LARGE', 'message': 'Request body too large', 'retryable': False}}, status_code=413)
        total = 0
        chunks = []
        async for chunk in request.stream():
            total += len(chunk)
            if total > 16384:
                return JSONResponse({'api_version': '1', 'error': {'code': 'REQUEST_TOO_LARGE', 'message': 'Request body too large', 'retryable': False}}, status_code=413)
            chunks.append(chunk)
        request._body = b''.join(chunks)
    response = await call_next(request)
    if response.status_code >= 400 and not response.headers.get('content-type', '').startswith('application/json'):
        headers = {'Content-Range': response.headers['content-range']} if 'content-range' in response.headers else None
        response = JSONResponse({'api_version': '1', 'error': {'code': 'HTTP_' + str(response.status_code), 'message': 'Request rejected', 'retryable': False}}, status_code=response.status_code, headers=headers)
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

async def asr_ready():
    return await asr_provider(CONFIG).ready()

@app.get('/health', dependencies=[Depends(auth)])
async def health(request: Request):
    free_bytes = shutil.disk_usage(DATA).free
    model_ok = safe_path(str((DATA / 'models' / Path(CONFIG['model']['path']).name).relative_to(DATA))).stat().st_size > 0
    ready = await asr_ready() and not app.state.worker.done() and model_ok and free_bytes > 1024**3
    with connect() as c:
        queue_depth = c.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
    if not ready:
        fail(503, 'NOT_READY', 'Model or worker not ready', True)
    return {'api_version': '1', 'status': 'ready', 'model': CONFIG['model']['name'], 'queue_depth': queue_depth, 'worker_count': 1, 'storage_free_bytes': free_bytes, 'client_address': request.client.host}

class Options(BaseModel):
    model_config = ConfigDict(extra='forbid')
    language: str = 'zh'
    max_height: int = 1080

class Job(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str
    options: Options = Options()

@app.post('/v1/jobs', dependencies=[Depends(auth)])
async def submit(body: Job, request: Request):
    key = request.headers.get('idempotency-key', '')
    if not ID.fullmatch(key):
        fail(400, 'INVALID_IDEMPOTENCY_KEY', 'Valid Idempotency-Key required')
    try:
        validate_url(body.url)
    except ValueError:
        fail(400, 'INVALID_URL', 'Only public HTTPS Douyin URLs accepted')
    if len(body.url) > 2048 or body.options.language != 'zh' or body.options.max_height != 1080:
        fail(400, 'UNSUPPORTED_OPTIONS', 'v1 supports language=zh and max_height=1080')
    canonical = json.dumps(body.model_dump(), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    request_hash = hashlib.sha256(canonical.encode()).hexdigest()
    config_hash = CONFIG.get('output_fingerprint', CONFIG['asr_fingerprint']) + str(CONFIG.get('allow_source_language', False))
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        known = c.execute('SELECT * FROM idempotency WHERE key=?', (key,)).fetchone()
        if known:
            if known['request_hash'] != request_hash:
                fail(409, 'IDEMPOTENCY_CONFLICT', 'Idempotency-Key already used for another request')
            j = dict(c.execute('SELECT * FROM jobs WHERE id=?', (known['job_id'],)).fetchone())
            reused = True
        else:
            prior = c.execute('SELECT * FROM jobs WHERE request_hash=? AND config_hash=? ORDER BY created DESC LIMIT 1', (request_hash, config_hash)).fetchone()
            if prior:
                j = dict(prior)
                reused = True
            else:
                if c.execute("SELECT COUNT(*) FROM jobs WHERE status NOT IN ('succeeded','failed','partial_failed','waiting_login','waiting_confirmation')").fetchone()[0] >= 100:
                    fail(429, 'QUEUE_FULL', 'Queue temporarily full', True)
                now = time.time()
                j = {'id': secrets.token_hex(16), 'status': 'queued'}
                c.execute('INSERT INTO jobs(id,url,request_json,request_hash,config_hash,status,created,updated) VALUES(?,?,?,?,?,?,?,?)', (j['id'], body.url, canonical, request_hash, config_hash, 'queued', now, now))
                reused = False
            c.execute('INSERT INTO idempotency VALUES(?,?,?)', (key, request_hash, j['id']))
    return JSONResponse({'api_version': '1', 'job_id': j['id'], 'status': j['status'], 'reused': reused}, status_code=200 if reused else 202)

@app.get('/v1/jobs/{jid}', dependencies=[Depends(auth)])
def status(jid: str):
    j = get(jid)
    result = {'api_version': '1', 'job_id': jid, 'status': j['status'], 'created_at': datetime.fromtimestamp(j['created'], timezone.utc).isoformat(), 'updated_at': datetime.fromtimestamp(j['updated'], timezone.utc).isoformat()}
    if j['error']:
        result['error'] = json.loads(j['error'])
    return result

def result_artifact(jid):
    j = get(jid)
    if j['status'] != 'succeeded':
        fail(409, 'RESULT_NOT_READY', 'Job result not ready')
    return load_artifact(j['artifact_id'])

@app.get('/v1/jobs/{jid}/manifest', dependencies=[Depends(auth)])
def manifest(jid: str):
    return public_manifest(result_artifact(jid), jid)

@app.get('/v1/jobs/{jid}/markdown', dependencies=[Depends(auth)])
def markdown(jid: str):
    m = result_artifact(jid)
    path = safe_path(m['files']['markdown']['path'])
    if digest(path) != m['files']['markdown']['sha256']:
        fail(409, 'INTEGRITY_FAILURE', 'Markdown integrity check failed')
    return FileResponse(path, media_type='text/markdown; charset=utf-8')

def media_artifact(mid):
    if ID.fullmatch(mid) and mid.isdigit():
        with connect() as c:
            j = c.execute('SELECT artifact_id FROM jobs WHERE media_id=? AND artifact_id IS NOT NULL ORDER BY updated DESC LIMIT 1', (mid,)).fetchone()
        if j:
            return load_artifact(j['artifact_id'])
    return load_artifact(mid)

def media_response(m, kind):
    if kind not in ('video', 'audio', 'cover'):
        fail(404, 'NOT_FOUND', 'Media not found')
    record = m['files'][kind]
    try:
        path = safe_path(record['path'])
        if path.stat().st_size != record['bytes']:
            fail(409, 'INTEGRITY_FAILURE', 'Media size mismatch')
    except (ValueError, OSError):
        fail(404, 'NOT_FOUND', 'Media unavailable')
    from .artifacts import mime
    return FileResponse(path, media_type=mime(path, kind), headers={'ETag': '"' + record['sha256'] + '"', 'Accept-Ranges': 'bytes'})

@app.api_route('/v1/media/{mid}/{kind}', methods=['GET', 'HEAD'], dependencies=[Depends(auth)])
def media(mid: str, kind: str):
    return media_response(media_artifact(mid), kind)

class Playback(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: str = 'video'

@app.post('/v1/media/{mid}/playback', dependencies=[Depends(auth)])
def playback(mid: str, body: Playback):
    if body.kind not in ('video', 'audio'):
        fail(400, 'INVALID_INPUT', 'Playback kind must be video or audio')
    m = media_artifact(mid)
    token = secrets.token_urlsafe(32)
    expires = time.time() + 599
    with connect() as c:
        c.execute('DELETE FROM playback WHERE expires < ?', (time.time(),))
        c.execute('INSERT INTO playback VALUES(?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), m['versioned_media_id'], body.kind, expires))
    return {'api_version': '1', 'url': CONFIG['base_url'] + '/play/' + token, 'expires_at': datetime.fromtimestamp(expires, timezone.utc).isoformat()}

@app.api_route('/play/{token}', methods=['GET', 'HEAD'])
def play(token: str):
    with connect() as c:
        row = c.execute('SELECT * FROM playback WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    if not row or row['expires'] <= time.time():
        fail(401, 'PLAYBACK_EXPIRED', 'Playback link invalid or expired')
    return media_response(load_artifact(row['artifact_id']), row['kind'])

if __name__ == '__main__':
    import uvicorn
    os.umask(0o077)
    uvicorn.run(app, host=CONFIG['bind'], port=8765, workers=1, access_log=False)
