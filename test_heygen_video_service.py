import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import heygen_video_service as service


class AvatarJobPersistenceTests(unittest.TestCase):
    def setUp(self):
        service._inventory_cache.update({
            'expires_at': 0, 'avatar_ids': set(), 'voice_ids': set()
        })

    def test_registered_job_is_recoverable_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / 'jobs.json'
            payload = {'record_id': 'rec1', 'callback_url': 'https://example.test/callback'}
            with patch.object(service, 'JOB_STORE', store):
                job_id = service.register_job(payload)
                self.assertEqual(service.unfinished_jobs(), [(job_id, payload)])

    def test_completed_job_is_not_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / 'jobs.json'
            with patch.object(service, 'JOB_STORE', store):
                job_id = service.register_job({'record_id': 'rec1'})
                service._save_job(job_id, state='completed')
                self.assertEqual(service.unfinished_jobs(), [])

    def test_binding_validation_rejects_explicitly_disabled_custom_avatar(self):
        with patch.dict('os.environ', {
            'LEO_AVATAR_1_LOOK_ID': 'avatar-configured',
            'AVATAR_B_VOICE_ID': 'voice-configured',
            'DISABLED_AVATAR_KEYS': 'leo_avatar_1',
        }, clear=False):
            with self.assertRaisesRegex(RuntimeError, '安全模式停用'):
                service.validate_avatar_binding('leo_avatar_1')

    def test_custom_resources_are_not_rejected_for_absence_from_stock_list(self):
        with patch.dict('os.environ', {
            'LEO_AVATAR_2_LOOK_ID': 'custom-avatar-2',
            'AVATAR_B_VOICE_ID': 'custom-voice-2',
            'DISABLED_AVATAR_KEYS': '',
        }, clear=False):
            self.assertTrue(service.validate_avatar_binding('leo_avatar_2'))

    def test_accept_job_does_not_acknowledge_provider_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / 'jobs.json'
            with patch.object(service, 'JOB_STORE', store), \
                    patch.object(service, 'create_video_job',
                                 side_effect=RuntimeError('数字人不可用')):
                with self.assertRaisesRegex(RuntimeError, '数字人不可用'):
                    service.accept_job({'record_id': 'rec1'})
                jobs = service._load_jobs()
                self.assertEqual(next(iter(jobs.values()))['state'], 'failed')

    def test_callback_pending_retries_callback_without_resubmitting_video(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / 'jobs.json'
            payload = {'record_id': 'rec1', 'callback_url': 'https://example.test/callback'}
            with patch.object(service, 'JOB_STORE', store):
                job_id = service.register_job(payload)
                service._save_job(job_id, state='callback_pending', status='failed',
                                  failure_reason='已分类错误')
                with patch.object(service, '_post_callback') as callback, \
                        patch.object(service, 'create_video_job') as create:
                    service.wait_and_callback(payload, job_id)
                create.assert_not_called()
                callback.assert_called_once_with(
                    payload, 'failed', failure_reason='已分类错误'
                )


if __name__ == '__main__':
    unittest.main()
