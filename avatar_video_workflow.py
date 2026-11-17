"""Feishu card workflow for explicitly-triggered avatar video jobs.

The bot owns selection and idempotency.  Rendering is delegated to one internal
service so a Feishu button click can never turn into an unbounded retry loop.
"""
import hashlib
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


class ScriptChangedError(RuntimeError):
    """The script text is missing, or differs from what the user reviewed and approved."""


def generation_script(fields):
    """The script for review and video is the 口播稿 column. 改写一/改写二 are optional extras."""
    return text_value((fields or {}).get('口播稿')).strip()


def script_digest(script):
    """Short fingerprint that ties a card to the exact text the user saw."""
    return hashlib.sha256((script or '').strip().encode('utf-8')).hexdigest()[:16]


_MARKDOWN_SAFE = str.maketrans({'<': '＜', '>': '＞', '`': 'ˋ', '*': '＊', '_': '＿',
                                '[': '［', ']': '］', '~': '～'})


FULL_ON_CARD = 800     # up to this many characters the whole script is shown on the card
PART_SIZE = 1500       # longer scripts are sent in full as plain-text messages of this size


def script_parts(script):
    """Split the script into consecutive pieces; ''.join(parts) == script.strip() exactly."""
    text = (script or '').strip()
    parts = []
    while text:
        if len(text) <= PART_SIZE:
            parts.append(text)
            break
        window = text[:PART_SIZE]
        cut = max(window.rfind('\n'), window.rfind('。'), window.rfind('！'), window.rfind('？'))
        cut = cut + 1 if cut >= PART_SIZE // 2 else PART_SIZE
        parts.append(text[:cut])
        text = text[cut:]
    return parts


def script_preview(script):
    """What the card says about the script. Short scripts are shown completely (markup
    characters swapped for look-alikes so they cannot render as formatting or @mentions).
    Long scripts are never cut: the card says the full text was sent as separate messages."""
    text = (script or '').strip()
    if len(text) <= FULL_ON_CARD:
        return text.translate(_MARKDOWN_SAFE)
    return (f'共 {len(text)} 字，太长，不在卡片内显示。全文已分 {len(script_parts(text))} 条消息'
            '发送在聊天记录里，请逐条核对完整文案。')


def _approval_path():
    return os.path.join(os.getenv('STATE_DIR', '/data'), 'approved_scripts.json')


_approval_lock = threading.Lock()


def approved_digest(record_id):
    """Digest of the 口播稿 a human approved for this record ('' if never approved)."""
    try:
        with open(_approval_path(), encoding='utf-8') as handle:
            return str(json.load(handle).get(record_id, ''))
    except (OSError, ValueError, AttributeError):
        return ''


def record_approval(record_id, digest):
    """Persist the approval BEFORE the table status changes. A later edit changes the
    digest, so the old approval silently stops applying."""
    with _approval_lock:
        try:
            with open(_approval_path(), encoding='utf-8') as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data[record_id] = digest
        os.makedirs(os.path.dirname(_approval_path()), exist_ok=True)
        temporary = _approval_path() + '.tmp'
        with open(temporary, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False)
        os.replace(temporary, _approval_path())


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
        raise ScriptChangedError(f'脚本 {number} 的“口播稿”是空的，任务没有提交。')
    return {
        'record_id': record['record_id'],
        'number': number,
        'title': text_value(fields.get('标题')).strip() or f'脚本 {number}',
        'script': script,
        'script_hash': script_digest(script),
        'status': text_value(fields.get('生成状态')).strip(),
    }


# ---------------------------------------------------------------------------
# Card layout. Every card in the workflow uses the same frame:
#   fixed header "口播脚本 · 编号 N"  ->  progress line  ->  step text  ->  buttons last.
# Only the look changed. Button `value` payloads, action names and the dedupe
# stages in feishu_bot are exactly what they were, so cards already sent keep working.
# ---------------------------------------------------------------------------
_STEP_NAMES = ('脚本库', '审核文案', '数字人', '确认生成')
_GLYPH = {'done': '✅', 'cur': '🔵', 'todo': '⚪', 'na': '➖', 'wait': '⏳', 'bad': '❌', 'stop': '⛔'}
# Marks for the four LEO steps: library, review, avatar, confirm.
_MARKS = {
    'pick': ('cur', 'todo', 'todo', 'todo'),
    'check': ('wait', 'todo', 'todo', 'todo'),
    'review': ('done', 'cur', 'todo', 'todo'),
    'redo': ('done', 'wait', 'todo', 'todo'),
    'approved': ('done', 'done', 'wait', 'todo'),
    'avatar': ('done', 'done', 'cur', 'todo'),
    'avatar_locked': ('done', 'done', 'done', 'wait'),
    'confirm': ('done', 'done', 'done', 'cur'),
    'validating': ('done', 'done', 'done', 'wait'),
    'rejected': ('done', 'done', 'done', 'bad'),
    'submitted': ('done', 'done', 'done', 'wait'),
    'cancelled': ('done', 'done', 'done', 'stop'),
    'notice': ('done', 'done', 'done', 'cur'),
}
# Which of the four steps each kind of card belongs to (for "第 N 步").
_STEP_OF = {'pick': 0, 'check': 0, 'review': 1, 'redo': 1, 'approved': 2, 'avatar': 2,
            'avatar_locked': 3, 'confirm': 3, 'validating': 3, 'rejected': 3,
            'submitted': 3, 'cancelled': 3, 'notice': 3}


