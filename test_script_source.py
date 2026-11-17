"""口播稿 is the script. 改写一/改写二 are optional. Simulated only: no HeyGen, no Feishu."""
import os
import unittest
from unittest.mock import MagicMock, patch

import avatar_video_workflow as flow
import feishu_bot

REVIEWED = '这是第六十七条的口播稿。'


class FakeTable:
    """A tiny in-memory Bitable: GET list, GET one, PUT fields. Records every PUT."""

    def __init__(self, table_id, rows):
        self.table_id = table_id
        self.base = '/b/' + table_id
        self.rows = rows
        self.puts = []

    def request(self, method, url, params=None, json=None, **kw):
        if method == 'PUT':
            rid = url.rsplit('/', 1)[1]
            self.puts.append((rid, dict(json['fields'])))
            next(r for r in self.rows if r['record_id'] == rid)['fields'].update(json['fields'])
            return {}
        if url.endswith('/records'):
            return {'data': {'items': self.rows, 'has_more': False}}
        rid = url.rsplit('/', 1)[1]
        return {'data': {'record': next(r for r in self.rows if r['record_id'] == rid)}}


def row(rid, **fields):
    return {'record_id': rid, 'fields': fields}


def leo_rows(script=REVIEWED, **extra):
    """66 older complete records, then record 67 with only 标题 + 口播稿."""
    rows = [row(f'old{i}', **{'标题': f'旧{i}', '口播稿': f'旧稿{i}', '改写一': f'旧改写{i}'})
            for i in range(1, 67)]
    rows.append(row('reczz28aZcalt1wn', **{'标题': '第67条', '口播稿': script, **extra}))
    return rows


ENV = {'LEO_TABLE_ID': 'tbl_leo', 'LIBBY_TABLE_ID': 'tbl_libby', 'CHEN_TABLE_ID': 'tbl_chen',
       'LEO_VIEW_ID': '', 'FEISHU_VIEW_ID': '', 'AVATAR_VIDEO_SERVICE_URL': 'https://svc.invalid',
       'AVATAR_VIDEO_SERVICE_SECRET': 's', 'PUBLIC_BASE_URL': 'https://bot.invalid'}


