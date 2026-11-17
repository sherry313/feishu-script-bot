"""Card redesign: one layout for every step, buttons unchanged, in-place updates
with a safe fallback. Fully mocked: no network, no paid API, no real state."""
import unittest
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks

import avatar_video_workflow as flow
import feishu_bot


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


RECORD = {'number': 11, 'title': '春季养生三要点', 'script': '测' * 336, 'status': ''}


def all_cards():
    """One sample of every card the workflow can show."""
    cards = {
        'pick': flow.avatar_card(11),
        'leo_avatar': flow.leo_avatar_card(11),
        'review': flow.rewrite_review_card(11, 'rec_11', '春季养生三要点'),
        'approved': flow.locked_rewrite_review_card(11, True),
        'redo': flow.locked_rewrite_review_card(11, False),
        'check': flow.locked_script_card(11, 'avatar_libby'),
        'avatar_locked': flow.locked_leo_avatar_card(11, 'leo_avatar_2'),
        'confirm_leo': flow.confirmation_card(RECORD, 'leo_avatar_2'),
        'confirm_libby': flow.confirmation_card(RECORD, 'avatar_libby'),
        'validating': flow.validating_card(11, 'avatar_libby'),
        'submitted': flow.submitted_card(11, 'avatar_libby'),
        'rejected': flow.rejected_card(11, 'avatar_libby', '数字人已失效。'),
        'cancelled': flow.cancelled_card(11, 'avatar_libby'),
        'notice': flow.notice_card(11, 'avatar_libby', '这条任务已经是生成中。'),
        'blocked': flow.blocked_card(11, 'avatar_libby', '当前视图只有 1 条，找不到脚本 11。'),
    }
    return cards


def actions(card):
    return [item for element in card['elements'] if element.get('tag') == 'action'
            for item in element['actions']]


class SameFrameTests(unittest.TestCase):
    def test_every_card_has_the_same_frame(self):
        for name, card in all_cards().items():
            self.assertEqual(card['header']['title']['content'], '口播脚本 · 编号 11', name)
            self.assertTrue(card['config']['update_multi'], name)
            self.assertTrue(card['config']['wide_screen_mode'], name)
            elements = card['elements']
            self.assertEqual(elements[0]['tag'], 'markdown', name)
            self.assertIn('脚本库', elements[0]['content'], name)           # progress line first
            self.assertEqual(elements[1], {'tag': 'hr'}, name)
            if actions(card):                                               # buttons last, after a divider
                self.assertEqual(elements[-1]['tag'], 'action', name)
                self.assertEqual(elements[-2], {'tag': 'hr'}, name)
                self.assertEqual(sum(e['tag'] == 'action' for e in elements), 1, name)

    def test_only_components_already_used_in_production_plus_hr(self):
        for name, card in all_cards().items():
            self.assertLessEqual({e['tag'] for e in card['elements']},
                                 {'markdown', 'hr', 'note', 'action'}, name)
            for button in actions(card):
                self.assertIn(button['type'], {'primary', 'default'}, name)
                self.assertEqual(set(button), {'tag', 'text', 'type', 'value'}, name)

    def test_locked_and_result_cards_have_no_buttons(self):
        for name in ('approved', 'redo', 'check', 'avatar_locked', 'validating',
                     'submitted', 'rejected', 'cancelled', 'notice', 'blocked'):
            self.assertEqual(actions(all_cards()[name]), [], name)

    def test_progress_shows_four_steps_for_leo_and_two_for_libby_and_chen(self):
        cards = all_cards()
        self.assertIn('共 4 步', str(cards['review']))
        self.assertIn('共 4 步', str(cards['confirm_leo']))
        self.assertIn('共 2 步', str(cards['confirm_libby']))
        self.assertIn('本库无此步', str(cards['confirm_libby']))
        self.assertNotIn('本库无此步', str(cards['confirm_leo']))
        self.assertIn('共 2 步', str(flow.confirmation_card(RECORD, 'avatar_a')))

    def test_colours_have_one_meaning_each(self):
        cards = all_cards()
        for name, colour in {'pick': 'blue', 'review': 'orange', 'confirm_leo': 'orange',
                             'submitted': 'green', 'rejected': 'red', 'blocked': 'red',
                             'cancelled': 'grey'}.items():
            self.assertEqual(cards[name]['header']['template'], colour, name)