def _is_leo(avatar_key):
    key = str(avatar_key or '')
    return key in ('', 'avatar_b') or key.startswith('leo_avatar_')


def _button(text, kind, value):
    return {'tag': 'button', 'text': {'tag': 'plain_text', 'content': text},
            'type': kind, 'value': value}


def _progress_and_step(kind, leo, blocked_step=0):
    if kind == 'blocked':
        marks = ['done'] * blocked_step + ['bad'] + ['todo'] * (3 - blocked_step)
        step = blocked_step
    else:
        marks = list(_MARKS[kind])
        step = _STEP_OF[kind]
    if not leo:
        # Libby / 陈总 have no review step and no avatar step in this bot.
        marks[1] = 'na'
        marks[2] = 'na'
    labels = list(_STEP_NAMES)
    if not leo:
        labels[1] = '审核（本库无此步）'
        labels[2] = '数字人（本库无此步）'
    parts = []
    for index, mark in enumerate(marks):
        text = f'{index + 1} {labels[index]}'
        parts.append(f'{_GLYPH[mark]} ' + (f'**{text}**' if mark in ('cur', 'wait', 'bad', 'stop') else text))
    return '　'.join(parts), step


def _workflow_card(number, kind, template, stage, main, *, avatar_key=None,
                   buttons=None, facts=None, warn='', note='', blocked_step=0, extra=''):
    leo = _is_leo(avatar_key)
    progress, step = _progress_and_step(kind, leo, blocked_step)
    total = 4
    position = step + 1
    if not leo:
        total = 2
        position = 1 if step == 0 else 2
    elements = [
        {'tag': 'markdown', 'content': progress},
        {'tag': 'hr'},
        {'tag': 'markdown', 'content': f"<font color='grey'>第 {position} 步 / 共 {total} 步 ｜ {stage}</font>"},
        {'tag': 'markdown', 'content': main},
    ]
    if extra:
        elements.append({'tag': 'markdown', 'content': extra})
    if facts:
        elements.append({'tag': 'markdown', 'content': '\n'.join(f'**{name}**：{value}' for name, value in facts)})
    if warn:
        elements.append({'tag': 'markdown', 'content': f'⚠️ {warn}'})
    if note:
        elements.append({'tag': 'note', 'elements': [{'tag': 'plain_text', 'content': note}]})
    if buttons:
        elements.append({'tag': 'hr'})
        elements.append({'tag': 'action', 'actions': buttons})
    return {
        'config': {'wide_screen_mode': True, 'update_multi': True},
        'header': {
            'template': template,
            'title': {'tag': 'plain_text', 'content': f'口播脚本 · 编号 {number}'},
        },
        'elements': elements,
    }


def avatar_card(number, choices=None, unavailable=None):
    choices = avatar_choices() if choices is None else choices
    buttons = [_button(choice['label'], 'default', {
        'action': 'select_avatar_script',
        'script_number': number,
        'avatar_key': choice['key'],
    }) for choice in choices]
    note = '找不到脚本会在你选择之后告诉你原因。'
    if len(choices) == 3:
        note = '三个脚本库都会显示；' + note
    if unavailable:
        note = '已自动隐藏不可用脚本库：' + '；'.join(unavailable)
    return _workflow_card(
        number, 'pick', 'blue', '选脚本库',
        f'请选择编号 **{number}** 属于哪一个脚本库。机器人只会从所选表格读取'
        '同编号的“口播稿”，不会混用其他表格。',
        buttons=buttons, note=note)


