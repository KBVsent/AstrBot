"""S3 兼容图床：用 AWS Signature V4 签名直传 S3 兼容对象存储，无需第三方 SDK。

适用于任何 S3 兼容服务：AWS S3、Cloudflare R2、MinIO、Backblaze B2 等。

需在 image_host 配置项里提供：
- ``access_key_id`` / ``secret_access_key``：访问密钥
- ``bucket``：桶名
- ``endpoint``：服务 endpoint；S3 兼容服务必填，如
  Cloudflare R2 ``https://<account_id>.r2.cloudflarestorage.com``；
  AWS S3 可留空，会用 ``s3.<region>.amazonaws.com``
- ``region``：签名区域（默认 ``auto``，适配 R2；AWS 需填真实区域如 ``us-east-1``）
- ``prefix``：对象键前缀（默认 ``temp``）
- ``public_domain``：可选，公开访问域名（如 r2.dev / 自定义 CDN 域名）；配置后返回该域名
  直链，否则返回预签名下载直链
- ``force_path_style``：是否用 path-style 寻址（默认 ``true``，兼容 R2/MinIO）；
  设为 ``false`` 用 virtual-hosted（``<bucket>.<host>``）
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
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_PREFIX = "temp"
DEFAULT_EXPIRES_SECONDS = 3600
DEFAULT_REGION = "auto"
_SERVICE = "s3"
_ALGORITHM = "AWS4-HMAC-SHA256"
_UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"


@dataclass(frozen=True)
class S3Config:
    access_key_id: str
    secret_access_key: str
    bucket: str
    endpoint: str | None = None
    region: str = DEFAULT_REGION
    prefix: str = DEFAULT_PREFIX
    public_domain: str | None = None
    force_path_style: bool = True


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


def _quote_key(key: str) -> str:
    return quote(key, safe="/-_.~")


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


class S3NoSdkClient:
    def __init__(self, config: S3Config) -> None:
        self.config = config

    @classmethod
    def from_config(cls, entry: dict) -> S3NoSdkClient:
        access_key_id = str(entry.get("access_key_id", "")).strip()
        secret_access_key = str(entry.get("secret_access_key", "")).strip()
        if not access_key_id or not secret_access_key:
            raise RuntimeError("请先配置 s3 图床的 access_key_id 和 secret_access_key")

        bucket = str(entry.get("bucket", "")).strip()
        if not bucket:
            raise RuntimeError("请先配置 s3 图床的 bucket")

        endpoint = str(entry.get("endpoint") or "").strip() or None
        region = str(entry.get("region") or "").strip() or DEFAULT_REGION
        # 非 AWS 的 S3 兼容服务必须给 endpoint；AWS 可省略 endpoint 但需真实 region。
        if not endpoint and region == DEFAULT_REGION:
            raise RuntimeError(
                "请配置 s3 图床的 endpoint（S3 兼容服务如 R2/MinIO），"
                "或为 AWS S3 配置真实 region"
            )

        public_domain = str(entry.get("public_domain") or "").strip() or None
        force_path_style = bool(entry.get("force_path_style", True))

        return cls(
            S3Config(
                access_key_id=access_key_id,
                secret_access_key=secret_access_key,
                bucket=bucket,
                endpoint=endpoint,
                region=region,
                prefix=str(entry.get("prefix") or DEFAULT_PREFIX),
                public_domain=public_domain,
                force_path_style=force_path_style,
            )
        )

    def _signing_key(self, date_stamp: str) -> bytes:
        k_date = _sign(
            ("AWS4" + self.config.secret_access_key).encode("utf-8"), date_stamp
        )
        k_region = _sign(k_date, self.config.region)
        k_service = _sign(k_region, _SERVICE)
        return _sign(k_service, "aws4_request")

    def base_host(self) -> str:
        if self.config.endpoint:
            return normalize_endpoint(self.config.endpoint)
        return f"s3.{self.config.region}.amazonaws.com"

    def host(self) -> str:
        base = self.base_host()
        if self.config.force_path_style:
            return base
        return f"{self.config.bucket}.{base}"

    def canonical_path(self, key: str) -> str:
        if self.config.force_path_style:
            return f"/{self.config.bucket}/{_quote_key(key)}"
        return f"/{_quote_key(key)}"

    def endpoint_url(self, key: str) -> str:
        return f"https://{self.host()}{self.canonical_path(key)}"

    def public_url(self, key: str) -> str:
        if self.config.public_domain:
            domain = normalize_endpoint(self.config.public_domain)
            return f"https://{domain}/{_quote_key(key)}"
        return self.endpoint_url(key)

    def presigned_download_url(
        self,
        key: str,
        *,
        expires: int = DEFAULT_EXPIRES_SECONDS,
    ) -> str:
        host = self.host()
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        scope = f"{date_stamp}/{self.config.region}/{_SERVICE}/aws4_request"
        credential = f"{self.config.access_key_id}/{scope}"

        query = {
            "X-Amz-Algorithm": _ALGORITHM,
            "X-Amz-Credential": credential,
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(expires),
            "X-Amz-SignedHeaders": "host",
        }
        canonical_query = "&".join(
            f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}"
            for k, v in sorted(query.items())
        )
        canonical_headers = f"host:{host}\n"
        canonical_request = "\n".join(
            [
                "GET",
                self.canonical_path(key),
                canonical_query,
                canonical_headers,
                "host",
                _UNSIGNED_PAYLOAD,
            ]
        )
        string_to_sign = "\n".join(
            [
                _ALGORITHM,
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature = hmac.new(
            self._signing_key(date_stamp),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{self.endpoint_url(key)}?{canonical_query}&X-Amz-Signature={signature}"

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

        # public_domain 配置时优先返回公开直链，否则用预签名下载直链。
        if public_url or self.config.public_domain:
            url = self.public_url(object_key)
        else:
            url = self.presigned_download_url(object_key, expires=expires)

        return UploadResult(
            key=object_key,
            url=url,
            public_url=self.public_url(object_key),
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
        host = self.host()
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()

        headers = {
            "host": host,
            "content-type": content_type,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        signed_headers = ";".join(sorted(headers.keys()))
        canonical_headers = "".join(
            f"{k}:{headers[k]}\n" for k in sorted(headers.keys())
        )
        canonical_request = "\n".join(
            [
                "PUT",
                self.canonical_path(key),
                "",
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )
        scope = f"{date_stamp}/{self.config.region}/{_SERVICE}/aws4_request"
        string_to_sign = "\n".join(
            [
                _ALGORITHM,
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature = hmac.new(
            self._signing_key(date_stamp),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        authorization = (
            f"{_ALGORITHM} "
            f"Credential={self.config.access_key_id}/{scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )

        request = Request(
            self.endpoint_url(key),
            data=body,
            headers={**headers, "Authorization": authorization},
            method="PUT",
        )
        try:
            with urlopen(request) as response:
                response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"S3 上传失败: HTTP {exc.code}\n{detail}") from exc
