"""把 QQ 官方的富媒体分片上传当作图床：取合并接口返回的 raw_url 作为图片外链。

上传接口挂在当前会话的群或用户 openid 下，openid 又按机器人隔离，所以只能由适配器在发送
现场构建，以调用方自带后端的形式挂进图床优先链。何时启用见 _markdown_media.image_to_markdown_fragment。

raw_url 是 COS 预签名的 GET 链接，1 小时后失效；QQ 发 markdown 时即时拉取，够用，
但不要拿去做需要长期有效的外链。每次上传都占用机器人当天的富媒体上传配额，与正常发图共用。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path

from botpy import Client
from botpy.http import Route

from astrbot.core.utils.imagehost import LocalBackend
from astrbot.core.utils.imagehost.backends._http import HTTP_CLIENT, OBJECT_PUT_TIMEOUT
from astrbot.core.utils.media_utils import detect_file_mime_type

from .qqofficial_chunked_upload import _compute_file_hashes

IMAGE_HOST_ID = "qqofficial"


def image_host_backends(
    bot: Client,
    platform_id: str,
    *,
    group_openid: str | None = None,
    user_openid: str | None = None,
) -> dict[str, LocalBackend]:
    """构建本次发送可用的 qqofficial 图床后端。

    Args:
        bot: 发送所用的 botpy 客户端，提供已认证的请求头。
        platform_id: 适配器 id，冷却状态按它分开记。
        group_openid: 群聊目标。
        user_openid: 私聊目标，给了 group_openid 时忽略。

    Returns:
        {"qqofficial": LocalBackend}；没有群或私聊目标（频道、频道私信）时为空字典。
    """
    if group_openid:
        scope = f"/v2/groups/{group_openid}"
    elif user_openid:
        scope = f"/v2/users/{user_openid}"
    else:
        return {}

    async def upload(path: str) -> str:
        # 只借 botpy 的 token，请求走共享连接池：botpy 的连接池开了 force_close，每个请求都要重新握手
        http = bot.api._http
        await http.check_session()
        headers = dict(http._headers or {})
        base_url = Route("POST", scope, is_sandbox=http.is_sandbox).url
        return await asyncio.to_thread(_upload, Path(path), base_url, headers)

    return {
        IMAGE_HOST_ID: LocalBackend(
            upload=upload, state_key=f"{IMAGE_HOST_ID}@{platform_id}"
        )
    }


def _upload(path: Path, base_url: str, headers: dict[str, str]) -> str:
    """走完 upload_prepare、PUT 分片、upload_part_finish、合并四步，返回 raw_url。"""
    # 图片超过 20MB 会被降级成普通文件，合并时不再返回 raw_url
    max_bytes = 20 * 1024 * 1024
    data = path.read_bytes()
    if len(data) > max_bytes:
        raise RuntimeError(f"图片 {len(data)} 字节，超过 QQ 富媒体图片上限 {max_bytes}")

    prepare = _call(
        f"{base_url}/upload_prepare",
        headers,
        {
            "file_type": 1,
            "file_size": str(len(data)),
            "file_name": path.name,
            **_compute_file_hashes(path),
        },
    )
    upload_id = str(prepare.get("upload_id") or "")
    parts = prepare.get("parts") or []
    if not upload_id or not parts:
        raise RuntimeError(f"upload_prepare 返回不完整: {prepare}")
    block_size = int(prepare["block_size"])
    first_index = min(int(part["index"]) for part in parts)
    # 预签名只签了 host，Content-Type 可以自由加；不加时 raw_url 回 application/octet-stream
    content_type = detect_file_mime_type(path)

    for part in parts:
        index = int(part["index"])
        offset = (index - first_index) * block_size
        chunk = data[offset : offset + block_size]
        resp = HTTP_CLIENT.put(
            part["presigned_url"],
            content=chunk,
            headers={"Content-Type": content_type},
            timeout=OBJECT_PUT_TIMEOUT,
        )
        if not resp.is_success:
            raise RuntimeError(
                f"分片 {index} 上传 COS 失败: HTTP {resp.status_code}\n{resp.text[:300]}"
            )
        _call(
            f"{base_url}/upload_part_finish",
            headers,
            {
                "upload_id": upload_id,
                "part_index": index,
                "block_size": str(len(chunk)),
                "md5": hashlib.md5(chunk, usedforsecurity=False).hexdigest(),
            },
        )

    merged = _call(
        f"{base_url}/files",
        headers,
        {
            "file_type": 1,
            "srv_send_msg": False,
            "file_name": path.name,
            "upload_id": upload_id,
        },
    )
    raw_url = str(merged.get("raw_url") or "")
    if not raw_url:
        raise RuntimeError(f"合并成功但没有返回 raw_url: {merged}")
    return raw_url


def _call(url: str, headers: dict[str, str], body: dict) -> dict:
    """调用开放平台接口，40093001（BDH 通道异常）短暂重试，其余错误直接抛出。

    重试刻意压短：失败了就该让图床优先链尽快换下一个后端，而不是照服务端下发的
    retry_timeout 等上几分钟。
    """
    retryable_code = 40093001
    max_attempts = 3
    attempt = 0
    while True:
        attempt += 1
        resp = HTTP_CLIENT.post(url, headers=headers, json=body, timeout=10.0)
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            payload = {}
        code = payload.get("code")
        if resp.is_success and not code:
            return payload.get("data", payload)
        if code != retryable_code or attempt >= max_attempts:
            step = url.rsplit("/", 1)[-1]
            detail = payload.get("message") or resp.text[:300]
            raise RuntimeError(
                f"{step} 失败: HTTP {resp.status_code} code={code} {detail}"
            )
        time.sleep(1.0)
