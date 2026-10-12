"""Feishu card workflow for explicitly-triggered avatar video jobs.

The bot owns selection and idempotency.  Rendering is delegated to one internal
service so a Feishu button click can never turn into an unbounded retry loop.
"""
import json
import os
import re
import threading

import requests

from feishu_sync import text_value


VIEW_ID = os.getenv('FEISHU_VIEW_ID', 'vewhIlXf4y').strip()
_record_locks = {}
_record_locks_guard = threading.Lock()


def command_without_mention(text):
    text = re.sub(r'<at\b[^>]*>.*?</at>', '', text or '',
                  flags=re.IGNORECASE | re.DOTALL)
    # Feishu group webhooks may replace the mention with an opaque placeholder
    # such as "@_user_1" instead of an HTML <at> tag.
    text = re.sub(r'@_user_\d+', '', text, flags=re.IGNORECASE)
    return re.sub(r'\s+', '', text).strip()


def parse_script_number(text):
    """Return the positive number in exactly `跑脚本27`, otherwise None."""
    match = re.fullmatch(r'跑脚本(\d+)', command_without_mention(text))
    if not match:
        return None
    number = int(match.group(1))
    return number if number > 0 else None


def avatar_choices():
    """Public button labels and opaque keys; provider IDs never enter cards."""
    raw = os.getenv('AVATAR_CHOICES_JSON', '').strip()
    if not raw:
        return [
            {'key': 'avatar_a', 'label': '数字人 A'},
            {'key': 'avatar_b', 'label': '数字人 B'},
        ]
    try:
        choices = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError('AVATAR_CHOICES_JSON 不是有效 JSON。') from exc
    if not isinstance(choices, list) or len(choices) != 2:
        raise RuntimeError('必须配置两个数字人选项。')
    cleaned = []
    for item in choices:
        key = str(item.get('key', '')).strip() if isinstance(item, dict) else ''
        label = str(item.get('label', '')).strip() if isinstance(item, dict) else ''
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', key) or not label:
            raise RuntimeError('数字人选项的 key 或 label 配置不正确。')
        cleaned.append({'key': key, 'label': label[:30]})
    if cleaned[0]['key'] == cleaned[1]['key']:
        raise RuntimeError('两个数字人的 key 不能相同。')
    return cleaned


def records_in_view(client):
    params = {'page_size': 100, 'view_id': VIEW_ID}
    records = []
    while True:
        data = client.request('GET', client.base + '/records', params=params)['data']
        records.extend(data.get('items', []))
        if not data.get('has_more'):
            return records
        token = data.get('page_token')
        if not token or token == params.get('page_token'):
            raise RuntimeError('飞书分页令牌异常。')
        params['page_token'] = token


def generation_script(fields):
    """Video commands always use the finalized first rewrite, never the source script."""
    return text_value((fields or {}).get('改写一')).strip()


def estimate_video_cost(script):
    """Estimate narration duration and API cost before a user submits."""
    chars = len(re.sub(r'\s+', '', script or ''))
    seconds = max(1, round(chars / 4))
    return {'seconds': seconds, 'dollars': seconds * 0.0667}


def script_record(client, number):
    rows = records_in_view(client)
    if number > len(rows):
        raise RuntimeError(f'当前视图只有 {len(rows)} 条，找不到脚本 {number}。')
    record = rows[number - 1]
    fields = record.get('fields', {})
    script = generation_script(fields)
    if not script:
        raise RuntimeError(f'脚本 {number} 的“改写一”是空的，任务没有提交。')
    return {
        'record_id': record['record_id'],
        'number': number,
        'title': text_value(fields.get('标题')).strip() or f'脚本 {number}',
        'script': script,
        'status': text_value(fields.get('生成状态')).strip(),
    }


def avatar_card(record):
    estimate = estimate_video_cost(record['script'])
    minutes, seconds = divmod(estimate['seconds'], 60)
    duration = f'{minutes}分{seconds:02d}秒' if minutes else f'{seconds}秒'
    buttons = []
    for choice in avatar_choices():
        buttons.append({
            'tag': 'button',
            'text': {'tag': 'plain_text', 'content': choice['label']},
            'type': 'primary',
            'value': {
                'action': 'generate_avatar_video',
                'record_id': record['record_id'],
                'script_number': record['number'],
                'avatar_key': choice['key'],
            },
        })
    return {
        'config': {'wide_screen_mode': True},
        'header': {
            'template': 'blue',
            'title': {'tag': 'plain_text', 'content': f'脚本 {record["number"]}｜选择数字人'},
        },
        'elements': [
            {'tag': 'markdown', 'content': (
                f'**{record["title"]}**\n\n'
                f'预计时长：**约 {duration}**\n'
                f'预计 API 成本：**约 ${estimate["dollars"]:.2f}**\n\n'
                '实际费用按最终成片时长计算。选定后只提交一次，不会自动重跑。'
            )},
            {'tag': 'action', 'actions': buttons},
        ],
    }


