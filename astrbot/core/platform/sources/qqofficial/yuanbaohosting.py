"""腾讯元宝图床：用元宝 genUploadInfo 取 COS 临时凭证后直传，返回带签名的直链。

需登录 Cookie（``YUANBAO_COOKIE``，从浏览器 genUploadInfo 请求里复制完整 Cookie 头）。
上限 20MB。返回的是 6 小时有效期的签名 URL——QQ 在发送时即时下载转存，故足够用。

可选经代理访问，见 :mod:`._imagehost_http`。
"""

from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ._imagehost_http import http_kwargs

DEFAULT_TIMEOUT = 30.0
MAX_IMAGE_BYTES = 20 * 1024 * 1024

_ORIGIN = "https://yuanbao.tencent.com"
_GEN_UPLOAD_INFO = f"{_ORIGIN}/api/resource/genUploadInfo"
_AGENT_ID = "naQivTmsDa"
_WEB_VERSION = "2.74.2"
_COMMIT_TAG = "0d878d3b"
_UPLOAD_ENDPOINT = "accelerate"  # 上传走全球加速
_OUTPUT_ENDPOINT = "regional"  # 直链走地域节点
_SIGNED_URL_TTL = 6 * 60 * 60  # 签名直链有效期（秒）
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

# genUploadInfo 返回里 COS 临时凭证的必备字段
_REQUIRED_INFO_FIELDS = frozenset(
    {
        "bucketName",
        "region",
        "location",
        "resourceUrl",
        "encryptTmpSecretId",
        "encryptTmpSecretKey",
        "encryptToken",
    }
)


@dataclass(frozen=True)
class UploadResult:
    url: str
    public_url: str
    size_bytes: int
    elapsed_seconds: float

    @property
    def speed_mb_s(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return self.size_bytes / 1024 / 1024 / self.elapsed_seconds


def detect_mime(filename: str | None, data: bytes) -> str:
    import mimetypes

    if filename:
        ct, _ = mimetypes.guess_type(filename)
        if ct and ct.startswith("image/"):
            return ct
    if data[:8].startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"GIF":
        return "image/gif"
    return "image/jpeg"


def _parse_cookie_field(cookie: str, name: str) -> str:
    prefix = f"{name}="
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith(prefix):
            return part[len(prefix) :]
    return ""


def _quote(value: Any) -> str:
    return urllib.parse.quote(str(value), safe="-_.~")


def _canonical_path(path: str) -> str:
    return urllib.parse.quote(urllib.parse.unquote(path), safe="/-_.~")


def _cos_authorization(
    *,
    method: str,
    path: str,
    secret_id: str,
    secret_key: str,
    start_time: int,
    expired_time: int,
    headers: dict[str, str],
) -> str:
    """生成腾讯云 COS 请求签名（q-sign-algorithm=sha1）。"""
    normalized = {
        k.strip().lower(): " ".join(str(v).strip().split()) for k, v in headers.items()
    }
    names = sorted(normalized)
    canonical_headers = "&".join(f"{_quote(n)}={_quote(normalized[n])}" for n in names)
    key_time = f"{int(start_time)};{int(expired_time)}"
    http_string = "\n".join(
        [method.lower(), _canonical_path(path), "", canonical_headers, ""]
    )
    sign_key = hmac.new(
        secret_key.encode(), key_time.encode(), hashlib.sha1
    ).hexdigest()
    sts = "\n".join(
        ["sha1", key_time, hashlib.sha1(http_string.encode()).hexdigest(), ""]
    )
    signature = hmac.new(sign_key.encode(), sts.encode(), hashlib.sha1).hexdigest()
    return (
        "q-sign-algorithm=sha1"
        f"&q-ak={_quote(secret_id)}"
        f"&q-sign-time={key_time}"
        f"&q-key-time={key_time}"
        f"&q-header-list={';'.join(_quote(n) for n in names)}"
        f"&q-url-param-list=&q-signature={signature}"
    )


def _credential_times(info: dict[str, Any], *, ttl: int) -> tuple[int, int]:
    now = int(time.time())
    start = int(info.get("startTime") or now) - 1
    credential_end = int(info.get("expiredTime") or now + ttl)
    end = min(now + ttl, credential_end - 1)
    if end <= now:
        raise RuntimeError("元宝 COS 临时凭证已过期")
    return start, end


def _cos_object_url(info: dict[str, Any], *, endpoint: str) -> str:
    bucket = str(info["bucketName"]).strip()
    location = str(info["location"]).lstrip("/")
    if endpoint == "accelerate":
        host = f"{bucket}.cos.accelerate.myqcloud.com"
    elif endpoint == "regional":
        host = f"{bucket}.cos.{str(info['region']).strip()}.myqcloud.com"
    else:
        raise ValueError(f"不支持的 COS endpoint: {endpoint}")
    return f"https://{host}/{urllib.parse.quote(location, safe='/-_.~')}"


def _unwrap_upload_info(payload: Any) -> dict[str, Any]:
    current = payload
    for _ in range(4):
        if isinstance(current, dict) and _REQUIRED_INFO_FIELDS.issubset(current):
            return current
        if isinstance(current, dict) and isinstance(current.get("data"), dict):
            current = current["data"]
            continue
        break
    raise RuntimeError("元宝 upload-info 响应缺少 COS 凭证字段")


def _make_signed_get_url(info: dict[str, Any], *, endpoint: str) -> str:
    parsed = urllib.parse.urlsplit(_cos_object_url(info, endpoint=endpoint))
    path = _canonical_path(parsed.path)
    start, end = _credential_times(info, ttl=_SIGNED_URL_TTL)
    auth = _cos_authorization(
        method="GET",
        path=path,
        secret_id=str(info["encryptTmpSecretId"]),
        secret_key=str(info["encryptTmpSecretKey"]),
        start_time=start,
        expired_time=end,
        headers={"host": parsed.netloc},
    )
    query = auth + "&x-cos-security-token=" + _quote(info["encryptToken"])
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))


