"""Immutable, hash checked result snapshots; never return server filesystem paths."""
import hashlib
import json
import mimetypes
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .runtime import DATA

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def safe_path(relative):
    path = DATA / relative
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(DATA.resolve()) or not resolved.is_file():
        raise ValueError('Unsafe artifact path')
    # Reject symlinks anywhere in an artifact path, even if currently in root.
    current = path
    while current != DATA:
        if current.is_symlink():
            raise ValueError('Symlink artifact rejected')
        current = current.parent
    return resolved

def valid_files(manifest):
    try:
        return all(safe_path(f['path']).stat().st_size == f['bytes'] and digest(safe_path(f['path'])) == f['sha256'] for f in manifest['files'].values())
    except (OSError, ValueError, KeyError):
        return False

def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.part')
    with temporary.open('w', encoding='utf-8') as f:
        os.chmod(temporary, 0o600)
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    temporary.replace(path)

def snapshot(legacy):
    if not valid_files(legacy):
        raise ValueError('Artifact integrity check failed')
    key = hashlib.sha256((legacy['files']['video']['sha256'] + legacy['asr']['fingerprint'] + legacy['files']['markdown']['sha256'] + 'v1-snapshot-2').encode()).hexdigest()[:24]
    mid = legacy['media_id'] + '_' + key
    destination = DATA / 'metadata' / 'v1' / (mid + '.json')
    if destination.exists():
        existing = json.loads(destination.read_text())
        if valid_files(existing):
            return mid, existing
        raise ValueError('Existing immutable snapshot failed integrity check')
    m = json.loads(json.dumps(legacy))
    # Mutable legacy transcript filenames are copied to immutable version directories.
    for kind, f in m['files'].items():
        if kind in ('video', 'audio', 'cover'):
            continue
        src = safe_path(f['path'])
        target = DATA / 'transcripts' / 'versions' / mid / (kind + src.suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        if kind == 'markdown':
            text = src.read_text(encoding='utf-8').replace('/v1/media/' + legacy['media_id'] + '/', '/v1/media/' + mid + '/')
            text = text.replace('在服务首页登录后，用浏览器会话播放；API 使用 Authorization 请求头。也可用客户端 play 命令生成短期播放地址。', 'API 使用 Authorization 请求头。浏览器播放请用客户端 playback 命令生成短期地址。')
            text = text.replace('先在服务首页登录，浏览器使用会话播放。API 使用 Authorization 请求头。', 'API 使用 Authorization 请求头。浏览器播放请用客户端 playback 命令生成短期地址。')
            expected = text.encode('utf-8')
            if not target.exists():
                with target.open('xb') as out:
                    os.chmod(target, 0o600)
                    out.write(expected)
                    out.flush()
                    os.fsync(out.fileno())
            f['bytes'] = len(expected)
            f['sha256'] = hashlib.sha256(expected).hexdigest()
        elif not target.exists():
            with target.open('xb') as out, src.open('rb') as inp:
                os.chmod(target, 0o600)
                shutil.copyfileobj(inp, out)
                out.flush()
                os.fsync(out.fileno())
        if digest(target) != f['sha256']:
            raise ValueError('Snapshot content differs')
        f['path'] = str(target.relative_to(DATA))
    m['versioned_media_id'] = mid
    # Stable Markdown references may use the original ID; it resolves to this snapshot.
    atomic_json(destination, m)
    return mid, m

def mime(path, kind):
    if kind == 'cover':
        with path.open('rb') as f:
            header = f.read(16)
        if header.startswith(b'\x89PNG'):
            return 'image/png'
        if header[8:12] == b'WEBP':
            return 'image/webp'
        return 'image/jpeg'
    return 'video/mp4' if kind == 'video' else 'audio/mp4' if path.suffix == '.m4a' else 'audio/x-matroska'

def public_manifest(m, job_id):
    language = {'english': 'en', 'chinese': 'zh'}.get(m['asr']['language'], m['asr']['language'])
    mid = m['versioned_media_id']
    result = {
        'api_version': '1', 'job_id': job_id,
        'source': {'platform': 'douyin', 'id': m['media_id'], 'url': m['canonical_url'], 'share_url': m['original_share_url'],
                   'title': m.get('title'), 'description': m.get('description'), 'author': m.get('author'),
                   'published_at': m.get('published_at'), 'duration_seconds': m.get('duration_seconds')},
        'transcription': {'engine': m['asr'].get('engine', 'whisper.cpp'), 'model': m['asr']['model'], 'model_sha256': m['asr']['model_sha256'],
                          'language': language, 'requested_language': 'zh', 'fingerprint': m['asr']['fingerprint'],
                          'elapsed_seconds': m['asr'].get('elapsed_seconds'), 'peak_server_rss_bytes': m['asr'].get('peak_server_rss_bytes')},
        'markdown': {'sha256': m['files']['markdown']['sha256']},
        'media': {k: {'path': f'/v1/media/{mid}/{k}', 'sha256': m['files'][k]['sha256'], 'bytes': m['files'][k]['bytes'],
                      'mime': mime(safe_path(m['files'][k]['path']), k)} for k in ('video', 'audio', 'cover')},
        'source_provider': m.get('source_provider', 'f2'), 'versions': m.get('versions'), 'validation': m.get('validation'), 'timings': m.get('timings'),
        'processed_at': m.get('processed_at'),
    }
    if m.get('translation'):
        result['translation'] = m['translation']
    return result
