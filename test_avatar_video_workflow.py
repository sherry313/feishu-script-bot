import os
import unittest
from unittest.mock import patch

from avatar_video_workflow import (avatar_card, avatar_choices,
                                   command_without_mention, parse_card_action,
                                   parse_script_number, generation_script)


class AvatarVideoWorkflowTests(unittest.TestCase):
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

    def test_two_safe_default_avatar_choices(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('AVATAR_CHOICES_JSON', None)
            choices = avatar_choices()
        self.assertEqual([item['key'] for item in choices], ['avatar_a', 'avatar_b'])

    def test_card_does_not_contain_script_or_provider_id(self):
        record = {'record_id': 'rec1', 'number': 27, 'title': '测试标题',
                  'script': '这一段正文不应该被放进卡片'}
        card = avatar_card(record)
        rendered = str(card)
        self.assertNotIn(record['script'], rendered)
        self.assertIn('脚本 27', rendered)

    def test_parse_card_action(self):
        payload = {'event': {
            'action': {'value': {'action': 'generate_avatar_video',
                                 'record_id': 'rec1', 'script_number': 27,
                                 'avatar_key': 'avatar_b'}},
            'context': {'open_chat_id': 'oc_1'},
        }}
        self.assertEqual(parse_card_action(payload), {
            'record_id': 'rec1', 'script_number': 27,
            'avatar_key': 'avatar_b', 'chat_id': 'oc_1',
        })

    def test_video_always_uses_first_rewrite(self):
        fields = {'口播稿': '原始口播稿', '改写一': '最终用于生成的视频文案',
                  '改写二': '另一版改写'}
        self.assertEqual(generation_script(fields), '最终用于生成的视频文案')

    def test_missing_first_rewrite_does_not_fall_back(self):
        self.assertEqual(generation_script({'口播稿': '原始口播稿'}), '')


if __name__ == '__main__':
    unittest.main()
