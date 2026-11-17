"""Simulated tests for automatic rewriting of text-only records. No network, no paid APIs."""
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import table_auto_worker as worker

NOW = 1_800_000_000.0
SINCE = int(NOW * 1000)          # activation moment
AFTER = SINCE + 5_000            # created after activation
BEFORE = SINCE - 5_000_000       # historical record


def text(value):
    return [{'type': 'text', 'text': value}]


def row(rid, created, **fields):
    return {'record_id': rid, 'created_time': created, 'fields': fields}


class Base(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = Path(tempfile.mkdtemp())
        self.patches = [
            patch.object(worker, 'STATE_DIR', self.dir),
            patch.object(worker, 'STATE_PATH', self.dir / 'seen.json'),
            patch.object(worker, 'QUEUE_PATH', self.dir / 'queue.json'),
            patch.object(worker, '_now', lambda: NOW),
            patch.object(worker, 'configured_tables', lambda: [('LEO', 'tbl')]),
            patch.dict(os.environ, {'TEXT_REWRITE_SINCE_MS': ''}),
        ]
        for p in self.patches:
            p.start()
        # Activate: first scan records the cutoff and the seen-state of history.
        self.rows = []
        self.client = MagicMock()
        self.client.pages.side_effect = lambda path, params=None: iter(self.rows)
        feishu = patch.object(worker, 'Feishu', return_value=self.client)
        feishu.start()
        self.patches.append(feishu)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def scan(self):
        return worker.scan_once()

    def queue(self):
        return json.loads((self.dir / 'queue.json').read_text(encoding='utf-8'))


class DetectionTests(Base):
    def test_new_text_only_record_waits_for_stable_text_then_queues(self):
        self.rows = [row('rec1', BEFORE, **{'视频链接': 'https://v.douyin.com/a/', '改写一': 'x'})]
        self.scan()                                   # activation scan
        self.rows.append(row('rec67', AFTER, **{'标题': 't', '口播稿': '你好世界'}))
        self.assertEqual(self.scan(), [])             # first sighting: only 'seen'
        self.assertEqual(self.queue()['tbl:rec67']['status'], 'seen')
        queued = self.scan()                          # unchanged text: now pending
        self.assertEqual([c['record_id'] for c in queued], ['rec67'])
        self.assertEqual(self.queue()['tbl:rec67']['status'], 'pending')
        self.assertEqual(self.queue()['tbl:rec67']['kind'], 'text')

    def test_text_still_being_edited_is_not_queued(self):
        self.scan()
        self.rows = [row('r', AFTER, **{'口播稿': '第一句'})]
        self.scan()
        self.rows = [row('r', AFTER, **{'口播稿': '第一句，第二句'})]
        self.assertEqual(self.scan(), [])
        self.assertEqual(self.queue()['tbl:r']['status'], 'seen')

    def test_detection_uses_record_id_not_row_count_or_position(self):
        self.scan()
        # 70 historical complete rows first; the new record is far past "67".
        self.rows = [row(f'h{i}', BEFORE, **{n: 'x' for n in worker.OUTPUTS}) for i in range(70)]
        self.rows.append(row('new71', AFTER, **{'口播稿': '新稿子'}))
        self.scan()
        self.assertEqual([c['record_id'] for c in self.scan()], ['new71'])
        self.assertEqual(self.queue()['tbl:new71']['number'], 71)

    def test_historical_records_are_never_bulk_processed(self):
        self.rows = [row(f'old{i}', BEFORE, **{'口播稿': '旧稿'}) for i in range(5)]
        self.scan()
        self.scan()
        self.assertEqual([k for k in self.queue() if k != '_meta'], [])

    def test_recovery_cutoff_override_only_when_explicitly_set(self):
        self.rows = [row('r67', BEFORE, **{'口播稿': '稿'})]
        self.scan(); self.scan()
        self.assertNotIn('tbl:r67', self.queue())
        with patch.dict(os.environ, {'TEXT_REWRITE_SINCE_MS': str(BEFORE - 1)}):
            self.scan(); self.scan()
        self.assertEqual(self.queue()['tbl:r67']['status'], 'pending')

    def test_record_with_existing_rewrite_or_missing_created_time_is_ignored(self):
        self.scan()
        self.rows = [
            row('has', AFTER, **{'口播稿': '稿', '改写一': '已有'}),
            {'record_id': 'nots', 'fields': {'口播稿': '稿'}},
            row('nosrc', AFTER, **{'标题': '只有标题'}),
        ]
        self.scan(); self.scan()
        self.assertEqual([k for k in self.queue() if k != '_meta'], [])

    def test_done_and_gave_up_text_entries_are_never_requeued(self):
        self.scan()
        self.rows = [row('r', AFTER, **{'口播稿': '稿'})]
        self.scan(); self.scan()
        for status in ('done', 'gave_up'):
            queue = self.queue()
            queue['tbl:r']['status'] = status
            (self.dir / 'queue.json').write_text(json.dumps(queue), encoding='utf-8')
            self.assertEqual(self.scan(), [])
            self.assertEqual(self.queue()['tbl:r']['status'], status)

    def test_source_only_in_original_content_is_not_auto_rewritten(self):
        self.scan()
        self.rows = [row('r', AFTER, **{'原视频内容': '残留稿'})]
        self.scan(); self.scan()
        self.assertNotIn('tbl:r', self.queue())

    def test_created_time_as_string_is_ignored_safely(self):
        self.scan()
        self.rows = [row('r', str(AFTER), **{'口播稿': '稿'})]
        self.scan(); self.scan()
        self.assertNotIn('tbl:r', self.queue())

    def test_link_entry_in_queue_keeps_its_attempt_budget(self):
        self.rows = [row('old', BEFORE, **{'视频链接': 'https://v.douyin.com/o/'})]
        self.scan()
        self.rows = [row('n', AFTER, **{'视频链接': 'https://v.douyin.com/n/'})]
        self.scan()
        queue = self.queue()
        queue['tbl:n'].update(status='retry', attempts=2)
        (self.dir / 'queue.json').write_text(json.dumps(queue), encoding='utf-8')
        # The link field changes shape (e.g. text/link object) -> new fingerprint.
        self.rows = [row('n', AFTER, **{'视频链接': {'link': 'https://v.douyin.com/n/',
                                                     'text': 'https://v.douyin.com/n/'}})]
        self.assertEqual(self.scan(), [])
        self.assertEqual(self.queue()['tbl:n']['attempts'], 2)

    def test_queue_is_saved_before_seen_state(self):
        order = []
        with patch.object(worker, '_qsave', side_effect=lambda q: order.append('queue')), \
                patch.object(worker, '_save', side_effect=lambda s: order.append('state')):
            self.scan()
        self.assertEqual(order, ['queue', 'state'])

    def test_missing_created_time_logs_one_warning_and_never_processes(self):
        self.scan()
        self.rows = [{'record_id': 'nots', 'fields': {'口播稿': '稿'}}]
        with self.assertLogs('uvicorn.error', level='WARNING') as logs:
            self.scan()
        self.assertIn('没有返回有效的创建时间', logs.output[0])
        self.assertEqual(len(logs.output), 1)
        with self.assertRaises(AssertionError):      # second scan: no repeat warning
            with self.assertLogs('uvicorn.error', level='WARNING'):
                self.scan()
        self.assertNotIn('tbl:nots', self.queue())

    def test_history_without_created_time_is_never_treated_as_new(self):
        self.scan()
        self.rows = [{'record_id': f'h{i}', 'fields': {'口播稿': '旧'}} for i in range(3)]
        with self.assertLogs('uvicorn.error', level='WARNING'):
            self.scan()
        self.scan()
        self.assertEqual([k for k in self.queue() if k != '_meta'], [])

    def test_both_sources_present_is_never_auto_rewritten(self):
        self.scan()
        self.rows = [row('r', AFTER, **{'口播稿': '新稿', '原视频内容': '残留稿'})]
        self.scan(); self.scan()
        self.assertNotIn('tbl:r', self.queue())

    def test_corrected_link_restarts_a_gave_up_entry_once(self):
        self.rows = [row('old', BEFORE, **{'视频链接': 'https://v.douyin.com/o/'})]
        self.scan()
        self.rows = [row('n', AFTER, **{'视频链接': 'https://v.douyin.com/bad/'})]
        self.scan()
        queue = self.queue()
        queue['tbl:n'].update(status='gave_up', attempts=1, last_error='转写任务提交失败')
        (self.dir / 'queue.json').write_text(json.dumps(queue), encoding='utf-8')
        self.assertEqual(self.scan(), [])                       # same bad link: stays parked
        self.rows = [row('n', AFTER, **{'视频链接': 'https://v.douyin.com/good/'})]
        got = self.scan()
        self.assertEqual([c['url'] for c in got], ['https://v.douyin.com/good/'])
        self.assertEqual(self.queue()['tbl:n']['attempts'], 0)
        self.assertEqual(self.scan(), [])                       # and only once

    def test_scan_asks_feishu_for_creation_times(self):
        self.scan()
        args = self.client.pages.call_args
        self.assertEqual(args[0][0], '/records')
        self.assertEqual(args[0][1], {'automatic_fields': 'true'})

    def test_timestamp_validation(self):
        v = worker.valid_created_ms
        self.assertEqual(v(1794881117000), 1794881117000)
        self.assertEqual(v(1794881117000.0), 1794881117000)
        for bad in (None, '1794881117000', True, 0, -5, 1794881117, 10 ** 15,
                    float('nan'), float('inf'), [], {}):
            self.assertIsNone(v(bad), repr(bad))

    def test_timestamp_in_seconds_is_not_trusted(self):
        self.scan()
        self.rows = [row('r', int(NOW), **{'口播稿': '稿'})]
        with self.assertLogs('uvicorn.error', level='WARNING'):
            self.scan()
        self.assertNotIn('tbl:r', self.queue())

    def test_all_three_libraries_are_scanned_with_the_parameter(self):
        seen = []
        clients = {}

        def make(table_id):
            client = MagicMock()
            client.pages.side_effect = lambda path, params=None: (
                seen.append((table_id, params)) or iter([]))
            clients[table_id] = client
            return client

        with patch.object(worker, 'Feishu', side_effect=lambda table_id: make(table_id)), \
                patch.object(worker, 'configured_tables',
                             lambda: [('LEO', 'a'), ('Libby', 'b'), ('Chen', 'c')]):
            self.scan()
        self.assertEqual(seen, [('a', {'automatic_fields': 'true'}),
                                ('b', {'automatic_fields': 'true'}),
                                ('c', {'automatic_fields': 'true'})])

    def test_one_failing_library_does_not_stop_the_others(self):
        good = MagicMock()
        good.pages.side_effect = lambda path, params=None: iter(
            [row('n', AFTER, **{'口播稿': '稿'})])
        bad = MagicMock()
        bad.pages.side_effect = RuntimeError('permission denied')
        clients = iter([bad, good])
        with patch.object(worker, 'Feishu', side_effect=lambda table_id: next(clients)), \
                patch.object(worker, 'configured_tables',
                             lambda: [('LEO', 'a'), ('Libby', 'b')]):
            with self.assertLogs('uvicorn.error', level='WARNING'):
                self.scan()
            clients = iter([bad, good])
            self.scan()
        self.assertEqual(self.queue()['b:n']['status'], 'pending')

    def test_link_flow_is_preserved(self):
        self.rows = [row('old', BEFORE, **{'视频链接': 'https://v.douyin.com/o/'})]
        self.assertEqual(self.scan(), [])             # first run: history skipped
        self.rows.append(row('new', AFTER, **{'视频链接': 'https://v.douyin.com/n/'}))
        got = self.scan()
        self.assertEqual([(c['record_id'], c['kind']) for c in got], [('new', 'link')])
        self.assertEqual(self.scan(), [])             # same link: not queued twice


class ProcessTests(Base):
    def record(self, **fields):
        self.client.request.return_value = {'data': {'record': {'fields': fields}}}

    def item(self, kind='text'):
        return {'label': 'LEO', 'table_id': 'tbl', 'record_id': 'rec', 'number': 67,
                'url': '', 'kind': kind}

    def run_item(self, kind='text', **fields):
        self.record(**fields)
        with patch.object(worker.subprocess, 'run') as run:
            run.return_value = MagicMock(returncode=0, stdout='', stderr='')
            worker.process_candidate(self.item(kind))
        return run

    def test_text_only_runs_rewrite_directly_without_transcription(self):
        run = self.run_item(**{'口播稿': [{'text': 'abc'}]})
        command = run.call_args[0][0]
        self.assertIn('run_feishu_one.py', command)
        self.assertNotIn('run_feishu_pipeline.py', command)
        self.assertEqual(command[-2:], ['--table', 'tbl'])
        self.client.request.assert_called_once()      # only the GET; nothing written here

    def test_existing_rewrite_one_is_never_overwritten(self):
        run = self.run_item(**{'口播稿': 'abc', '改写一': '人工写的'})
        run.assert_not_called()

    def test_approved_or_generating_records_are_skipped(self):
        for status in ('文案已确认', '生成中', '已生成'):
            run = self.run_item(**{'口播稿': 'abc', '生成状态': status})
            run.assert_not_called()

    def test_link_appearing_meanwhile_hands_over_to_link_flow(self):
        run = self.run_item(**{'口播稿': 'abc', '视频链接': {'link': 'https://v.douyin.com/z/'}})
        run.assert_not_called()

    def test_text_record_with_original_content_is_skipped_at_run_time(self):
        run = self.run_item(**{'口播稿': 'abc', '原视频内容': 'def'})
        run.assert_not_called()

    def test_link_flow_still_uses_pipeline(self):
        run = self.run_item('link', **{'视频链接': {'link': 'https://v.douyin.com/z/'}})
        self.assertIn('run_feishu_pipeline.py', run.call_args[0][0])


class RetryTests(Base):
    def put(self, **entry):
        base = {'kind': 'text', 'label': 'LEO', 'table_id': 'tbl', 'record_id': 'r',
                'number': 67, 'url': '', 'status': 'pending', 'attempts': 0}
        base.update(entry)
        (self.dir / 'queue.json').write_text(json.dumps({'tbl:r': base}), encoding='utf-8')

    def test_failure_schedules_bounded_retries_then_gives_up_visibly(self):
        self.put()
        boom = MagicMock(side_effect=RuntimeError('Claude 超时'))
        with self.assertLogs('uvicorn.error', level='WARNING'):
            self.assertFalse(worker.run_queued('tbl:r', boom))
        entry = self.queue()['tbl:r']
        self.assertEqual((entry['status'], entry['attempts']), ('retry', 1))
        self.assertEqual(entry['next_at'], NOW + 300)
        self.assertEqual(worker.due_items(NOW + 299), [])
        self.assertEqual(len(worker.due_items(NOW + 300)), 1)
        with self.assertLogs('uvicorn.error', level='WARNING'):
            worker.run_queued('tbl:r', boom)
        self.assertEqual(self.queue()['tbl:r']['next_at'], NOW + 1800)
        with self.assertLogs('uvicorn.error', level='ERROR') as logs:
            worker.run_queued('tbl:r', boom)
        entry = self.queue()['tbl:r']
        self.assertEqual((entry['status'], entry['attempts']), ('gave_up', 3))
        self.assertIn('需要人工处理', logs.output[0])
        self.assertEqual(worker.due_items(NOW + 10**6), [])
        self.assertEqual(boom.call_count, 3)

    def test_link_flow_failures_are_reported_not_retried(self):
        self.put(kind='link', url='https://v.douyin.com/a/')
        failing = MagicMock(side_effect=RuntimeError('转写任务提交失败'))
        with self.assertLogs('uvicorn.error', level='ERROR'):
            worker.run_queued('tbl:r', failing)
        self.assertEqual(self.queue()['tbl:r']['status'], 'gave_up')
        self.assertEqual(failing.call_count, 1)

    def test_unknown_outcome_is_never_retried_even_in_long_messages(self):
        self.put()
        failing = MagicMock(side_effect=RuntimeError('转写提交结果未知，' + 'x' * 600))
        with self.assertLogs('uvicorn.error', level='ERROR'):
            worker.run_queued('tbl:r', failing)
        self.assertEqual(self.queue()['tbl:r']['status'], 'gave_up')

    def test_unknown_outcome_is_never_retried(self):
        self.put(kind='link', url='https://v.douyin.com/a/')
        failing = MagicMock(side_effect=RuntimeError('转写提交结果未知，已停止以避免重复扣费'))
        with self.assertLogs('uvicorn.error', level='ERROR'):
            worker.run_queued('tbl:r', failing)
        self.assertEqual(self.queue()['tbl:r']['status'], 'gave_up')
        self.assertEqual(worker.due_items(NOW + 10**6), [])

    def test_success_marks_done_and_is_not_run_again(self):
        self.put()
        ok = MagicMock()
        self.assertTrue(worker.run_queued('tbl:r', ok))
        self.assertEqual(self.queue()['tbl:r']['status'], 'done')
        self.assertEqual(worker.due_items(NOW + 10**6), [])

    def test_interrupted_link_job_is_parked_for_a_human_not_rerun(self):
        self.put(kind='link', url='https://v.douyin.com/a/', status='running',
                 attempts=1, started_at=NOW - worker.STALE_RUNNING_SECONDS - 1)
        with self.assertLogs('uvicorn.error', level='ERROR') as logs:
            worker.reconcile_interrupted()
        entry = self.queue()['tbl:r']
        self.assertEqual(entry['status'], 'gave_up')
        self.assertIn('未知', entry['last_error'])
        self.assertIn('人工核对', logs.output[0])
        self.assertEqual(worker.due_items(NOW + 10**6), [])

    def test_fresh_running_link_job_is_left_alone(self):
        self.put(kind='link', url='https://v.douyin.com/a/', status='running',
                 attempts=1, started_at=NOW)
        worker.reconcile_interrupted()
        self.assertEqual(self.queue()['tbl:r']['status'], 'running')

    def test_stale_running_link_job_is_never_due_even_if_not_reconciled(self):
        self.put(kind='link', url='https://v.douyin.com/a/', status='running',
                 attempts=1, started_at=0)
        self.assertEqual(worker.due_items(NOW), [])

    def test_restart_while_running_is_resumed_only_after_it_is_stale(self):
        self.put(status='running', attempts=1, started_at=NOW)
        self.assertEqual(worker.due_items(NOW + 60), [])
        self.assertEqual(len(worker.due_items(NOW + worker.STALE_RUNNING_SECONDS)), 1)

    def test_retry_budget_counts_stale_restarts(self):
        self.put(status='running', attempts=3, started_at=NOW - 10**5)
        self.assertEqual(worker.due_items(NOW), [])

    def test_busy_lock_leaves_work_queued_instead_of_dropping_it(self):
        self.put()
        lock = threading.Lock()
        lock.acquire()
        stop = threading.Event()
        with patch.object(worker, 'scan_once'), patch.object(worker, 'run_queued') as run, \
                patch.object(stop, 'wait', side_effect=lambda _t: stop.set()):
            worker.watch_forever(lock, stop, interval=0)
        run.assert_not_called()
        self.assertEqual(self.queue()['tbl:r']['status'], 'pending')
        self.assertEqual(len(worker.due_items(NOW)), 1)

    def test_two_runs_cannot_overlap_on_one_lock(self):
        self.put()
        lock = threading.Lock()
        stop = threading.Event()
        calls = []
        with patch.object(worker, 'scan_once'), \
                patch.object(worker, 'run_queued', side_effect=lambda k: calls.append(k)), \
                patch.object(stop, 'wait', side_effect=lambda _t: stop.set()):
            worker.watch_forever(lock, stop, interval=0)
        self.assertEqual(calls, ['tbl:r'])
        self.assertFalse(lock.locked())


class PaginationTests(unittest.TestCase):
    def test_pages_keeps_extra_params_on_every_page_and_follows_tokens(self):
        from feishu_sync import Feishu
        client = Feishu.__new__(Feishu)
        client.base = '/b'
        calls = []
        replies = iter([
            {'data': {'items': [{'record_id': '1'}], 'has_more': True, 'page_token': 't1'}},
            {'data': {'items': [{'record_id': '2'}], 'has_more': True, 'page_token': 't2'}},
            {'data': {'items': [{'record_id': '3'}], 'has_more': False}},
        ])

        def request(method, url, params=None, **kw):
            calls.append((method, dict(params)))
            return next(replies)

        client.request = request
        items = list(client.pages('/records', {'automatic_fields': 'true'}))
        self.assertEqual([i['record_id'] for i in items], ['1', '2', '3'])
        self.assertEqual([c[0] for c in calls], ['GET'] * 3)
        self.assertTrue(all(c[1]['automatic_fields'] == 'true' and c[1]['page_size'] == 100
                            for c in calls))
        self.assertEqual([c[1].get('page_token') for c in calls], [None, 't1', 't2'])

    def test_pages_without_extra_params_is_unchanged(self):
        from feishu_sync import Feishu
        client = Feishu.__new__(Feishu)
        client.base = '/b'
        seen = []
        client.request = lambda m, u, params=None, **k: (
            seen.append(dict(params)) or {'data': {'items': [], 'has_more': False}})
        list(client.pages('/fields'))
        self.assertEqual(seen, [{'page_size': 100}])


class FakeFeishu:
    """In-memory Feishu table: records every PUT so the test can inspect writes."""
    def __init__(self, fields):
        self.fields = dict(fields)
        self.puts = []
        self.base = '/b'

    def pages(self, path):
        if path == '/fields':
            return iter([{'field_name': n, 'type': 1} for n in
                         ['口播稿', '改写一', '改写二', '改写标题', '标签', '简介']])
        return iter([])

    def request(self, method, url, params=None, json=None):
        if method == 'PUT':
            self.puts.append(json['fields'])
            self.fields.update(json['fields'])
            return {}
        if url.endswith('/records'):
            return {'data': {'items': [{'record_id': 'rec', 'fields': dict(self.fields)}],
                             'has_more': False}}
        return {'data': {'record': {'fields': dict(self.fields)}}}


class RewriteScriptLinkTolerance(unittest.TestCase):
    def test_record_without_video_link_is_rewritten_end_to_end(self):
        import run_feishu_one as one
        source = '这是一段很短的口播稿' * 10
        fake = FakeFeishu({'标题': 't', '口播稿': source})
        out = 'x' * int(len(source) * 0.9)
        meta = {'标题': '一' * 18, '标签': '#a #b #c #d #e', '简介': '简介一句话'}
        with patch.object(one, 'Feishu', return_value=fake), \
                patch.object(one, '_one_version', return_value=out), \
                patch.object(one, 'make_meta', return_value=meta), \
                patch.object(one.anthropic, 'Anthropic'), \
                patch.object(sys, 'argv', ['x', '--record', 'rec', '--table', 'tbl']), \
                patch.object(one.Path, 'write_text'), \
                patch.object(one.Path, 'replace'), \
                patch.object(one.Path, 'exists', return_value=False):
            one.main()
        self.assertTrue(all('视频链接' not in put for put in fake.puts))
        self.assertEqual(fake.fields['改写一'], out)
        self.assertEqual(fake.fields['口播稿'], source)

    def test_existing_rewrite_one_is_kept_by_the_rewrite_script(self):
        import run_feishu_one as one
        source = '这是一段很短的口播稿' * 10
        fake = FakeFeishu({'口播稿': source, '改写一': '人工写的版本'})
        out = 'y' * int(len(source) * 0.9)
        meta = {'标题': '一' * 18, '标签': '#a #b #c #d #e', '简介': '简介一句话'}
        with patch.object(one, 'Feishu', return_value=fake), \
                patch.object(one, '_one_version', return_value=out), \
                patch.object(one, 'make_meta', return_value=meta), \
                patch.object(one.anthropic, 'Anthropic'), \
                patch.object(sys, 'argv', ['x', '--record', 'rec', '--table', 'tbl']), \
                patch.object(one.Path, 'write_text'), \
                patch.object(one.Path, 'replace'), \
                patch.object(one.Path, 'exists', return_value=False):
            one.main()
        self.assertEqual(fake.fields['改写一'], '人工写的版本')
        self.assertTrue(all('改写一' not in put for put in fake.puts))


if __name__ == '__main__':
    unittest.main()
