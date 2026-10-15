"""Process one Feishu source record: original transcript, two rewrites and metadata.

The transcript task ID is persisted before polling, so restarting this command never
blindly submits the same source URL a second time.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

from core import API_BASE, _dig, _headers, cache_get, cache_put, quota
from feishu_sync import Feishu, text_value

OUTPUTS = ['改写一', '改写二', '改写标题', '标签', '简介']
STATE_DIR = Path(os.getenv('STATE_DIR', Path(__file__).parent))
STATE_PATH = STATE_DIR / 'pipeline_tasks.json'


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp = STATE_PATH.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(STATE_PATH)


def link_of(value):
    return value.get('link', '').rstrip('/') if isinstance(value, dict) else ''


def extract_one(client, record, state):
    """Return stored transcript, or submit/poll exactly one persistent task."""
    fields = record['fields']
    original = text_value(fields.get('原视频内容')) or text_value(fields.get('口播稿'))
    if original:
        return original
    url = link_of(fields.get('视频链接'))
    if not url:
        raise RuntimeError('记录缺少视频链接')
    cached = cache_get(url) or cache_get(url + '/') or {}
    original = cached.get('文案') if isinstance(cached, dict) else ''
    if original:
        client.request('PUT', client.base + '/records/' + record['record_id'],
                       json={'fields': {'原视频内容': original}})
        return original

    entry = state.get(url)
    if entry is None:
        available = float(quota().get('availableMinutes') or 0)
        if available < 6:
            raise RuntimeError('转写额度不足 6 分钟，未提交任务')
        entry = {'record_id': record['record_id'], 'status': 'submitting'}
        state[url] = entry
        save_state(state)
        try:
            response = requests.post(API_BASE + '/task/create', headers=_headers(),
                                     json={'workUrl': url}, timeout=60)
            body = response.json()
        except (requests.RequestException, ValueError):
            entry['status'] = 'submission_unknown'
            save_state(state)
            raise RuntimeError('转写提交结果未知，已停止以避免重复扣费') from None
        task_id = _dig(body.get('data', {}), 'taskId', 'taskID', 'id')
        if body.get('code') != 200 or not task_id:
            entry['status'] = 'submission_failed'
            save_state(state)
            raise RuntimeError('转写任务提交失败')
        entry.update(task_id=task_id, status='pending')
        save_state(state)

    if entry.get('status') == 'submission_unknown':
        raise RuntimeError('该链接的转写提交状态未知，需人工核对后才可重新提交')
    if entry.get('text'):
        original = entry['text']
    else:
        task_id = entry.get('task_id')
        if not task_id:
            raise RuntimeError('转写任务缺少 task ID')
        deadline = time.time() + 600
        while time.time() < deadline:
            try:
                result = requests.get(API_BASE + '/task/query', headers=_headers(),
                                      params={'taskId': task_id}, timeout=60).json()
            except (requests.RequestException, ValueError):
                time.sleep(5)
                continue
            data = result.get('data') or {}
            status = str(_dig(data, 'status', 'taskStatus')).upper()
            if status == 'SUCCESS':
                original = _dig(data, 'textContent', 'text', 'content', 'transcript')
                if not original:
                    raise RuntimeError('转写完成但没有返回正文')
                entry.update(text=original, status='extracted')
                save_state(state)
                break
            if status == 'FAILURE':
                entry['status'] = 'failed'
                save_state(state)
                raise RuntimeError('转写服务返回失败')
            time.sleep(4)
        else:
            raise RuntimeError('转写仍在处理中；任务 ID 已保存，下次会继续查询')
    cache_put(url, {'文案': original})
    client.request('PUT', client.base + '/records/' + record['record_id'],
                   json={'fields': {'原视频内容': original}})
    entry['status'] = 'written'
    save_state(state)
    return original


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--next', action='store_true', help='process the first unfinished source record')
    parser.add_argument('--record', help='process this exact Feishu record')
    args = parser.parse_args()
    if not args.next and not args.record:
        parser.error('use --next or --record')
    client = Feishu()
    rows = [row for row in client.pages('/records') if link_of(row.get('fields', {}).get('视频链接'))]
    if args.record:
        target = next((row for row in rows if row['record_id'] == args.record), None)
        if target and all(text_value(target['fields'].get(k)) for k in OUTPUTS):
            print('记录已经处理完成')
            return
    else:
        target = next((row for row in rows if not all(text_value(row['fields'].get(k)) for k in OUTPUTS)), None)
    if target is None:
        print('没有待处理记录')
        return
    state = load_state()
    original = extract_one(client, target, state)
    if not original:
        raise RuntimeError('未取得原视频内容')
    command = [sys.executable, '-u', 'run_feishu_one.py', '--record', target['record_id']]
    result = subprocess.run(command, cwd=Path(__file__).parent, text=True,
                            encoding='utf-8', capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr[-500:] or result.stdout[-500:] or '改写失败')
    print('处理完成', target['record_id'])


if __name__ == '__main__':
    main()
