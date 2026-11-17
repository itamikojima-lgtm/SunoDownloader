"""Phase A3: authorized live compatibility diagnostic for Mango media.

Does not capture cookies, use browser automation, bypass rights denials, or log key material.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path
import requests
from mango_engine import MangoError, request_rights, decrypt_local_file, inspect_media
from suno_poc import PoCError, SUNO_HOSTS, valid_url, get_page, extract_song_id, gather_public_metadata, Diagnosis

DEFAULT_URL = 'https://suno.com/s/nwUlS9Rz33fy5GzS'


def resolve_song(url: str, session: requests.Session) -> Diagnosis:
    valid_url(url, SUNO_HOSTS)
    resolved, document = get_page(session, url)
    song_id = extract_song_id(resolved)
    if not song_id:
        raise PoCError('NO_SONG_ID', '共有URLから楽曲IDを特定できませんでした')
    result = Diagnosis(input_url=url.split('?', 1)[0], resolved_url=resolved.split('?', 1)[0], song_id=song_id)
    gather_public_metadata(document, resolved, result)
    return result


def run_live(url: str, encrypted: Path, output: Path, rights_provider=request_rights) -> dict:
    """Resolve public metadata; request rights; decrypt only when rights grant material.

    All diagnostic output is sanitized; no rights response, token, or URLs are logged.
    """
    report = {'phase': 'A3', 'status': 'STARTED', 'song_id': None, 'title': None,
              'encrypted_bytes': None, 'output_file': None, 'audio': None, 'message': None}
    try:
        encrypted = encrypted.expanduser().resolve()
        output = output.expanduser().resolve()
        if not encrypted.is_file():
            raise MangoError('FILE_MISSING', '暗号化M4Aが見つかりません。先にローカルに保存してください')
        with requests.Session() as session:
            session.headers.update({'User-Agent': 'Suno-Mango-A3/0.1', 'Accept': 'text/html,application/xhtml+xml'})
            song = resolve_song(url, session)
            report['song_id'], report['title'] = song.song_id, song.title
            report['encrypted_bytes'] = encrypted.stat().st_size
            # Detect pre-existing playable audio and refuse misleading decrypt attempt.
            try:
                info = inspect_media(encrypted)
            except MangoError as e:
                if e.code not in ('INVALID_MEDIA',):
                    raise
            else:
                report['status'] = 'ALREADY_PLAYABLE'
                report['audio'] = info
                report['message'] = '入力ファイルはすでに再生可能です。復号は不要です'
                return report
            # The provider raises a terminal error if rights cannot be granted.
            rights = rights_provider(song.song_id, session)
            info = decrypt_local_file(encrypted, output, song.song_id, rights)
            report['status'] = info['status']
            report['audio'] = info['audio']
            report['output_file'] = str(output)
            report['sha256'] = info['sha256']
            report['message'] = '復号結果をFFprobeで検証して保存しました'
    except (MangoError, PoCError) as err:
        report['status'] = err.code
        # Deliberately avoid raw errors from HTTP response bodies or secrets.
        report['message'] = str(err).split('?', 1)[0][:200]
    except (requests.RequestException, ValueError, OSError) as err:
        report['status'] = 'ENVIRONMENT_ERROR'
        report['message'] = type(err).__name__
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Phase A3: Suno Mango live compatibility test')
    parser.add_argument('--url', default=DEFAULT_URL, help='Suno song share URL')
    parser.add_argument('--encrypted', type=Path, required=True, help='Locally downloaded encrypted M4A of the SAME song')
    parser.add_argument('--output', type=Path, default=Path('verified_output.m4a'))
    parser.add_argument('--report', type=Path, default=Path('a3_report.json'))
    args = parser.parse_args(argv)
    report = run_live(args.url, args.encrypted, args.output)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(rendered, encoding='utf-8')
    return 0 if report['status'] in ('VALIDATED', 'ALREADY_PLAYABLE') else 1


if __name__ == '__main__':
    raise SystemExit(main())
