"""Share a clip as a link that plays inside Discord (like Medal), from your own free storage.

The clip goes to a private Backblaze B2 bucket; the Cloudflare Worker in worker/ serves the page Discord reads and
sends the player to a short-lived download link. Keys live in ~/.config/assistant/clip-share.env, never in config.toml
(that one is backed up) and never in the logs. Stdlib only: the upload window runs on the system Python.

Discord played a 123 MB clip and refused 197 MB, so clips are kept under a cap at 1080p60: what already fits is only
rearranged for fast start (no quality loss), the rest is re-encoded on the GPU. The originals are never touched.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import CONFIG_DIR

ENV_FILE = CONFIG_DIR / "clip-share.env"
WORK_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "assistant" / "clipshare"  # not /tmp: that's RAM here
REQUIRED = ("B2_BUCKET", "B2_ENDPOINT", "B2_WRITE_KEY_ID", "B2_WRITE_KEY", "CLIP_BASE_URL")
EMPTY_SHA = hashlib.sha256(b"").hexdigest()

Progress = Callable[[str, float], None]


class ShareError(Exception):
    """Something the user should read."""


class Cancelled(Exception):
    pass


def load_settings() -> dict:
    """The key file as a dict; ShareError if it's missing or incomplete."""
    try:
        lines = ENV_FILE.read_text().splitlines()
    except OSError:
        raise ShareError("clip sharing isn't set up yet (no clip-share.env)") from None
    out = {}
    for line in lines:
        key, eq, value = line.strip().partition("=")
        if eq and not key.startswith("#"):
            out[key] = value.strip()
    if missing := [k for k in REQUIRED if not out.get(k)]:
        raise ShareError("clip sharing isn't set up yet (missing " + ", ".join(missing) + ")")
    out["CLIP_BASE_URL"] = out["CLIP_BASE_URL"].rstrip("/")
    return out


def default_title(path: Path) -> str:
    """'Roblox 2026-11-04 19-22-36.mp4' → 'Roblox'."""
    return re.sub(r"\s*\d{4}-\d{2}-\d{2}[ _]\d{2}-\d{2}-\d{2}$", "", path.stem).strip() or "Clip"


def clean_title(title: str, fallback: str) -> str:
    title = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", title or "")).strip()
    return title[:100].strip() or fallback


def say_length(seconds: float) -> str:
    s = max(0, round(seconds))
    return f"{s // 60}:{s % 60:02d}"


@dataclass
class ClipInfo:
    seconds: float
    size: int
    width: int
    height: int
    fps: int
    video_codec: str
    pix_fmt: str
    audio_codec: str  # "" = no sound


def probe(path: Path) -> ClipInfo:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                             capture_output=True, text=True, timeout=30, check=True).stdout
        data = json.loads(out)
        video = next(s for s in data["streams"] if s["codec_type"] == "video")
        audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), {})
        num, _, den = video.get("avg_frame_rate", "0/1").partition("/")
        return ClipInfo(float(data["format"]["duration"]), int(data["format"]["size"]), int(video["width"]),
                        int(video["height"]), round(float(num) / (float(den or 1) or 1)), video["codec_name"],
                        video.get("pix_fmt", ""), audio.get("codec_name", ""))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, StopIteration) as e:
        raise ShareError(f"couldn't read that clip ({type(e).__name__})") from None


@dataclass
class Plan:
    remux: bool  # True: the video isn't touched, only rearranged
    video_kbps: int
    audio_kbps: int
    size: int  # expected bytes


def plan_for(info: ClipInfo, cap_bytes: int, max_kbps: int, audio: bool = True, audio_kbps: int = 256) -> Plan:
    if not audio or not info.audio_codec:
        audio_kbps = 0
    playable = info.video_codec == "h264" and info.pix_fmt in ("yuv420p", "yuvj420p", "") and info.audio_codec in ("aac", "")
    if playable and info.size <= cap_bytes:
        return Plan(True, 0, audio_kbps, info.size)
    budget = cap_bytes * 8 / 1000 / max(info.seconds, 0.1) - audio_kbps
    video = max(500, min(max_kbps, int(budget * 0.97)))  # 3 %: mp4 overhead and the encoder's aim isn't perfect
    return Plan(False, video, audio_kbps, int((video + audio_kbps) * 1000 / 8 * info.seconds))


