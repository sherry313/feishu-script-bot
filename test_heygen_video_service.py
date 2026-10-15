import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import heygen_video_service as service


class AvatarJobPersistenceTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
