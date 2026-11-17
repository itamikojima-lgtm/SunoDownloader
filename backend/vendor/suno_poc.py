"""Suno Phase A Core PoC: public metadata, safe source classification, audio validation.

No DRM decryption, login-cookie collection, or access-control bypass.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

VERSION = "0.1.0"
SUNO_HOSTS = {"suno.com", "www.suno.com"}
MEDIA_HOSTS = {"cdn1.suno.ai", "cdn2.suno.ai", "d2lwuy8qc234o3.cloudfront.net"}
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SHORT_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
MAX_HTML_BYTES = 3_000_000
MAX_AUDIO_BYTES = 400_000_000

class PoCError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(f"{code}: {message}")

@dataclass
class MediaCandidate:
    url: str
    origin: str
    encrypted: bool | None = None
    delivery: str | None = None
    content_type: str | None = None

@dataclass
class Diagnosis:
    input_url: str
    resolved_url: str | None = None
    song_id: str | None = None
    title: str | None = None
    artist: str | None = None
    image_url: str | None = None
    candidates: list[MediaCandidate] = field(default_factory=list)
    status: str = "UNKNOWN"
    messages: list[str] = field(default_factory=list)
    downloaded_file: str | None = None
    audio: dict | None = None

def valid_url(url: str, hosts: set[str], *, allow_path: bool = False) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise PoCError("INVALID_URL", "URLが長すぎるか不正です")
    try:
        p = urlparse(url)
        port = p.port
    except ValueError as exc:
        raise PoCError("INVALID_URL", "URLに不正なポートがあります") from exc
    if (p.scheme != "https" or p.hostname not in hosts or p.username or p.password
            or port not in (None, 443) or p.fragment):
        raise PoCError("INVALID_URL", "許可済みHTTPSホストのみ対応")
    if not allow_path:
        path = p.path.rstrip("/")
        parts = path.split("/")
        if len(parts) != 3 or parts[1] not in {"song", "s", "playlist"}:
            raise PoCError("UNSUPPORTED_URL", "対応するSuno共有URLではありません")
        if parts[1] in {"song", "playlist"} and not UUID_RE.fullmatch(parts[2]):
            raise PoCError("UNSUPPORTED_URL", "楽曲/プレイリストIDがUUIDではありません")
        if parts[1] == "s" and not SHORT_RE.fullmatch(parts[2]):
            raise PoCError("UNSUPPORTED_URL", "短縮トークンの形式が不正です")
    return url

def get_page(session: requests.Session, url: str, max_redirects: int = 5, *,
             cancel_event=None) -> tuple[str, bytes]:
    visited: set[str] = set()
    for _ in range(max_redirects + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise PoCError("CANCELLED", "共有ページの取得を中止しました")
        valid_url(url, SUNO_HOSTS, allow_path=True)
        if url in visited:
            raise PoCError("REDIRECT_LOOP", "リダイレクトが循環しています")
        visited.add(url)
        try:
            with session.get(url, timeout=(15, 60), allow_redirects=False, stream=True) as response:
                if response.status_code in (401, 403, 429):
                    raise PoCError("ACCESS_BLOCKED", f"HTTP {response.status_code}。自動再試行しません")
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        raise PoCError("REDIRECT_INVALID", "Locationヘッダーなし")
                    url = urljoin(url, location)
                    continue
                if response.status_code == 404:
                    raise PoCError("NOT_FOUND", "公開ページが見つかりません")
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                    raise PoCError("NOT_HTML", "期待したHTMLページではありません")
                body = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    if cancel_event is not None and cancel_event.is_set():
                        raise PoCError("CANCELLED", "共有ページの取得を中止しました")
                    body.extend(chunk)
                    if len(body) > MAX_HTML_BYTES:
                        raise PoCError("PAGE_TOO_LARGE", "HTMLが上限を超えました")
                return url, bytes(body)
        except requests.RequestException as exc:
            raise PoCError("NETWORK_ERROR", f"ページへのアクセスに失敗: {type(exc).__name__}") from exc
    raise PoCError("TOO_MANY_REDIRECTS", "リダイレクト回数の上限超過")

def meta_value(soup: BeautifulSoup, names: tuple[str, ...]) -> str | None:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return html.unescape(str(tag["content"])).strip()
    return None

def gather_public_metadata(document: bytes, resolved_url: str, diagnosis: Diagnosis) -> None:
    soup = BeautifulSoup(document, "html.parser")
    title = meta_value(soup, ("og:title", "twitter:title"))
    if title:
        diagnosis.title = re.sub(r"\s*[|｜]\s*Suno\s*$", "", title).strip()
    elif soup.title and soup.title.string:
        diagnosis.title = re.sub(r"\s*[|｜]\s*Suno\s*$", "", soup.title.string).strip()
    diagnosis.image_url = meta_value(soup, ("og:image", "twitter:image"))
    author = meta_value(soup, ("music:musician", "author", "article:author"))
    if author:
        diagnosis.artist = author
    if not diagnosis.artist and diagnosis.title:
        match = re.match(r"(.+?)\s+by\s+(.+)$", diagnosis.title, flags=re.IGNORECASE)
        if match:
            diagnosis.title, diagnosis.artist = match.group(1), match.group(2)
    # Only explicit media metadata, not the public player media_urls encrypted field.
    for key in ("og:audio:secure_url", "og:audio", "twitter:player:stream"):
        raw = meta_value(soup, (key,))
        if raw:
            diagnosis.candidates.append(MediaCandidate(url=raw, origin=key, encrypted=None))
    # JSON-LD may contain AudioObject URLs; treats encryption as UNKNOWN until validated.
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not script.string or len(script.string) > 200_000:
            continue
        try:
            data = json.loads(script.string)
        except (ValueError, TypeError):
            continue
        def walk(node, depth=0):
            if depth > 8: return
            if isinstance(node, list):
                for item in node[:100]: walk(item, depth+1)
            elif isinstance(node, dict):
                typ = str(node.get("@type", "")).lower()
                if "audioobject" in typ or "musicrecording" in typ:
                    for field in ("contentUrl", "audio", "url"):
                        val = node.get(field)
                        if isinstance(val, str) and (".mp3" in val or ".m4a" in val or ".wav" in val):
                            diagnosis.candidates.append(MediaCandidate(url=val, origin=f"jsonld:{field}"))
                for value in node.values(): walk(value, depth+1)
        walk(data)
    # De-duplicate without leaking URLs into diagnostics output.
    diagnosis.candidates = list({c.url: c for c in diagnosis.candidates}.values())

def extract_song_id(url: str) -> str | None:
    m = re.search(r"/song/([0-9a-fA-F-]{36})(?:/|$)", urlparse(url).path)
    return str(uuid.UUID(m.group(1))) if m and UUID_RE.fullmatch(m.group(1)) else None

def ffprobe(path: Path, ffprobe_exe: str = "ffprobe") -> dict:
    exe = shutil.which(ffprobe_exe)
    if not exe:
        raise PoCError("FFPROBE_MISSING", "FFprobeがありません。FFmpegをインストールしてください")
    try:
        proc = subprocess.run(
            [exe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=90, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PoCError("PROBE_FAILURE", type(exc).__name__) from exc
    if proc.returncode:
        raise PoCError("INVALID_MEDIA", "FFprobeで音声として認識できません")
    try:
        result = json.loads(proc.stdout)
        streams = [s for s in result.get("streams", []) if s.get("codec_type") == "audio"]
        if not streams:
            raise PoCError("INVALID_MEDIA", "音声ストリームがありません")
        stream = streams[0]
        duration = result.get("format", {}).get("duration") or stream.get("duration")
        duration_num = float(duration) if duration is not None else None
        if duration_num is not None and duration_num <= 0:
            raise PoCError("INVALID_MEDIA", "長さ0秒の音声です")
        with path.open("rb") as audio_file:
            digest = hashlib.file_digest(audio_file, "sha256").hexdigest()
        return {"codec": stream.get("codec_name"), "sample_rate": stream.get("sample_rate"),
                "duration_s": duration_num, "bytes": path.stat().st_size,
                "sha256": digest}
    except (TypeError, ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        raise PoCError("INVALID_MEDIA", "音声メタデータが不正です") from exc

def fetch_audio(session: requests.Session, media_url: str, destination: Path) -> Path:
    valid_url(media_url, MEDIA_HOSTS, allow_path=True)
    if destination.exists():
        raise PoCError("FILE_EXISTS", "同名ファイルが既に存在します。上書きしません")
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with session.get(media_url, stream=True, timeout=(15, 60), allow_redirects=False,
                         headers={"Accept-Encoding": "identity"}) as resp:
            if resp.status_code in (401, 403, 429):
                raise PoCError("ACCESS_BLOCKED", f"HTTP {resp.status_code}")
            if 300 <= resp.status_code < 400:
                raise PoCError("REDIRECT_BLOCKED", "音声URLのリダイレクトは自動追跡しません")
            if resp.status_code != 200:
                raise PoCError("HTTP_ERROR", f"HTTP {resp.status_code}")
            ctype = resp.headers.get("Content-Type", "").lower()
            if ctype.startswith("text/") or "xml" in ctype or "json" in ctype:
                raise PoCError("INVALID_MEDIA", "音声ではなくエラー文書が返りました")
            expected_raw = resp.headers.get("Content-Length")
            expected = int(expected_raw) if expected_raw and expected_raw.isdigit() else None
            if expected is not None and expected > MAX_AUDIO_BYTES:
                raise PoCError("FILE_TOO_LARGE", "取得サイズが上限を超えました")
            with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as f:
                tmp_path = Path(f.name)
                total = 0
                for chunk in resp.iter_content(chunk_size=64*1024):
                    if not chunk: continue
                    total += len(chunk)
                    if total > MAX_AUDIO_BYTES:
                        raise PoCError("FILE_TOO_LARGE", "取得サイズが上限を超えました")
                    f.write(chunk)
            if total == 0 or (expected is not None and expected != total):
                raise PoCError("INCOMPLETE_TRANSFER", "受信サイズが一致しないか空ファイルです")
        return tmp_path
    except requests.RequestException as exc:
        raise PoCError("NETWORK_ERROR", type(exc).__name__) from exc
    except Exception:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise

def redact_candidate(candidate: MediaCandidate) -> dict:
    p = urlparse(candidate.url)
    return {"host": p.hostname, "path_suffix": Path(p.path).suffix,
            "origin": candidate.origin, "encrypted": candidate.encrypted}

def cli(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Suno Phase A PoC (公開メタデータ/音声の完全性検証)")
    parser.add_argument("url", nargs="?", help="Sunoの公開楽曲共有URL")
    parser.add_argument("--inspect-file", type=Path, help="既存のローカル音声をFFprobeで検証")
    parser.add_argument("--media-url", help="保存権限のある非暗号化の音声URLを明示指定")
    parser.add_argument("--out", type=Path, default=Path("downloads"), help="出力フォルダ")
    parser.add_argument("--json", type=Path, help="診断JSONの出力先")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args(argv)
    diagnosis = Diagnosis(input_url=args.url or str(args.inspect_file or ""))
    try:
        if args.inspect_file:
            diagnosis.audio = ffprobe(args.inspect_file, args.ffprobe)
            diagnosis.status = "VERIFIED_LOCAL_AUDIO"
        else:
            if not args.url:
                parser.error("Suno共有URLまたは --inspect-file を指定してください")
            valid_url(args.url, SUNO_HOSTS)
            with requests.Session() as session:
                session.headers.update({"User-Agent": f"Suno-Core-PoC/{VERSION}", "Accept": "text/html,application/xhtml+xml"})
                final_url, content = get_page(session, args.url)
                diagnosis.resolved_url = final_url.split("?", 1)[0]
                diagnosis.song_id = extract_song_id(final_url)
                gather_public_metadata(content, final_url, diagnosis)
                if diagnosis.song_id is None:
                    diagnosis.status = "UNRESOLVED_SONG_ID"
                    diagnosis.messages.append("楽曲IDが特定できません。公開プレイリストには別アダプタが必要です")
                else:
                    diagnosis.status = "METADATA_OK"
                if not diagnosis.candidates:
                    diagnosis.messages.append("HTMLから直接保存可能な音声URLは見つかりません（失敗ではなく要検証）")
                if args.media_url:
                    diagnosis.messages.append("指定URLは許可済み非暗号化ソースであることを利用者が確認してください")
                    out = args.out.resolve() / f"{diagnosis.song_id or 'audio'}.media"
                    tmp = fetch_audio(session, args.media_url, out)
                    try:
                        diagnosis.audio = ffprobe(tmp, args.ffprobe)
                        extension = {"mp3": ".mp3", "aac": ".m4a", "alac": ".m4a", "opus": ".opus", "flac": ".flac", "pcm_s16le": ".wav"}.get(diagnosis.audio["codec"], ".audio")
                        out = out.with_suffix(extension)
                        if out.exists():
                            raise PoCError("FILE_EXISTS", "出力ファイルが既にあります")
                        os.replace(tmp, out)
                        diagnosis.downloaded_file = str(out)
                        diagnosis.status = "VERIFIED_DOWNLOADED_AUDIO"
                    finally:
                        tmp.unlink(missing_ok=True)
        success = not diagnosis.status.startswith(("ERROR", "UNRESOLVED"))
    except PoCError as exc:
        diagnosis.status = exc.code
        diagnosis.messages.append(exc.message)
        success = False
    except Exception as exc:
        diagnosis.status = "UNEXPECTED_ERROR"
        diagnosis.messages.append(f"{type(exc).__name__}: {exc}")
        success = False
    report = asdict(diagnosis)
    report["candidates"] = [redact_candidate(x) for x in diagnosis.candidates]
    # Never print/persist raw media URLs, auth query parameters, or redirects' query tokens.
    report["input_url"] = args.url.split("?", 1)[0] if args.url else diagnosis.input_url
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if success else 1

if __name__ == "__main__":
    raise SystemExit(cli())
