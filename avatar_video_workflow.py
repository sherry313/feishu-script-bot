"""Feishu card workflow for explicitly-triggered avatar video jobs.

The bot owns selection and idempotency.  Rendering is delegated to one internal
service so a Feishu button click can never turn into an unbounded retry loop.
"""
import json
import os
import re
import threading
from urllib.parse import quote

import requests

from feishu_sync import text_value


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


def parse_plain_script_number(text):
    """Allow a bare positive integer as the shortest group command."""
    normalized = command_without_mention(text)
    if not re.fullmatch(r'\d+', normalized):
        return None
    number = int(normalized)
    return number if number > 0 else None


def avatar_choices():
    """Public button labels and opaque keys; provider IDs never enter cards."""
    raw = os.getenv('AVATAR_CHOICES_JSON', '').strip()
    if not raw:
        return [
            {'key': 'avatar_b', 'label': 'LEO脚本'},
            {'key': 'avatar_libby', 'label': 'Libby脚本'},
            {'key': 'avatar_a', 'label': '陈总脚本'},
        ]
    try:
        choices = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError('AVATAR_CHOICES_JSON 不是有效 JSON。') from exc
    if not isinstance(choices, list) or len(choices) != 3:
        raise RuntimeError('必须配置三个数字人选项。')
    cleaned = []
    for item in choices:
        key = str(item.get('key', '')).strip() if isinstance(item, dict) else ''
        label = str(item.get('label', '')).strip() if isinstance(item, dict) else ''
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', key) or not label:
            raise RuntimeError('数字人选项的 key 或 label 配置不正确。')
        cleaned.append({'key': key, 'label': label[:30]})
    if len({item['key'] for item in cleaned}) != 3:
        raise RuntimeError('三个数字人的 key 不能相同。')
    return cleaned


def leo_avatar_choices():
    """Public LEO presenter labels. Private look IDs stay in service configuration."""
    return [
        {'key': 'leo_avatar_1', 'label': '数字人 1'},
        {'key': 'leo_avatar_2', 'label': '数字人 2'},
        {'key': 'leo_avatar_3', 'label': '数字人 3'},
    ]


def avatar_label(avatar_key):
    choices = avatar_choices() + leo_avatar_choices()
    return next((item['label'] for item in choices if item['key'] == avatar_key), '')


def avatar_source(avatar_key):
    """Map a public avatar choice to its private script table and optional view."""
    settings = {
        'avatar_b': (
            os.getenv('LEO_TABLE_ID') or os.getenv('FEISHU_TABLE_ID', ''),
            os.getenv('LEO_VIEW_ID') or os.getenv('FEISHU_VIEW_ID', 'vewhIlXf4y'),
            '',
        ),
        'avatar_a': (
            os.getenv('CHEN_TABLE_ID', ''), os.getenv('CHEN_VIEW_ID', ''), ''
        ),
        'avatar_libby': (
            os.getenv('LIBBY_TABLE_ID', ''), os.getenv('LIBBY_VIEW_ID', ''), ''
        ),
    }
    leo_source = settings['avatar_b']
    for item in leo_avatar_choices():
        settings[item['key']] = leo_source
    table_id, view_id, library = settings.get(avatar_key, ('', '', ''))
    if not table_id:
        label = next((item['label'] for item in avatar_choices()
                      if item['key'] == avatar_key), avatar_key)
        raise RuntimeError(f'{label}脚本表尚未完成后台绑定。')
    return {
        'table_id': table_id.strip(),
        'view_id': view_id.strip(),
        'library': library,
    }


def records_in_view(client, view_id=''):
    params = {'page_size': 100}
    if view_id:
        params['view_id'] = view_id
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
    return {'seconds': seconds, 'dollars': seconds * 0.0167}


def script_record(client, number, view_id='', library=''):
    rows = records_in_view(client, view_id)
    if library:
        rows = [row for row in rows
                if text_value(row.get('fields', {}).get('脚本库')).strip() == library]
    if number > len(rows):
        label = f'{library}脚本库' if library else '当前视图'
        raise RuntimeError(f'{label}只有 {len(rows)} 条，找不到脚本 {number}。')
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


def avatar_card(number):
    buttons = []
    for choice in avatar_choices():
        buttons.append({
            'tag': 'button',
            'text': {'tag': 'plain_text', 'content': choice['label']},
            'type': 'primary',
            'value': {
                'action': 'select_avatar_script',
                'script_number': number,
                'avatar_key': choice['key'],
            },
        })
    return {
        'config': {'wide_screen_mode': True},
        'header': {
            'template': 'blue',
            'title': {'tag': 'plain_text', 'content': f'编号 {number}｜选择脚本库'},
        },
        'elements': [
            {'tag': 'markdown', 'content': (
                f'请选择编号 **{number}** 属于哪一个脚本库。机器人只会从所选表格读取'
                '同编号的“改写一”，确认文案和预计成本后再生成，不会混用其他表格。'
            )},
            {'tag': 'action', 'actions': buttons},
        ],
    }


def leo_avatar_card(number):
    buttons = [{
        'tag': 'button',
        'text': {'tag': 'plain_text', 'content': item['label']},
        'type': 'primary',
        'value': {
            'action': 'select_leo_avatar',
            'script_number': number,
            'avatar_key': item['key'],
        },
    } for item in leo_avatar_choices()]
    return {
        'config': {'wide_screen_mode': True},
        'header': {
            'template': 'blue',
            'title': {'tag': 'plain_text', 'content': f'LEO脚本 {number}｜选择数字人'},
        },
        'elements': [
            {'tag': 'markdown', 'content': '请选择本次视频使用的数字人形象。'},
            {'tag': 'action', 'actions': buttons},
        ],
    }


