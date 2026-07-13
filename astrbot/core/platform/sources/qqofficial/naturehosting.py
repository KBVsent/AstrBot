"""Nature 图床：用内置密钥直传腾讯云 COS（地域节点），借助 download.nature.qq.com CDN 得到外链。

**无需任何登录凭证**（密钥内置），开箱即用；上限 100MB，仅支持 PNG/JPG/WebP/GIF。

可选经代理访问，见 :mod:`._imagehost_http`。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from base64 import b64decode
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from ._imagehost_http import http_kwargs

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=120.0, pool=10.0)
MAX_IMAGE_BYTES = 100 * 1024 * 1024

# 内置 COS 密钥（借用某游戏服务的公开直传配置，可能随时失效，失败时自动切换其它图床）。
_SECRET_ID = b64decode(b"QUtJRHJiOFRiZlhBWnJ5cVRzMnlnQlNWSkdzSFRROGR0d21O").decode()
_SECRET_KEY = b64decode(b"UFphTnhLV2ZjTHAzNHJQanJ1dGtXRnlaQ2N5REdCMGQ=").decode()
_BUCKET = "sgame-data-service-1252931805"
_REGION = "ap-nanjing"
_HOST = f"{_BUCKET}.cos.{_REGION}.myqcloud.com"
_CDN = "https://download.nature.qq.com"
_PATH_PREFIX = "SnsShare/SocialProfile"


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


def detect_mime(data: bytes) -> tuple[str | None, str | None]:
    """返回 (mime, 扩展名)；不支持的格式返回 (None, None)。"""
    if data[:8].startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    if data[:3] == b"GIF":
        return "image/gif", "jpg"
    return None, None


def _sign_auth(upload_path: str, host: str, ts: int) -> str:
    sign_time = f"{ts};{ts + 3600}"
    sign_key = hmac.new(_SECRET_KEY.encode(), sign_time.encode(), "sha1").hexdigest()
    fmt = f"put\n/{upload_path}\n\nhost={host}\n"
    sts = f"sha1\n{sign_time}\n{hashlib.sha1(fmt.encode()).hexdigest()}\n"
    sig = hmac.new(sign_key.encode(), sts.encode(), "sha1").hexdigest()
    return (
        f"q-sign-algorithm=sha1&q-ak={_SECRET_ID}"
        f"&q-sign-time={sign_time}&q-key-time={sign_time}"
        f"&q-header-list=host&q-url-param-list=&q-signature={sig}"
    )


class NatureImageHost:
    """Nature 图床客户端，无需凭证。接口与 :class:`ChatGLMImageHost` 对齐。"""

    def __init__(self, *, timeout: httpx.Timeout | float = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout

    @classmethod
    def from_env(cls, *, env_file: str | Path | None = None) -> NatureImageHost:
        # 无需登录凭证；保留 env_file 形参以对齐其它图床的 from_env 签名。
        return cls()

    def upload_bytes(self, body: bytes) -> str:
        if not body:
            raise RuntimeError("无效的图片数据")
        if len(body) > MAX_IMAGE_BYTES:
            raise RuntimeError(f"图片 {len(body)} 字节超过 100MB 限制")

        mime, ext = detect_mime(body)
        if not mime:
            raise RuntimeError("Nature 仅支持 PNG/JPG/WebP/GIF 格式")
        content_type = "image/jpeg" if mime == "image/gif" else mime

        ts = int(datetime.now().timestamp())
        rand = os.urandom(4).hex()
        upload_path = f"{_PATH_PREFIX}/{ts}_{rand}.{ext}"

        resp = httpx.put(
            f"https://{_HOST}/{upload_path}",
            content=body,
            headers={
                "Host": _HOST,
                "Content-Type": content_type,
                "Authorization": _sign_auth(upload_path, _HOST, ts),
            },
            **http_kwargs(self.timeout),
        )
        if resp.status_code != 200:
            detail = resp.text[:200].strip()
            reason = f"Nature 上传失败 (HTTP {resp.status_code})"
            if detail:
                reason += f": {detail}"
            raise RuntimeError(reason)
        return f"{_CDN}/{upload_path}"

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
        url = self.upload_bytes(body)
        elapsed = time.time() - start

        return UploadResult(
            url=url,
            public_url=url,
            size_bytes=len(body),
            elapsed_seconds=elapsed,
        )
