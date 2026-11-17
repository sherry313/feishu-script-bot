"""Poll the three script tables and process newly created or edited link rows."""
import hashlib
import json
import os
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

from feishu_link_intake import record_video_url
from feishu_sync import Feishu, text_value


OUTPUTS = ['改写一', '改写二', '改写标题', '标签', '简介']
STATE_DIR = Path(os.getenv('STATE_DIR', Path(__file__).parent))
STATE_EPOCH = os.getenv('TABLE_WATCH_EPOCH', '1').strip() or '1'
STATE_PATH = STATE_DIR / f'table_watch_seen_{STATE_EPOCH}.json'


def configured_tables():
    values = [
        ('LEO', os.getenv('LEO_TABLE_ID') or os.getenv('FEISHU_TABLE_ID', '')),
        ('Libby', os.getenv('LIBBY_TABLE_ID', '')),
        ('Chen', os.getenv('CHEN_TABLE_ID', '')),
    ]
    result = []
    seen = set()
    for label, table_id in values:
        table_id = table_id.strip()
        if table_id and table_id not in seen:
            result.append((label, table_id))
            seen.add(table_id)
    return result


def _load():
    try:
        return json.loads(STATE_PATH.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp = STATE_PATH.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(STATE_PATH)


QUEUE_PATH = STATE_DIR / f'table_watch_queue_{STATE_EPOCH}.json'
MAX_ATTEMPTS = 3
RETRY_DELAYS = (300, 1800)        # seconds to wait after attempt 1 and attempt 2
STALE_RUNNING_SECONDS = 1800      # a 'running' entry this old means the container restarted
UNSAFE_TO_RETRY = ('未知',)       # outcome unknown: never retry automatically
_log = logging.getLogger('uvicorn.error')


def _now():
    return time.time()


def _qload():
    try:
        return json.loads(QUEUE_PATH.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _qsave(queue):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp = QUEUE_PATH.with_suffix('.tmp')
    temp.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(QUEUE_PATH)


def _text_since_ms(queue):
    """Records created before this moment are never picked up automatically.

    Defaults to the first time this code ran, so existing history is not bulk
    processed. TEXT_REWRITE_SINCE_MS can move it earlier for an approved recovery.
    """
    meta = queue.setdefault('_meta', {})
    override = os.getenv('TEXT_REWRITE_SINCE_MS', '').strip()
    if override.isdigit():
        return int(override)
    if 'text_since_ms' not in meta:
        meta['text_since_ms'] = int(_now() * 1000)
    return int(meta['text_since_ms'])


def _source_text(fields):
    return text_value(fields.get('口播稿')).strip()


def fingerprint(record):
    fields = record.get('fields', {})
    raw = fields.get('视频链接')
    return hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True,
                                     default=str).encode('utf-8')).hexdigest()


def scan_once(process_existing=False):
    """Find new work and put it in the persistent queue; return what was newly queued."""
    state = _load()
    queue = _qload()
    since_ms = _text_since_ms(queue)
    first_run = not bool(state)
    candidates = []
    for label, table_id in configured_tables():
        try:
            client = Feishu(table_id=table_id)
            rows = list(client.pages('/records'))
        except Exception as exc:
            _log.warning('Skipping inaccessible script table %s: %s', label, exc)
            continue
        for number, row in enumerate(rows, 1):
            rid = row['record_id']
            key = table_id + ':' + rid
            mark = fingerprint(row)
            previous = state.get(key)
            state[key] = mark
            fields = row.get('fields', {})
            url = record_video_url(fields.get('视频链接'))
            complete = all(text_value(fields.get(name)).strip() for name in OUTPUTS)
            if complete:
                continue
            item = {'label': label, 'table_id': table_id, 'record_id': rid,
                    'number': number, 'url': url}
            if url:
                if (first_run and not process_existing) or previous == mark:
                    continue
                existing = queue.get(key)
                corrected = (existing and existing.get('status') == 'gave_up'
                             and (existing.get('url') or '').rstrip('/') != url.rstrip('/'))
                if existing and not corrected and existing.get('status') in {
                        'pending', 'running', 'retry', 'gave_up'}:
                    continue  # already owned by the queue; keep its attempt count
                item.update(kind='link', status='pending', attempts=0, mark=mark)
                queue[key] = item
                candidates.append(item)
                continue
            # No video link: a record that already has its own 口播稿 but no 改写一.
            if text_value(fields.get('改写一')).strip():
                continue
            source = _source_text(fields)
            if not source:
                continue
            if text_value(fields.get('原视频内容')).strip():
                continue  # two possible sources: never guess, never auto-rewrite
            created = row.get('created_time')
            if not isinstance(created, (int, float)) or isinstance(created, bool):
                warned = queue['_meta'].setdefault('warned_no_created_time', [])
                if key not in warned:
                    warned.append(key)
                    _log.warning('飞书没有返回有效的创建时间，已跳过自动改写（不会当作历史记录处理）：'
                                 '%s 第%s行 record=%s', label, number, rid)
                continue
            if created < since_ms:
                continue
            digest = hashlib.sha256(source.encode('utf-8')).hexdigest()
            entry = queue.get(key)
            if entry is None or (entry.get('kind') == 'text' and entry.get('status') == 'seen'
                                 and entry.get('source') != digest):
                # First sighting, or still being typed: wait until the text stops changing.
                queue[key] = dict(item, kind='text', status='seen', attempts=0, source=digest)
            elif entry.get('kind') == 'text' and entry.get('status') == 'seen':
                entry.update(status='pending')
                candidates.append(entry)
    _qsave(queue)
    _save(state)
    return candidates