def locked_script_card(number, avatar_key):
    """Replace the script-library picker after one option is selected."""
    label = avatar_label(avatar_key) or '所选脚本库'
    next_step = ('下一步请选择数字人形象。' if avatar_key == 'avatar_b'
                 else '下一步请确认文案和预计成本。')
    return {
        'config': {'wide_screen_mode': True, 'update_multi': True},
        'header': {
            'template': 'green',
            'title': {'tag': 'plain_text', 'content': f'编号 {number}｜已选择 {label}'},
        },
        'elements': [
            {'tag': 'markdown', 'content': (
                f'已选择 **{label}**，脚本库选项已锁定，不能重复选择。\n\n{next_step}'
            )},
        ],
    }


def locked_leo_avatar_card(number, avatar_key):
    """Replace the LEO presenter picker after one presenter is selected."""
    label = avatar_label(avatar_key) or '所选数字人'
    return {
        'config': {'wide_screen_mode': True, 'update_multi': True},
        'header': {
            'template': 'green',
            'title': {'tag': 'plain_text', 'content': f'LEO脚本 {number}｜已选择 {label}'},
        },
        'elements': [
            {'tag': 'markdown', 'content': (
                f'已选择 **{label}**，数字人选项已锁定，不能重复选择。\n\n'
                '下一步请确认文案和预计成本。'
            )},
        ],
    }


def confirmation_card(record, avatar_key):
    label = avatar_label(avatar_key)
    if not label:
        raise RuntimeError('数字人选择无效。')
    estimate = estimate_video_cost(record['script'])
    minutes, seconds = divmod(estimate['seconds'], 60)
    duration = f'{minutes}分{seconds:02d}秒' if minutes else f'{seconds}秒'
    return {
        'config': {'wide_screen_mode': True},
        'header': {
            'template': 'orange',
            'title': {'tag': 'plain_text', 'content': (
                f'{label}｜脚本 {record["number"]}｜确认生成'
            )},
        },
        'elements': [
            {'tag': 'markdown', 'content': (
                f'**{record["title"]}**\n\n'
                f'预计时长：**约 {duration}**\n'
                f'预计 API 成本：**约 ${estimate["dollars"]:.2f}**\n\n'
                '实际费用按最终成片时长计算。确认后只提交一次，不会自动重跑。'
            )},
            {'tag': 'action', 'actions': [
                {
                    'tag': 'button',
                    'text': {'tag': 'plain_text', 'content': '确认生成'},
                    'type': 'primary',
                    'value': {
                        'action': 'generate_avatar_video',
                        'script_number': record['number'],
                        'avatar_key': avatar_key,
                    },
                },
                {
                    'tag': 'button',
                    'text': {'tag': 'plain_text', 'content': '取消生成'},
                    'type': 'default',
                    'value': {
                        'action': 'cancel_avatar_video',
                        'script_number': record['number'],
                        'avatar_key': avatar_key,
                    },
                },
            ]},
        ],
    }


def submitted_card(number, avatar_key):
    """Replacement card returned after confirmation; intentionally has no controls."""
    label = avatar_label(avatar_key) or '所选脚本'
    return {
        'config': {'wide_screen_mode': True, 'update_multi': True},
        'header': {
            'template': 'green',
            'title': {'tag': 'plain_text', 'content': f'{label}｜脚本 {number}｜已提交'},
        },
        'elements': [
            {'tag': 'markdown', 'content': (
                '**视频正在生成中。**\n\n'
                '确认按钮已锁定，请勿重复提交；完成后会自动回填原表格并通知群聊。'
            )},
        ],
    }


def parse_card_action(payload):
    event = payload.get('event') or payload
    action = event.get('action') or {}
    value = action.get('value') or {}
    action_type = value.get('action')
    if action_type not in {
        'select_avatar_script', 'select_leo_avatar',
        'generate_avatar_video', 'cancel_avatar_video'
    }:
        return None
    context = event.get('context') or {}
    return {
        'action': action_type,
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
    source = avatar_source(action['avatar_key'])
    if client.table_id != source['table_id']:
        raise RuntimeError('脚本表与所选数字人不匹配，任务没有提交。')
    record = script_record(client, action['script_number'], source['view_id'],
                           source['library'])
    record_id = record['record_id']
    label = avatar_label(action['avatar_key'])
    if not record_id or not label:
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
                    'table_id': client.table_id,
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
                    'avatar_label': label}
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
        video_id = str(body.get('video_id', '')).strip()
        number = int(body.get('script_number') or 0)
        if re.fullmatch(r'[a-zA-Z0-9_-]{8,80}', video_id) and number > 0:
            base = os.getenv('PUBLIC_BASE_URL', '').rstrip('/')
            url = f'{base}/avatar/videos/{video_id}/download?name={quote(f"脚本{number}")}'
        fields['HeyGen 成片链接'] = {
            'link': url,
            'text': f'脚本{number}' if number > 0 else '查看成片',
        }
    client.request('PUT', client.base + '/records/' + record_id,
                   json={'fields': fields})
    return fields
