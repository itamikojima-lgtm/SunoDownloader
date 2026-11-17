"""Mango format proof of concept.

Only process audio for which you have permission. A rights denial is terminal.
No login tokens, hard-coded keys, browser cookies, or license bypasses.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import requests
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

RIGHTS_ENDPOINT = "https://studio-api.prod.suno.com/api/mango/rights"
CHUNK_SIZE = 1024 * 1024
MAX_FILE_SIZE = 400 * 1024 * 1024


class MangoError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def _decode_b64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise MangoError("INVALID_RIGHTS", "Malformed base64 in rights response") from exc


def _unwrap(wrapped: str, song_id: str, guest_key: bytes) -> bytes:
    payload = _decode_b64(wrapped)
    if len(payload) < 12 + 16 + 1:
        raise MangoError("INVALID_RIGHTS", "Wrapped value is too short")
    nonce = payload[:12]
    ciphertext_and_tag = payload[12:]
    try:
        ctx = Cipher(algorithms.AES(guest_key), modes.GCM(nonce, ciphertext_and_tag[-16:])).decryptor()
        ctx.authenticate_additional_data(song_id.encode("utf-8"))
        return ctx.update(ciphertext_and_tag[:-16]) + ctx.finalize()
    except (InvalidTag, ValueError) as exc:
        raise MangoError("RIGHTS_AUTH_FAILED", "Rights data authentication failed") from exc


def get_media_key_material(song_id: str, rights: dict) -> tuple[bytes, bytes]:
    try:
        guest_token = rights["glt"]
        if not isinstance(guest_token, str) or not guest_token:
            raise ValueError("empty token")
        guest_key = hashlib.sha256(guest_token.encode("utf-8")).digest()
        key = _unwrap(rights["key"], song_id, guest_key)
        iv = _unwrap(rights["iv"], song_id, guest_key)
        if len(key) not in (16, 24, 32) or len(iv) != 16:
            raise MangoError("INVALID_RIGHTS", "AES key or CTR counter has an invalid length")
        return key, iv
    except (KeyError, TypeError, ValueError) as exc:
        raise MangoError("INVALID_RIGHTS", "Incomplete rights response") from exc


def request_rights(song_id: str, session: requests.Session | None = None) -> dict:
    song_id = str(uuid.UUID(song_id))
    own = session is None
    session = session or requests.Session()
    try:
        response = session.post(
            RIGHTS_ENDPOINT,
            json={"content_params": {"content_id": song_id, "content_type": "clip"}},
            timeout=(15, 30),
            headers={"Accept": "application/json", "Origin": "https://suno.com"},
            allow_redirects=False,
        )
        if response.status_code in (401, 403, 429):
            raise MangoError("RIGHTS_DENIED", f"Rights server returned HTTP {response.status_code}; stopped")
        if response.status_code != 200:
            raise MangoError("RIGHTS_UNAVAILABLE", f"Rights server returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise MangoError("RIGHTS_UNAVAILABLE", "Rights server did not return JSON") from exc
        if not isinstance(payload, dict):
            raise MangoError("INVALID_RIGHTS", "Rights response is not an object")
        return payload
    except requests.RequestException as exc:
        raise MangoError("NETWORK_ERROR", f"Rights request failed: {type(exc).__name__}") from exc
    finally:
        if own:
            session.close()


def inspect_media(path: Path) -> dict:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise MangoError("FFPROBE_MISSING", "ffprobe was not found in PATH")
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration,format_name:stream=codec_type,codec_name,sample_rate", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=90,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise MangoError("PROBE_FAILURE", type(exc).__name__) from exc
    if result.returncode:
        raise MangoError("INVALID_MEDIA", "FFprobe rejected the audio file")
    try:
        data = json.loads(result.stdout)
        streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
        if not streams:
            raise ValueError("no audio stream")
        duration = float(data.get("format", {}).get("duration") or 0)
        if duration <= 0:
            raise ValueError("invalid duration")
    except (KeyError, TypeError, ValueError) as exc:
        raise MangoError("INVALID_MEDIA", "Missing audio stream or duration") from exc
    return {"duration_s": duration, "codec": streams[0].get("codec_name"), "container": data.get("format", {}).get("format_name")}


def decrypt_local_file(source: Path, destination: Path, song_id: str, rights: dict) -> dict:
    """Decrypt into sibling temp file, verify, then atomically commit.

    The source file is never modified. No output is committed on failure.
    """
    song_id = str(uuid.UUID(song_id))
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        raise MangoError("INVALID_PATH", "Input and output must differ")
    if not source.is_file():
        raise MangoError("FILE_MISSING", "Encrypted input file is missing")
    if destination.exists():
        raise MangoError("FILE_EXISTS", "Output exists; will not overwrite")
    size = source.stat().st_size
    if size <= 0 or size > MAX_FILE_SIZE:
        raise MangoError("INVALID_SIZE", "Input size is invalid")
    key, ctr = get_media_key_material(song_id, rights)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".mango_", suffix=".part", dir=destination.parent)
    os.close(fd)
    tmp = Path(temp_name)
    try:
        decoder = Cipher(algorithms.AES(key), modes.CTR(ctr)).decryptor()
        with source.open("rb") as inp, tmp.open("wb") as out:
            for block in iter(lambda: inp.read(CHUNK_SIZE), b""):
                out.write(decoder.update(block))
            out.write(decoder.finalize())
            out.flush()
            os.fsync(out.fileno())
        info = inspect_media(tmp)
        # Do not overwrite existing outputs, including those created concurrently.
        try:
            os.link(tmp, destination)
        except FileExistsError as exc:
            raise MangoError("FILE_EXISTS", "Output appeared during processing") from exc
        except OSError:
            # Windows may disallow hardlinks on some volumes; avoid unsafe overwrite fallback.
            raise MangoError("COMMIT_FAILED", "Could not commit output safely")
        return {"status": "VALIDATED", "bytes": destination.stat().st_size, "audio": info,
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "saved_to": str(destination)}
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Mango encrypted audio local-file verification PoC")
    parser.add_argument("--song-id", required=True, help="UUID of the audio clip")
    parser.add_argument("--encrypted", required=True, type=Path, help="Local encrypted .m4a")
    parser.add_argument("--output", type=Path, default=Path("mango_verified.m4a"))
    args = parser.parse_args()
    try:
        song_id = str(uuid.UUID(args.song_id))
        print("Checking the Suno rights endpoint (no credential capture)...")
        rights = request_rights(song_id)
        result = decrypt_local_file(args.encrypted, args.output, song_id, rights)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (MangoError, ValueError) as exc:
        code = exc.code if isinstance(exc, MangoError) else "INVALID_ID"
        print(json.dumps({"status": code, "message": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