class ButtonsAreUnchangedTests(unittest.TestCase):
    """Cards already sent keep working only if every button payload stays identical."""

    def test_picker_payloads(self):
        got = [b['value'] for b in actions(flow.avatar_card(11))]
        self.assertEqual(got, [
            {'action': 'select_avatar_script', 'script_number': 11, 'avatar_key': 'avatar_b'},
            {'action': 'select_avatar_script', 'script_number': 11, 'avatar_key': 'avatar_libby'},
            {'action': 'select_avatar_script', 'script_number': 11, 'avatar_key': 'avatar_a'},
        ])

    def test_leo_avatar_payloads(self):
        got = [b['value'] for b in actions(flow.leo_avatar_card(11))]
        self.assertEqual(got, [{'action': 'select_leo_avatar', 'script_number': 11,
                                'avatar_key': f'leo_avatar_{n}'} for n in (1, 2, 3)])

    def test_review_payloads(self):
        got = [b['value'] for b in actions(flow.rewrite_review_card(11, 'rec_11', 't'))]
        self.assertEqual(got, [
            {'action': 'approve_rewrite', 'script_number': 11, 'record_id': 'rec_11'},
            {'action': 'redo_rewrite', 'script_number': 11, 'record_id': 'rec_11'},
        ])

    def test_confirmation_payloads(self):
        got = [b['value'] for b in actions(flow.confirmation_card(RECORD, 'avatar_libby'))]
        self.assertEqual(got, [
            {'action': 'generate_avatar_video', 'script_number': 11, 'avatar_key': 'avatar_libby'},
            {'action': 'cancel_avatar_video', 'script_number': 11, 'avatar_key': 'avatar_libby'},
        ])

    def test_every_payload_is_still_accepted_by_the_parser(self):
        for name, card in all_cards().items():
            for button in actions(card):
                payload = {'event': {'action': {'value': button['value']},
                                     'context': {'open_chat_id': 'oc', 'open_message_id': 'om'}}}
                self.assertIsNotNone(flow.parse_card_action(payload), name)

    def test_only_the_cost_confirmation_card_can_start_paid_generation(self):
        for name, card in all_cards().items():
            has_generate = 'generate_avatar_video' in str(card)
            self.assertEqual(has_generate, name.startswith('confirm_'), name)
        self.assertEqual(len([b for b in actions(all_cards()['confirm_leo'])
                              if b['value']['action'] == 'generate_avatar_video']), 1)

    def test_confirmation_still_shows_cost_and_one_time_warning(self):
        text = str(flow.confirmation_card(RECORD, 'leo_avatar_2'))
        self.assertIn('$1.40', text)
        self.assertIn('不会自动重跑', text)
        self.assertIn('预计时长', text)


