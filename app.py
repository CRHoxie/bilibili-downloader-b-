"""B 站视频下载器 —— Web 服务入口。"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bili.downloader import TaskManager, find_ffmpeg
from bili.parser import ShortLinkError, extract_target, get_streams, get_video_info
from bili.wbi import BiliClient, BiliError

BASE_DIR = Path(__file__).resolve().parent

# 打包成 exe 后：只读资源在解包目录里，用户数据放在 exe 同目录，
# 这样下载的文件和配置不会跟着临时目录一起消失
if getattr(sys, "frozen", False):
    RES_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
    DATA_DIR = Path(sys.executable).resolve().parent
else:
    RES_DIR = BASE_DIR
    DATA_DIR = BASE_DIR

CONFIG_PATH = DATA_DIR / "config.json"
# 下载目录跟着程序走，避免换机器后路径失效
DOWNLOAD_DIR = DATA_DIR / "downloads"
STATIC_DIR = RES_DIR / "static"

DEFAULT_CONFIG: dict[str, Any] = {"sessdata": "", "threads": 8, "use_proxy": False}


# ------------------------------------------------------------------ 配置

def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


config = load_config()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

client = BiliClient(config.get("sessdata", ""), use_proxy=bool(config.get("use_proxy")))
manager = TaskManager(DOWNLOAD_DIR, threads=int(config.get("threads", 8)))

app = FastAPI(title="B 站视频下载器", docs_url=None, redoc_url=None)


# ------------------------------------------------------------- 解析进度
# 本地单用户应用，同一时刻只会有一个解析在进行，所以用一个全局状态就够，
# 前端在发起解析请求的同时轮询这个状态来画进度条。

_parse_lock = threading.Lock()
_parse_state: dict[str, Any] = {
    "seq": 0,          # 每次解析自增，前端用它区分「是不是新的一轮」
    "active": False,
    "percent": 0,
    "stage": "",
}


def parse_progress_reset() -> int:
    with _parse_lock:
        _parse_state["seq"] += 1
        _parse_state["active"] = True
        _parse_state["percent"] = 0
        _parse_state["stage"] = "正在识别链接"
        return _parse_state["seq"]


def parse_progress(percent: int, stage: str) -> None:
    with _parse_lock:
        # 进度只增不减：并发上报或重试时不会出现回跳
        _parse_state["percent"] = max(_parse_state["percent"], int(percent))
        _parse_state["stage"] = stage


def parse_progress_finish(stage: str = "") -> None:
    with _parse_lock:
        _parse_state["active"] = False
        if stage:
            _parse_state["stage"] = stage
        if _parse_state["percent"] >= 100:
            _parse_state["percent"] = 100


# ------------------------------------------------------------------ 模型

class ParseReq(BaseModel):
    url: str = Field(..., description="视频链接、BV 号或分享文案")


class StreamsReq(BaseModel):
    bvid: str
    cid: int


class DownloadReq(BaseModel):
    bvid: str
    cid: int
    qn: int
    codec: str = "avc1"
    audio_id: int | None = None
    title: str
    mode: str = "video"  # video | audio
    audio_format: str = "m4a"


class SettingsReq(BaseModel):
    sessdata: str | None = None
    threads: int | None = None
    use_proxy: bool | None = None


# ------------------------------------------------------------------ 工具

def strip_urls(payload: dict[str, Any]) -> dict[str, Any]:
    """响应里不返回直链，避免泄漏签名 URL 并减小传输体积。"""
    data = json.loads(json.dumps(payload, default=str))
    for q in data.get("qualities", []):
        q.pop("streams", None)
    for a in data.get("audios", []):
        a.pop("url", None)
        a.pop("backup", None)
    return data


# ------------------------------------------------------------------ 路由

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/parse/progress")
def api_parse_progress() -> dict[str, Any]:
    """解析进度快照，供前端在 /api/parse 进行期间轮询。"""
    with _parse_lock:
        return dict(_parse_state)


@app.post("/api/parse")
def api_parse(body: ParseReq) -> dict[str, Any]:
    parse_progress_reset()
    try:
        parse_progress(12, "正在识别链接")
        target = extract_target(body.url)

        parse_progress(26, "正在获取视频信息")
        info = get_video_info(client, target)

        parse_progress(58, "正在拉取播放流")
        streams = get_streams(client, info["bvid"], info["current_cid"])

        parse_progress(90, "正在整理清晰度")
        payload = {"info": info, "streams": strip_urls(streams), "logged_in": client.logged_in}

        parse_progress(100, "即将完成")
        parse_progress_finish()
        return payload
    except ShortLinkError as exc:
        parse_progress_finish("链接无法识别")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BiliError as exc:
        parse_progress_finish(exc.message)
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except Exception as exc:
        parse_progress_finish("解析失败")
        raise HTTPException(status_code=500, detail=f"解析失败：{exc}") from exc


@app.post("/api/streams")
def api_streams(body: StreamsReq) -> dict[str, Any]:
    try:
        streams = get_streams(client, body.bvid, body.cid)
    except BiliError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    return {"streams": strip_urls(streams), "logged_in": client.logged_in}


@app.post("/api/download")
def api_download(body: DownloadReq) -> dict[str, Any]:
    try:
        streams = get_streams(client, body.bvid, body.cid)
    except BiliError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    qualities = streams.get("qualities") or []
    if not qualities:
        raise HTTPException(status_code=400, detail="没有可下载的清晰度")

    quality = next((q for q in qualities if q["qn"] == body.qn), qualities[0])
    codec = body.codec if body.codec in quality["streams"] else quality["codecs"][0]
    video_stream = {**quality["streams"][codec], "codec": codec}

    audios = streams.get("audios") or []
    audio_stream = None
    if audios:
        audio_stream = next((a for a in audios if a["id"] == body.audio_id), audios[0])
        if body.mode == "audio" and body.audio_format == "mp3":
            audio_stream = audios[0]

    label = quality["label"]

    task = manager.create(
        client=client,
        title=body.title,
        quality_label=label,
        video_stream=video_stream if body.mode == "video" else None,
        audio_stream=audio_stream,
        mode=body.mode,
        audio_format=body.audio_format,
    )
    return {"task": task.to_dict()}


@app.get("/api/tasks")
def api_tasks() -> dict[str, Any]:
    return {"tasks": manager.list()}


@app.get("/api/tasks/{task_id}")
def api_task(task_id: str) -> dict[str, Any]:
    task = manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"task": task.to_dict()}


@app.post("/api/tasks/{task_id}/cancel")
def api_cancel(task_id: str) -> dict[str, Any]:
    ok = manager.cancel(task_id)
    return {"ok": ok}


@app.get("/api/tasks/{task_id}/file")
def api_file(task_id: str) -> FileResponse:
    task = manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status != "done" or not task.filepath.exists():
        raise HTTPException(status_code=409, detail="文件尚未就绪")
    return FileResponse(
        task.filepath,
        filename=task.filepath.name,
        media_type="application/octet-stream",
    )


@app.get("/api/settings")
def api_get_settings() -> dict[str, Any]:
    return {
        "logged_in": client.is_logged_in() if client.logged_in else False,
        "has_cookie": bool(config.get("sessdata")),
        "threads": int(config.get("threads", 8)),
        "use_proxy": bool(config.get("use_proxy")),
        "download_dir": str(DOWNLOAD_DIR),
        "ffmpeg": bool(find_ffmpeg()),
    }


@app.post("/api/settings")
def api_set_settings(body: SettingsReq) -> dict[str, Any]:
    if body.sessdata is not None:
        raw = body.sessdata.strip()
        # 允许用户直接粘贴整段 Cookie
        if "SESSDATA=" in raw:
            for part in raw.replace("; ", ";").split(";"):
                if part.strip().startswith("SESSDATA="):
                    raw = part.split("=", 1)[1].strip()
                    break
        config["sessdata"] = raw
        client.set_sessdata(raw)
        save_config(config)
    if body.threads is not None:
        threads = max(1, min(16, int(body.threads)))
        config["threads"] = threads
        manager.threads = threads
        save_config(config)
    if body.use_proxy is not None:
        config["use_proxy"] = bool(body.use_proxy)
        client.session.trust_env = bool(body.use_proxy)
        save_config(config)
    return api_get_settings()


@app.post("/api/open-folder")
def api_open_folder() -> dict[str, Any]:
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(DOWNLOAD_DIR)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(DOWNLOAD_DIR)])
        else:
            subprocess.Popen(["xdg-open", str(DOWNLOAD_DIR)])
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
