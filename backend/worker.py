"""Isolated, killable process for the original download pipeline."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / 'vendor'))
from phase_b import process
from phase_a3 import resolve_song


def emit(data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


def resolve(url, session):
    song = resolve_song(url, session)
    emit({'title': (song.title or 'Suno')[:200]})
    return song


if __name__ == '__main__':
    request = json.loads(sys.stdin.readline())
    report = process(request['url'], Path(request['directory']), request['format'],
                     resolver=resolve,
                     callback=lambda stage, fraction: emit({'stage': stage, 'progress': fraction}))
    emit({'result': {'status': report['status'], 'filename': Path(report['file']).name if report.get('file') else None}})
