"""ChatGLM 图床：把本地图片上传到智谱 ChatGLM 的文件上传接口，得到公网外链。

**无需任何登录凭证**，开箱即用；上限 20MB。
"""

from __future__ import annotations

import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ._imagehost_http import http_kwargs

DEFAULT_TIMEOUT = 10.0
MAX_IMAGE_BYTES = 20 * 1024 * 1024

_ENDPOINT = "https://chatglm.cn/chatglm/backend-api/assistant/file_upload"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
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


class ChatGLMImageHost:
    """ChatGLM 图床客户端，无需凭证。接口与 :class:`CosNoSdkClient` 对齐。"""

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout

    @classmethod
    def from_env(cls, *, env_file: str | Path | None = None) -> ChatGLMImageHost:
        # 无需登录凭证；保留 env_file 形参以对齐其它图床的 from_env 签名。
        return cls()

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
            headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "gzip, deflate, br"},
            **http_kwargs(self.timeout),
        )
        if resp.status_code != 200:
            raise RuntimeError(f"ChatGLM 上传失败 (HTTP {resp.status_code})")

        try:
            url = (resp.json().get("result") or {}).get("file_url", "")
        except Exception as exc:
            raise RuntimeError("ChatGLM 上传失败：响应非 JSON") from exc
        if not url:
            raise RuntimeError("ChatGLM 上传失败：响应无 file_url")
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
