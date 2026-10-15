"""Poll the three script tables and process newly created or edited link rows."""
import hashlib
import json
import os
import subprocess
import sys
import threading
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


def fingerprint(record):
    fields = record.get('fields', {})
    raw = fields.get('视频链接')
    return hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True,
                                     default=str).encode('utf-8')).hexdigest()


def scan_once(process_existing=False):
    state = _load()
    first_run = not bool(state)
    candidates = []
    for label, table_id in configured_tables():
        client = Feishu(table_id=table_id)
        rows = list(client.pages('/records'))
        for number, row in enumerate(rows, 1):
            rid = row['record_id']
            key = table_id + ':' + rid
            mark = fingerprint(row)
            previous = state.get(key)
            state[key] = mark
            fields = row.get('fields', {})
            url = record_video_url(fields.get('视频链接'))
            complete = all(text_value(fields.get(name)).strip() for name in OUTPUTS)
            if not url or complete:
                continue
            if first_run and not process_existing:
                continue
            if previous == mark:
                continue
            candidates.append({'label': label, 'table_id': table_id,
                               'record_id': rid, 'number': number, 'url': url})
    _save(state)
    return candidates


def process_candidate(item):
    client = Feishu(table_id=item['table_id'])
    record = client.request('GET', client.base + '/records/' + item['record_id'])['data']['record']
    fields = record.get('fields', {})
    url = record_video_url(fields.get('视频链接'))
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


def watch_forever(shared_job_lock, stop_event, interval=30):
    while not stop_event.is_set():
        try:
            items = scan_once(process_existing=False)
            for item in items:
                if stop_event.is_set():
                    return
                if not shared_job_lock.acquire(blocking=False):
                    break
                try:
                    process_candidate(item)
                finally:
                    shared_job_lock.release()
        except Exception:
            # Keep the watcher alive. Uvicorn captures the traceback from the bot logger.
            import logging
            logging.getLogger('uvicorn.error').exception('Automatic table processing failed')
        stop_event.wait(interval)
