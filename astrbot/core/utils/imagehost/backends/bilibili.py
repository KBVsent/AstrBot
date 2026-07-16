"""Bilibili 图床：把本地图片上传到 B 站 Web 图片上传接口，借助 B 站 CDN 得到公网外链。

需在 image_host 配置项里提供 B 站登录 Cookie：
- ``sessdata`` / ``csrf_token``：B 站登录 Cookie（SESSDATA 与 bili_jct）
- ``bucket``：上传桶名（默认 ``openplatform``）

上限 20MB。
"""

from __future__ import annotations

import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

DEFAULT_TIMEOUT = 30.0
MAX_IMAGE_BYTES = 20 * 1024 * 1024
DEFAULT_BUCKET = "openplatform"

_ENDPOINT = "https://api.bilibili.com/x/upload/web/image"
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


@dataclass(frozen=True)
class BilibiliConfig:
    sessdata: str
    csrf_token: str
    bucket: str = DEFAULT_BUCKET


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


def detect_mime(data: bytes, filename: str | None = None) -> str:
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


class BilibiliImageHost:
    """Bilibili 图床客户端，需登录 Cookie。接口与 :class:`ChatGLMImageHost` 对齐。"""

    def __init__(
        self, config: BilibiliConfig, *, timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        self.config = config
        self.timeout = timeout

    @classmethod
    def from_config(cls, entry: dict) -> BilibiliImageHost:
        sessdata = str(entry.get("sessdata", "")).strip()
        csrf_token = str(entry.get("csrf_token", "")).strip()
        if not sessdata or not csrf_token:
            raise RuntimeError(
                "请先配置 bilibili 图床的 sessdata 和 csrf_token（B 站登录 Cookie）"
            )
        bucket = str(entry.get("bucket", DEFAULT_BUCKET)).strip() or DEFAULT_BUCKET
        return cls(
            BilibiliConfig(sessdata=sessdata, csrf_token=csrf_token, bucket=bucket)
        )

    def upload_bytes(self, body: bytes, *, filename: str | None = None) -> str:
        if not body:
            raise RuntimeError("无效的图片数据")
        if len(body) > MAX_IMAGE_BYTES:
            raise RuntimeError(f"图片 {len(body)} 字节超过 20MB 限制")

        mime_type = detect_mime(body, filename)
        ext = mime_type.split("/")[-1] if "/" in mime_type else "jpg"

        resp = httpx.post(
            _ENDPOINT,
            files={"file": (f"image.{ext}", body, mime_type)},
            data={"bucket": self.config.bucket, "csrf": self.config.csrf_token},
            headers={
                "User-Agent": _USER_AGENT,
                "Cookie": f"SESSDATA={self.config.sessdata}; bili_jct={self.config.csrf_token}",
            },
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Bilibili 上传失败 (HTTP {resp.status_code})")

        try:
            data = resp.json()
        except Exception as exc:
            raise RuntimeError("Bilibili 上传失败：响应非 JSON") from exc
        if data.get("code") != 0:
            raise RuntimeError(
                f"Bilibili 业务错误: code={data.get('code')} msg={data.get('message', '')}"
            )

        url = (data.get("data") or {}).get("location", "")
        if not url:
            raise RuntimeError("Bilibili 上传失败：响应无 location")
        if url.startswith("http://"):
            url = "https://" + url[7:]
        return url

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
