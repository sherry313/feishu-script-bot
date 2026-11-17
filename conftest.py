"""Test-environment isolation only. No production code lives here.

A plain `pytest` run gets: no real .env secrets, fake test credentials, an empty
temporary state directory (also at import time, because feishu_bot reads its
state path when it is imported), and no real network access.

core.py replaces sys.stdout when imported, which breaks pytest's output capture
on Windows. It is imported once here with a stand-in stdout so it skips that.
"""
import io
import os
import socket
import sys
import tempfile

import pytest

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
for _key in ('FEISHU_APP_SECRET', 'HEYGEN_API_KEY', 'ANTHROPIC_API_KEY',
             'TRANSCRIPT_API_KEY', 'TRANSCRIPT_API_SECRET', 'JOB_TRIGGER_SECRET',
             'AVATAR_VIDEO_SERVICE_SECRET', 'FEISHU_VERIFICATION_TOKEN',
             'FEISHU_APP_ID', 'FEISHU_ALLOWED_OPEN_IDS'):
    os.environ.pop(_key, None)
os.environ['FEISHU_APP_ID'] = 'cli_test_only'
os.environ['FEISHU_VERIFICATION_TOKEN'] = 'test_token_only'
os.environ['FEISHU_ALLOWED_OPEN_IDS'] = 'ou_test'
_IMPORT_STATE = tempfile.mkdtemp(prefix='c1-release-tests-')
os.environ['STATE_DIR'] = _IMPORT_STATE
os.environ['AVATAR_JOB_STORE'] = os.path.join(_IMPORT_STATE, 'avatar_jobs.json')

_real_stdout = sys.stdout
sys.stdout = io.StringIO()
try:
    import core  # noqa: F401
finally:
    sys.stdout = _real_stdout


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setenv('STATE_DIR', str(state))
    monkeypatch.setenv('AVATAR_JOB_STORE', str(state / 'avatar_jobs.json'))
    import feishu_bot
    import heygen_video_service
    monkeypatch.setattr(heygen_video_service, 'JOB_STORE', state / 'avatar_jobs.json')
    monkeypatch.setattr(feishu_bot, '_card_action_path', state / 'handled_card_actions.json')
    monkeypatch.setattr(feishu_bot, '_handled_card_actions', set())

    def no_network(*args, **kwargs):
        raise AssertionError('Unmocked network call in tests')

    original_connect = socket.socket.connect

    def local_only(sock, address):
        if isinstance(address, tuple) and address[0] in {'127.0.0.1', '::1'}:
            return original_connect(sock, address)
        return no_network()

    monkeypatch.setattr('requests.sessions.Session.request', no_network)
    monkeypatch.setattr(socket.socket, 'connect', local_only)
    yield
