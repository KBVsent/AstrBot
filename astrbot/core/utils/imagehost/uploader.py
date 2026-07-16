"""共享图床上传编排层：把本地图片上传到第三方 CDN，得到长期可访问的公网外链。

供各平台适配器复用（如 QQ 官方 markdown 内嵌图片、LINE 出站图片外链）。后端配置来自
全局配置项 ``image_host``（后端实例列表的 JSON 字符串），每个适配器可传入自己的
``chain``（后端 id 有序优先级）。上传失败按指数退避冷却并自动切换其它后端。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger

from .backends import (
    BilibiliImageHost,
    ChatGLMImageHost,
    CosNoSdkClient,
    QQChannelImageHost,
    S3NoSdkClient,
    YuanbaoImageHost,
)

# 各图床客户端接口对齐：upload_file(path, public_url=...) -> 带 .url 的结果对象
_ImageUploader = (
    ChatGLMImageHost
    | BilibiliImageHost
    | YuanbaoImageHost
    | QQChannelImageHost
    | CosNoSdkClient
    | S3NoSdkClient
)

# type -> 后端类。新增图床类型：加一个后端类并在此登记即可。
_BACKEND_TYPES: dict[str, type] = {
    "chatglm": ChatGLMImageHost,
    "bilibili": BilibiliImageHost,
    "yuanbao": YuanbaoImageHost,
    "qqchannel": QQChannelImageHost,
    "cos": CosNoSdkClient,
    "s3": S3NoSdkClient,
}

# 动态冷却（指数退避）：上传失败累加 failure_score，冷却时长随之指数增长；
# 上传成功递减 failure_score，逐步恢复优先级。这样偶发抖动只短暂跳过，
# 而持续不稳定的后端会被越冻越久，避免每个周期都让用户白等一次超时。
_BASE_COOLDOWN = 60.0  # failure_score=1 时的冷却秒数
_MAX_COOLDOWN = 600.0  # 冷却时长上限（10 分钟）
# failure_score 上限：60 * 2**4 = 960 已超过 _MAX_COOLDOWN，再大也会被 min 截断，
# 故封顶在恢复仍可接受的范围内（满分后需对应次数的成功才能完全恢复）。
_MAX_FAILURE_SCORE = 5


def _cooldown_for(failure_score: int) -> float:
    """根据失败累计分计算冷却秒数（指数退避，封顶 ``_MAX_COOLDOWN``）。"""
    if failure_score <= 0:
        return 0.0
    return min(_BASE_COOLDOWN * (2 ** (failure_score - 1)), _MAX_COOLDOWN)


@dataclass
class _Backend:
    id: str
    client: _ImageUploader
    cooldown_until: float = 0.0  # time.monotonic() 时间戳；<= now 表示可用
    failure_score: int = 0  # 失败累计分：失败 +1、成功 -1，决定冷却时长


# 懒加载缓存：id -> 已初始化的 _Backend；冷却状态跨适配器共享（某后端不稳则各处都退避）。
_backends_by_id: dict[str, _Backend] | None = None
_config_order: list[str] = []  # 配置里 enable 后端的 id 顺序，chain 为空时按此顺序


def _get_backends() -> dict[str, _Backend]:
    """懒加载图床后端。

    读全局配置 ``image_host``（JSON 字符串或列表，每项 ``{id, type, enable, ...凭据}``），
    对每个启用项用 ``_BACKEND_TYPES[type].from_config(entry)`` 构建，缺凭据/未知类型则跳过。
    """
    global _backends_by_id, _config_order
    if _backends_by_id is not None:
        return _backends_by_id

    from astrbot.core import astrbot_config

    entries = astrbot_config.get("image_host") or []
    if isinstance(entries, str):
        try:
            entries = json.loads(entries)
        except ValueError as e:
            logger.warning(f"[ImageHost] image_host 配置不是合法 JSON，已忽略：{e}")
            entries = []
    if not isinstance(entries, list):
        logger.warning("[ImageHost] image_host 配置应为 JSON 数组，已忽略")
        entries = []
    backends: dict[str, _Backend] = {}
    order: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("enable", True):
            continue
        backend_id = str(entry.get("id") or entry.get("type") or "").strip()
        backend_type = str(entry.get("type") or "").strip()
        if not backend_id or backend_type not in _BACKEND_TYPES:
            logger.debug(f"[ImageHost] 忽略无效图床配置项：{entry}")
            continue
        if backend_id in backends:
            logger.debug(f"[ImageHost] 图床 id 重复，忽略：{backend_id}")
            continue
        try:
            client = _BACKEND_TYPES[backend_type].from_config(entry)
        except Exception as e:
            logger.debug(f"[ImageHost] 图床 {backend_id}({backend_type}) 未启用：{e}")
            continue
        backends[backend_id] = _Backend(id=backend_id, client=client)
        order.append(backend_id)

    if not backends:
        logger.debug("[ImageHost] 无可用图床后端")
    _backends_by_id = backends
    _config_order = order
    return _backends_by_id


def reset_backends() -> None:
    """清空后端缓存，下次上传时按最新配置重建（配置变更后调用）。"""
    global _backends_by_id, _config_order
    _backends_by_id = None
    _config_order = []


def _resolve_candidates(chain: list[str] | None) -> list[_Backend]:
    """把 chain（后端 id 有序列表）解析成 _Backend 列表；空则按配置顺序取全部。"""
    backends = _get_backends()
    if not backends:
        return []
    ids = chain if chain else _config_order
    result: list[_Backend] = []
    seen: set[str] = set()
    for backend_id in ids:
        backend_id = str(backend_id).strip()
        if backend_id in seen:
            continue
        backend = backends.get(backend_id)
        if backend is None:
            logger.debug(f"[ImageHost] chain 引用了不存在的图床 id：{backend_id}")
            continue
        result.append(backend)
        seen.add(backend_id)
    return result


async def upload_image(
    file_path: str | Path, chain: list[str] | None = None
) -> str | None:
    """把本地图片上传到图床，返回公网外链；全部失败返回 ``None``。

    Args:
        file_path: 本地图片路径。
        chain: 后端 id 的有序优先级（对应 ``image_host`` 配置里的 id）；空则用全部已启用后端。
    """
    candidates = _resolve_candidates(chain)
    if not candidates:
        return None

    path = str(file_path)
    now = time.monotonic()
    # 优先用未在冷却中的后端；若全部冷却中则仍然全试一遍，不直接放弃。
    usable = [b for b in candidates if b.cooldown_until <= now] or candidates

    for backend in usable:
        try:
            result = await asyncio.to_thread(
                backend.client.upload_file, path, public_url=False
            )
            # 成功：失败分递减、解除冷却，逐步恢复优先级。
            backend.failure_score = max(0, backend.failure_score - 1)
            backend.cooldown_until = 0.0
            return result.url
        except Exception as e:
            # 失败：累加失败分，冷却时长按指数退避增长；持续不稳定者被冻得越来越久。
            backend.failure_score = min(backend.failure_score + 1, _MAX_FAILURE_SCORE)
            cooldown = _cooldown_for(backend.failure_score)
            backend.cooldown_until = time.monotonic() + cooldown
            logger.warning(
                f"[ImageHost] 图床 {backend.id} 上传失败（累计 "
                f"{backend.failure_score} 次），冷却 {cooldown:.0f}s 后再试，"
                f"期间自动切换其它后端：{e}"
            )

    logger.warning("[ImageHost] 所有图床后端均失败")
    return None