class InPlaceUpdateTests(unittest.TestCase):
    CARD = {'header': {}, 'elements': []}

    def test_update_succeeds_so_no_second_card_is_sent(self):
        with patch.object(feishu_bot, 'update_card') as update, \
                patch.object(feishu_bot, 'send_card') as send:
            feishu_bot.show_card('oc', 'om', self.CARD)
        update.assert_called_once_with('om', self.CARD)
        send.assert_not_called()

    def test_update_failure_falls_back_to_a_new_card(self):
        with patch.object(feishu_bot, 'update_card', side_effect=RuntimeError('expired')), \
                patch.object(feishu_bot, 'send_card') as send:
            feishu_bot.show_card('oc', 'om', self.CARD)
        send.assert_called_once_with('oc', self.CARD)

    def test_without_a_clicked_card_a_new_card_is_sent(self):
        with patch.object(feishu_bot, 'update_card') as update, \
                patch.object(feishu_bot, 'send_card') as send:
            feishu_bot.show_card('oc', '', self.CARD)
        update.assert_not_called()
        send.assert_called_once_with('oc', self.CARD)

    def test_blocked_reason_goes_on_the_card_then_new_card_then_text(self):
        reason = '当前视图只有 1 条，找不到脚本 11。'
        with patch.object(feishu_bot, 'update_card') as update, \
                patch.object(feishu_bot, 'send_card') as send, \
                patch.object(feishu_bot, 'send_text') as text:
            feishu_bot.show_blocked('oc', 'om', 11, 'avatar_a', reason, 'legacy text')
        self.assertEqual(update.call_args.args[1]['header']['template'], 'red')
        send.assert_not_called()
        text.assert_not_called()
        with patch.object(feishu_bot, 'update_card', side_effect=RuntimeError('x')), \
                patch.object(feishu_bot, 'send_card') as send, \
                patch.object(feishu_bot, 'send_text') as text:
            feishu_bot.show_blocked('oc', 'om', 11, 'avatar_a', reason, 'legacy text')
        send.assert_called_once()
        text.assert_not_called()
        with patch.object(feishu_bot, 'update_card', side_effect=RuntimeError('x')), \
                patch.object(feishu_bot, 'send_card', side_effect=RuntimeError('y')), \
                patch.object(feishu_bot, 'send_text') as text:
            feishu_bot.show_blocked('oc', 'om', 11, 'avatar_a', reason, 'legacy text')
        text.assert_called_once_with('oc', 'legacy text')


