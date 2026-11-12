"""视频链接识别、视频信息与播放流解析。"""

from __future__ import annotations

import re
from typing import Any

import requests

from .wbi import USER_AGENT, BiliClient, BiliError

# 从分享文案里捞出 URL，容忍中文标点与括号
URL_RE = re.compile(r"https?://[^\s\"'<>，。；、）】]+", re.I)
BV_RE = re.compile(r"(BV[1-9A-HJ-NP-Za-km-z]{10})")
AV_RE = re.compile(r"(?:^|[^0-9a-zA-Z])av(\d+)", re.I)
PAGE_RE = re.compile(r"[?&]p=(\d+)")
EP_RE = re.compile(r"/(?:ep|ss)(\d+)", re.I)

QUALITY_LABELS: dict[int, str] = {
    127: "8K 超高清",
    126: "杜比视界",
    125: "HDR 真彩",
    120: "4K 超清",
    116: "1080P60 高帧率",
    112: "1080P+ 高码率",
    80: "1080P 高清",
    74: "720P60 高帧率",
    64: "720P 高清",
    32: "480P 清晰",
    16: "360P 流畅",
    6: "240P 极速",
}

AUDIO_LABELS: dict[int, str] = {
    30216: "64K 普通",
    30232: "132K 标准",
    30280: "192K 高品质",
    30250: "杜比全景声",
    30251: "Hi-Res 无损",
}

# codecid 与编码名称的对应，avc 兼容性最好，优先选择
CODEC_BY_ID = {7: "avc1", 12: "hevc", 13: "av1"}
CODEC_PRIORITY = {"avc1": 0, "hevc": 1, "av1": 2}


class ShortLinkError(RuntimeError):
    """短链或输入无法解析为视频。"""


def extract_target(text: str) -> dict[str, Any]:
    """从任意用户输入中提取视频标识。

    支持：BV 号、av 号、完整链接、b23.tv 短链、带分享文案的文本。
    返回 {"bvid"|"aid", "page", "raw_url"}。
    """
    text = (text or "").strip()
    if not text:
        raise ShortLinkError("请输入视频链接或 BV 号")

    urls = URL_RE.findall(text)
    page = 1
    target_url = urls[0] if urls else ""

    if target_url:
        m = PAGE_RE.search(target_url)
        if m:
            page = max(1, int(m.group(1)))
        if EP_RE.search(target_url) and not BV_RE.search(target_url):
            raise ShortLinkError("暂不支持番剧 / 影视专区链接，请输入普通投稿视频的链接")

    # 1) 文本里直接有 BV / av 号，最省事
    m = BV_RE.search(text)
    if m:
        return {"bvid": m.group(1), "page": page, "raw_url": target_url}
    m = AV_RE.search(text)
    if m:
        return {"aid": int(m.group(1)), "page": page, "raw_url": target_url}

    # 2) 短链（b23.tv / bili2233.cn）需要跟随跳转
    if target_url and re.search(r"(b23\.tv|bili2233\.cn|bili2233\.com)", target_url, re.I):
        final = _follow_short_link(target_url)
        m = BV_RE.search(final)
        if m:
            pm = PAGE_RE.search(final)
            return {
                "bvid": m.group(1),
                "page": int(pm.group(1)) if pm else page,
                "raw_url": final,
            }
        m = AV_RE.search(final)
        if m:
            return {"aid": int(m.group(1)), "page": page, "raw_url": final}
        raise ShortLinkError("短链解析失败，可能已失效，请直接粘贴完整视频链接")

    # 3) 完整链接里带 av 号但被正则漏掉的情况
    if target_url:
        m = re.search(r"video/(?:BV[0-9A-Za-z]+|av\d+)", target_url, re.I)
        if m:
            seg = m.group(0).split("/")[-1]
            if seg.lower().startswith("av"):
                return {"aid": int(seg[2:]), "page": page, "raw_url": target_url}
            return {"bvid": seg, "page": page, "raw_url": target_url}

    raise ShortLinkError("没能识别出视频，请粘贴形如 https://www.bilibili.com/video/BV1xx411c7mD 的链接")


def _follow_short_link(url: str) -> str:
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"},
            timeout=12,
            allow_redirects=True,
        )
        return resp.url or url
    except Exception as exc:
        raise ShortLinkError(f"短链请求失败：{exc}") from exc


def get_video_info(client: BiliClient, target: dict[str, Any]) -> dict[str, Any]:
    """获取视频基础信息与分 P 列表。"""
    params: dict[str, Any] = {}
    if target.get("bvid"):
        params["bvid"] = target["bvid"]
    else:
        params["aid"] = target["aid"]

    data = client.get("/x/web-interface/wbi/view", params)
    pages = [
        {
            "cid": p.get("cid"),
            "page": p.get("page", i + 1),
            "part": p.get("part") or f"P{p.get('page', i + 1)}",
            "duration": p.get("duration", 0),
        }
        for i, p in enumerate(data.get("pages") or [])
    ]
    if not pages:
        raise BiliError(-1, "该视频没有可播放的分集")

    owner = data.get("owner") or {}
    stat = data.get("stat") or {}
    # 优先使用分 P 参数指定的集数
    current = pages[0]
    want = int(target.get("page") or 1)
    for p in pages:
        if p["page"] == want:
            current = p
            break

    return {
        "bvid": data.get("bvid") or target.get("bvid"),
        "aid": data.get("aid"),
        "title": (data.get("title") or "未命名视频").strip(),
        "cover": data.get("pic") or "",
        "duration": data.get("duration", 0),
        "desc": (data.get("desc") or "").strip(),
        "pubdate": data.get("pubdate", 0),
        "owner": {"name": owner.get("name", ""), "face": owner.get("face", ""), "mid": owner.get("mid")},
        "stat": {
            "view": stat.get("view", 0),
            "danmaku": stat.get("danmaku", 0),
            "like": stat.get("like", 0),
            "coin": stat.get("coin", 0),
            "favorite": stat.get("favorite", 0),
        },
        "pages": pages,
        "current_page": current["page"],
        "current_cid": current["cid"],
    }


