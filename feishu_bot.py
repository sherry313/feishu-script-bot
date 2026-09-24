"""Feishu event webhook for the 口播稿助手.

It only acts on direct messages or @mentions in groups.  The long-running rewrite
work is moved to a background thread so Feishu receives an immediate HTTP 200.
"""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from feishu_sync import API, Feishu, TABLE_URL, text_value

app = FastAPI(title='口播稿助手')
_seen_message_ids = set()
_seen_lock = threading.Lock()
_job_lock = threading.Lock()


def send_text(chat_id, text):
    """Send one ordinary text reply to the same Feishu chat."""
    client = Feishu()
    response = client.session.post(
        API + '/im/v1/messages',
        params={'receive_id_type': 'chat_id'},
        json={'receive_id': chat_id, 'msg_type': 'text',
              'content': json.dumps({'text': text}, ensure_ascii=False)},
        timeout=30,
    )
    body = response.json()
    if not response.ok or body.get('code') != 0:
        raise RuntimeError('飞书消息发送失败')


def status_text():
    client = Feishu()
    rows = [row for row in client.pages('/records') if row.get('fields', {}).get('视频链接')]
    complete = sum(bool(text_value(row['fields'].get('改写一'))) and
                   bool(text_value(row['fields'].get('改写二'))) for row in rows)
    return f'当前共有 {len(rows)} 条视频链接，已完成两版改写 {complete} 条。\n{TABLE_URL}'


def run_next(chat_id):
    run_next_job(chat_id)


def run_next_job(chat_id=None):
    if not _job_lock.acquire(blocking=False):
        if chat_id:
            send_text(chat_id, '已有一条任务正在处理，请稍后查询“状态”。')
        return
    try:
        result = subprocess.run([sys.executable, '-u', 'run_feishu_pipeline.py', '--next'],
                                cwd=Path(__file__).parent, text=True,
                                encoding='utf-8', capture_output=True, timeout=900)
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()[-300:]
            if chat_id:
                send_text(chat_id, '这一条没有完成：' + (detail or '请查看 Railway 日志。'))
        elif '没有待处理记录' in result.stdout:
            if chat_id:
                send_text(chat_id, '当前没有待处理的文案。')
        else:
            if chat_id:
                send_text(chat_id, '这一条已处理完成并回填到飞书多维表格。\n' + TABLE_URL)
    except subprocess.TimeoutExpired:
        if chat_id:
            send_text(chat_id, '任务运行超过 15 分钟，已停止。转写任务状态已保存，可稍后重试。')
    except Exception:
        if chat_id:
            send_text(chat_id, '任务执行异常，请查看 Railway 日志。')
    finally:
        _job_lock.release()


def reply_for(text):
    normalized = text.strip().lower().replace(' ', '')
    if normalized in {'帮助', 'help', '/help', '开始'}:
        return '我是口播稿助手。\n发送“状态”查看进度；发送“跑一条”处理下一条未完成文案。'
    if normalized in {'状态', 'status', '/status'}:
        return status_text()
    if normalized in {'表格', 'table'}:
        return TABLE_URL
    return None


@app.get('/')
def health():
    return {'ok': True, 'service': '口播稿助手'}


@app.post('/feishu/events')
async def feishu_events(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    # Feishu sends this once while configuring the webhook URL.
    if payload.get('type') == 'url_verification':
        token = os.getenv('FEISHU_VERIFICATION_TOKEN', '').strip()
        if token and payload.get('token') != token:
            raise HTTPException(status_code=403, detail='verification token mismatch')
        return JSONResponse({'challenge': payload.get('challenge', '')})

    header = payload.get('header') or {}
    if header.get('event_type') != 'im.message.receive_v1':
        return {'ok': True}
    event = payload.get('event') or {}
    message = event.get('message') or {}
    message_id = message.get('message_id', '')
    with _seen_lock:
        if message_id in _seen_message_ids:
            return {'ok': True}
        _seen_message_ids.add(message_id)
        if len(_seen_message_ids) > 2000:
            _seen_message_ids.clear()
    if message.get('message_type') != 'text':
        return {'ok': True}
    try:
        text = json.loads(message.get('content') or '{}').get('text', '')
    except json.JSONDecodeError:
        return {'ok': True}
    chat_id = message.get('chat_id')
    if not chat_id:
        return {'ok': True}
    # Group conversations require a mention; otherwise the bot stays quiet.
    if message.get('chat_type') == 'group' and not message.get('mentions'):
        return {'ok': True}
    if text.strip().replace(' ', '') in {'跑一条', 'runone', '/run'}:
        background_tasks.add_task(send_text, chat_id, '收到，开始处理下一条未完成文案。')
        background_tasks.add_task(run_next, chat_id)
        return {'ok': True}
    response = reply_for(text)
    if response:
        background_tasks.add_task(send_text, chat_id, response)
    return {'ok': True}


@app.post('/jobs/daily')
async def daily_job(request: Request, background_tasks: BackgroundTasks):
    """Private Railway cron trigger; protected by an application-owned secret."""
    expected = os.getenv('JOB_TRIGGER_SECRET', '').strip()
    supplied = request.headers.get('X-Job-Secret', '')
    if not expected or supplied != expected:
        raise HTTPException(status_code=403, detail='unauthorized')
    background_tasks.add_task(run_next_job)
    return {'accepted': True}