def describe(info: ClipInfo, plan: Plan) -> str:
    head = f"{info.height}p{info.fps} · {say_length(info.seconds)} · {info.size / 1e6:.0f} MB"
    if plan.remux:
        return f"{head} · full quality"
    note = " · long clip, so lower quality" if plan.video_kbps < 8000 else ""
    return f"{head} → about {plan.size / 1e6:.0f} MB at {plan.video_kbps / 1000:.0f} Mbps{note}"


def encode_args(src: Path, out: Path, plan: Plan, audio: bool, nvenc: bool) -> list[str]:
    head = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-progress", "pipe:1", "-i", str(src)]
    sound = ["-c:a", "copy"] if audio else ["-an"]
    tail = ["-movflags", "+faststart", str(out)]
    if plan.remux:
        return head + ["-c:v", "copy"] + sound + tail
    if audio:
        sound = ["-c:a", "aac", "-b:a", f"{plan.audio_kbps or 256}k"]
    rate = ["-b:v", f"{plan.video_kbps}k", "-maxrate", f"{plan.video_kbps * 3 // 2}k", "-bufsize", f"{plan.video_kbps * 2}k"]
    if nvenc:
        video = ["-c:v", "h264_nvenc", "-preset", "p7", "-tune", "hq", "-rc", "vbr", *rate, "-multipass", "fullres",
                 "-spatial-aq", "1", "-temporal-aq", "1"]
    else:
        video = ["-c:v", "libx264", "-preset", "medium", *rate]
    return head + video + ["-pix_fmt", "yuv420p"] + sound + tail


def _run_ffmpeg(argv: list[str], seconds: float, progress: Progress, stage: str, cancel: threading.Event) -> bool:
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        for line in proc.stdout:
            if cancel.is_set():
                proc.kill()
                raise Cancelled
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and value.isdigit():
                progress(stage, min(1.0, int(value) / 1e6 / max(seconds, 0.1)))
        return proc.wait() == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def prepare(src: Path, out: Path, info: ClipInfo, plan: Plan, cap_bytes: int, audio: bool, progress: Progress,
            cancel: threading.Event) -> None:
    """Writes the shareable mp4 to `out`. Tries again smaller if the encoder overshot the cap."""
    if plan.remux:
        if not _run_ffmpeg(encode_args(src, out, plan, audio, True), info.seconds, progress, "Getting it ready…", cancel):
            raise ShareError("couldn't prepare the clip")
        return
    nvenc = True
    for _ in range(3):
        ok = _run_ffmpeg(encode_args(src, out, plan, audio, nvenc), info.seconds, progress, "Compressing…", cancel)
        if not ok and nvenc:  # no room on the GPU right now: the CPU is slower but gets there
            nvenc = False
            ok = _run_ffmpeg(encode_args(src, out, plan, audio, False), info.seconds, progress, "Compressing…", cancel)
        if not ok:
            raise ShareError("couldn't compress the clip")
        size = out.stat().st_size
        if size <= cap_bytes:
            return
        plan = Plan(False, int(plan.video_kbps * cap_bytes / size * 0.95), plan.audio_kbps, cap_bytes)
    raise ShareError("couldn't get the clip small enough for Discord")


def thumbnail(src: Path, out: Path, seconds: float) -> None:
    done = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", f"{seconds / 2:.2f}", "-i", str(src),
                           "-frames:v", "1", "-vf", "scale=1280:-2", "-q:v", "3", str(out)], timeout=60, capture_output=True)
    if done.returncode or not out.exists():
        raise ShareError("couldn't make the preview picture")


# ── S3 API of the bucket ─────────────────────────────────────────────────────────────────────────────────────────