def leo_avatar_card(number, choices=None, unavailable=None, script_hash=''):
    choices = leo_avatar_choices() if choices is None else choices
    buttons = [_button(item['label'], 'default', {
        'action': 'select_leo_avatar',
        'script_number': number,
        'avatar_key': item['key'],
        **({'script_hash': script_hash} if script_hash else {}),
    }) for item in choices]
    note = '只会使用你选的这个形象，不会自动更换。'
    if unavailable:
        note = '已自动隐藏不可用数字人：' + '；'.join(unavailable)
    return _workflow_card(
        number, 'avatar', 'blue', '选数字人',
        '文案已确认。请选择本次视频使用的数字人形象。',
        avatar_key='avatar_b', buttons=buttons, note=note)


def rewrite_review_card(number, record_id, title='', script=''):
    """Ask a human to approve the 口播稿 shown here before any video step."""
    heading = (title or f'脚本 {number}').strip()
    return _workflow_card(
        number, 'review', 'orange', '人工审核文案',
        f'**{heading}**\n请核对下面的口播稿。视频将使用这份文案；要修改请先改飞书表格的「口播稿」，'
        '改完重新发送编号。',
        avatar_key='avatar_b',
        extra='**口播稿**\n' + script_preview(script),
        note='未经人工确认，不会提交 HeyGen，也不会产生任何费用。',
        buttons=[
            _button('确认文案，下一步', 'primary', {
                'action': 'approve_rewrite',
                'script_number': number,
                'record_id': record_id,
                'script_hash': script_digest(script),
            }),
        ])


def locked_rewrite_review_card(number, approved):
    """Replace a review card after either human decision; no buttons remain."""
    if approved:
        return _workflow_card(
            number, 'approved', 'blue', '文案已确认',
            '文案已确认，审核已锁定。正在准备数字人选项……',
            avatar_key='avatar_b', note='请不用重复点击。')
    return _workflow_card(
        number, 'redo', 'orange', '正在重新改写',
        '正在基于原始转写重新生成文案，完成后会发一张新的审核卡，请稍等。',
        avatar_key='avatar_b', note='本卡片已锁定，不用重复点击。')


def locked_script_card(number, avatar_key):
    """Replace the script-library picker after one option is selected."""
    label = avatar_label(avatar_key) or '所选脚本库'
    return _workflow_card(
        number, 'check', 'blue', f'已选择 {label}',
        f'已选择 {label}，脚本库选项已锁定。正在读取第 {number} 条脚本并做安全检查……',
        avatar_key=avatar_key, note='请不用重复点击。')


def locked_leo_avatar_card(number, avatar_key):
    """Replace the LEO presenter picker after one presenter is selected."""
    label = avatar_label(avatar_key) or '所选数字人'
    return _workflow_card(
        number, 'avatar_locked', 'blue', f'已选择 {label}',
        f'已选择 {label}，数字人选项已锁定。正在准备费用确认……',
        avatar_key='avatar_b', note='请不用重复点击。')


def confirmation_card(record, avatar_key):
    label = avatar_label(avatar_key)
    if not label:
        raise RuntimeError('数字人选择无效。')
    estimate = estimate_video_cost(record['script'])
    minutes, seconds = divmod(estimate['seconds'], 60)
    duration = f'{minutes}分{seconds:02d}秒' if minutes else f'{seconds}秒'
    if _is_leo(avatar_key):
        library, presenter = 'LEO脚本', label
    else:
        library = label
        presenter = (label[:-2] if label.endswith('脚本') else label) + ' 数字人'
    return _workflow_card(
        record['number'], 'confirm', 'orange', '确认生成',
        f'**{record["title"]}**',
        avatar_key=avatar_key,
        extra='**将用于生成视频的口播稿**\n' + script_preview(record['script']),
        facts=[('脚本库', library), ('数字人', presenter),
               ('预计时长', f'约 {duration}'), ('预计 API 成本', f'约 ${estimate["dollars"]:.2f}')],
        warn='点「确认生成」会向视频平台提交一次付费任务，确认后只提交一次，不会自动重跑。',
        note='实际费用按最终成片时长计算。',
        buttons=[
            _button('确认生成', 'primary', {
                'action': 'generate_avatar_video',
                'script_number': record['number'],
                'avatar_key': avatar_key,
                'script_hash': script_digest(record['script']),
            }),
            _button('取消生成', 'default', {
                'action': 'cancel_avatar_video',
                'script_number': record['number'],
                'avatar_key': avatar_key,
            }),
        ])


def submitted_card(number, avatar_key):
    """Replacement card returned after confirmation; intentionally has no controls."""
    return _workflow_card(
        number, 'submitted', 'green', '已提交，生成中',
        '**视频正在生成中。** 确认按钮已锁定，请勿重复提交。',
        avatar_key=avatar_key,
        note='完成后会通知本群，并把成片链接回填到原表格。')