class Base(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.state = tempfile.mkdtemp()
        patcher = patch.dict(os.environ, {**ENV, 'STATE_DIR': self.state})
        patcher.start()
        self.addCleanup(patcher.stop)


class ScriptRecordTests(Base):
    def test_record_67_is_found_by_position_and_uses_the_script_column(self):
        table = FakeTable('tbl_leo', leo_rows())
        record = flow.script_record(table, 67)
        self.assertEqual(record['record_id'], 'reczz28aZcalt1wn')
        self.assertEqual(record['script'], REVIEWED)
        self.assertEqual(record['script_hash'], flow.script_digest(REVIEWED))

    def test_empty_rewrite_one_never_blocks(self):
        for fields in ({'口播稿': '稿'}, {'口播稿': '稿', '改写一': ''}, {'口播稿': '稿', '改写二': '二'}):
            record = flow.script_record(FakeTable('t', [row('r', **fields)]), 1)
            self.assertEqual(record['script'], '稿')

    def test_old_record_with_both_columns_uses_the_script_not_the_rewrite(self):
        table = FakeTable('t', [row('r', **{'口播稿': '原稿', '改写一': '改写版', '改写二': '改写二版'})])
        self.assertEqual(flow.script_record(table, 1)['script'], '原稿')

    def test_empty_script_is_a_clear_error_and_never_substitutes_another_field(self):
        for fields in ({}, {'口播稿': ''}, {'口播稿': '   ', '改写一': '改写版', '改写二': '二'}):
            with self.assertRaisesRegex(RuntimeError, '“口播稿”是空的'):
                flow.script_record(FakeTable('t', [row('r', **fields)]), 1)

    def test_all_three_libraries_resolve_from_their_own_table(self):
        for key, table_id in (('avatar_b', 'tbl_leo'), ('avatar_libby', 'tbl_libby'),
                              ('avatar_a', 'tbl_chen')):
            source = flow.avatar_source(key)
            self.assertEqual(source['table_id'], table_id)
            table = FakeTable(table_id, [row('r1', **{'口播稿': table_id + '的稿'})])
            self.assertEqual(flow.script_record(table, 1, source['view_id'], source['library'])['script'],
                             table_id + '的稿')


class ReviewCardTests(Base):
    def send(self, rows, message_id=''):
        table = FakeTable('tbl_leo', rows)
        with patch.object(feishu_bot, 'Feishu', return_value=table), \
                patch.object(feishu_bot, 'send_card') as send, \
                patch.object(feishu_bot, 'update_card') as update:
            feishu_bot.send_rewrite_review('oc', 67, 'reczz28aZcalt1wn', message_id)
        return send, update

    def test_review_shows_the_script_and_binds_approval_to_it(self):
        send, _ = self.send(leo_rows())
        card = send.call_args.args[1]
        rendered = str(card)
        self.assertIn(REVIEWED, rendered)
        buttons = [b for e in card['elements'] if e.get('tag') == 'action' for b in e['actions']]
        self.assertEqual([b['value']['action'] for b in buttons], ['approve_rewrite'])
        self.assertEqual(buttons[0]['value']['script_hash'], flow.script_digest(REVIEWED))

    def test_review_of_a_record_with_empty_script_errors_instead_of_using_a_rewrite(self):
        rows = leo_rows(script='', **{'改写一': '不应被使用的改写'})
        with self.assertRaisesRegex(RuntimeError, '「口播稿」是空的'):
            self.send(rows)

    def test_long_script_is_previewed_with_its_full_length(self):
        card = flow.rewrite_review_card(67, 'r', 't', '字' * 2000)
        self.assertIn('共 2000 字', str(card))


class ApprovalToGenerationConsistencyTests(Base):
    def approve(self, table, script_hash):
        source = {'table_id': 'tbl_leo', 'view_id': '', 'library': ''}
        with patch.object(feishu_bot, 'avatar_source', return_value=source), \
                patch.object(feishu_bot, 'Feishu', return_value=table), \
                patch.object(feishu_bot, 'send_leo_picker') as picker, \
                patch.object(feishu_bot, 'show_blocked') as blocked, \
                patch.object(feishu_bot.subprocess, 'run') as run:
            feishu_bot.approve_rewrite('oc', {
                'script_number': 67, 'record_id': 'reczz28aZcalt1wn', 'script_hash': script_hash,
                'message_id': 'om'})
        run.assert_not_called()        # approving never triggers a rewrite
        return picker, blocked

    def test_approving_the_reviewed_text_continues_and_carries_the_hash(self):
        table = FakeTable('tbl_leo', leo_rows())
        picker, blocked = self.approve(table, flow.script_digest(REVIEWED))
        blocked.assert_not_called()
        self.assertEqual(picker.call_args.kwargs['script_hash'], flow.script_digest(REVIEWED))
        self.assertEqual(table.puts, [('reczz28aZcalt1wn', {'生成状态': '文案已确认'})])

    def test_text_edited_after_the_review_card_is_not_approved(self):
        table = FakeTable('tbl_leo', leo_rows(script='审核之后被改过的稿'))
        picker, blocked = self.approve(table, flow.script_digest(REVIEWED))
        picker.assert_not_called()
        self.assertIn('被修改过', blocked.call_args.args[4])
        self.assertEqual(table.puts, [])

    def test_old_review_card_without_a_hash_is_refused(self):
        picker, blocked = self.approve(FakeTable('tbl_leo', leo_rows()), '')
        picker.assert_not_called()
        self.assertIn('版本过旧', blocked.call_args.args[4])

    def confirm(self, table, action_hash, key='leo_avatar_1', source=None):
        source = source or {'table_id': 'tbl_leo', 'view_id': '', 'library': ''}
        with patch.object(feishu_bot, 'validate_avatar_binding'), \
                patch.object(feishu_bot, 'avatar_source', return_value=source), \
                patch.object(feishu_bot, 'Feishu', return_value=table), \
                patch.object(feishu_bot, 'show_card') as show, \
                patch.object(feishu_bot, 'show_blocked') as blocked:
            feishu_bot.send_avatar_confirmation('oc', {
                'action': 'select_leo_avatar', 'script_number': 67, 'avatar_key': key,
                'script_hash': action_hash, 'message_id': 'om'})
        return show, blocked

    def test_confirmation_shows_the_exact_script_and_binds_generation_to_it(self):
        table = FakeTable('tbl_leo', leo_rows(**{'生成状态': '文案已确认'}))
        flow.record_approval('reczz28aZcalt1wn', flow.script_digest(REVIEWED))
        show, blocked = self.confirm(table, flow.script_digest(REVIEWED))
        blocked.assert_not_called()
        card = show.call_args.args[2]
        self.assertIn(REVIEWED, str(card))
        buttons = [b for e in card['elements'] if e.get('tag') == 'action' for b in e['actions']]
        generate = next(b for b in buttons if b['value']['action'] == 'generate_avatar_video')
        self.assertEqual(generate['value']['script_hash'], flow.script_digest(REVIEWED))

    def test_text_edited_between_approval_and_confirmation_is_blocked(self):
        table = FakeTable('tbl_leo', leo_rows(script='改过了', **{'生成状态': '文案已确认'}))
        show, blocked = self.confirm(table, flow.script_digest(REVIEWED))
        show.assert_not_called()
        self.assertIn('被修改过', blocked.call_args.args[4])

    def test_libby_and_chen_have_no_review_step_and_still_show_the_script(self):
        for key, table_id in (('avatar_libby', 'tbl_libby'), ('avatar_a', 'tbl_chen')):
            table = FakeTable(table_id, [row('r1', **{'标题': 't', '口播稿': table_id + '稿'})])
            source = {'table_id': table_id, 'view_id': '', 'library': ''}
            with patch.object(feishu_bot, 'validate_avatar_binding'), \
                    patch.object(feishu_bot, 'avatar_source', return_value=source), \
                    patch.object(feishu_bot, 'Feishu', return_value=table), \
                    patch.object(feishu_bot, 'show_card') as show:
                feishu_bot.send_avatar_confirmation('oc', {
                    'action': 'select_avatar_script', 'script_number': 1, 'avatar_key': key,
                    'message_id': 'om'})
            self.assertIn(flow.script_preview(table_id + '稿'), str(show.call_args.args[2]))


class SubmittedTextIsTheApprovedTextTests(Base):
    def submit(self, table, action_hash, key='leo_avatar_1', status='文案已确认'):
        for r in table.rows:
            if r['record_id'] == 'reczz28aZcalt1wn':
                r['fields']['生成状态'] = status
        response = MagicMock(ok=True)
        response.json.return_value = {'job_id': 'job1'}
        with patch('heygen_video_service.validate_avatar_binding'), \
                patch.object(flow.requests, 'post', return_value=response) as post:
            error = None
            try:
                result = flow.submit_video_job(table, {
                    'avatar_key': key, 'script_number': 67, 'script_hash': action_hash,
                    'chat_id': 'oc'})
            except flow.ScriptChangedError as exc:
                result, error = None, exc
        return result, error, post

    def test_submitted_script_is_exactly_the_reviewed_script(self):
        table = FakeTable('tbl_leo', leo_rows(**{'改写一': '另一版不应被提交'}))
        flow.record_approval('reczz28aZcalt1wn', flow.script_digest(REVIEWED))
        result, error, post = self.submit(table, flow.script_digest(REVIEWED))
        self.assertIsNone(error)
        self.assertFalse(result['duplicate'])
        self.assertEqual(post.call_args.kwargs['json']['script'], REVIEWED)
        self.assertEqual(post.call_count, 1)

    def test_original_script_is_never_overwritten(self):
        table = FakeTable('tbl_leo', leo_rows())
        flow.record_approval('reczz28aZcalt1wn', flow.script_digest(REVIEWED))
        self.submit(table, flow.script_digest(REVIEWED))
        for _rid, fields in table.puts:
            self.assertEqual(set(fields), {'生成状态'})
        self.assertEqual(table.rows[-1]['fields']['口播稿'], REVIEWED)

    def test_text_changed_after_confirmation_blocks_before_any_paid_call_or_status_write(self):
        table = FakeTable('tbl_leo', leo_rows(script='确认之后被偷偷改了'))
        result, error, post = self.submit(table, flow.script_digest(REVIEWED))
        self.assertIsNone(result)
        self.assertIn('被修改过', str(error))
        post.assert_not_called()
        self.assertEqual(table.puts, [])
        self.assertEqual(table.rows[-1]['fields']['生成状态'], '文案已确认')

    def test_old_confirmation_card_without_a_hash_cannot_start_paid_generation(self):
        table = FakeTable('tbl_leo', leo_rows())
        result, error, post = self.submit(table, '')
        self.assertIsNone(result)
        self.assertIn('版本过旧', str(error))
        post.assert_not_called()
        self.assertEqual(table.puts, [])

    def test_empty_script_blocks_and_never_submits_a_rewrite(self):
        table = FakeTable('tbl_leo', leo_rows(script='', **{'改写一': '改写版'}))
        result, error, post = self.submit(table, flow.script_digest('改写版'))
        self.assertIsNone(result)
        self.assertIn('口播稿', str(error))
        post.assert_not_called()

    def test_leo_still_requires_human_approval(self):
        table = FakeTable('tbl_leo', leo_rows())
        with self.assertRaisesRegex(RuntimeError, '尚未人工确认'):
            self.submit(table, flow.script_digest(REVIEWED), status='待确认')

    def test_libby_and_chen_submit_their_own_script(self):
        for key, table_id in (('avatar_libby', 'tbl_libby'), ('avatar_a', 'tbl_chen')):
            table = FakeTable(table_id, [row('reczz28aZcalt1wn', **{'标题': 't', '口播稿': table_id + '稿'})])
            with patch('heygen_video_service.validate_avatar_binding'), \
                    patch.object(flow.requests, 'post') as post:
                post.return_value = MagicMock(ok=True, **{'json.return_value': {'job_id': 'j'}})
                flow.submit_video_job(table, {'avatar_key': key, 'script_number': 1,
                                              'script_hash': flow.script_digest(table_id + '稿')})
            self.assertEqual(post.call_args.kwargs['json']['script'], table_id + '稿')

    def test_changed_script_error_reaches_the_user_as_a_clear_reason(self):
        table = FakeTable('tbl_leo', leo_rows(script='改了'))
        source = {'table_id': 'tbl_leo', 'view_id': '', 'library': ''}
        with patch.object(feishu_bot, 'avatar_source', return_value=source), \
                patch.object(feishu_bot, 'Feishu', return_value=table), \
                patch('heygen_video_service.validate_avatar_binding'), \
                patch.object(feishu_bot, 'safely_update_card') as update, \
                patch.object(feishu_bot, 'send_text') as text:
            table.rows[-1]['fields']['生成状态'] = '文案已确认'
            feishu_bot.run_avatar_selection('oc', {
                'avatar_key': 'leo_avatar_1', 'script_number': 67, 'chat_id': 'oc',
                'script_hash': flow.script_digest(REVIEWED), 'message_id': 'om'})
        self.assertIn('被修改过', text.call_args.args[1])
        self.assertIn('被修改过', str(update.call_args.args[1]))


class LegacyAndPreviewTests(Base):
    def test_old_redo_button_does_nothing_and_spends_nothing(self):
        with patch.object(feishu_bot, 'send_text') as text,                 patch.object(feishu_bot.subprocess, 'run') as run,                 patch.object(feishu_bot, 'Feishu') as feishu,                 patch.object(feishu_bot, 'send_card') as card:
            feishu_bot.redo_rewrite('oc', {'script_number': 67, 'record_id': 'r'})
        run.assert_not_called()
        feishu.assert_not_called()
        card.assert_not_called()
        self.assertIn('已停用', text.call_args.args[1])

    def test_old_leo_picker_without_hash_still_shows_current_text_and_a_fresh_hash(self):
        table = FakeTable('tbl_leo', leo_rows(**{'生成状态': '文案已确认'}))
        flow.record_approval('reczz28aZcalt1wn', flow.script_digest(REVIEWED))
        source = {'table_id': 'tbl_leo', 'view_id': '', 'library': ''}
        with patch.object(feishu_bot, 'validate_avatar_binding'),                 patch.object(feishu_bot, 'avatar_source', return_value=source),                 patch.object(feishu_bot, 'Feishu', return_value=table),                 patch.object(feishu_bot, 'show_card') as show:
            feishu_bot.send_avatar_confirmation('oc', {
                'action': 'select_leo_avatar', 'script_number': 67,
                'avatar_key': 'leo_avatar_1', 'script_hash': '', 'message_id': 'om'})
        self.assertIn(REVIEWED, str(show.call_args.args[2]))

    def test_markup_characters_in_a_script_cannot_become_card_markup(self):
        nasty = '看<at id=all></at> [点我](http://x) `代码` *粗* _斜_ <font color="red">'
        preview = flow.script_preview(nasty)
        for ch in '<>`*_[]':
            self.assertNotIn(ch, preview)
        self.assertIn('点我', preview)
        # the hash still covers the real, unmodified text
        self.assertEqual(flow.script_digest(nasty), flow.script_digest(nasty.strip()))


class ApprovalInvalidationTests(Base):
    RID = 'reczz28aZcalt1wn'

    def route(self, table):
        source = {'table_id': 'tbl_leo', 'view_id': '', 'library': ''}
        with patch.object(feishu_bot, 'avatar_source', return_value=source), \
                patch.object(feishu_bot, 'Feishu', return_value=table), \
                patch.object(feishu_bot, 'send_rewrite_review') as review, \
                patch.object(feishu_bot, 'send_leo_picker') as picker, \
                patch.object(feishu_bot, 'send_text'):
            feishu_bot.route_leo_selection('oc', 67, 'om')
        return review, picker

    def approve_flow(self, table, text):
        source = {'table_id': 'tbl_leo', 'view_id': '', 'library': ''}
        with patch.object(feishu_bot, 'avatar_source', return_value=source), \
                patch.object(feishu_bot, 'Feishu', return_value=table), \
                patch.object(feishu_bot, 'send_leo_picker') as picker, \
                patch.object(feishu_bot, 'show_blocked') as blocked:
            feishu_bot.approve_rewrite('oc', {
                'script_number': 67, 'record_id': self.RID,
                'script_hash': flow.script_digest(text), 'message_id': 'om'})
        return picker, blocked

    def test_unchanged_approved_text_goes_straight_to_the_picker(self):
        table = FakeTable('tbl_leo', leo_rows())
        picker, _ = self.approve_flow(table, REVIEWED)
        picker.assert_called_once()
        review, picker = self.route(table)
        review.assert_not_called()
        picker.assert_called_once()

    def test_edit_after_approval_invalidates_it_and_requires_a_new_review(self):
        table = FakeTable('tbl_leo', leo_rows())
        self.approve_flow(table, REVIEWED)
        table.rows[-1]['fields']['口播稿'] = '审核通过之后被改掉的新稿'
        self.assertEqual(table.rows[-1]['fields']['生成状态'], '文案已确认')   # stale status
        review, picker = self.route(table)
        picker.assert_not_called()
        review.assert_called_once()

    def test_old_approval_cannot_be_reused_to_submit_a_changed_text(self):
        table = FakeTable('tbl_leo', leo_rows())
        self.approve_flow(table, REVIEWED)
        table.rows[-1]['fields']['口播稿'] = '新稿'
        sub = SubmittedTextIsTheApprovedTextTests('test_leo_still_requires_human_approval')
        # even a card that carries the NEW hash is refused: the new text was never approved
        result, error, post = sub.submit(table, flow.script_digest('新稿'))
        self.assertIsNone(result)
        self.assertIn('还没有被人工确认', str(error))
        post.assert_not_called()
        self.assertEqual([p for p in table.puts if '生成状态' in p[1] and p[1]['生成状态'] == '生成中'], [])

    def test_confirmation_card_is_refused_for_text_that_was_not_approved(self):
        table = FakeTable('tbl_leo', leo_rows(**{'生成状态': '文案已确认'}))
        flow.record_approval(self.RID, flow.script_digest('别的版本'))
        probe = ApprovalToGenerationConsistencyTests('test_old_review_card_without_a_hash_is_refused')
        show, blocked = probe.confirm(table, flow.script_digest(REVIEWED))
        show.assert_not_called()
        self.assertIn('还没有被人工确认', blocked.call_args.args[4])

    def test_status_alone_is_not_an_approval_for_legacy_records(self):
        table = FakeTable('tbl_leo', leo_rows(**{'生成状态': '文案已确认'}))   # no stored approval
        review, picker = self.route(table)
        picker.assert_not_called()
        review.assert_called_once()

    def test_re_approving_the_new_text_works_and_the_old_text_stays_unapproved(self):
        table = FakeTable('tbl_leo', leo_rows())
        self.approve_flow(table, REVIEWED)
        table.rows[-1]['fields']['口播稿'] = '新稿'
        picker, blocked = self.approve_flow(table, '新稿')
        picker.assert_called_once()
        self.assertEqual(flow.approved_digest(self.RID), flow.script_digest('新稿'))
        self.assertNotEqual(flow.approved_digest(self.RID), flow.script_digest(REVIEWED))

    def test_failed_job_with_unchanged_approved_text_can_be_resubmitted_by_the_user(self):
        table = FakeTable('tbl_leo', leo_rows())
        flow.record_approval(self.RID, flow.script_digest(REVIEWED))
        sub = SubmittedTextIsTheApprovedTextTests('test_leo_still_requires_human_approval')
        result, error, _post = sub.submit(table, flow.script_digest(REVIEWED), status='失败')
        self.assertIsNone(error)
        self.assertFalse(result['duplicate'])

    def test_approval_store_fails_closed_when_corrupt_and_recovers_on_next_write(self):
        with open(os.path.join(self.state, 'approved_scripts.json'), 'w', encoding='utf-8') as fh:
            fh.write('{broken')
        self.assertEqual(flow.approved_digest(self.RID), '')
        flow.record_approval(self.RID, 'abc')
        self.assertEqual(flow.approved_digest(self.RID), 'abc')

    def test_approval_is_written_before_the_table_status(self):
        order = []
        table = FakeTable('tbl_leo', leo_rows())
        real_put = table.request

        def spy(method, url, **kw):
            if method == 'PUT':
                order.append(('put', flow.approved_digest(self.RID)))
            return real_put(method, url, **kw)

        table.request = spy
        self.approve_flow(table, REVIEWED)
        self.assertEqual(order, [('put', flow.script_digest(REVIEWED))])


class FullTextBeforeApprovalTests(Base):
    RID = 'reczz28aZcalt1wn'

    def long_script(self, n=2000):
        return ('这是很长的一段口播稿，用来测试分页。' * (n // 18 + 1))[:n] + '末尾'

    def review(self, script, send_text_effect=None):
        table = FakeTable('tbl_leo', leo_rows(script=script))
        order = []
        with patch.object(feishu_bot, 'Feishu', return_value=table), \
                patch.object(feishu_bot, 'send_text', side_effect=send_text_effect or (
                    lambda chat, text: order.append(('text', text)))), \
                patch.object(feishu_bot, 'show_card', side_effect=lambda c, m, card: order.append(
                    ('card', card))):
            try:
                feishu_bot.send_rewrite_review('oc', 67, self.RID)
                error = None
            except Exception as exc:
                error = exc
        return order, error

    def test_split_is_exact_and_each_part_fits(self):
        for text in ('甲' * 801, '甲' * 1500, '甲' * 1501, '句。' * 3000,
                     '段落内容\n' * 800, '甲' * 5000, ('一' * 1499 + '。') * 3):
            parts = flow.script_parts(text)
            self.assertEqual(''.join(parts), text.strip())
            self.assertTrue(all(0 < len(p) <= flow.PART_SIZE for p in parts), len(text))

    def test_short_script_is_shown_whole_on_the_card_with_no_extra_messages(self):
        script = '甲' * 800
        order, error = self.review(script)
        self.assertIsNone(error)
        self.assertEqual([k for k, _ in order], ['card'])
        self.assertIn(script, str(order[0][1]))

    def test_script_over_the_limit_is_delivered_in_full_before_the_approve_button(self):
        script = self.long_script(2000)
        order, error = self.review(script)
        self.assertIsNone(error)
        kinds = [k for k, _ in order]
        self.assertEqual(kinds[-1], 'card')
        self.assertTrue(all(k == 'text' for k in kinds[:-1]) and len(kinds) >= 3)
        texts = [v for k, v in order if k == 'text']
        bodies = [t.split('\n', 1)[1] for t in texts]
        self.assertEqual(''.join(bodies), script.strip())          # nothing missing, in order
        self.assertIn('第 1/', texts[0])
        card = str(order[-1][1])
        self.assertIn('approve_rewrite', card)
        self.assertIn(f'共 {len(script.strip())} 字', card)
        self.assertNotIn('……', card)                                # never a truncated preview
        self.assertNotIn(script.strip()[:30], card)                # partial text is not on the card

    def test_boundary_800_vs_801(self):
        self.assertEqual([k for k, _ in self.review('甲' * 800)[0]], ['card'])
        self.assertGreater(len(self.review('甲' * 801)[0]), 1)

    def test_if_the_full_text_cannot_be_sent_no_approve_button_is_shown(self):
        def boom(chat, text):
            raise RuntimeError('飞书发送失败')
        order, error = self.review(self.long_script(), send_text_effect=boom)
        self.assertIsNotNone(error)
        self.assertEqual(order, [])

    def test_approval_hash_covers_the_whole_long_text_not_a_prefix(self):
        script = self.long_script(3000)
        card = flow.rewrite_review_card(67, self.RID, 't', script)
        button = [b for e in card['elements'] if e.get('tag') == 'action' for b in e['actions']][0]
        self.assertEqual(button['value']['script_hash'], flow.script_digest(script))
        self.assertNotEqual(button['value']['script_hash'], flow.script_digest(script[:800]))

    def test_libby_and_chen_get_the_full_long_text_before_the_confirmation_card(self):
        script = self.long_script(2500)
        for key, table_id in (('avatar_libby', 'tbl_libby'), ('avatar_a', 'tbl_chen')):
            table = FakeTable(table_id, [row('r1', **{'标题': 't', '口播稿': script})])
            source = {'table_id': table_id, 'view_id': '', 'library': ''}
            order = []
            with patch.object(feishu_bot, 'validate_avatar_binding'), \
                    patch.object(feishu_bot, 'avatar_source', return_value=source), \
                    patch.object(feishu_bot, 'Feishu', return_value=table), \
                    patch.object(feishu_bot, 'send_text',
                                 side_effect=lambda c, t: order.append(('text', t))), \
                    patch.object(feishu_bot, 'show_card',
                                 side_effect=lambda c, m, card: order.append(('card', card))):
                feishu_bot.send_avatar_confirmation('oc', {
                    'action': 'select_avatar_script', 'script_number': 1, 'avatar_key': key,
                    'message_id': 'om'})
            self.assertEqual(order[-1][0], 'card')
            bodies = [t.split('\n', 1)[1] for k, t in order if k == 'text']
            self.assertEqual(''.join(bodies), script.strip())
            self.assertNotIn(script.strip()[:30], str(order[-1][1]))

    def test_leo_confirmation_does_not_resend_the_text_but_requires_the_approval(self):
        script = self.long_script(2500)
        table = FakeTable('tbl_leo', leo_rows(script=script, **{'生成状态': '文案已确认'}))
        flow.record_approval(self.RID, flow.script_digest(script))
        source = {'table_id': 'tbl_leo', 'view_id': '', 'library': ''}
        with patch.object(feishu_bot, 'validate_avatar_binding'), \
                patch.object(feishu_bot, 'avatar_source', return_value=source), \
                patch.object(feishu_bot, 'Feishu', return_value=table), \
                patch.object(feishu_bot, 'send_text') as text, \
                patch.object(feishu_bot, 'show_card') as show:
            feishu_bot.send_avatar_confirmation('oc', {
                'action': 'select_leo_avatar', 'script_number': 67, 'avatar_key': 'leo_avatar_1',
                'script_hash': flow.script_digest(script), 'message_id': 'om'})
        text.assert_not_called()
        self.assertIn('generate_avatar_video', str(show.call_args.args[2]))


if __name__ == '__main__':
    unittest.main()
