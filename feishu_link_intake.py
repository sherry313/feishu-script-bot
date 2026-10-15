"""Turn supported social-video shares into numbered Feishu source records."""
import re
import threading
from urllib.parse import urlsplit, urlunsplit

from feishu_sync import Feishu, text_value


SUPPORTED_HOSTS = {
    'xiaohongshu.com',
    'www.xiaohongshu.com',
    'xhslink.com',
    'www.xhslink.com',
    'channels.weixin.qq.com',
    'finder.video.qq.com',
    'weixin.qq.com',
    'www.weixin.qq.com',
}
URL_RE = re.compile(r'https?://[^\s<>\]\["\']+', re.IGNORECASE)
_intake_lock = threading.Lock()


def normalize_url(url):
    """Remove share-message punctuation and fragments without changing the resource."""
    cleaned = (url or '').strip().rstrip('，。！？；：,.!?;:)）】》')
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return ''
    host = (parts.hostname or '').lower()
    if parts.scheme not in {'http', 'https'} or host not in SUPPORTED_HOSTS:
        return ''
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path,
                       parts.query, ''))


def shared_video_links(text):
    """Return unique Xiaohongshu/Weixin Channels links in message order."""
    result = []
    seen = set()
    for match in URL_RE.findall(text or ''):
        url = normalize_url(match)
        key = url.rstrip('/')
        if url and key not in seen:
            result.append(url)
            seen.add(key)
    return result


def share_title(text, url, number):
    """Keep the human share caption when available; otherwise use a stable fallback."""
    caption = (text or '').replace(url, ' ')
    caption = re.sub(r'<at\b[^>]*>.*?</at>|@_user_\d+', ' ', caption,
                     flags=re.IGNORECASE | re.DOTALL)
    caption = re.sub(r'复制.*?(打开|看看)|打开(小红书|微信).*', ' ', caption)
    caption = re.sub(r'\s+', ' ', caption).strip(' -—|：:，,。')
    if caption:
        return caption[:100]
    host = (urlsplit(url).hostname or '').lower()
    platform = '小红书' if 'xiaohongshu' in host or 'xhslink' in host else '视频号'
    return f'{platform}分享｜脚本{number}'


def _link_value(value):
    if isinstance(value, dict):
        return str(value.get('link', '')).strip().rstrip('/')
    return str(value or '').strip().rstrip('/')


def _library_value(fields):
    return text_value((fields or {}).get('脚本库')).strip()


def ingest_links(text, library='LEO', client=None):
    """Append new links atomically and return their stable library numbers."""
    links = shared_video_links(text)
    if not links:
        return []
    client = client or Feishu()
    with _intake_lock:
        rows = list(client.pages('/records'))
        library_rows = [row for row in rows if _library_value(row.get('fields')) == library]
        by_url = {
            _link_value(row.get('fields', {}).get('视频链接')): (index, row)
            for index, row in enumerate(library_rows, 1)
            if _link_value(row.get('fields', {}).get('视频链接'))
        }
        results = []
        next_number = len(library_rows) + 1
        for url in links:
            existing = by_url.get(url.rstrip('/'))
            if existing:
                number, row = existing
                results.append({'number': number, 'record_id': row['record_id'],
                                'url': url, 'duplicate': True})
                continue
            fields = {
                '标题': share_title(text, url, next_number),
                '视频链接': {'link': url, 'text': url},
                '生成状态': '待确认',
                '脚本库': library,
            }
            body = client.request('POST', client.base + '/records', json={'fields': fields})
            record = body['data']['record']
            results.append({'number': next_number, 'record_id': record['record_id'],
                            'url': url, 'duplicate': False})
            by_url[url.rstrip('/')] = (next_number, record)
            next_number += 1
        return results
