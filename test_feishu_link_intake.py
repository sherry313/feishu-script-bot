import unittest
from unittest.mock import Mock

from feishu_link_intake import ingest_links, normalize_url, shared_video_links


class LinkIntakeTests(unittest.TestCase):
    def test_extracts_xiaohongshu_and_channels_links(self):
        text = ('看看 https://xhslink.com/a/abc123 ，还有 '
                'https://channels.weixin.qq.com/foo/bar。')
        self.assertEqual(shared_video_links(text), [
            'https://xhslink.com/a/abc123',
            'https://channels.weixin.qq.com/foo/bar',
        ])

    def test_rejects_unrelated_links(self):
        self.assertEqual(normalize_url('https://example.com/video'), '')

    def test_assigns_next_library_number_and_avoids_duplicate(self):
        client = Mock()
        client.base = '/table'
        client.pages.return_value = iter([
            {'record_id': 'leo-1', 'fields': {
                '脚本库': 'LEO', '视频链接': {'link': 'https://xhslink.com/a/old'}}},
            {'record_id': 'libby-1', 'fields': {
                '脚本库': 'Libby', '视频链接': {'link': 'https://xhslink.com/a/other'}}},
        ])
        client.request.return_value = {'data': {'record': {'record_id': 'leo-2'}}}
        results = ingest_links(
            '标题 https://xhslink.com/a/old https://xhslink.com/a/new', client=client)
        self.assertEqual([r['number'] for r in results], [1, 2])
        self.assertTrue(results[0]['duplicate'])
        self.assertFalse(results[1]['duplicate'])
        fields = client.request.call_args.kwargs['json']['fields']
        self.assertEqual(fields['脚本库'], 'LEO')
        self.assertEqual(fields['生成状态'], '待确认')


if __name__ == '__main__':
    unittest.main()
