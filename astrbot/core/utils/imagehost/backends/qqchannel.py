"""QQ 频道图床：把本地图片上传到一个中转子频道消息，借助 QQ CDN 得到公网外链。

需在 image_host 配置项里提供：
- ``appid`` / ``secret``：QQ 机器人凭证
- ``channel_id``：用于中转上传的子频道 ID
- ``max_image_bytes``：单图上限（默认 3MB，超限自动压缩）
"""

from __future__ import annotations

import hashlib
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

DEFAULT_MAX_IMAGE_BYTES = 3 * 1024 * 1024  # 频道 file_image 上限约 3MB（错误码 304020）
DEFAULT_TIMEOUT = 30.0

_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
_QQ_API_BASE = "https://api.sgroup.qq.com"
_TOKEN_REFRESH_SKEW = 60  # 提前刷新秒数，避免边界过期


@dataclass(frozen=True)
class QQChannelConfig:
    appid: str
    secret: str
    channel_id: str
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES
    auto_compress: bool = True


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
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"GIF"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def compress_image(data: bytes, *, max_bytes: int = DEFAULT_MAX_IMAGE_BYTES) -> bytes:
    """把图片压缩到 max_bytes 以内；需要 pillow。"""
    if len(data) <= max_bytes:
        return data

    try:
        from io import BytesIO

        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("需要安装 pillow 才能压缩超限图片") from exc

    with Image.open(BytesIO(data)) as img:
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        for quality in (85, 75, 65, 55, 45, 35):
            for scale in (1.0, 0.85, 0.7, 0.55, 0.4):
                nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
                resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
                buf = BytesIO()
                resized.save(buf, format="JPEG", quality=quality, optimize=True)
                out = buf.getvalue()
                if len(out) <= max_bytes:
                    return out

    raise RuntimeError(f"无法将图片压缩到 {max_bytes} 字节以内，请换更小的原图")


class QQChannelImageHost:
    """QQ 频道图床客户端，接口与 :class:`CosNoSdkClient` 对齐（``upload_file`` -> URL）。"""

    def __init__(self, config: QQChannelConfig) -> None:
        self.config = config
        self._token: str | None = None
        self._token_expire_at: float = 0.0

    @classmethod
    def from_config(cls, entry: dict) -> QQChannelImageHost:
        appid = str(entry.get("appid", "")).strip()
        secret = str(entry.get("secret", "")).strip()
        if not appid or not secret:
            raise RuntimeError("请先配置 qqchannel 图床的 appid 和 secret")

        channel_id = str(entry.get("channel_id", "")).strip()
        if not channel_id:
            raise RuntimeError(
                "请先配置 qqchannel 图床的 channel_id（用于中转上传的子频道 ID）"
            )

        max_bytes = entry.get("max_image_bytes")
        return cls(
            QQChannelConfig(
                appid=appid,
                secret=secret,
                channel_id=channel_id,
                max_image_bytes=int(max_bytes)
                if max_bytes
                else DEFAULT_MAX_IMAGE_BYTES,
            )
        )

    def _access_token(self, *, timeout: float = DEFAULT_TIMEOUT) -> str:
        now = time.time()
        if self._token and now < self._token_expire_at:
            return self._token

        resp = httpx.post(
            _TOKEN_URL,
            json={
                "appId": str(self.config.appid),
                "clientSecret": str(self.config.secret),
            },
            timeout=timeout,
        )
        try:
            data = resp.json()
        except Exception as exc:
            raise RuntimeError(
                f"QQ Token 响应非 JSON (HTTP {resp.status_code})"
            ) from exc
        if resp.status_code != 200 or "access_token" not in data:
            raise RuntimeError(f"QQ Token 获取失败: HTTP {resp.status_code}, {data}")

        token: str = data["access_token"]
        try:
            expires_in = int(data.get("expires_in", 7200))
        except (TypeError, ValueError):
            expires_in = 7200
        self._token = token
        self._token_expire_at = now + max(0, expires_in - _TOKEN_REFRESH_SKEW)
        return token

    def upload_bytes(
        self,
        body: bytes,
        *,
        filename: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> str:
        if not body:
            raise RuntimeError("无效的图片数据")

        if self.config.auto_compress:
            body = compress_image(body, max_bytes=self.config.max_image_bytes)
        elif len(body) > self.config.max_image_bytes:
            raise RuntimeError(
                f"图片 {len(body)} 字节超过 QQ 频道限制 "
                f"({self.config.max_image_bytes} 字节)"
            )

        md5hash = hashlib.md5(body).hexdigest().upper()
        mime_type = detect_mime(body, filename)
        ext = mime_type.split("/")[-1] if "/" in mime_type else "jpg"
        token = self._access_token(timeout=timeout)

        resp = httpx.post(
            f"{_QQ_API_BASE}/channels/{self.config.channel_id}/messages",
            files={"file_image": (f"image.{ext}", body, mime_type)},
            data={"msg_id": "1"},
            headers={"Authorization": f"QQBot {token}"},
            timeout=timeout,
        )
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text[:300]
            if isinstance(detail, dict) and detail.get("code") == 304020:
                raise RuntimeError(
                    f"QQ 频道上传失败: 文件大小超限 (304020)，当前 {len(body)} 字节"
                )
            raise RuntimeError(f"QQ 频道上传失败 (HTTP {resp.status_code}): {detail}")

        return f"https://gchat.qpic.cn/qmeetpic/0/0-0-{md5hash}/0"

    def upload_file(
        self,
        file_path: str | Path,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        **_: object,  # 吞掉 public_url 等 COS 接口参数；QQ CDN 外链本即公开
    ) -> UploadResult:
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"文件不存在: {path}")

        body = path.read_bytes()
        start = time.time()
        url = self.upload_bytes(body, filename=path.name, timeout=timeout)
        elapsed = time.time() - start

        return UploadResult(
            url=url,
            public_url=url,
            size_bytes=len(body),
            elapsed_seconds=elapsed,
        )
