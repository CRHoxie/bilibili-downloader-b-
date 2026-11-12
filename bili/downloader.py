"""下载引擎：多线程分块下载、音视频合并、任务状态管理。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import requests

from .wbi import BiliClient


# ------------------------------------------------------------------ ffmpeg

def find_ffmpeg() -> str | None:
    """优先用系统 ffmpeg，其次用 imageio-ffmpeg 自带的便携版。"""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


# ------------------------------------------------------------------ 工具

_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')


def safe_filename(name: str, max_len: int = 80) -> str:
    name = _ILLEGAL.sub("_", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name[:max_len].strip() or "video")


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for i in range(1, 1000):
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    return parent / f"{stem}-{int(time.time())}{suffix}"


def make_session(client: BiliClient) -> requests.Session:
    s = requests.Session()
    s.trust_env = client.session.trust_env
    s.headers.update(client.session.headers)
    s.cookies.update(client.session.cookies)
    return s


def probe_size(session: requests.Session, url: str) -> tuple[int | None, bool]:
    """探测文件大小以及服务端是否支持 Range 分片。"""
    try:
        resp = session.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=20)
        try:
            if resp.status_code == 206:
                content_range = resp.headers.get("Content-Range", "")
                total = content_range.rsplit("/", 1)[-1]
                return (int(total) if total.isdigit() else None), True
            length = resp.headers.get("Content-Length")
            return (int(length) if length and length.isdigit() else None), False
        finally:
            resp.close()
    except Exception:
        return None, False


# ------------------------------------------------------------- 分块下载

def download_stream(
    url: str,
    dest: Path,
    session: requests.Session,
    threads: int = 8,
    on_bytes: Callable[[int], None] | None = None,
    cancel: threading.Event | None = None,
    expect_size: int | None = None,
) -> int:
    """下载单个媒体流到 dest，返回写入字节数。

    支持 Range 的源会切成多段并行下载，这在 B 站 CDN 上能显著提速。
    """
    total, supports_range = probe_size(session, url)
    total = total or expect_size

    if not supports_range or not total or total < 2 * 1024 * 1024 or threads <= 1:
        return _download_single(url, dest, session, on_bytes, cancel, total)

    threads = min(threads, max(1, total // (512 * 1024)))
    part_paths = [dest.with_suffix(f"{dest.suffix}.part{i}") for i in range(threads)]
    chunk = total // threads
    ranges = []
    for i in range(threads):
        start = i * chunk
        end = total - 1 if i == threads - 1 else (i + 1) * chunk - 1
        ranges.append((start, end, part_paths[i]))

    def fetch(index: int, start: int, end: int, path: Path) -> None:
        if cancel and cancel.is_set():
            return
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                done = path.stat().st_size if path.exists() else 0
                if done >= end - start + 1:
                    return
                headers = {"Range": f"bytes={start + done}-{end}"}
                with session.get(url, headers=headers, stream=True, timeout=30) as resp:
                    if resp.status_code not in (200, 206):
                        raise RuntimeError(f"HTTP {resp.status_code}")
                    with open(path, "ab") as fh:
                        for block in resp.iter_content(256 * 1024):
                            if cancel and cancel.is_set():
                                return
                            if not block:
                                continue
                            fh.write(block)
                            if on_bytes:
                                on_bytes(len(block))
                return
            except Exception as exc:  # 断线重试，利用已下载部分续传
                last_err = exc
                time.sleep(0.8 * (attempt + 1))
                if cancel and cancel.is_set():
                    return
        raise RuntimeError(f"分片 {index} 下载失败：{last_err}")

    try:
        with ThreadPoolExecutor(max_workers=threads) as pool:
            futures = [pool.submit(fetch, i, s, e, p) for i, (s, e, p) in enumerate(ranges)]
            for f in futures:
                f.result()
        if cancel and cancel.is_set():
            raise _Cancelled()

        with open(dest, "wb") as out:
            for _, _, part in ranges:
                with open(part, "rb") as src:
                    shutil.copyfileobj(src, out, 1024 * 1024)
        return dest.stat().st_size
    finally:
        for part in part_paths:
            part.unlink(missing_ok=True)


def _download_single(
    url: str,
    dest: Path,
    session: requests.Session,
    on_bytes: Callable[[int], None] | None,
    cancel: threading.Event | None,
    total: int | None,
) -> int:
    """不支持分片时退化为单流下载。"""
    with session.get(url, stream=True, timeout=30) as resp:
        if resp.status_code not in (200, 206):
            raise RuntimeError(f"HTTP {resp.status_code}")
        with open(dest, "wb") as fh:
            for block in resp.iter_content(256 * 1024):
                if cancel and cancel.is_set():
                    raise _Cancelled()
                if not block:
                    continue
                fh.write(block)
                if on_bytes:
                    on_bytes(len(block))
    return dest.stat().st_size


class _Cancelled(Exception):
    pass


# --------------------------------------------------------------- ffmpeg

def merge_av(
    ffmpeg: str,
    video: Path,
    audio: Path,
    out: Path,
    video_codec: str = "avc1",
) -> None:
    """无损合并音视频流到 mp4 容器。"""
    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-i", str(video), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c", "copy",
    ]
    if video_codec == "hevc":
        cmd += ["-tag:v", "hvc1"]  # 提升苹果设备兼容性
    cmd.append(str(out))

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 合并失败：{(result.stderr or '').strip()[:300]}")


def extract_audio(ffmpeg: str, source: Path, out: Path, to_mp3: bool) -> None:
    """把音频流转存为 m4a 或转码为 mp3。"""
    if to_mp3:
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(source),
               "-vn", "-c:a", "libmp3lame", "-b:a", "192k", str(out)]
    else:
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(source),
               "-vn", "-c:a", "copy", str(out)]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        if to_mp3:
            raise RuntimeError(f"转码 MP3 失败：{(result.stderr or '').strip()[:300]}")
        shutil.copyfile(source, out)  # 容器识别失败时直接落盘


# ------------------------------------------------------------- 任务模型

class DownloadTask:
    def __init__(self, title: str, quality_label: str, mode: str, filename: str, filepath: Path):
        self.id = uuid.uuid4().hex[:12]
        self.title = title
        self.quality_label = quality_label
        self.mode = mode  # video | audio
        self.filename = filename
        self.filepath = filepath
        self.status = "queued"  # queued|downloading|merging|done|error|canceled
        self.error = ""
        self.downloaded = 0
        self.total = 0
        self.speed = 0.0
        self.stage = "等待开始"
        self.created_at = time.time()
        self.cancel = threading.Event()
        self._lock = threading.Lock()
        self._last_time = time.time()
        self._last_bytes = 0

    # --- 进度统计 ---
    def set_total(self, total: int) -> None:
        with self._lock:
            self.total = max(0, total)

    def add_bytes(self, n: int) -> None:
        with self._lock:
            self.downloaded += n
            now = time.time()
            dt = now - self._last_time
            if dt >= 0.5:
                inst = (self.downloaded - self._last_bytes) / dt
                self.speed = inst if self.speed == 0 else self.speed * 0.55 + inst * 0.45
                self._last_bytes = self.downloaded
                self._last_time = now

    @property
    def progress(self) -> float:
        if self.total <= 0:
            return 0.0 if self.status != "done" else 100.0
        return min(100.0, self.downloaded / self.total * 100.0)

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "title": self.title,
                "quality": self.quality_label,
                "mode": self.mode,
                "filename": self.filename,
                "status": self.status,
                "stage": self.stage,
                "progress": round(self.progress, 2),
                "downloaded": self.downloaded,
                "total": self.total,
                "speed": round(self.speed, 1),
                "error": self.error,
                "created_at": self.created_at,
            }


# ----------------------------------------------------------- 任务管理器

class TaskManager:
    def __init__(self, download_dir: Path, threads: int = 8) -> None:
        self.download_dir = download_dir
        self.tmp_dir = download_dir / ".tmp"
        self.threads = threads
        self.tasks: dict[str, DownloadTask] = {}
        self._lock = threading.Lock()
        # 限制同时下载的任务数，批量下载时不至于把带宽和磁盘打满
        self._concurrency = threading.Semaphore(3)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    # --- 查询 ---
    def get(self, task_id: str) -> DownloadTask | None:
        return self.tasks.get(task_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            items = sorted(self.tasks.values(), key=lambda t: t.created_at, reverse=True)
        return [t.to_dict() for t in items]

    # --- 创建并启动 ---
    def create(
        self,
        client: BiliClient,
        title: str,
        quality_label: str,
        video_stream: dict[str, Any] | None,
        audio_stream: dict[str, Any] | None,
        mode: str,
        audio_format: str = "m4a",
    ) -> DownloadTask:
        ext = ".mp3" if (mode == "audio" and audio_format == "mp3") else (".m4a" if mode == "audio" else ".mp4")
        base = safe_filename(title)
        filepath = unique_path(self.download_dir / f"{base}{ext}")
        task = DownloadTask(title, quality_label, mode, filepath.name, filepath)

        with self._lock:
            self.tasks[task.id] = task

        thread = threading.Thread(
            target=self._run,
            args=(task, client, video_stream, audio_stream, audio_format),
            daemon=True,
        )
        thread.start()
        return task

    def cancel(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if not task or task.status in ("done", "error", "canceled"):
            return False
        task.cancel.set()
        return True

    # --- 执行体 ---
    def _run(
        self,
        task: DownloadTask,
        client: BiliClient,
        video_stream: dict[str, Any] | None,
        audio_stream: dict[str, Any] | None,
        audio_format: str,
    ) -> None:
        with self._concurrency:
            self._execute(task, client, video_stream, audio_stream, audio_format)

    def _execute(
        self,
        task: DownloadTask,
        client: BiliClient,
        video_stream: dict[str, Any] | None,
        audio_stream: dict[str, Any] | None,
        audio_format: str,
    ) -> None:
        work = self.tmp_dir / task.id
        work.mkdir(parents=True, exist_ok=True)
        ffmpeg = find_ffmpeg()
        try:
            task.status = "downloading"
            task.stage = "探测文件大小"
            session = make_session(client)

            video_path = work / "video.m4s"
            audio_path = work / "audio.m4s"

            # 预先探测大小，用于整体进度
            video_size = audio_size = 0
            if video_stream:
                video_size, _ = probe_size(session, video_stream["url"])
            if audio_stream:
                audio_size, _ = probe_size(session, audio_stream["url"])
            total = (video_size or 0) + (audio_size or 0)
            if total <= 0:
                total = sum(
                    int(s.get("bandwidth", 0)) * 1000 // 8
                    for s in (video_stream, audio_stream)
                    if s
                )
            task.set_total(total)

            if video_stream:
                task.stage = "下载视频流"
                self._fetch(task, session, video_stream, video_path, video_size)

            if audio_stream:
                if task.cancel.is_set():
                    raise _Cancelled()
                task.stage = "下载音频流"
                self._fetch(task, session, audio_stream, audio_path, audio_size)

            if task.cancel.is_set():
                raise _Cancelled()

            task.status = "merging"
            task.stage = "ffmpeg 处理中"
            if not ffmpeg:
                raise RuntimeError("未找到 ffmpeg，无法合并音视频。请安装 ffmpeg 或 pip install imageio-ffmpeg")

            if task.mode == "audio":
                out = task.filepath
                to_mp3 = out.suffix.lower() == ".mp3"
                if audio_path.exists():
                    extract_audio(ffmpeg, audio_path, out, to_mp3)
                elif video_path.exists():  # 极少数视频只有合并流
                    extract_audio(ffmpeg, video_path, out, to_mp3)
                else:
                    raise RuntimeError("没有可用的媒体流")
            else:
                if video_stream and audio_stream and audio_path.exists():
                    codec = video_stream.get("codec", "avc1")
                    merge_av(ffmpeg, video_path, audio_path, task.filepath, codec)
                elif video_path.exists():
                    shutil.move(str(video_path), str(task.filepath))
                else:
                    raise RuntimeError("没有可用的视频流")

            task.downloaded = task.total
            task.status = "done"
            task.stage = "已完成"

        except _Cancelled:
            task.status = "canceled"
            task.stage = "已取消"
        except Exception as exc:
            task.status = "error"
            task.stage = "失败"
            task.error = str(exc)
            task.filepath.unlink(missing_ok=True)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _fetch(
        self,
        task: DownloadTask,
        session: requests.Session,
        stream: dict[str, Any],
        dest: Path,
        expect_size: int | None,
    ) -> None:
        urls = [stream["url"], *(stream.get("backup") or [])]
        last_err: Exception | None = None
        for url in urls:
            if not url:
                continue
            try:
                download_stream(
                    url,
                    dest,
                    session,
                    threads=self.threads,
                    on_bytes=task.add_bytes,
                    cancel=task.cancel,
                    expect_size=expect_size,
                )
                return
            except _Cancelled:
                raise
            except Exception as exc:  # 主 URL 失败时尝试备用 CDN
                last_err = exc
                dest.unlink(missing_ok=True)
                for leftover in dest.parent.glob(f"{dest.name}.part*"):
                    leftover.unlink(missing_ok=True)
        raise RuntimeError(f"下载失败：{last_err}")


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024:
            return f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}TB"
