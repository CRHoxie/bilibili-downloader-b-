"""B 站 Web API 客户端。

封装了 WBI 签名（2023 年后 bilibili 接口的必需签名）、请求会话、
Cookie 管理与统一的错误处理。
"""

from __future__ import annotations

import hashlib
import threading
import time
import urllib.parse
from typing import Any

import requests

# WBI 签名用的固定重排表
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

API_ROOT = "https://api.bilibili.com"

# 常见业务错误码的中文提示，比接口原始 message 更易懂
FRIENDLY_ERRORS: dict[int, str] = {
    -101: "需要登录才能访问该内容，请先在设置中填入 Cookie",
    -400: "请求参数有误",
    -403: "访问权限不足",
    -404: "视频不存在或已被删除",
    -10403: "该视频有地区限制，当前网络无法观看",
    62002: "稿件不可见（可能已被删除或设为私密）",
    62004: "稿件正在审核中，暂时无法观看",
    62012: "该视频仅 UP 主本人可见",
    87007: "充电专属视频，需要为 UP 主充电后才能观看",
    87008: "付费内容，暂不支持下载",
}


class BiliError(RuntimeError):
    """B 站接口返回的业务错误（code != 0）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{message}（错误码 {code}）")
        self.code = code
        self.message = message


class BiliClient:
    """带 WBI 签名能力的 B 站 API 客户端，线程安全。"""

    _KEYS_TTL = 600  # WBI 密钥缓存 10 分钟

    def __init__(self, sessdata: str = "", use_proxy: bool = False) -> None:
        self.session = requests.Session()
        # B 站是国内站点，默认直连。系统代理（环境变量里的 HTTP_PROXY 等）
        # 常常导致请求被劫持或长时间挂起，需要代理的用户可显式打开 use_proxy
        self.session.trust_env = use_proxy
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        self._lock = threading.Lock()
        self._keys: tuple[str, str] | None = None
        self._keys_at = 0.0
        self._buvid_ready = False
        self._sessdata = ""
        self.set_sessdata(sessdata)

    # ---------------------------------------------------------------- cookie

    def set_sessdata(self, sessdata: str) -> None:
        """设置登录凭证 SESSDATA，空字符串表示匿名访问。"""
        sessdata = (sessdata or "").strip()
        self._sessdata = sessdata
        if sessdata:
            self.session.cookies.set("SESSDATA", sessdata, domain=".bilibili.com")
        else:
            self.session.cookies.pop("SESSDATA", None)
        self._keys = None  # 登录态变化后重新取密钥

    @property
    def logged_in(self) -> bool:
        return bool(self._sessdata)

    def is_logged_in(self) -> bool:
        """向服务端确认当前 Cookie 是否仍然有效。"""
        if not self._sessdata:
            return False
        try:
            data = self.get("/x/web-interface/nav", signed=False, allow_codes=(-101,))
            return bool(data.get("isLogin"))
        except Exception:
            return False

    def _ensure_buvid(self) -> None:
        """补充设备指纹 Cookie，降低被风控拦截的概率。

        这是个可选的增强项，所以用独立的短超时、不重试：
        绝不能因为它抖一下就把整个解析流程拖住。
        """
        if self._buvid_ready:
            return
        self._buvid_ready = True
        try:
            resp = self.session.get(f"{API_ROOT}/x/frontend/finger/spi", timeout=(3, 3))
            data = (resp.json() or {}).get("data") or {}
            if data.get("b_3"):
                self.session.cookies.set("buvid3", data["b_3"], domain=".bilibili.com")
            if data.get("b_4"):
                self.session.cookies.set("buvid4", data["b_4"], domain=".bilibili.com")
        except Exception:
            pass

    # ------------------------------------------------------------- WBI 签名

    def _fetch_keys(self) -> tuple[str, str]:
        # 未登录时 nav 返回 -101，但 data 里依然带着 WBI 密钥，所以这里要容忍
        data = self.get("/x/web-interface/nav", signed=False, allow_codes=(-101,))
        wbi = data.get("wbi_img") or {}
        img = str(wbi.get("img_url", "")).rsplit("/", 1)[-1].split(".")[0]
        sub = str(wbi.get("sub_url", "")).rsplit("/", 1)[-1].split(".")[0]
        if not img or not sub:
            raise BiliError(-1, "无法获取 WBI 签名密钥")
        return img, sub

    def _mixin_key(self, img_key: str, sub_key: str) -> str:
        raw = img_key + sub_key
        return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB if i < len(raw))[:32]

    def _sign(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._keys is None or time.time() - self._keys_at > self._KEYS_TTL:
                self._keys = self._fetch_keys()
                self._keys_at = time.time()
            img_key, sub_key = self._keys
        mixin_key = self._mixin_key(img_key, sub_key)

        signed = dict(params)
        signed["wts"] = int(time.time())
        # WBI 要求：参数按 key 排序，并过滤掉值里的 !'()* 字符
        cleaned = {
            k: "".join(ch for ch in str(v) if ch not in "!'()*")
            for k, v in sorted(signed.items())
        }
        query = urllib.parse.urlencode(cleaned)
        cleaned["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
        return cleaned

    # ---------------------------------------------------------------- 请求

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = True,
        timeout: int = 15,
        allow_codes: tuple[int, ...] = (),
    ) -> Any:
        """发起 GET 请求并返回 data 字段，业务错误抛出 BiliError。

        allow_codes 中的错误码不会抛异常，用于「未登录也可取数据」这类接口。
        """
        self._ensure_buvid()
        query = self._sign(params or {}) if signed else dict(params or {})
        url = f"{API_ROOT}{path}"

        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                resp = self.session.get(url, params=query, timeout=timeout)
                payload = resp.json()
                break
            except Exception as exc:  # 网络抖动重试一次
                last_exc = exc
                if attempt == 1:
                    raise BiliError(-1, f"网络请求失败，请检查网络后重试：{exc}") from exc
                time.sleep(0.5)
        else:  # pragma: no cover
            raise BiliError(-1, f"网络请求失败：{last_exc}")

        code = payload.get("code", -1)
        if code != 0 and code not in allow_codes:
            message = FRIENDLY_ERRORS.get(code) or payload.get("message") or "接口返回异常"
            raise BiliError(code, message)
        return payload.get("data") or {}