class FlowUsesTheClickedCardTests(unittest.TestCase):
    SOURCE = {'table_id': 'tbl', 'view_id': '', 'library': ''}

    def route(self, status, message_id='om_pick'):
        record = {'record_id': 'rec_11', 'number': 11, 'status': status, 'script': 's', 'title': 't'}
        with patch.object(feishu_bot, 'avatar_source', return_value=self.SOURCE), \
                patch.object(feishu_bot, 'Feishu', MagicMock()), \
                patch.object(feishu_bot, 'script_record', return_value=record), \
                patch.object(feishu_bot, 'validate_avatar_binding'), \
                patch.object(feishu_bot, 'update_card') as update, \
                patch.object(feishu_bot, 'send_card') as send, \
                patch.object(feishu_bot, 'send_text') as text, \
                patch.object(feishu_bot, 'submit_video_job') as submit:
            with patch.object(feishu_bot, 'send_rewrite_review',
                              wraps=lambda chat, number, rid, message_id='': feishu_bot.show_card(
                                  chat, message_id, flow.rewrite_review_card(number, rid, 't'))):
                feishu_bot.route_leo_selection('oc', 11, message_id=message_id)
        submit.assert_not_called()
        return update, send, text

    def test_approved_leo_script_shows_avatars_on_the_same_card(self):
        update, send, text = self.route('文案已确认')
        self.assertEqual(update.call_args.args[0], 'om_pick')
        self.assertIn('select_leo_avatar', str(update.call_args.args[1]))
        send.assert_not_called()
        text.assert_not_called()

    def test_pending_leo_script_shows_review_on_the_same_card_without_extra_text(self):
        update, send, text = self.route('待确认')
        self.assertIn('approve_rewrite', str(update.call_args.args[1]))
        self.assertNotIn('generate_avatar_video', str(update.call_args.args[1]))
        send.assert_not_called()
        text.assert_not_called()

    def test_old_callers_without_a_card_still_get_a_new_card_and_text(self):
        update, send, text = self.route('待确认', message_id='')
        update.assert_not_called()
        send.assert_called_once()
        self.assertIn('等待人工确认', text.call_args.args[1])

    def test_redo_still_returns_to_a_new_review_card_not_the_old_one(self):
        # Re-using the old card would reuse its de-duplication key and make the next
        # "确认文案" click look like a repeated click, so redo must send a NEW card.
        client = MagicMock()
        client.base = '/x'
        client.request.return_value = {'data': {'record': {'fields': {'改写一': '文案', '标题': '标题'}}}}
        with patch.object(feishu_bot, 'Feishu', return_value=client), \
                patch.object(feishu_bot, 'update_card') as update, \
                patch.object(feishu_bot, 'send_card') as send:
            feishu_bot.send_rewrite_review('oc', 11, 'rec_11')
        update.assert_not_called()
        send.assert_called_once()

    def confirm(self, status, key='avatar_libby', message_id='om_pick', error=None, action='select_avatar_script'):
        record = {'record_id': 'rec', 'number': 11, 'title': '春季养生三要点',
                  'script': '测' * 336, 'status': status}
        with patch.object(feishu_bot, 'avatar_source', return_value=self.SOURCE), \
                patch.object(feishu_bot, 'Feishu', MagicMock()), \
                patch.object(feishu_bot, 'script_record', return_value=record, side_effect=error), \
                patch.object(feishu_bot, 'validate_avatar_binding'), \
                patch.object(feishu_bot, 'update_card') as update, \
                patch.object(feishu_bot, 'send_card') as send, \
                patch.object(feishu_bot, 'send_text') as text, \
                patch.object(feishu_bot, 'submit_video_job') as submit, \
                patch.object(feishu_bot, 'run_avatar_selection') as run:
            feishu_bot.send_avatar_confirmation('oc', {
                'action': action, 'script_number': 11, 'avatar_key': key, 'message_id': message_id})
        submit.assert_not_called()
        run.assert_not_called()
        return update, send, text

    def test_confirmation_card_replaces_the_clicked_card(self):
        update, send, text = self.confirm('')
        self.assertIn('generate_avatar_video', str(update.call_args.args[1]))
        send.assert_not_called()
        text.assert_not_called()

    def test_already_generating_script_shows_a_notice_with_no_paid_button(self):
        update, send, text = self.confirm('生成中')
        self.assertNotIn('generate_avatar_video', str(update.call_args.args[1]))
        self.assertIn('不会重复提交', str(update.call_args.args[1]))
        send.assert_not_called()

    def test_missing_script_is_shown_on_the_card_with_the_real_reason(self):
        update, send, text = self.confirm('', error=RuntimeError('当前视图只有 1 条，找不到脚本 11。'))
        shown = str(update.call_args.args[1])
        self.assertIn('找不到脚本 11', shown)
        self.assertNotIn('generate_avatar_video', shown)
        self.assertEqual(update.call_args.args[1]['header']['template'], 'red')
        text.assert_not_called()


class CancelAndEventsTests(unittest.IsolatedAsyncioTestCase):
    @patch('feishu_bot._claim_card_action', return_value=True)
    async def test_cancel_locks_the_clicked_card_and_nothing_is_submitted(self, claim):
        payload = {'header': {'event_type': 'card.action.trigger'}, 'event': {
            'action': {'value': {'action': 'cancel_avatar_video', 'script_number': 11,
                                 'avatar_key': 'avatar_libby'}},
            'context': {'open_chat_id': 'oc_test', 'open_message_id': 'om_confirm'}}}
        tasks = BackgroundTasks()
        with patch.object(feishu_bot, 'submit_video_job') as submit, \
                patch.object(feishu_bot, 'run_avatar_selection') as run:
            response = await feishu_bot.feishu_events(FakeRequest(payload), tasks)
        self.assertIn('toast', response)
        funcs = [t.func for t in tasks.tasks]
        self.assertIs(funcs[0], feishu_bot.safely_update_card)
        self.assertEqual(tasks.tasks[0].args[0], 'om_confirm')
        self.assertEqual(actions(tasks.tasks[0].args[1]), [])
        self.assertIn(feishu_bot.send_text, funcs)
        self.assertNotIn(feishu_bot.run_avatar_selection, funcs)
        submit.assert_not_called()
        run.assert_not_called()

    @patch('feishu_bot._claim_card_action', return_value=True)
    async def test_library_click_hands_the_clicked_card_to_the_lookup(self, claim):
        payload = {'header': {'event_type': 'card.action.trigger'}, 'event': {
            'action': {'value': {'action': 'select_avatar_script', 'script_number': 11,
                                 'avatar_key': 'avatar_b'}},
            'context': {'open_chat_id': 'oc_test', 'open_message_id': 'om_pick'}}}
        tasks = BackgroundTasks()
        await feishu_bot.feishu_events(FakeRequest(payload), tasks)
        self.assertIs(tasks.tasks[1].func, feishu_bot.route_leo_selection)
        self.assertEqual(tasks.tasks[1].kwargs, {'message_id': 'om_pick'})


