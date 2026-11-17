"""Phase B: one-song Suno download pipeline.

Uses Mango rights returned by Suno. No hardcoded secrets, login extraction,
license bypass, or rights-denial retry. For content you may download only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from typing import Callable
from pathlib import Path
from urllib.parse import urlparse

import requests

from mango_engine import MangoError, decrypt_local_file, inspect_media, request_rights
from suno_poc import PoCError, SUNO_HOSTS, MEDIA_HOSTS, valid_url, fetch_audio
from phase_a3 import resolve_song

VERSION = "0.3.0"
CDN_HOST = "d2lwuy8qc234o3.cloudfront.net"
MAX_ATTEMPTS = 3
KNOWN_EXTENSIONS = {"m4a": ".m4a", "mp3": ".mp3", "wav": ".wav"}

class JobCancelled(RuntimeError):
    pass


def check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise JobCancelled("User cancelled")


def notify(callback, stage, fraction=None):
    if callback:
        callback(stage, fraction)


class PipelineError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def safe_name(title: str | None, song_id: str) -> str:
    text = (title or "Suno").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if text.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        text = "_" + text
    return (text[:90].rstrip(" .") or "Suno") + f" [{song_id[:8]}]"


def media_url_for(song_id: str, given: str | None) -> str:
    # Exact host/path pattern has been observed for v6 clips, not guaranteed for all tracks.
    url = given or f"https://{CDN_HOST}/1/clip/{song_id}.m4a"
    valid_url(url, MEDIA_HOSTS, allow_path=True)
    p = urlparse(url)
    if p.hostname != CDN_HOST or p.path != f"/1/clip/{song_id}.m4a" or p.query:
        raise PipelineError("MEDIA_URL_MISMATCH", "Mango URL must match this song's known CloudFront clip path")
    return url


def fetch_with_retries(session: requests.Session, url: str, tmp_path: Path,
                       report: dict, callback=None, cancel_event=None) -> None:
    """Stream validated origin data; remove incomplete input on error/cancel."""
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        check_cancel(cancel_event)
        last = None
        try:
            valid_url(url, MEDIA_HOSTS, allow_path=True)
            with session.get(url, stream=True, timeout=(15, 60), allow_redirects=False,
                             headers={"Accept-Encoding": "identity"}) as resp:
                if resp.status_code in (401, 403, 429):
                    raise PoCError("ACCESS_BLOCKED", f"HTTP {resp.status_code}")
                if 300 <= resp.status_code < 400:
                    raise PoCError("REDIRECT_BLOCKED", "Unexpected audio redirect")
                if resp.status_code != 200:
                    raise PoCError("HTTP_ERROR", f"HTTP {resp.status_code}")
                ctype = resp.headers.get("Content-Type", "").lower()
                if ctype.startswith("text/") or "xml" in ctype or "json" in ctype:
                    raise PoCError("INVALID_MEDIA", "Server returned a non-audio document")
                size_text = resp.headers.get("Content-Length", "")
                expected = int(size_text) if size_text.isdecimal() else None
                if expected is not None and expected > 128_000_000:
                    raise PoCError("FILE_TOO_LARGE", "Audio exceeds maximum file size")
                total = 0
                with tmp_path.open("wb") as f:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        check_cancel(cancel_event)
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > 128_000_000:
                            raise PoCError("FILE_TOO_LARGE", "Audio exceeds maximum file size")
                        f.write(chunk)
                        if expected:
                            notify(callback, "download", min(total / expected, 1.0))
                if total == 0 or (expected is not None and total != expected):
                    raise PoCError("INCOMPLETE_TRANSFER", "Partial or empty audio transfer")
            report["steps"].append({"step": "download", "result": "ok", "attempt": attempt,
                                     "bytes": total})
            notify(callback, "download", 1.0)
            return
        except (requests.RequestException, OSError) as exc:
            last = PoCError("NETWORK_ERROR", type(exc).__name__)
        except PoCError as exc:
            last = exc
        finally:
            if last is not None:
                tmp_path.unlink(missing_ok=True)
        if last.code not in ("NETWORK_ERROR", "HTTP_ERROR", "INCOMPLETE_TRANSFER") or attempt == MAX_ATTEMPTS:
            break
        report["steps"].append({"step": "download", "result": "retry", "attempt": attempt,
                                "code": last.code})
        deadline = time.monotonic() + min(2 ** (attempt - 1), 4)
        while time.monotonic() < deadline:
            check_cancel(cancel_event)
            time.sleep(min(0.1, deadline - time.monotonic()))
    raise PipelineError(last.code, "Mango download failed: " + last.code)


def convert_audio(source: Path, destination: Path, output_format: str, cancel_event=None) -> None:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise PipelineError("FFMPEG_MISSING", "FFmpeg was not found in PATH")
    if output_format == "mp3":
        codec_args = ["-c:a", "libmp3lame", "-q:a", "2"]
    elif output_format == "wav":
        codec_args = ["-c:a", "pcm_s16le"]
    else:
        raise PipelineError("FORMAT_ERROR", "Unsupported format")
    cmd = [exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
           "-map", "0:a:0", "-vn", *codec_args, "-f", output_format, str(destination)]
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        start = time.monotonic()
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                proc.kill()
                proc.communicate()
                raise JobCancelled("User cancelled")
            if time.monotonic() - start > 1800:
                proc.kill()
                proc.communicate()
                raise PipelineError("CONVERT_FAILED", "FFmpeg timeout")
            time.sleep(0.1)
        proc.communicate()
    except OSError as exc:
        raise PipelineError("CONVERT_FAILED", type(exc).__name__) from exc
    if proc.returncode != 0 or not destination.is_file() or destination.stat().st_size == 0:
        raise PipelineError("CONVERT_FAILED", "FFmpeg conversion failed")


def commit_new(src: Path, dst: Path) -> None:
    """Create file without overwrite. Working temp must be on same filesystem."""
    try:
        os.link(src, dst)
    except FileExistsError as exc:
        raise PipelineError("FILE_EXISTS", "Destination already exists") from exc
    except OSError as exc:
        raise PipelineError("COMMIT_FAILED", "Cannot safely save destination") from exc


def check_format(info: dict, kind: str) -> None:
    codec = info.get("codec")
    if kind == "mp3" and codec != "mp3":
        raise PipelineError("VERIFY_FAILED", "MP3 codec mismatch")
    if kind == "wav" and codec != "pcm_s16le":
        raise PipelineError("VERIFY_FAILED", "WAV codec mismatch")
    if kind == "m4a" and "mp4" not in (info.get("container") or ""):
        raise PipelineError("VERIFY_FAILED", "M4A container mismatch")


def process(url: str, output_dir: Path, fmt: str = "m4a", encrypted_file: Path | None = None,
            media_url: str | None = None, *, session_factory=requests.Session,
            rights_provider=request_rights, resolver=resolve_song,
            callback: Callable | None = None, cancel_event=None) -> dict:
    """Run the pipeline. Each stage reports structured, non-secret progress.

    `encrypted_file` is useful for offline testing. Live paths download the known
    Mango clip URL. The caller must have rights to save the source material.
    """
    report = {"version": VERSION, "status": "STARTED", "song_id": None, "title": None,
              "format": fmt, "file": None, "audio": None, "steps": [], "message": None}
    try:
        if fmt not in KNOWN_EXTENSIONS:
            raise PipelineError("FORMAT_ERROR", "Choose m4a, mp3, or wav")
        check_cancel(cancel_event)
        valid_url(url, SUNO_HOSTS)
        if output_dir.exists() and not output_dir.is_dir():
            raise PipelineError("OUTPUT_ERROR", "Output path is not a directory")
        output_dir = output_dir.resolve()
        with session_factory() as session:
            session.headers.update({"User-Agent": "Suno-Mango-PhaseB/0.2", "Accept": "text/html,application/xhtml+xml"})
            notify(callback, "resolve", None)
            song = resolver(url, session)
            check_cancel(cancel_event)
            report["song_id"], report["title"] = song.song_id, song.title
            if not song.song_id:
                raise PipelineError("NO_SONG_ID", "Unable to identify song")
            song_id = str(uuid.UUID(song.song_id))
            report["steps"].append({"step": "resolve", "result": "ok"})
            if encrypted_file is None:
                link = media_url_for(song_id, media_url)
            elif media_url is not None:
                raise PipelineError("INPUT_CONFLICT", "Choose either --encrypted or --media-url")
            else:
                link = None

            output_dir.mkdir(parents=True, exist_ok=True)
            filename = safe_name(song.title, song_id) + KNOWN_EXTENSIONS[fmt]
            destination = output_dir / filename
            if destination.exists():
                raise PipelineError("FILE_EXISTS", "Existing output is protected; nothing overwritten")

            with tempfile.TemporaryDirectory(prefix=".suno_b_", dir=output_dir) as root:
                work = Path(root)
                encrypted = work / "encrypted.m4a"
                if encrypted_file is None:
                    notify(callback, "download", 0.0)
                    fetch_with_retries(session, link, encrypted, report, callback, cancel_event)
                else:
                    original = encrypted_file.expanduser().resolve()
                    if not original.is_file():
                        raise PipelineError("FILE_MISSING", "Encrypted input not found")
                    if original.stat().st_size > 128_000_000 or original.stat().st_size == 0:
                        raise PipelineError("INVALID_SIZE", "Invalid encrypted file size")
                    shutil.copyfile(original, encrypted)
                    report["steps"].append({"step": "input_copy", "result": "ok"})

                check_cancel(cancel_event)
                decrypted = work / "verified.m4a"
                try:
                    already_playable = inspect_media(encrypted)
                except MangoError as exc:
                    if exc.code != "INVALID_MEDIA":
                        raise
                else:
                    # Already playable file: avoid rights call and needless decryption.
                    shutil.copyfile(encrypted, decrypted)
                    report["steps"].append({"step": "decrypt", "result": "not_needed"})

                if not decrypted.exists():
                    notify(callback, "rights", None)
                    rights = rights_provider(song_id, session)
                    check_cancel(cancel_event)
                    report["steps"].append({"step": "rights", "result": "granted"})
                    notify(callback, "decrypt", None)
                    decrypt_local_file(encrypted, decrypted, song_id, rights)
                    check_cancel(cancel_event)
                    report["steps"].append({"step": "decrypt", "result": "ok"})
                notify(callback, "validate_source", None)
                check_cancel(cancel_event)
                info = inspect_media(decrypted)
                check_format(info, "m4a")
                report["steps"].append({"step": "validate_source", "result": "ok", "duration_s": info["duration_s"]})
                output_tmp = work / ("ready" + KNOWN_EXTENSIONS[fmt])
                if fmt == "m4a":
                    shutil.copyfile(decrypted, output_tmp)
                else:
                    notify(callback, "convert", None)
                    convert_audio(decrypted, output_tmp, fmt, cancel_event)
                    report["steps"].append({"step": "convert", "result": "ok"})
                notify(callback, "validate_output", None)
                check_cancel(cancel_event)
                verified = inspect_media(output_tmp)
                check_format(verified, fmt)
                report["steps"].append({"step": "validate_output", "result": "ok"})
                check_cancel(cancel_event)
                notify(callback, "commit", None)
                commit_new(output_tmp, destination)
                # No signed URLs, keys, or rights payload in output.
                report["status"] = "COMPLETED"
                report["file"] = str(destination)
                report["audio"] = verified
                with destination.open("rb") as handle:
                    report["sha256"] = hashlib.file_digest(handle, "sha256").hexdigest()
                report["steps"].append({"step": "commit", "result": "ok"})
                notify(callback, "complete", 1.0)
    except (KeyboardInterrupt, JobCancelled):
        report["status"], report["message"] = "CANCELLED", "Interrupted by user"
    except (PipelineError, MangoError, PoCError) as exc:
        report["status"] = exc.code
        report["message"] = str(exc).split("?", 1)[0][:180]
    except (ValueError, OSError, requests.RequestException) as exc:
        report["status"] = "ENVIRONMENT_ERROR"
        report["message"] = type(exc).__name__
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Suno Phase B authorized Mango download pipeline")
    parser.add_argument("url", help="Suno /song/ or /s/ URL")
    parser.add_argument("--out", type=Path, default=Path("downloads"), help="Output folder")
    parser.add_argument("--format", choices=tuple(KNOWN_EXTENSIONS), default="m4a")
    parser.add_argument("--encrypted", type=Path, help="Use locally downloaded encrypted M4A")
    parser.add_argument("--media-url", help="Explicit matching CloudFront Mango URL")
    parser.add_argument("--report", type=Path, default=Path("phase_b_report.json"))
    args = parser.parse_args(argv)
    report = process(args.url, args.out, args.format, args.encrypted, args.media_url)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(rendered, encoding="utf-8")
    return 0 if report["status"] == "COMPLETED" else 1

if __name__ == "__main__":
    raise SystemExit(main())
