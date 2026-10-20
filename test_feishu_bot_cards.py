import unittest

from fastapi import BackgroundTasks

from feishu_bot import feishu_events, safely_update_card


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class CardCallbackResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_script_selection_queues_explicit_original_card_update(self):
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

    async def test_presenter_selection_queues_explicit_original_card_update(self):
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