class DedupeChainTests(unittest.TestCase):
    """The real de-duplication on ONE card as it moves through every step."""

    def test_each_step_on_one_card_is_accepted_once_and_a_repeat_is_ignored(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory,                 patch.object(feishu_bot, '_card_action_path', Path(directory) / 'handled.json'),                 patch.object(feishu_bot, '_handled_card_actions', set()):
            chain = ['select_avatar_script', 'approve_rewrite', 'select_leo_avatar',
                     'generate_avatar_video']
            for step in chain:
                action = {'message_id': 'om_one_card', 'event_id': '', 'action': step}
                self.assertTrue(feishu_bot._claim_card_action(action), step)
                self.assertFalse(feishu_bot._claim_card_action(action), step)
            # cancel and generate share one decision, so only one of them can win
            cancel = {'message_id': 'om_one_card', 'event_id': '', 'action': 'cancel_avatar_video'}
            self.assertFalse(feishu_bot._claim_card_action(cancel))

    def test_redo_then_a_new_review_card_can_still_be_approved(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory,                 patch.object(feishu_bot, '_card_action_path', Path(directory) / 'handled.json'),                 patch.object(feishu_bot, '_handled_card_actions', set()):
            redo = {'message_id': 'om_old_review', 'event_id': '', 'action': 'redo_rewrite'}
            approve_old = {'message_id': 'om_old_review', 'event_id': '', 'action': 'approve_rewrite'}
            approve_new = {'message_id': 'om_new_review', 'event_id': '', 'action': 'approve_rewrite'}
            self.assertTrue(feishu_bot._claim_card_action(redo))
            self.assertFalse(feishu_bot._claim_card_action(approve_old))   # why redo must send a new card
            self.assertTrue(feishu_bot._claim_card_action(approve_new))


class FailureEdgeTests(unittest.IsolatedAsyncioTestCase):
    def test_update_and_new_card_both_failing_still_reaches_the_user_as_text(self):
        with patch.object(feishu_bot, 'update_card', side_effect=RuntimeError('a')),                 patch.object(feishu_bot, 'send_card', side_effect=RuntimeError('b')),                 patch.object(feishu_bot, 'send_text') as text:
            feishu_bot.show_blocked('oc', 'om', 11, 'avatar_a', 'reason', 'plain text')
        text.assert_called_once_with('oc', 'plain text')

    @patch('feishu_bot._claim_card_action', return_value=True)
    async def test_cancel_without_a_card_id_does_not_try_to_update_a_card(self, claim):
        payload = {'header': {'event_type': 'card.action.trigger'}, 'event': {
            'action': {'value': {'action': 'cancel_avatar_video', 'script_number': 11,
                                 'avatar_key': 'avatar_libby'}},
            'context': {'open_chat_id': 'oc_test', 'open_message_id': ''}}}
        tasks = BackgroundTasks()
        with patch.object(feishu_bot, 'submit_video_job') as submit:
            await feishu_bot.feishu_events(FakeRequest(payload), tasks)
        self.assertNotIn(feishu_bot.safely_update_card, [t.func for t in tasks.tasks])
        self.assertIn(feishu_bot.send_text, [t.func for t in tasks.tasks])
        submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