def get_streams(client: BiliClient, bvid: str | None, cid: int) -> dict[str, Any]:
    """获取视频的真实播放流，并按清晰度归类。"""
    params: dict[str, Any] = {"cid": cid, "fnval": 4048, "fnver": 0, "fourk": 1}
    if bvid:
        params["bvid"] = bvid

    data = client.get("/x/player/wbi/playurl", params)

    if data.get("is_preview"):
        raise BiliError(-1, "该视频为预览片段，需要大会员或购买后才能完整播放")

    dash = data.get("dash")
    if not dash:
        if data.get("durl"):
            raise BiliError(-1, "该视频仅提供 FLV/MP4 整段流（多为番剧或付费内容），暂不支持")
        raise BiliError(-1, "未获取到播放流，可能是付费、地区限制或需要登录")

    # ---- 视频流按清晰度归组 ----
    grouped: dict[int, dict[str, Any]] = {}
    for v in dash.get("video") or []:
        qn = int(v.get("id", 0))
        codec = CODEC_BY_ID.get(int(v.get("codecid", 0)))
        if not codec:
            codecs = str(v.get("codecs", ""))
            codec = "avc1" if codecs.startswith("avc") else "hevc" if codecs.startswith(("hev", "hvc")) else "av1"
        codec = CODEC_BY_ID.get(int(v.get("codecid", 0))) or codec
        entry = grouped.setdefault(
            qn,
            {
                "qn": qn,
                "label": QUALITY_LABELS.get(qn, f"清晰度 {qn}"),
                "height": v.get("height", 0),
                "width": v.get("width", 0),
                "fps": _fps(v.get("frameRate")),
                "codecs": [],
                "bandwidth": 0,
                "streams": {},
            },
        )
        entry["codecs"].append(codec)
        entry["bandwidth"] = max(entry["bandwidth"], int(v.get("bandwidth", 0)))
        entry["streams"][codec] = {
            "url": v.get("baseUrl") or v.get("base_url"),
            "backup": (v.get("backupUrl") or v.get("backup_url") or [])[:2],
            "bandwidth": int(v.get("bandwidth", 0)),
            "size_hint": v.get("size"),
        }

    qualities = sorted(grouped.values(), key=lambda x: x["qn"], reverse=True)
    for q in qualities:
        q["codecs"].sort(key=lambda c: CODEC_PRIORITY.get(c, 9))

    # ---- 音频流 ----
    audios: list[dict[str, Any]] = []
    for a in list(dash.get("audio") or []):
        audios.append(
            {
                "id": int(a.get("id", 0)),
                "label": AUDIO_LABELS.get(int(a.get("id", 0)), f"音轨 {a.get('id')}"),
                "bandwidth": int(a.get("bandwidth", 0)),
                "codecs": a.get("codecs", ""),
                "url": a.get("baseUrl") or a.get("base_url"),
                "backup": (a.get("backupUrl") or a.get("backup_url") or [])[:2],
            }
        )
    for extra_key, extra_label in (("dolby", "杜比全景声"), ("flac", "Hi-Res 无损")):
        extra = (dash.get(extra_key) or {}).get("audio")
        if extra and extra.get("baseUrl"):
            audios.append(
                {
                    "id": int(extra.get("id", 0)) or (30250 if extra_key == "dolby" else 30251),
                    "label": extra_label,
                    "bandwidth": int(extra.get("bandwidth", 0)),
                    "codecs": extra.get("codecs", ""),
                    "url": extra.get("baseUrl"),
                    "backup": (extra.get("backupUrl") or [])[:2],
                }
            )
    # 音频按音轨 id 排序：30216(64K) < 30232(132K) < 30280(192K) < 30250(杜比) < 30251(Hi-Res)
    # 不用带宽排序，因为不少老视频各档音轨的实际码率几乎一样，会排错
    audios.sort(key=lambda x: x["id"], reverse=True)

    # ---- 需要登录才能解锁的清晰度 ----
    accept_q = data.get("accept_quality") or []
    accept_desc = data.get("accept_description") or []
    available = {q["qn"] for q in qualities}
    locked = []
    for i, qn in enumerate(accept_q):
        qn = int(qn)
        if qn in available:
            continue
        desc = accept_desc[i] if i < len(accept_desc) else QUALITY_LABELS.get(qn, str(qn))
        locked.append({"qn": qn, "label": desc})

    return {
        "qualities": qualities,
        "audios": audios,
        "locked": locked,
        "logged_in": client.logged_in,
    }


def _fps(raw: Any) -> int:
    """把 '30.000' / '60' 之类解析成整数帧率。"""
    try:
        return int(round(float(raw)))
    except (TypeError, ValueError):
        return 0
