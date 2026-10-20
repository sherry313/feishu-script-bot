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
_inventory_lock = threading.Lock()
_inventory_cache = {'expires_at': 0, 'avatar_ids': set(), 'voice_ids': set()}


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


def accept_job(payload):
    """Persist, validate, and obtain a provider video ID before acknowledging."""
    job_id = register_job(payload)
    try:
        _save_job(job_id, state='submitting')
        video_id = create_video_job(payload)
        _save_job(job_id, state='polling', video_id=video_id)
        return job_id
    except Exception as exc:
        _save_job(job_id, state='failed', error=str(exc), finished_at=time.time())
        raise


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
    bindings = {
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
    for number in (1, 2, 3):
        bindings[f'leo_avatar_{number}'] = {
            'avatar_id': os.getenv(f'LEO_AVATAR_{number}_LOOK_ID', '').strip(),
            'voice_id': os.getenv('AVATAR_B_VOICE_ID', '').strip(),
        }
    return bindings


def _listed_ids(path, collection_name, id_name):
    response = requests.get(API_BASE + path, headers=_headers(), timeout=20)
    response.raise_for_status()
    body = response.json()
    if body.get('error'):
        raise RuntimeError('视频平台资源校验失败。')
    items = (body.get('data') or {}).get(collection_name) or []
    return {str(item.get(id_name, '')).strip() for item in items
            if isinstance(item, dict) and item.get(id_name)}


def provider_inventory(force=False):
    """Fetch API-visible avatars and voices. Failure closes the safety gate."""
    now = time.time()
    with _inventory_lock:
        if not force and _inventory_cache['expires_at'] > now:
            return {
                'avatar_ids': set(_inventory_cache['avatar_ids']),
                'voice_ids': set(_inventory_cache['voice_ids']),
            }
        avatar_ids = _listed_ids('/v2/avatars', 'avatars', 'avatar_id')
        voice_ids = _listed_ids('/v2/voices', 'voices', 'voice_id')
        if not avatar_ids or not voice_ids:
            raise RuntimeError('视频平台没有返回可用的数字人或声音。')
        _inventory_cache.update({
            'expires_at': now + 300,
            'avatar_ids': set(avatar_ids),
            'voice_ids': set(voice_ids),
        })
        return {'avatar_ids': avatar_ids, 'voice_ids': voice_ids}


def validate_avatar_binding(avatar_key, force=False):
    """Fail closed unless both configured resources are visible to this API key."""
    binding = avatar_bindings().get(avatar_key) or {}
    avatar_id = binding.get('avatar_id', '')
    voice_id = binding.get('voice_id', '')
    if not avatar_id or not voice_id:
        raise RuntimeError('所选数字人尚未完成后台绑定。')
    inventory = provider_inventory(force=force)
    if avatar_id not in inventory['avatar_ids']:
        raise RuntimeError('所选数字人当前不可用，已禁止提交。')
    if voice_id not in inventory['voice_ids']:
        raise RuntimeError('所选声音当前不可用，已禁止提交。')
    return True


def create_video_job(payload):
    validate_avatar_binding(payload.get('avatar_key'), force=True)
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


def _safe_failure_reason(error):
    """Translate provider details without leaking private resource identifiers."""
    text = str(error or '').lower()
    if 'avatar_not_found' in text or 'avatar not found' in text:
        return '数字人已失效或 API 账号无权使用。'
    if 'voice' in text and ('not_found' in text or 'not found' in text or 'invalid' in text):
        return '声音已失效或 API 账号无权使用。'
    if 'credit' in text or 'quota' in text or 'balance' in text:
        return '视频平台额度不足，任务已停止。'
    if 'timeout' in text or 'timed out' in text:
        return '视频平台连接超时，系统没有自动重复提交。'
    return '视频平台处理失败，系统没有自动重试。'


def wait_and_callback(payload, job_id=None):
    """Poll one submitted job. Never resubmit or automatically retry it."""
    job_id = job_id or uuid.uuid4().hex
    status = 'failed'
    video_url = ''
    video_id = ''
    failure_reason = ''
    try:
        saved = _load_jobs().get(job_id, {})
        if saved.get('state') == 'callback_pending':
            retry_status = saved.get('status') or 'failed'
            retry_extra = {key: saved[key] for key in
                           ('video_url', 'video_id', 'failure_reason') if saved.get(key)}
            _post_callback(payload, retry_status, **retry_extra)
            _save_job(job_id, state=retry_status, finished_at=time.time())
            return
        video_id = str(saved.get('video_id', '')).strip()
        if not video_id:
            _save_job(job_id, state='submitting', payload=payload)
            video_id = create_video_job(payload)
            _save_job(job_id, state='polling', video_id=video_id)
        deadline = time.time() + 7200
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
                failure_reason = _safe_failure_reason(
                    data.get('error') or data.get('message') or remote_status
                )
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
        failure_reason = _safe_failure_reason(exc)

    extra = {'video_url': video_url, 'video_id': video_id} if video_url else {}
    if status == 'failed':
        extra['failure_reason'] = failure_reason or '视频平台未完成任务。'
    try:
        _post_callback(payload, status, **extra)
        _save_job(job_id, state=status, finished_at=time.time())
    except Exception:
        _save_job(job_id, state='callback_pending', status=status, **extra)
        raise
