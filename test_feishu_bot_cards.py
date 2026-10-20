import unittest

from fastapi import BackgroundTasks

from feishu_bot import feishu_events


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class CardCallbackResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_script_selection_returns_card_content_without_wrapper(self):
        payload = {
            'header': {'event_type': 'card.action.trigger'},
            'event': {
                'action': {'value': {
                    'action': 'select_avatar_script',
                    'script_number': 10,
                    'avatar_key': 'avatar_b',
                }},
                'context': {'open_chat_id': 'oc_test'},
            },
        }
        response = await feishu_events(FakeRequest(payload), BackgroundTasks())
        self.assertIn('header', response)
        self.assertIn('elements', response)
        self.assertNotIn('card', response)
        self.assertNotIn('toast', response)

    async def test_presenter_selection_returns_card_content_without_wrapper(self):
        payload = {
            'header': {'event_type': 'card.action.trigger'},
            'event': {
                'action': {'value': {
                    'action': 'select_leo_avatar',
                    'script_number': 10,
                    'avatar_key': 'leo_avatar_1',
                }},
                'context': {'open_chat_id': 'oc_test'},
            },
        }
        response = await feishu_events(FakeRequest(payload), BackgroundTasks())
        self.assertIn('header', response)
        self.assertIn('elements', response)
        self.assertNotIn('card', response)
        self.assertNotIn('toast', response)


if __name__ == '__main__':
    unittest.main()
