import os
import unittest
from unittest.mock import patch

from avatar_video_workflow import (avatar_card, avatar_choices, confirmation_card,
                                   avatar_source,
                                   command_without_mention, parse_card_action,
                                   parse_plain_script_number, parse_script_number, generation_script,
                                   estimate_video_cost, leo_avatar_card,
                                   locked_leo_avatar_card, locked_script_card,
                                   script_record, submitted_card)


class AvatarVideoWorkflowTests(unittest.TestCase):
    def test_separate_leo_table_does_not_require_legacy_library_tag(self):
        with patch.dict(os.environ, {
            'LEO_TABLE_ID': 'table-leo',
            'LEO_VIEW_ID': 'view-leo',
        }, clear=False):
            self.assertEqual(avatar_source('avatar_b')['library'], '')

    def test_cost_estimate_uses_script_length(self):
        estimate = estimate_video_cost('测' * 400)
        self.assertEqual(estimate['seconds'], 100)
        self.assertAlmostEqual(estimate['dollars'], 1.67)

    def test_parse_script_number_with_group_mention(self):
        text = '<at user_id="ou_123">口播稿助手</at> 跑脚本 27'
        self.assertEqual(command_without_mention(text), '跑脚本27')
        self.assertEqual(parse_script_number(text), 27)

    def test_parse_script_number_with_feishu_opaque_mention(self):
        # This is the exact text shape observed in the production webhook.
        text = '@_user_1 跑脚本 27'
        self.assertEqual(command_without_mention(text), '跑脚本27')
        self.assertEqual(parse_script_number(text), 27)

    def test_rejects_loose_or_zero_commands(self):
        self.assertIsNone(parse_script_number('请跑脚本27吧'))
        self.assertIsNone(parse_script_number('跑脚本0'))
        self.assertIsNone(parse_script_number('跑一条'))

    def test_bare_number_is_a_script_command(self):
        self.assertEqual(parse_plain_script_number(' 27 '), 27)
        self.assertIsNone(parse_plain_script_number('脚本27'))
        self.assertIsNone(parse_plain_script_number('0'))

    def test_three_safe_default_avatar_choices(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('AVATAR_CHOICES_JSON', None)
            choices = avatar_choices()
        self.assertEqual([item['key'] for item in choices],
                         ['avatar_b', 'avatar_libby', 'avatar_a'])
        self.assertEqual([item['label'] for item in choices],
                         ['LEO脚本', 'Libby脚本', '陈总脚本'])

    def test_card_does_not_contain_script_or_provider_id(self):
        card = avatar_card(27)
        rendered = str(card)
        self.assertIn('编号 27', rendered)
        self.assertIn('LEO脚本', rendered)
        self.assertIn('Libby脚本', rendered)
        self.assertIn('陈总脚本', rendered)

    def test_script_picker_can_hide_unavailable_libraries(self):
        card = avatar_card(27, [{'key': 'avatar_b', 'label': 'LEO脚本'}],
                           ['Libby脚本（改写一为空）'])
        rendered = str(card)
        self.assertIn('LEO脚本', rendered)
        self.assertIn('已自动隐藏', rendered)
        self.assertNotIn("'content': 'Libby脚本', 'type': 'primary'", rendered)

    def test_leo_choice_opens_three_safe_presenter_options(self):
        rendered = str(leo_avatar_card(65))
        self.assertIn('数字人 1', rendered)
        self.assertIn('数字人 2', rendered)
        self.assertIn('数字人 3', rendered)
        self.assertNotIn('d528add', rendered)
        self.assertNotIn('0b1914', rendered)
        self.assertNotIn('62893f', rendered)

    def test_presenter_picker_can_hide_invalid_provider_resource(self):
        rendered = str(leo_avatar_card(10, [
            {'key': 'leo_avatar_2', 'label': '数字人 2'},
        ], ['数字人 1（当前不可用）']))
        self.assertIn('数字人 2', rendered)
        self.assertIn('已自动隐藏', rendered)
        self.assertNotIn("'content': '数字人 1', 'type': 'primary'", rendered)

    def test_leo_presenters_use_leo_table(self):
        with patch.dict(os.environ, {'LEO_TABLE_ID': 'table-leo'}, clear=False):
            for key in ('leo_avatar_1', 'leo_avatar_2', 'leo_avatar_3'):
                self.assertEqual(avatar_source(key)['table_id'], 'table-leo')

    def test_selected_script_card_has_no_clickable_controls(self):
        rendered = str(locked_script_card(10, 'avatar_b'))
        self.assertIn('已选择 LEO脚本', rendered)
        self.assertIn('已锁定', rendered)
        self.assertNotIn("'tag': 'button'", rendered)

    def test_selected_leo_presenter_card_has_no_clickable_controls(self):
        rendered = str(locked_leo_avatar_card(10, 'leo_avatar_1'))
        self.assertIn('已选择 数字人 1', rendered)
        self.assertIn('已锁定', rendered)
        self.assertNotIn("'tag': 'button'", rendered)

    def test_parse_card_action(self):
        payload = {'event': {
            'action': {'value': {'action': 'generate_avatar_video',
                                 'script_number': 27,
                                 'avatar_key': 'avatar_b'}},
            'context': {'open_chat_id': 'oc_1'},
        }}
        self.assertEqual(parse_card_action(payload), {
            'action': 'generate_avatar_video', 'script_number': 27,
            'avatar_key': 'avatar_b', 'chat_id': 'oc_1', 'message_id': '',
            'event_id': '',
        })

    def test_confirmation_card_shows_cost_after_avatar_selection(self):
        record = {'number': 27, 'title': '测试标题', 'script': '测' * 400}
        rendered = str(confirmation_card(record, 'avatar_b'))
        self.assertIn('LEO', rendered)
        self.assertIn('$1.67', rendered)
        self.assertIn('确认生成', rendered)
        self.assertIn('取消生成', rendered)

    def test_submitted_card_has_no_clickable_controls(self):
        card = submitted_card(65, 'avatar_b')
        rendered = str(card)
        self.assertIn('已提交', rendered)
        self.assertIn('按钮已锁定', rendered)
        self.assertNotIn("'tag': 'button'", rendered)

    def test_parse_cancel_card_action(self):
        payload = {'event': {
            'action': {'value': {'action': 'cancel_avatar_video',
                                 'script_number': 62,
                                 'avatar_key': 'avatar_b'}},
            'context': {'open_chat_id': 'oc_1'},
        }}
        self.assertEqual(parse_card_action(payload), {
            'action': 'cancel_avatar_video', 'script_number': 62,
            'avatar_key': 'avatar_b', 'chat_id': 'oc_1', 'message_id': '',
            'event_id': '',
        })

    def test_video_always_uses_first_rewrite(self):
        fields = {'口播稿': '原始口播稿', '改写一': '最终用于生成的视频文案',
                  '改写二': '另一版改写'}
        self.assertEqual(generation_script(fields), '最终用于生成的视频文案')

    def test_missing_first_rewrite_does_not_fall_back(self):
        self.assertEqual(generation_script({'口播稿': '原始口播稿'}), '')

    @patch('avatar_video_workflow.records_in_view')
    def test_script_number_is_scoped_to_selected_library(self, records):
        records.return_value = [
            {'record_id': 'leo-1', 'fields': {
                '脚本库': 'LEO', '标题': 'LEO内容', '改写一': 'LEO文案'}},
            {'record_id': 'libby-1', 'fields': {
                '脚本库': 'Libby', '标题': 'Libby内容', '改写一': 'Libby文案'}},
        ]
        record = script_record(object(), 1, 'view-libby', 'Libby')
        self.assertEqual(record['record_id'], 'libby-1')
        self.assertEqual(record['script'], 'Libby文案')


if __name__ == '__main__':
    unittest.main()
