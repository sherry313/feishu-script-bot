"""Small, single-submit video worker used by the Feishu avatar buttons."""
import os
import json
import time
import uuid
import logging
import threading
from pathlib import Path

import requests


API_BASE = 'https://api.heygen.com'
logger = logging.getLogger('uvicorn.error')
JOB_STORE = Path(os.getenv('AVATAR_JOB_STORE', '/data/avatar_jobs.json'))
_store_lock = threading.Lock()


def _load_jobs():
    try:
        return json.loads(JOB_STORE.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_job(job_id, **changes):
    with _store_lock:
        jobs = _load_jobs()
        job = jobs.setdefault(job_id, {})
        job.update(changes)
        JOB_STORE.parent.mkdir(parents=True, exist_ok=True)
        temporary = JOB_STORE.with_suffix('.tmp')
        temporary.write_text(json.dumps(jobs, ensure_ascii=False), encoding='utf-8')
        temporary.replace(JOB_STORE)


def register_job(payload):
    job_id = uuid.uuid4().hex
    _save_job(job_id, state='queued', payload=payload, created_at=time.time())
    return job_id


def unfinished_jobs():
    return [(job_id, item.get('payload') or {}) for job_id, item in _load_jobs().items()
            if item.get('state') in {'queued', 'polling', 'callback_pending'}
            and item.get('payload')]


def _headers(json_body=False):
    key = os.getenv('HEYGEN_API_KEY', '').strip()
    if not key:
        raise RuntimeError('视频生成密钥尚未配置。')
    headers = {'X-Api-Key': key, 'Accept': 'application/json'}
    if json_body:
        headers['Content-Type'] = 'application/json'
    return headers


def avatar_bindings():
    """Private API look/voice bindings. These values never enter Feishu cards."""
    return {
        'avatar_a': {
            'avatar_id': os.getenv('AVATAR_A_LOOK_ID', '').strip(),
            'voice_id': os.getenv('AVATAR_A_VOICE_ID', '').strip(),
        },
        'avatar_b': {
            'avatar_id': os.getenv('AVATAR_B_LOOK_ID', '').strip(),
            'voice_id': os.getenv('AVATAR_B_VOICE_ID', '').strip(),
        },
        'avatar_libby': {
            'avatar_id': os.getenv('AVATAR_LIBBY_LOOK_ID', '').strip(),
            'voice_id': os.getenv('AVATAR_LIBBY_VOICE_ID', '').strip(),
        },
    }


def create_video_job(payload):
    binding = avatar_bindings().get(payload.get('avatar_key')) or {}
    if not binding.get('avatar_id') or not binding.get('voice_id'):
        raise RuntimeError('所选数字人尚未完成后台绑定。')
    request_body = {
        'type': 'avatar',
        'avatar_id': binding['avatar_id'],
        'voice_id': binding['voice_id'],
        'script': payload['script'],
        'title': (payload.get('title') or f"脚本 {payload.get('script_number', '')}")[:100],
        'aspect_ratio': '9:16',
        'resolution': '1080p',
        'engine': {'type': 'avatar_iii'},
    }
    response = requests.post(API_BASE + '/v3/videos', headers=_headers(True),
                             json=request_body, timeout=60)
    body = response.json()
    if not response.ok or body.get('error'):
        error = body.get('error') or {}
        code = error.get('code') if isinstance(error, dict) else ''
        message = error.get('message') if isinstance(error, dict) else str(error)
        raise RuntimeError(
            f'视频平台拒绝任务（HTTP {response.status_code}, code={code}, message={message}）'
        )
    data = body.get('data') or body
    video_id = data.get('video_id') or data.get('id')
    if not video_id:
        raise RuntimeError('视频平台没有返回任务编号。')
    return str(video_id)


def _post_callback(payload, status, **extra):
    callback = {
        'secret': os.getenv('AVATAR_VIDEO_SERVICE_SECRET', '').strip(),
        'record_id': payload.get('record_id', ''),
        'table_id': payload.get('table_id', ''),
        'chat_id': payload.get('chat_id', ''),
        'script_number': payload.get('script_number', ''),
        'status': status,
        **extra,
    }
    requests.post(payload['callback_url'], json=callback, timeout=30).raise_for_status()


def wait_and_callback(payload, job_id=None):
    """Poll one submitted job. Never resubmit or automatically retry it."""
    job_id = job_id or uuid.uuid4().hex
    status = 'failed'
    video_url = ''
    video_id = ''
    try:
        saved = _load_jobs().get(job_id, {})
        video_id = str(saved.get('video_id', '')).strip()
        if not video_id:
            _save_job(job_id, state='submitting', payload=payload)
            video_id = create_video_job(payload)
            _save_job(job_id, state='polling', video_id=video_id)
        deadline = time.time() + 1800
        started = time.time()
        next_progress = 60
        while time.time() < deadline:
            response = requests.get(API_BASE + '/v3/videos/' + video_id,
                                    headers=_headers(), timeout=30)
            response.raise_for_status()
            data = response.json().get('data') or response.json()
            remote_status = str(data.get('status', '')).lower()
            if remote_status == 'completed':
                video_url = str(data.get('video_url', '')).strip()
                if not video_url.startswith('https://'):
                    raise RuntimeError('视频已完成，但没有取得下载地址。')
                status = 'completed'
                break
            if remote_status in {'failed', 'error'}:
                break
            elapsed = int(time.time() - started)
            if elapsed >= next_progress:
                try:
                    _post_callback(payload, 'progress', elapsed_seconds=elapsed)
                except Exception as exc:
                    logger.warning('[avatar-video] progress notice failed: %s', exc)
                next_progress += 180
            time.sleep(10)
    except Exception as exc:
        logger.error('[avatar-video] generation failed: %s', exc)
        status = 'failed'

    extra = {'video_url': video_url, 'video_id': video_id} if video_url else {}
    try:
        _post_callback(payload, status, **extra)
        _save_job(job_id, state=status, finished_at=time.time())
    except Exception:
        _save_job(job_id, state='callback_pending', status=status, **extra)
        raise
