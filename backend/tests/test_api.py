import time
import subprocess
from fastapi.testclient import TestClient
from backend.app import Manager, create_app, Job

URL='https://suno.com/song/12345678-1234-1234-1234-123456789abc'

def auth(c):
    return {'X-CSRF-Token':c.get('/api/session').json()['csrf']}

def test_validation_security():
    m=Manager()
    with TestClient(create_app(m)) as c:
        h=auth(c)
        assert c.post('/api/jobs',json={'url':URL}).status_code==403
        assert c.post('/api/jobs',json={'url':URL},headers=h|{'Origin':'https://evil.test'}).status_code==403
        for u in ['http://suno.com/s/abcd','https://suno.com.evil/s/abcd','https://suno.com/playlist/123','https://u@suno.com/s/abcd']:
            assert c.post('/api/jobs',json={'url':u},headers=h).status_code==422
        assert c.post('/api/jobs',json={'url':URL,'format':'exe'},headers=h).status_code==422

def test_file_owner_expiry():
    m=Manager()
    with TestClient(create_app(m)) as c:
        auth(c)
        d=m.root/'fixture';d.mkdir();f=d/'音源.wav'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.2','-y',str(f)],check=True)
        j=Job('fixture',c.cookies.get('suno_session'),d,'wav',status='completed',stage='complete',filename=f.name,size_bytes=f.stat().st_size,expires_at=time.time()+60)
        m.jobs[j.id]=j
        r=c.get('/api/jobs/fixture/download')
        assert r.status_code==200 and r.content[:4]==b'RIFF'
        other=TestClient(c.app);auth(other)
        assert other.get('/api/jobs/fixture').status_code==404
        j.expires_at=time.time()-1
        assert c.get('/api/jobs/fixture').status_code==404
        assert not d.exists()

def test_cancel_capacity(monkeypatch):
    m=Manager()
    def wait(j,u):
        j.status='running';j.cancel.wait(3);j.status='cancelled';j.expires_at=time.time()+60
    monkeypatch.setattr(m,'run',wait)
    with TestClient(create_app(m)) as c:
        h=auth(c);r=c.post('/api/jobs',json={'url':URL,'format':'mp3'},headers=h)
        assert r.status_code==202
        assert c.post('/api/jobs',json={'url':URL},headers=h).status_code==429
        ident=r.json()['id']
        assert c.post(f'/api/jobs/{ident}/cancel',headers=h).status_code==200
        deadline=time.monotonic()+2
        while m.jobs[ident].status!='cancelled' and time.monotonic()<deadline:time.sleep(.01)
        assert c.get(f'/api/jobs/{ident}').json()['status']=='cancelled'
