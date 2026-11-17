"""C1 release: the picker always offers LEO / Libby / 陈总, reads no table first,
and every safety check still runs after the user chooses a library.

Fully mocked: no network, no paid API, no real state directory.
"""
import unittest
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks

import feishu_bot


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


LABELS = ['LEO脚本', 'Libby脚本', '陈总脚本']


def buttons(card):
    return [item for element in card['elements'] if element.get('tag') == 'action'
            for item in element['actions']]


class PickerAlwaysShowsThreeTests(unittest.TestCase):
    def send(self, number):
        sent = []
        with patch.object(feishu_bot, 'send_card', lambda chat, card: sent.append(card)), \
                patch.object(feishu_bot, 'send_text', MagicMock()) as text, \
                patch.object(feishu_bot, 'Feishu', MagicMock(side_effect=AssertionError(
                    'table touched before selection'))) as feishu, \
                patch.object(feishu_bot, 'script_record', MagicMock(side_effect=AssertionError(
                    'script looked up before selection'))) as lookup:
            feishu_bot.send_avatar_picker('oc_test', number)
        return sent, text, feishu, lookup

    def test_every_number_shows_all_three_and_reads_no_table(self):
        for number in (1, 2, 3, 4, 11, 27, 999):
            sent, text, feishu, lookup = self.send(number)
            self.assertEqual(len(sent), 1, number)
            found = buttons(sent[0])
            self.assertEqual([b['text']['content'] for b in found], LABELS, number)
            self.assertEqual({b['value']['script_number'] for b in found}, {number})
            self.assertEqual(len({b['value']['avatar_key'] for b in found}), 3)
            feishu.assert_not_called()
            lookup.assert_not_called()
            text.assert_not_called()
            self.assertNotIn('已自动隐藏', str(sent[0]))

    def test_picker_buttons_can_only_select_a_library_never_generate(self):
        sent, *_ = self.send(11)
        self.assertEqual({b['value']['action'] for b in buttons(sent[0])},
                         {'select_avatar_script'})
        self.assertNotIn('generate_avatar_video', str(sent[0]))

    def test_feishu_timeout_or_rate_limit_cannot_hide_an_option(self):
        sent = []
        broken = MagicMock(side_effect=RuntimeError('飞书网络请求失败；结果可能未确认'))
        with patch.object(feishu_bot, 'send_card', lambda chat, card: sent.append(card)), \
                patch.object(feishu_bot, 'Feishu', broken), \
                patch.object(feishu_bot, 'script_record', broken):
            feishu_bot.send_avatar_picker('oc_test', 11)
        self.assertEqual([b['text']['content'] for b in buttons(sent[0])], LABELS)
        broken.assert_not_called()

    def test_repeated_number_messages_never_start_video_generation(self):
        with patch.object(feishu_bot, 'send_card', MagicMock()) as card, \
                patch.object(feishu_bot, 'submit_video_job', MagicMock()) as submit, \
                patch.object(feishu_bot, 'run_avatar_selection', MagicMock()) as run:
            feishu_bot.send_avatar_picker('oc_test', 11)
            feishu_bot.send_avatar_picker('oc_test', 11)
        self.assertEqual(card.call_count, 2)
        submit.assert_not_called()
        run.assert_not_called()

    def test_card_build_failure_is_reported_not_silent(self):
        with patch.object(feishu_bot, 'avatar_choices',
                          MagicMock(side_effect=RuntimeError('必须配置三个数字人选项。'))), \
                patch.object(feishu_bot, 'send_card', MagicMock()) as card, \
                patch.object(feishu_bot, 'send_text', MagicMock()) as text:
            feishu_bot.send_avatar_picker('oc_test', 11)
        card.assert_not_called()
        text.assert_called_once_with('oc_test', '必须配置三个数字人选项。')