def parse_card_action(payload):
    event = payload.get('event') or payload
    action = event.get('action') or {}
    value = action.get('value') or {}
    if value.get('action') != 'generate_avatar_video':
        return None
    context = event.get('context') or {}
    return {
        'record_id': str(value.get('record_id', '')).strip(),
        'script_number': int(value.get('script_number') or 0),
        'avatar_key': str(value.get('avatar_key', '')).strip(),
        'chat_id': (context.get('open_chat_id') or event.get('open_chat_id') or
                    payload.get('open_chat_id') or ''),
    }


def _lock_for(record_id):
    with _record_locks_guard:
        return _record_locks.setdefault(record_id, threading.Lock())


def _fresh_record(client, record_id):
    body = client.request('GET', client.base + '/records/' + record_id)
    return body['data']['record']


def submit_video_job(client, action):
    """Atomically-enough guard a single instance, then submit exactly once."""
    record_id = action['record_id']
    choices = {item['key']: item['label'] for item in avatar_choices()}
    if not record_id or action['avatar_key'] not in choices:
        raise RuntimeError('数字人选择无效，请重新发送“跑脚本+编号”。')
    service_url = os.getenv('AVATAR_VIDEO_SERVICE_URL', '').strip()
    service_secret = os.getenv('AVATAR_VIDEO_SERVICE_SECRET', '').strip()
    if not service_url or not service_secret:
        raise RuntimeError('视频生成服务尚未连接，任务没有提交。')

    with _lock_for(record_id):
        record = _fresh_record(client, record_id)
        fields = record.get('fields', {})
        status = text_value(fields.get('生成状态')).strip()
        existing_url = fields.get('HeyGen 成片链接')
        if status in {'生成中', '已生成'} or existing_url:
            return {'duplicate': True, 'status': status or '已生成'}
        script = generation_script(fields)
        if not script:
            raise RuntimeError('这条记录的“改写一”为空，任务没有提交。')

        # Mark first.  There is deliberately no automatic retry if the remote
        # result is ambiguous; a human may explicitly submit again after failure.
        client.request('PUT', client.base + '/records/' + record_id, json={'fields': {
            '生成状态': '生成中',
        }})
        try:
            response = requests.post(
                service_url.rstrip('/') + '/jobs',
                headers={'Authorization': f'Bearer {service_secret}'},
                json={
                    'record_id': record_id,
                    'script_number': action['script_number'],
                    'title': text_value(fields.get('标题')).strip(),
                    'script': script,
                    'avatar_key': action['avatar_key'],
                    'chat_id': action.get('chat_id', ''),
                    'callback_url': os.getenv('PUBLIC_BASE_URL', '').rstrip('/') + '/avatar/jobs/callback',
                },
                timeout=30,
            )
            body = response.json()
            if not response.ok or not body.get('job_id'):
                raise RuntimeError('生成服务没有接受任务。')
            return {'duplicate': False, 'job_id': str(body['job_id']),
                    'avatar_label': choices[action['avatar_key']]}
        except Exception:
            client.request('PUT', client.base + '/records/' + record_id,
                           json={'fields': {'生成状态': '失败'}})
            raise


def apply_job_callback(client, body):
    expected = os.getenv('AVATAR_VIDEO_SERVICE_SECRET', '').strip()
    if not expected or body.get('secret') != expected:
        raise PermissionError('invalid callback secret')
    record_id = str(body.get('record_id', '')).strip()
    status = str(body.get('status', '')).strip().lower()
    if not record_id or status not in {'completed', 'failed'}:
        raise RuntimeError('回调内容不完整。')
    fields = {'生成状态': '已生成' if status == 'completed' else '失败'}
    if status == 'completed':
        url = str(body.get('video_url', '')).strip()
        if not url.startswith('https://'):
            raise RuntimeError('完成回调缺少有效视频地址。')
        fields['HeyGen 成片链接'] = {'link': url, 'text': '查看成片'}
    client.request('PUT', client.base + '/records/' + record_id,
                   json={'fields': fields})
    return fields
