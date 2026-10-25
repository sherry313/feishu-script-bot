import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
