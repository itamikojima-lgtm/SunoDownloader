"""Independent regression for worker event/exit ordering."""
import subprocess
from pathlib import Path

from backend import app as module


def test_result_before_process_exit_is_retained(monkeypatch):
    real_popen = subprocess.Popen
    script = '''import json,sys,time
from pathlib import Path
r=json.loads(sys.stdin.readline())
(Path(r['directory'])/'result.mp3').write_bytes(b'audio fixture')
print(json.dumps({'result':{'status':'COMPLETED','filename':'result.mp3'}}),flush=True)
time.sleep(.4)
'''
    def popen(args, **kwargs):
        return real_popen([args[0], '-c', script], **kwargs)
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    manager = module.Manager()
    directory = manager.root / 'regression'
    directory.mkdir()
    job = module.Job('regression', 'owner', directory, 'mp3')
    try:
        manager.run(job, 'https://suno.com/s/testcase')
        assert job.status == 'completed'
        assert (directory / 'result.mp3').read_bytes() == b'audio fixture'
        assert job.expires_at is not None
    finally:
        manager.stop()


def test_render_external_origin_and_body_limits(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv('RENDER_EXTERNAL_URL', 'https://example.onrender.com')
    manager = module.Manager()
    with TestClient(module.create_app(manager)) as client:
        response = client.get('/api/session', headers={'Origin': 'https://example.onrender.com'})
        assert response.status_code == 200
        assert client.get('/api/session', headers={'Origin': 'https://evil.example'}).status_code == 403
        token = response.json()['csrf']
        # Generator body has no predeclared size; protect actual received bytes too.
        assert client.post('/api/jobs', content=iter([b'x' * 3000, b'x' * 3000]), headers={'X-CSRF-Token': token}).status_code == 413
        assert client.post('/api/jobs', content=b'{}', headers={'Content-Length': 'oops'}).status_code == 400
