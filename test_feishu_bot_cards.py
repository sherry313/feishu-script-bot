import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks

import feishu_bot
from feishu_bot import feishu_events, safely_update_card


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class CardCallbackResponseTests(unittest.IsolatedAsyncioTestCase):
    def test_same_card_stage_is_claimed_only_once(self):
        action = {'message_id': 'om_once', 'event_id': '',
                  'action': 'select_avatar_script'}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(feishu_bot, '_card_action_path',
                             Path(directory) / 'handled.json'), \
                patch.object(feishu_bot, '_handled_card_actions', set()):
            self.assertTrue(feishu_bot._claim_card_action(action))
            self.assertFalse(feishu_bot._claim_card_action(action))

    @patch('feishu_bot._claim_card_action', return_value=True)
    async def test_rewrite_approval_locks_card_and_opens_leo_picker(self, claim):
        payload = {
            'header': {'event_type': 'card.action.trigger'},
            'event': {
                'action': {'value': {
                    'action': 'approve_rewrite',
                    'script_number': 10,
                    'record_id': 'rec_10',
                }},
                'context': {'open_chat_id': 'oc_test', 'open_message_id': 'om_review'},
            },
        }
        tasks = BackgroundTasks()
        response = await feishu_events(FakeRequest(payload), tasks)
        self.assertEqual(response, {})
        self.assertIs(tasks.tasks[0].func, safely_update_card)
        self.assertNotIn("'tag': 'button'", str(tasks.tasks[0].args[1]))
        self.assertIs(tasks.tasks[1].func, feishu_bot.approve_rewrite)

    @patch('feishu_bot._claim_card_action', return_value=True)
    async def test_script_selection_queues_explicit_original_card_update(self, claim):
        payload = {
            'header': {'event_type': 'card.action.trigger'},
            'event': {
                'action': {'value': {
                    'action': 'select_avatar_script',
                    'script_number': 10,
                    'avatar_key': 'avatar_b',
                }},
                'context': {'open_chat_id': 'oc_test', 'open_message_id': 'om_test'},
            },
        }
        tasks = BackgroundTasks()
        response = await feishu_events(FakeRequest(payload), tasks)
        self.assertEqual(response, {})
        self.assertIs(tasks.tasks[0].func, safely_update_card)
        self.assertEqual(tasks.tasks[0].args[0], 'om_test')
        self.assertNotIn("'tag': 'button'", str(tasks.tasks[0].args[1]))
        self.assertIs(tasks.tasks[1].func, feishu_bot.route_leo_selection)


class PendingRewriteRoutingTests(unittest.IsolatedAsyncioTestCase):
    @patch('feishu_bot.send_text')
    @patch('feishu_bot.send_rewrite_review')
    @patch('feishu_bot.script_record')
    @patch('feishu_bot.Feishu')
    @patch('feishu_bot.avatar_source')
    def test_pending_leo_script_returns_to_human_review(
            self, source, feishu, script_record, send_review, send_text):
        source.return_value = {'table_id': 'tbl', 'view_id': 'view', 'library': ''}
        feishu.return_value = MagicMock()
        script_record.return_value = {
            'record_id': 'rec_33', 'number': 33, 'status': '待确认',
            'script': '已有改写一', 'title': '标题',
        }

        feishu_bot.route_leo_selection('oc_test', 33)

        send_review.assert_called_once_with('oc_test', 33, 'rec_33')
        self.assertIn('等待人工确认', send_text.call_args.args[1])

    @patch('feishu_bot.send_leo_picker')
    @patch('feishu_bot.script_record')
    @patch('feishu_bot.Feishu')
    @patch('feishu_bot.avatar_source')
    def test_approved_leo_script_continues_to_presenter_picker(
            self, source, feishu, script_record, send_picker):
        source.return_value = {'table_id': 'tbl', 'view_id': 'view', 'library': ''}
        feishu.return_value = MagicMock()
        script_record.return_value = {
            'record_id': 'rec_33', 'number': 33, 'status': '文案已确认',
            'script': '已有改写一', 'title': '标题',
        }

        feishu_bot.route_leo_selection('oc_test', 33)

        send_picker.assert_called_once_with('oc_test', 33)

    @patch('feishu_bot._claim_card_action', return_value=True)
    async def test_presenter_selection_queues_explicit_original_card_update(self, claim):
        payload = {
            'header': {'event_type': 'card.action.trigger'},
            'event': {
                'action': {'value': {
                    'action': 'select_leo_avatar',
                    'script_number': 10,
                    'avatar_key': 'leo_avatar_1',
                }},
                'context': {'open_chat_id': 'oc_test', 'open_message_id': 'om_presenter'},
            },
        }
        tasks = BackgroundTasks()
        response = await feishu_events(FakeRequest(payload), tasks)
        self.assertEqual(response, {})
        self.assertIs(tasks.tasks[0].func, safely_update_card)
        self.assertEqual(tasks.tasks[0].args[0], 'om_presenter')
        self.assertNotIn("'tag': 'button'", str(tasks.tasks[0].args[1]))


if __name__ == '__main__':
    unittest.main()