@dataclass(frozen=True)
class YuanbaoConfig:
    cookie: str


class YuanbaoImageHost:
    """腾讯元宝图床客户端，需登录 Cookie。接口与 :class:`ChatGLMImageHost` 对齐。"""

    def __init__(
        self, config: YuanbaoConfig, *, timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        self.config = config
        self.timeout = timeout

    @classmethod
    def from_env(
        cls,
        *,
        env_file: str | Path | None = None,
        cookie: str | None = None,
    ) -> YuanbaoImageHost:
        import os

        if env_file is not None:
            from dotenv import load_dotenv

            load_dotenv(Path(env_file))

        resolved = (cookie or os.getenv("YUANBAO_COOKIE", "")).strip()
        if not resolved:
            raise RuntimeError("请先设置 YUANBAO_COOKIE（元宝登录 Cookie）")
        return cls(YuanbaoConfig(cookie=resolved))

    def _upload_info_headers(self) -> dict[str, str]:
        cookie = self.config.cookie
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": _ORIGIN,
            "Referer": f"{_ORIGIN}/chat/{_AGENT_ID}",
            "User-Agent": _UA,
            "X-AgentID": _AGENT_ID,
            "X-Instance-ID": "5",
            "X-Language": "en-US",
            "X-Platform": "mac",
            "X-Requested-With": "XMLHttpRequest",
            "X-Source": "web",
            "X-WebVersion": _WEB_VERSION,
            "X-os_version": "Mac OS(10.15.7)-Blink",
            "X-webdriver": "0",
            "X-ybuitest": "0",
            "x-commit-tag": _COMMIT_TAG,
            "Cookie": cookie,
        }
        x_id = _parse_cookie_field(cookie, "hy_user")
        if x_id:
            headers["X-ID"] = x_id
            headers["T-UserID"] = x_id
        device_id = _parse_cookie_field(cookie, "_qimei_uuid42")
        if device_id:
            headers["X-HY93"] = device_id
            headers["X-device-id"] = device_id
        return headers

    def upload_bytes(self, body: bytes, *, filename: str | None = None) -> str:
        if not body:
            raise RuntimeError("无效的图片数据")
        if len(body) > MAX_IMAGE_BYTES:
            raise RuntimeError(f"图片 {len(body)} 字节超过 20MB 限制")

        image_name = filename or "image.jpg"
        info_resp = httpx.post(
            _GEN_UPLOAD_INFO,
            json={"fileName": image_name, "docFrom": "localDoc", "docOpenId": ""},
            headers=self._upload_info_headers(),
            **http_kwargs(self.timeout),
        )
        if info_resp.status_code != 200:
            raise RuntimeError(f"元宝 upload-info 失败 (HTTP {info_resp.status_code})")
        info = _unwrap_upload_info(info_resp.json())
        info["_fileId"] = uuid.uuid4().hex

        mime = detect_mime(filename, body)
        parsed = urllib.parse.urlsplit(_cos_object_url(info, endpoint=_UPLOAD_ENDPOINT))
        host = parsed.netloc
        path = _canonical_path(parsed.path)
        start, end = _credential_times(info, ttl=_SIGNED_URL_TTL)
        auth = _cos_authorization(
            method="PUT",
            path=path,
            secret_id=str(info["encryptTmpSecretId"]),
            secret_key=str(info["encryptTmpSecretKey"]),
            start_time=start,
            expired_time=end,
            headers={"content-type": mime, "host": host},
        )
        cos_endpoint = urllib.parse.urlunsplit(
            (parsed.scheme, host, path, parsed.query, "")
        )

        cos_resp = httpx.put(
            cos_endpoint,
            content=body,
            headers={
                "Authorization": auth,
                "Content-Type": mime,
                "Host": host,
                "x-cos-security-token": str(info["encryptToken"]),
            },
            **http_kwargs(120),
        )
        if cos_resp.status_code >= 300:
            raise RuntimeError(f"元宝 COS 上传失败 (HTTP {cos_resp.status_code})")

        return _make_signed_get_url(info, endpoint=_OUTPUT_ENDPOINT)

    def upload_file(
        self,
        file_path: str | Path,
        **_: object,  # 吞掉 public_url 等其它图床接口参数
    ) -> UploadResult:
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"文件不存在: {path}")

        body = path.read_bytes()
        start = time.time()
        url = self.upload_bytes(body, filename=path.name)
        elapsed = time.time() - start

        return UploadResult(
            url=url,
            public_url=url,
            size_bytes=len(body),
            elapsed_seconds=elapsed,
        )