def validating_card(number, avatar_key):
    """Temporary locked state while the provider is deciding whether to accept."""
    return _workflow_card(
        number, 'validating', 'orange', '正在校验',
        '**正在请求视频平台确认数字人和声音。** 目前尚未确认提交，请勿重复操作。',
        avatar_key=avatar_key, note='确认通过后才会显示「已提交」。')


def rejected_card(number, avatar_key, reason):
    """Locked safe failure state; never contains provider IDs or raw responses."""
    return _workflow_card(
        number, 'rejected', 'red', '未提交',
        f'**安全校验未通过，视频没有进入生成队列。**\n原因：{reason}',
        avatar_key=avatar_key, note='没有提交任何任务，不会扣费。')


def cancelled_card(number, avatar_key):
    """The clicked confirmation card after 取消生成; no controls, nothing was submitted."""
    return _workflow_card(
        number, 'cancelled', 'grey', '已取消',
        f'已取消脚本 **{number}**。任务没有提交，**不会扣费**。',
        avatar_key=avatar_key, note='需要的话，重新发送编号即可再来一遍。')


def notice_card(number, avatar_key, text):
    """Plain status for a script that is already generating or generated (duplicate guard)."""
    return _workflow_card(
        number, 'notice', 'blue', '没有重复提交', text,
        avatar_key=avatar_key, note='同一条脚本不会被提交两次。')


def blocked_card(number, avatar_key, reason, step=0):
    """The flow cannot continue. Nothing was submitted; `reason` is the safe error text."""
    return _workflow_card(
        number, 'blocked', 'red', '无法继续',
        f'**无法进入下一步。**\n{reason}',
        avatar_key=avatar_key, blocked_step=step,
        note='没有提交任何任务，不会扣费。请重新发送编号再试。')


def parse_card_action(payload):
    event = payload.get('event') or payload
    action = event.get('action') or {}
    value = action.get('value') or {}
    action_type = value.get('action')
    if action_type not in {
        'approve_rewrite', 'redo_rewrite',
        'select_avatar_script', 'select_leo_avatar',
        'generate_avatar_video', 'cancel_avatar_video'
    }:
        return None
    context = event.get('context') or {}
    return {
        'action': action_type,
        'script_number': int(value.get('script_number') or 0),
        'record_id': str(value.get('record_id', '')).strip(),
        'avatar_key': str(value.get('avatar_key', '')).strip(),
        'script_hash': str(value.get('script_hash', '')).strip(),
        'chat_id': (context.get('open_chat_id') or event.get('open_chat_id') or
                    payload.get('open_chat_id') or ''),
        'message_id': (context.get('open_message_id') or
                       context.get('message_id') or
                       event.get('open_message_id') or
                       event.get('message_id') or
                       payload.get('open_message_id') or
                       payload.get('message_id') or ''),
        'event_id': str((payload.get('header') or {}).get('event_id') or ''),
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

    # The final safety gate is deliberately repeated immediately before any
    # table state changes or paid provider submission.
    from heygen_video_service import validate_avatar_binding
    validate_avatar_binding(action['avatar_key'])

    with _lock_for(record_id):
        record = _fresh_record(client, record_id)
        fields = record.get('fields', {})
        status = text_value(fields.get('生成状态')).strip()
        existing_url = fields.get('HeyGen 成片链接')
        if status in {'生成中', '已生成'} or existing_url:
            return {'duplicate': True, 'status': status or '已生成'}
        script = generation_script(fields)
        if not script:
            raise ScriptChangedError('这条记录的“口播稿”为空，任务没有提交。')
        expected = (action.get('script_hash') or '').strip()
        if not expected:
            raise ScriptChangedError('这张确认卡版本过旧，缺少文案校验，任务没有提交。请重新发送编号。')
        if expected != script_digest(script):
            raise ScriptChangedError('口播稿在你确认之后被修改过，任务没有提交。请重新发送编号确认最新文案。')
        if action['avatar_key'].startswith('leo_avatar_') and status not in {
                '文案已确认', '失败'}:
            raise RuntimeError('文案尚未人工确认，任务没有提交。')
        if action['avatar_key'].startswith('leo_avatar_') and \
                approved_digest(record_id) != script_digest(script):
            raise ScriptChangedError('这份口播稿的当前版本还没有被人工确认（可能确认后被修改），'
                                     '任务没有提交。请重新发送编号审核。')

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
                timeout=90,
            )
            try:
                body = response.json()
            except ValueError:
                body = {}
            if not response.ok or not body.get('job_id'):
                detail = str(body.get('detail', '')).strip()
                raise RuntimeError(detail or '生成服务没有接受任务，未进入生成队列。')
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
