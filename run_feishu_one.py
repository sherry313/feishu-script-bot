"""Run one existing-transcript Feishu record; never writes Google Sheets."""
import hashlib
import json
import argparse
from pathlib import Path
import anthropic
from core import cfg, _one_version, make_meta, _targets
from feishu_sync import Feishu, text_value

OUTPUTS = ['改写一', '改写二', '改写标题', '标签', '简介']

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--redo-record', help='Explicitly authorized record to rewrite; preserve a backup')
    parser.add_argument('--redo-token', help='Unique Feishu review-card token for this redo attempt')
    parser.add_argument('--record', help='Fill missing outputs for this record only')
    parser.add_argument('--table', help='Feishu table ID containing the record')
    args = parser.parse_args()
    c = Feishu(table_id=args.table)
    fields = {f['field_name']: f['type'] for f in c.pages('/fields')}
    for name in OUTPUTS:
        if name not in fields:
            c.request('POST', c.base+'/fields', json={'field_name':name, 'type':1})
        elif fields[name] != 1:
            raise RuntimeError('Output field type mismatch')
    params = {'page_size':100}
    if not args.table:
        params['view_id'] = 'vewhIlXf4y'
    rows = []
    while True:
        data = c.request('GET', c.base+'/records', params=params)['data']
        rows.extend(data.get('items', []))
        if not data.get('has_more'): break
        params['page_token'] = data['page_token']
    selected = args.redo_record or args.record
    r = next((r for r in rows if r['record_id'] == selected), None) if selected else next((r for r in rows if r.get('fields',{}).get('视频链接')
              and not all(text_value(r['fields'].get(k)) for k in OUTPUTS)), None)
    if r is None:
        print('没有待处理记录'); return
    rid, before = r['record_id'], r['fields']
    source_field = '原视频内容' if text_value(before.get('原视频内容')) else '口播稿'
    source = text_value(before.get(source_field))
    if not source:
        raise RuntimeError('首条缺原文，请使用持久化taskId的转写流程后续跑；未跳过该条')
    c.request('PUT', c.base+'/records/'+rid, json={'fields':{'视频链接':before['视频链接']}})
    model = cfg('CLAUDE_MODEL', 'claude-sonnet-5')
    key_material = rid + source + model + 'rules-v2-60-90sec'
    if args.redo_record:
        # A human-requested redo must not reuse the previous completed rewrite.
        # Hash the current visible outputs so retries of the same redo resume,
        # while a later redo after new content gets a fresh cache file.
        redo_identity = args.redo_token or '|'.join(text_value(before.get(k)) for k in OUTPUTS)
        key_material += '|redo|' + redo_identity
    key = hashlib.sha256(key_material.encode()).hexdigest()
    path = Path(__file__).with_name('feishu_rewrite_'+key+'.json')
    state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    def save():
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
        tmp.replace(path)
    if args.redo_record and 'previous_fields' not in state:
        state['previous_fields'] = before
        save()
    client = anthropic.Anthropic(api_key=cfg('ANTHROPIC_API_KEY'), max_retries=0)
    print('试跑记录',rid,'原文',len(source),'字',flush=True)
    for i, name in enumerate(OUTPUTS[:2]):
        existing = text_value(before.get(name))
        if existing and not args.redo_record: state[name] = existing; continue
        if state.get(name): continue
        repair = state.get(name+'_draft')
        for attempt in range(3):
            try:
                out = _one_version(source, client, model, avoid=state.get('改写一') if i else None, repair=repair)
            except RuntimeError as error:
                repair = getattr(error, 'draft', None)
                if repair: state[name+'_draft'] = repair; save()
                if attempt == 2: raise
                continue
            lo, hi = _targets(len(source))
            if lo <= len(out) <= hi:
                state[name] = out; save(); print(name,len(out),'字，已缓存',flush=True); break
        else: raise RuntimeError(name+'不满足目标字数，已停止')
    if not all(state.get(k) for k in OUTPUTS[2:]):
        for attempt in range(3):
            try:
                meta = make_meta(state['改写一'], client, model)
            except RuntimeError:
                if attempt == 2: raise
                continue
            tags = meta['标签'].split()
            if 15 <= len(meta['标题']) <= 25 and 0 < len(meta['简介']) <= 25 and len(tags)==5 and all(t.startswith('#') for t in tags):
                state.update({'改写标题':meta['标题'],'标签':meta['标签'],'简介':meta['简介']}); save(); break
        else: raise RuntimeError('元数据不符合规则')
    fresh = c.request('GET',c.base+'/records/'+rid)['data']['record']['fields']
    assert text_value(fresh.get(source_field)) == source, 'Original changed'
    assert fresh.get('视频链接') == before.get('视频链接'), 'URL changed'
    patch = {}
    for name in OUTPUTS:
        current = text_value(fresh.get(name))
        if args.redo_record:
            if current != text_value(before.get(name)): raise RuntimeError('运行中人工编辑冲突：'+name)
            patch[name] = state[name]
        else:
            if current and current != state[name]: raise RuntimeError('人工编辑冲突：'+name)
            if not current: patch[name] = state[name]
    if patch: c.request('PUT',c.base+'/records/'+rid,json={'fields':patch})
    after = c.request('GET',c.base+'/records/'+rid)['data']['record']['fields']
    assert all(text_value(after.get(k)) == state[k] for k in OUTPUTS)
    assert all(after.get(k) == v for k,v in before.items() if k not in OUTPUTS)
    state['verified_record_id'] = rid; save()
    print('回填核对成功',state['改写标题'], '缓存文件',path,flush=True)

if __name__ == '__main__':
    main()