def process_candidate(item):
    client = Feishu(table_id=item['table_id'])
    record = client.request('GET', client.base + '/records/' + item['record_id'])['data']['record']
    fields = record.get('fields', {})
    # Never touch a record a human already wrote or approved.
    if item.get('kind') == 'text' and (text_value(fields.get('改写一')).strip()
                                       or text_value(fields.get('原视频内容')).strip()):
        return
    if text_value(fields.get('生成状态')).strip() in {'文案已确认', '生成中', '已生成'}:
        return
    url = record_video_url(fields.get('视频链接'))
    if item.get('kind') == 'text':
        if url:
            return  # a link appeared meanwhile; the link flow owns this record now
        command = [sys.executable, '-u', 'run_feishu_one.py',
                   '--record', item['record_id'], '--table', item['table_id']]
    else:
        if not url:
            return
        patch = {'视频链接': {'link': url, 'text': url}}
        if not text_value(fields.get('标题')).strip():
            patch['标题'] = f'{item["label"]}脚本{item["number"]}'
        client.request('PUT', client.base + '/records/' + item['record_id'],
                       json={'fields': patch})
        command = [sys.executable, '-u', 'run_feishu_pipeline.py',
                   '--record', item['record_id'], '--table', item['table_id']]
    result = subprocess.run(command, cwd=Path(__file__).parent, text=True,
                            encoding='utf-8', capture_output=True, timeout=900)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[-500:] or '表格自动整理失败')


def reconcile_interrupted(now=None):
    """A link job left 'running' by a restart may have a paid request in flight.

    Its outcome is unknown, so it is parked for a human instead of being re-run.
    """
    now = _now() if now is None else now
    queue = _qload()
    changed = False
    for key, entry in queue.items():
        if key == '_meta' or entry.get('kind') == 'text' or entry.get('status') != 'running':
            continue
        if now - entry.get('started_at', now) < STALE_RUNNING_SECONDS:
            continue
        entry.update(status='gave_up',
                     last_error='服务重启时该转写任务状态未知，需人工核对后再处理')
        _log.error('转写任务被重启打断，结果未知，已停止自动重试，需要人工核对：%s 第%s行 record=%s',
                   entry.get('label'), entry.get('number'), entry.get('record_id'))
        changed = True
    if changed:
        _qsave(queue)


def due_items(now=None):
    """Queued work that is ready: new, waiting for a retry, or left 'running' by a restart."""
    now = _now() if now is None else now
    due = []
    for key, entry in _qload().items():
        if key == '_meta' or entry.get('attempts', 0) >= MAX_ATTEMPTS:
            continue
        status = entry.get('status')
        if status == 'pending':
            due.append((key, entry))
        elif status == 'retry' and entry.get('next_at', 0) <= now:
            due.append((key, entry))
        elif (status == 'running' and entry.get('kind') == 'text'
              and now - entry.get('started_at', now) >= STALE_RUNNING_SECONDS):
            due.append((key, entry))
    return due


def run_queued(key, process=None):
    """Run one queued item, with a bounded retry budget and a visible final failure."""
    process = process or process_candidate
    queue = _qload()
    entry = queue[key]
    entry.update(status='running', started_at=_now(), attempts=entry.get('attempts', 0) + 1)
    _qsave(queue)
    try:
        process(entry)
    except Exception as exc:
        full = str(exc)
        message = full[-300:]
        queue = _qload()
        entry = queue[key]
        entry['last_error'] = message
        no_retry = entry.get('kind') != 'text' or any(word in full for word in UNSAFE_TO_RETRY)
        if entry['attempts'] >= MAX_ATTEMPTS or no_retry:
            entry['status'] = 'gave_up'
            _log.error('自动改写失败，已停止重试，需要人工处理：%s 第%s行 record=%s 原因=%s',
                       entry['label'], entry['number'], entry['record_id'], message)
        else:
            entry['status'] = 'retry'
            entry['next_at'] = _now() + RETRY_DELAYS[min(entry['attempts'], len(RETRY_DELAYS)) - 1]
            _log.warning('自动改写失败，稍后重试（第%s次）：%s record=%s 原因=%s',
                         entry['attempts'], entry['label'], entry['record_id'], message)
        _qsave(queue)
        return False
    queue = _qload()
    queue[key]['status'] = 'done'
    _qsave(queue)
    return True


def watch_forever(shared_job_lock, stop_event, interval=30):
    while not stop_event.is_set():
        try:
            scan_once(process_existing=False)
            reconcile_interrupted()
            for key, _entry in due_items():
                if stop_event.is_set():
                    return
                if not shared_job_lock.acquire(blocking=False):
                    break  # still queued; picked up on the next pass
                try:
                    run_queued(key)
                finally:
                    shared_job_lock.release()
        except Exception:
            # Keep the watcher alive. Uvicorn captures the traceback from the bot logger.
            _log.exception('Automatic table processing failed')
        stop_event.wait(interval)