class SafetyAfterSelectionTests(unittest.IsolatedAsyncioTestCase):
    ACTION = {'action': 'select_avatar_script', 'script_number': 11,
              'avatar_key': 'avatar_libby', 'chat_id': 'oc_test'}

    @patch('feishu_bot._claim_card_action', return_value=True)
    async def test_selecting_a_library_only_queues_lookup_and_never_generation(self, claim):
        payload = {'header': {'event_type': 'card.action.trigger'}, 'event': {
            'action': {'value': {'action': 'select_avatar_script', 'script_number': 11,
                                 'avatar_key': 'avatar_libby'}},
            'context': {'open_chat_id': 'oc_test', 'open_message_id': 'om_pick_11'}}}
        tasks = BackgroundTasks()
        with patch.object(feishu_bot, 'submit_video_job', MagicMock()) as submit:
            response = await feishu_bot.feishu_events(FakeRequest(payload), tasks)
        self.assertEqual(response, {})
        self.assertEqual([t.func for t in tasks.tasks],
                         [feishu_bot.safely_update_card, feishu_bot.send_avatar_confirmation])
        submit.assert_not_called()

    def confirm(self, **patches):
        source = {'table_id': 'tbl_test', 'view_id': '', 'library': ''}
        with patch.object(feishu_bot, 'avatar_source', MagicMock(return_value=source)), \
                patch.object(feishu_bot, 'Feishu', MagicMock()), \
                patch.object(feishu_bot, 'send_card', MagicMock()) as card, \
                patch.object(feishu_bot, 'send_text', MagicMock()) as text, \
                patch.object(feishu_bot, 'submit_video_job', MagicMock()) as submit, \
                patch.object(feishu_bot, 'run_avatar_selection', MagicMock()) as run:
            with patch.multiple(feishu_bot, **patches):
                feishu_bot.send_avatar_confirmation('oc_test', dict(self.ACTION))
        submit.assert_not_called()
        run.assert_not_called()
        return card, text

    def test_missing_script_is_explained_after_selection(self):
        card, text = self.confirm(
            validate_avatar_binding=MagicMock(),
            script_record=MagicMock(side_effect=RuntimeError('当前视图只有 1 条，找不到脚本 11。')))
        card.assert_not_called()
        self.assertIn('找不到脚本 11', text.call_args.args[1])

    def test_empty_script_is_explained_after_selection(self):
        card, text = self.confirm(
            validate_avatar_binding=MagicMock(),
            script_record=MagicMock(side_effect=RuntimeError('脚本 11 的“改写一”是空的，任务没有提交。')))
        card.assert_not_called()
        self.assertIn('“改写一”是空的', text.call_args.args[1])

    def test_avatar_safety_check_still_runs_before_any_table_read(self):
        lookup = MagicMock()
        card, text = self.confirm(
            validate_avatar_binding=MagicMock(side_effect=RuntimeError('所选数字人尚未完成后台绑定。')),
            script_record=lookup)
        lookup.assert_not_called()
        card.assert_not_called()
        self.assertIn('尚未完成后台绑定', text.call_args.args[1])

    def test_already_generating_script_is_not_offered_again(self):
        record = {'record_id': 'rec', 'number': 11, 'title': 't', 'script': 's', 'status': '生成中'}
        card, text = self.confirm(validate_avatar_binding=MagicMock(),
                                  script_record=MagicMock(return_value=record))
        card.assert_not_called()
        self.assertIn('不会重复提交', text.call_args.args[1])

    def test_usable_script_gets_the_cost_confirmation_card_not_a_video(self):
        record = {'record_id': 'rec', 'number': 11, 'title': 't', 'script': '测试文案', 'status': ''}
        card, text = self.confirm(validate_avatar_binding=MagicMock(),
                                  script_record=MagicMock(return_value=record))
        card.assert_called_once()
        self.assertIn('确认生成', str(card.call_args.args[1]))


if __name__ == '__main__':
    unittest.main()
