"""腾讯云 COS 图床：用密钥直传对象存储，返回预签名下载直链（或公开直链）。

需在 image_host 配置项里提供：
- ``secret_id`` / ``secret_key``：COS 密钥
- ``bucket`` / ``region``：桶名与地域
- ``prefix``：对象键前缀（默认 ``temp``）
- ``endpoint``：可选自定义/加速域名
"""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

DEFAULT_PREFIX = "temp"
DEFAULT_EXPIRES_SECONDS = 3600


@dataclass(frozen=True)
class CosConfig:
    secret_id: str
    secret_key: str
    bucket: str
    region: str
    prefix: str = DEFAULT_PREFIX
    endpoint: str | None = None


@dataclass(frozen=True)
class UploadResult:
    key: str
    url: str
    public_url: str
    size_bytes: int
    elapsed_seconds: float

    @property
    def speed_mb_s(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return self.size_bytes / 1024 / 1024 / self.elapsed_seconds


def make_object_key(file_path: str | Path, prefix: str = DEFAULT_PREFIX) -> str:
    path = Path(file_path)
    now = datetime.now(timezone.utc)
    date_path = now.strftime("%Y/%m/%d")
    suffix = path.suffix.lower()
    filename = f"{uuid.uuid4().hex}{suffix}"
    return "/".join(part.strip("/") for part in (prefix, date_path, filename) if part)


def normalize_endpoint(endpoint: str) -> str:
    return endpoint.removeprefix("https://").removeprefix("http://").rstrip("/")


def object_url(bucket: str, region: str, key: str) -> str:
    quoted_key = quote(key, safe="/-_.~")
    return f"https://{bucket}.cos.{region}.myqcloud.com/{quoted_key}"


def quote_sign_value(value: object) -> str:
    return quote(str(value), safe="-_.~")


def build_cos_auth(
    method: str,
    key: str,
    secret_id: str,
    secret_key: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    expires: int = DEFAULT_EXPIRES_SECONDS,
) -> str:
    headers = headers or {}
    params = params or {}

    encoded_headers = {
        quote_sign_value(name).lower(): quote_sign_value(value)
        for name, value in headers.items()
    }
    encoded_params = {
        quote_sign_value(name).lower(): quote_sign_value(value)
        for name, value in params.items()
    }

    canonical_path = "/" + quote(key.lstrip("/"), safe="/-_.~")
    canonical_params = "&".join(
        f"{name}={value}" for name, value in sorted(encoded_params.items())
    )
    canonical_headers = "&".join(
        f"{name}={value}" for name, value in sorted(encoded_headers.items())
    )
    format_string = (
        f"{method.lower()}\n{canonical_path}\n{canonical_params}\n{canonical_headers}\n"
    )

    now = int(time.time())
    sign_time = f"{now - 60};{now + expires}"
    format_digest = hashlib.sha1(format_string.encode("utf-8")).hexdigest()
    string_to_sign = f"sha1\n{sign_time}\n{format_digest}\n"
    sign_key = hmac.new(
        secret_key.encode("utf-8"),
        sign_time.encode("utf-8"),
        hashlib.sha1,
    ).hexdigest()
    signature = hmac.new(
        sign_key.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).hexdigest()

    header_list = ";".join(sorted(encoded_headers.keys()))
    param_list = ";".join(sorted(encoded_params.keys()))
    return (
        "q-sign-algorithm=sha1"
        f"&q-ak={secret_id}"
        f"&q-sign-time={sign_time}"
        f"&q-key-time={sign_time}"
        f"&q-header-list={header_list}"
        f"&q-url-param-list={param_list}"
        f"&q-signature={signature}"
    )


class CosNoSdkClient:
    def __init__(self, config: CosConfig) -> None:
        self.config = config

    @classmethod
    def from_config(cls, entry: dict) -> CosNoSdkClient:
        secret_id = str(entry.get("secret_id", "")).strip()
        secret_key = str(entry.get("secret_key", "")).strip()
        if not secret_id or not secret_key:
            raise RuntimeError("请先配置 cos 图床的 secret_id 和 secret_key")

        bucket = str(entry.get("bucket", "")).strip()
        region = str(entry.get("region", "")).strip()
        if not bucket or not region:
            raise RuntimeError("请先配置 cos 图床的 bucket 和 region")

        return cls(
            CosConfig(
                secret_id=secret_id,
                secret_key=secret_key,
                bucket=bucket,
                region=region,
                prefix=str(entry.get("prefix") or DEFAULT_PREFIX),
                endpoint=(str(entry.get("endpoint")).strip() or None)
                if entry.get("endpoint")
                else None,
            )
        )

    def host(self) -> str:
        if self.config.endpoint:
            return normalize_endpoint(self.config.endpoint)
        return self.normal_host()

    def normal_host(self) -> str:
        return f"{self.config.bucket}.cos.{self.config.region}.myqcloud.com"

    def object_url(self, key: str) -> str:
        return object_url(
            self.config.bucket,
            self.config.region,
            key,
        )

    def upload_url(self, key: str) -> str:
        quoted_key = quote(key, safe="/-_.~")
        return f"https://{self.host()}/{quoted_key}"

    def presigned_download_url(
        self,
        key: str,
        *,
        expires: int = DEFAULT_EXPIRES_SECONDS,
    ) -> str:
        authorization = build_cos_auth(
            "GET",
            key,
            self.config.secret_id,
            self.config.secret_key,
            headers={"Host": self.normal_host()},
            expires=expires,
        )
        params = dict(item.split("=", 1) for item in authorization.split("&"))
        return f"{self.object_url(key)}?{urlencode(params)}"

    def upload_file(
        self,
        file_path: str | Path,
        *,
        key: str | None = None,
        prefix: str | None = None,
        public_url: bool = False,
        expires: int = DEFAULT_EXPIRES_SECONDS,
    ) -> UploadResult:
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"文件不存在: {path}")

        object_key = key or make_object_key(
            path, self.config.prefix if prefix is None else prefix
        )
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

        start = time.time()
        self.upload_bytes(body, object_key, content_type=content_type)
        elapsed = time.time() - start

        return UploadResult(
            key=object_key,
            url=self.object_url(object_key)
            if public_url
            else self.presigned_download_url(object_key, expires=expires),
            public_url=self.object_url(object_key),
            size_bytes=len(body),
            elapsed_seconds=elapsed,
        )

    def upload_bytes(
        self,
        body: bytes,
        key: str,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        headers = {
            "Host": self.host(),
            "Content-Type": content_type,
            "x-cos-storage-class": "DEFAULT",
        }
        authorization = build_cos_auth(
            "PUT",
            key,
            self.config.secret_id,
            self.config.secret_key,
            headers=headers,
            expires=DEFAULT_EXPIRES_SECONDS,
        )

        request = Request(
            self.upload_url(key),
            data=body,
            headers={**headers, "Authorization": authorization},
            method="PUT",
        )
        try:
            with urlopen(request) as response:
                response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"COS 上传失败: HTTP {exc.code}\n{detail}") from exc