def _key(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4(method: str, host: str, path: str, query: dict, headers: dict, payload_hash: str, key_id: str, secret: str,
          region: str, when: dt.datetime) -> str:
    """The Authorization header. `headers` are all signed (plus host, x-amz-content-sha256 and x-amz-date, which must be in it)."""
    amz, day = when.strftime("%Y%m%dT%H%M%SZ"), when.strftime("%Y%m%d")
    names = sorted(k.lower() for k in headers)
    lower = {k.lower(): str(v).strip() for k, v in headers.items()}
    canon_query = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(str(v), safe='-_.~')}" for k, v in sorted(query.items()))
    canonical = "\n".join([method, urllib.parse.quote(path, safe="/-_.~"), canon_query,
                           "".join(f"{n}:{lower[n]}\n" for n in names), ";".join(names), payload_hash])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    signing = _key(_key(_key(_key(("AWS4" + secret).encode(), day), region), "s3"), "aws4_request")
    signature = hmac.new(signing, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={key_id}/{scope},SignedHeaders={';'.join(names)},Signature={signature}"


class _Reader:
    """A file for urllib that reports how far the upload got and stops when cancelled."""

    def __init__(self, path: Path, total: int, on_read: Callable[[float], None], cancel: threading.Event):
        self.f, self.total, self.sent, self.on_read, self.cancel = open(path, "rb"), total, 0, on_read, cancel

    def read(self, n: int = 1 << 18) -> bytes:
        if self.cancel.is_set():
            raise Cancelled
        chunk = self.f.read(min(n, 1 << 18) if n and n > 0 else 1 << 18)
        self.sent += len(chunk)
        self.on_read(self.sent / max(self.total, 1))
        return chunk

    def close(self):
        self.f.close()


class Bucket:
    RETRIES = 4  # B2 answers a 500 now and then; its docs say to just try again

    def __init__(self, settings: dict, cancel: threading.Event | None = None):
        self.s, self.cancel = settings, cancel or threading.Event()
        self.host = f"{settings['B2_BUCKET']}.{settings['B2_ENDPOINT']}"
        self.region = settings["B2_ENDPOINT"].split(".")[1]

    def _call(self, method: str, key: str = "", query: dict | None = None, ctype: str = "", file: Path | None = None,
              body: bytes = b"", on_read: Callable[[float], None] = lambda _: None) -> bytes:
        query = query or {}
        path = "/" + key
        size = file.stat().st_size if file else len(body)
        if file:
            digest = hashlib.sha256()
            with open(file, "rb") as f:
                while chunk := f.read(1 << 20):
                    digest.update(chunk)
            payload = digest.hexdigest()
        else:
            payload = hashlib.sha256(body).hexdigest()
        qs = urllib.parse.urlencode(sorted(query.items()), quote_via=urllib.parse.quote, safe="-_.~")
        url = f"https://{self.host}{urllib.parse.quote(path, safe='/-_.~')}" + (f"?{qs}" if qs else "")
        last = ""
        for attempt in range(self.RETRIES):
            if self.cancel.is_set():
                raise Cancelled
            when = dt.datetime.now(dt.timezone.utc)
            headers = {"host": self.host, "x-amz-content-sha256": payload, "x-amz-date": when.strftime("%Y%m%dT%H%M%SZ")}
            if ctype:
                headers["content-type"] = ctype
            auth = sigv4(method, self.host, path, query, headers, payload, self.s["B2_WRITE_KEY_ID"], self.s["B2_WRITE_KEY"], self.region, when)
            send = dict(headers, authorization=auth)
            reader = _Reader(file, size, on_read, self.cancel) if file else None
            if method == "PUT":
                send["content-length"] = str(size)
            req = urllib.request.Request(url, data=reader or (body if method == "PUT" else None), method=method, headers=send)
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                code = re.search(rb"<Code>(.*?)</Code>", e.read() or b"")
                last = f"{e.code}" + (f" {code[1].decode(errors='replace')}" if code else "")
                if e.code < 500 and e.code != 429:
                    raise ShareError(f"the storage said no ({last})") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last = type(e).__name__
            finally:
                if reader:
                    reader.close()
            if attempt < self.RETRIES - 1:
                self.cancel.wait(3 * (attempt + 1))
        raise ShareError(f"the upload kept failing ({last})")

    def put(self, key: str, file: Path, ctype: str, on_read: Callable[[float], None] = lambda _: None):
        self._call("PUT", key, ctype=ctype, file=file, on_read=on_read)

    def put_bytes(self, key: str, body: bytes, ctype: str):
        self._call("PUT", key, ctype=ctype, body=body)

    def delete(self, key: str):
        self._call("DELETE", key)

    def listing(self, prefix: str = "clips/") -> list[tuple[str, int, str]]:
        """[(key, bytes, modified)] of everything stored."""
        out, token = [], None
        while True:
            query = {"list-type": "2", "prefix": prefix, **({"continuation-token": token} if token else {})}
            root = ET.fromstring(self._call("GET", query=query))
            ns = {"s": root.tag[1:].split("}")[0]} if root.tag.startswith("{") else {"s": ""}
            for item in root.findall("s:Contents", ns):
                out.append((item.findtext("s:Key", "", ns), int(item.findtext("s:Size", "0", ns)), item.findtext("s:LastModified", "", ns)))
            token = root.findtext("s:NextContinuationToken", None, ns)
            if not token:
                return out


def oldest_to_remove(stored: list[tuple[str, int, str]], incoming: int, keep_bytes: int) -> list[str]:
    """Clip ids to delete, oldest first, so what's stored plus the new clip stays under keep_bytes."""
    clips: dict[str, list] = {}
    for key, size, modified in stored:
        cid = Path(key).stem
        entry = clips.setdefault(cid, [0, modified])
        entry[0] += size
        entry[1] = min(entry[1], modified)
    total = sum(c[0] for c in clips.values()) + incoming
    gone = []
    for cid, (size, _) in sorted(clips.items(), key=lambda kv: kv[1][1]):
        if total <= keep_bytes:
            break
        gone.append(cid)
        total -= size
    return gone


@dataclass
class Shared:
    url: str
    bytes: int
    removed: int  # older clips deleted to make room
    remuxed: bool


def share(src: Path, title: str, audio: bool, cap_bytes: int, max_kbps: int, keep_bytes: int,
          progress: Progress = lambda *_: None, cancel: threading.Event | None = None) -> Shared:
    """Prepare, upload and return the link. Nothing is left behind in the bucket if it fails or is cancelled."""
    cancel = cancel or threading.Event()
    settings = load_settings()
    info = probe(src)
    plan = plan_for(info, cap_bytes, max_kbps, audio)
    cid = secrets.token_urlsafe(12)
    work = WORK_DIR / cid
    work.mkdir(parents=True, exist_ok=True)
    bucket = Bucket(settings, cancel)
    done: list[str] = []
    try:
        out, thumb = work / "clip.mp4", work / "thumb.jpg"
        progress("Getting it ready…", 0)
        prepare(src, out, info, plan, cap_bytes, audio, progress, cancel)
        thumbnail(out, thumb, info.seconds)
        size = out.stat().st_size
        progress("Making room…", 0)
        removed = oldest_to_remove(bucket.listing(), size + thumb.stat().st_size, keep_bytes)
        for old in removed:
            for ext in ("json", "jpg", "mp4"):
                bucket.delete(f"clips/{old}.{ext}")
        progress("Uploading…", 0)
        bucket.put(f"clips/{cid}.mp4", out, "video/mp4", lambda f: progress("Uploading…", f))
        done.append(f"clips/{cid}.mp4")
        bucket.put(f"clips/{cid}.jpg", thumb, "image/jpeg")
        done.append(f"clips/{cid}.jpg")
        meta = {"title": clean_title(title, default_title(src)), "game": default_title(src), "width": info.width,
                "height": info.height, "seconds": round(info.seconds, 2), "created": int(time.time())}
        bucket.put_bytes(f"clips/{cid}.json", json.dumps(meta).encode(), "application/json")  # last: the page 404s until here
        return Shared(f"{settings['CLIP_BASE_URL']}/c/{cid}", size, len(removed), plan.remux)
    except BaseException:
        leftover = Bucket(settings)  # its own cancel flag, so a cancelled upload still cleans up
        for key in done:
            try:
                leftover.delete(key)
            except ShareError:
                pass
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
