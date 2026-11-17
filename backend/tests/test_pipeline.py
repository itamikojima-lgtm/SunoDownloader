"""Offline fixture proves original Mango crypto + FFmpeg, not Suno live access."""
import base64
import hashlib
import subprocess
import sys
import uuid
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
sys.path.insert(0,str(Path(__file__).parents[1]/'vendor'))
from phase_b import process
from suno_poc import Diagnosis


def test_encrypted_fixture_all_formats(tmp_path):
    song=str(uuid.uuid4());plain=tmp_path/'original.m4a'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.3','-c:a','aac','-y',str(plain)],check=True)
    key=b'k'*32;iv=b'i'*16;token='offline-test-only'
    enc=Cipher(algorithms.AES(key),modes.CTR(iv)).encryptor()
    encrypted=tmp_path/'encrypted.m4a';encrypted.write_bytes(enc.update(plain.read_bytes())+enc.finalize())
    wrapkey=hashlib.sha256(token.encode()).digest()
    def wrap(value,nonce):return base64.b64encode(nonce+AESGCM(wrapkey).encrypt(nonce,value,song.encode())).decode()
    rights={'glt':token,'key':wrap(key,b'1'*12),'iv':wrap(iv,b'2'*12)}
    for fmt in ['m4a','mp3','wav']:
        result=process('https://suno.com/song/'+song,tmp_path/fmt,fmt,encrypted_file=encrypted,
                       resolver=lambda u,s:Diagnosis(u,song_id=song,title='テスト音源'),rights_provider=lambda i,s:rights)
        assert result['status']=='COMPLETED', result
        assert Path(result['file']).stat().st_size>0
        assert result['audio']['duration_s']>0
