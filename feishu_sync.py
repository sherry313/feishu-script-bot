"""将 Google 表格中已有的两版口播稿追加到飞书；不重新生成、不覆盖人工编辑。

python feishu_sync.py --check        # 只验证飞书连接和字段
python feishu_sync.py                # 预览待同步数量
python feishu_sync.py --apply        # 执行同步
"""
import argparse
import hashlib
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name('.env'))
API = 'https://open.feishu.cn/open-apis'
APP_ID = 'cli_aa3926dc6b38dbe0'
WIKI_TOKEN = 'VVqCwFiTEi5lczkb8L6cXvtfnS8'
TABLE_ID = 'tbl6xbauVAY1KE7W'
TABLE_URL = f'https://bkqc58qk51.feishu.cn/wiki/{WIKI_TOKEN}?table={TABLE_ID}'


def text_value(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ''.join(part.get('text', '') for part in value if isinstance(part, dict))
    return ''


def script_key(script):
    return hashlib.sha256(script.strip().encode('utf-8')).hexdigest()


def candidates(rows):
    """两版均保留；失败状态或空白改写不导入。"""
    for number, row in enumerate(rows[1:], 2):
        if len(row) < 9 or row[8].strip() != '成功':
            continue
        for column, version in ((6, '版本一'), (7, '版本二')):
            script = row[column].strip()
            if not script:
                continue
            title = row[1].strip() or f'口播稿第{number}行'
            yield {'标题': f'{title}｜{version}', '口播稿': script,
                   '生成状态': '待确认'}


class Feishu:
    def __init__(self):
        secret = os.getenv('FEISHU_APP_SECRET', '').strip()
        if not secret:
            raise RuntimeError('缺少 FEISHU_APP_SECRET，请保存在项目 .env 文件中。')
        self.session = requests.Session()
        result = self.request('POST', '/auth/v3/tenant_access_token/internal',
                              json={'app_id': os.getenv('FEISHU_APP_ID', APP_ID),
                                    'app_secret': secret})
        token = result.get('tenant_access_token')
        if not token:
            raise RuntimeError('飞书未返回访问令牌。')
        self.session.headers['Authorization'] = f'Bearer {token}'
        self.app_token = os.getenv('FEISHU_BITABLE_APP_TOKEN', '').strip()
        if not self.app_token:
            node = self.request('GET', '/wiki/v2/spaces/get_node',
                                params={'token': WIKI_TOKEN})['data']['node']
            if node.get('obj_type') != 'bitable':
                raise RuntimeError('目标知识库节点不是多维表格，已停止。')
            self.app_token = node['obj_token']
        self.table_id = os.getenv('FEISHU_TABLE_ID', TABLE_ID).strip()
        self.base = f'/bitable/v1/apps/{self.app_token}/tables/{self.table_id}'

    def request(self, method, path, **kwargs):
        read_retry = kwargs.pop('_read_retry', 0)
        # 写入不自动重试：若服务已写入但网络断开，下次先读取记录再去重。
        try:
            response = self.session.request(method, API + path, timeout=30, **kwargs)
            body = response.json()
        except (requests.RequestException, ValueError):
            raise RuntimeError('飞书网络请求失败；结果可能未确认，请重新检查连接后再同步。') from None
        if not response.ok or body.get('code') != 0:
            if method == 'GET' and body.get('code') == 1254607 and read_retry < 2:
                time.sleep(2 * (read_retry + 1))
                return self.request(method, path, _read_retry=read_retry+1, **kwargs)
            # 不输出服务端响应体，以免日志带入令牌、密钥或文案。
            raise RuntimeError(f"飞书 API 失败：HTTP {response.status_code}，"
                               f"错误码 {body.get('code', '未知')}。请检查应用权限、发布状态和文档授权。")
        return body

    def pages(self, suffix):
        params = {'page_size': 100}
        while True:
            data = self.request('GET', self.base + suffix, params=params)['data']
            yield from data.get('items', [])
            if not data.get('has_more'):
                return
            token = data.get('page_token')
            if not token or token == params.get('page_token'):
                raise RuntimeError('飞书分页令牌异常，已停止同步。')
            params['page_token'] = token

    def check(self):
        fields = {field['field_name']: field['type'] for field in self.pages('/fields')}
        expected = {'标题': 1, '口播稿': 1, '生成状态': 3, '视频链接': 15}
        for name, kind in expected.items():
            if fields.get(name) != kind:
                raise RuntimeError(f'飞书字段缺失或类型不匹配：{name}')
        self.request('GET', self.base + '/records', params={'page_size': 1})

    def sync(self, rows, apply=False, limit=None):
        self.check()
        existing = {script_key(text_value(record.get('fields', {}).get('口播稿')))
                    for record in self.pages('/records')}
        pending = []
        for fields in candidates(rows):
            key = script_key(fields['口播稿'])
            if key not in existing:
                pending.append(fields)
                existing.add(key)
        if limit is not None:
            pending = pending[:limit]
        print(f'飞书待新增 {len(pending)} 条口播稿；已有相同正文自动跳过。')
        if apply:
            for index, fields in enumerate(pending, 1):
                self.request('POST', self.base + '/records', json={'fields': fields})
                print(f'已同步 {index}/{len(pending)} 条')
        return len(pending)


def sync_sheet(ws, apply=True):
    return Feishu().sync(ws.get_all_values(), apply=apply)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error('--limit 必须大于零')
    try:
        client = Feishu()
        if args.check:
            client.check()
            print('飞书认证、字段与记录读取检查通过；写入权限需实际写入验证。')
        else:
            from core import open_sheet
            client.sync(open_sheet().get_all_values(), apply=args.apply, limit=args.limit)
        print(TABLE_URL)
    except RuntimeError as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
