"""共享图床上传编排层：把本地媒体上传到第三方 CDN，得到长期可访问的公网外链。

供各平台适配器复用（如 QQ 官方 markdown 内嵌图片、LINE 出站媒体外链）。后端配置来自
全局配置项 image_host（后端实例列表的 JSON 字符串），每个适配器可传入自己的
chain（后端 id 有序优先级）。上传失败按指数退避冷却并自动切换其它后端。

每个后端可用 mime_types 声明自己接受的 MIME 类型（支持 image/* 等通配），
上传时只在声明接受该类型的后端里挑候选 —— 否则拿视频去试只吃图片的第三方图床，
失败累积的冷却会把图片上传路径一起冻上。字段缺失时默认只接受图片，旧配置行为不变。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger
from astrbot.core.utils.media_utils import (
    detect_file_mime_type_async,
    normalize_mime_type,
)

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
    """根据失败累计分计算冷却秒数（指数退避，封顶 _MAX_COOLDOWN）。"""
    if failure_score <= 0:
        return 0.0
    return min(_BASE_COOLDOWN * (2 ** (failure_score - 1)), _MAX_COOLDOWN)


# 后端可接受的 MIME 类型由配置者在 image_host 条目里用 mime_types 声明，不由后端类型推断。
# 字段缺失时默认只接受图片 —— 显式空数组表示不接受任何上传。
_DEFAULT_ACCEPTED_MIME_TYPES = ("image/*",)


@dataclass
class _Backend:
    id: str
    client: _ImageUploader
    accepted_mime_types: tuple[str, ...] = _DEFAULT_ACCEPTED_MIME_TYPES
    cooldown_until: float = 0.0  # time.monotonic() 时间戳；<= now 表示可用
    failure_score: int = 0  # 失败累计分：失败 +1、成功 -1，决定冷却时长

    def accepts(self, mime_type: str) -> bool:
        """判断该后端是否声明接受给定 MIME 类型（支持 type/* 与 */* 通配）。"""
        if not self.accepted_mime_types:
            return False
        mime_type = normalize_mime_type(mime_type) or "application/octet-stream"
        major = mime_type.split("/", 1)[0]
        for pattern in self.accepted_mime_types:
            if pattern == "*/*":
                return True
            # 未能识别出具体类型的文件只匹配 */*
            if mime_type == "application/octet-stream":
                continue
            if pattern == mime_type or pattern == f"{major}/*":
                return True
        return False


def _parse_accepted_mime_types(entry: dict) -> tuple[str, ...]:
    """解析配置项里的 mime_types 声明并做别名归一化。"""
    if "mime_types" not in entry:
        return _DEFAULT_ACCEPTED_MIME_TYPES
    declared = entry.get("mime_types")
    if isinstance(declared, str):
        declared = [declared]
    if not isinstance(declared, list):
        logger.warning(
            f"[ImageHost] {entry.get('id') or entry.get('type')} 的 mime_types "
            "应为字符串数组，已按默认（仅图片）处理"
        )
        return _DEFAULT_ACCEPTED_MIME_TYPES
    patterns: list[str] = []
    for item in declared:
        pattern = str(item).strip().lower()
        if not pattern:
            continue
        if pattern.endswith("/*") or pattern == "*/*":
            patterns.append(pattern)
        else:
            patterns.append(normalize_mime_type(pattern))
    return tuple(dict.fromkeys(patterns))


# 懒加载缓存：id -> 已初始化的 _Backend；冷却状态跨适配器共享（某后端不稳则各处都退避）。
_backends_by_id: dict[str, _Backend] | None = None
_config_order: list[str] = []  # 配置里 enable 后端的 id 顺序，chain 为空时按此顺序


def _get_backends() -> dict[str, _Backend]:
    """懒加载图床后端。

    读全局配置 image_host（JSON 字符串或列表，每项 {id, type, enable, ...凭据}），
    对每个启用项用 _BACKEND_TYPES[type].from_config(entry) 构建，缺凭据/未知类型则跳过。
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
        backends[backend_id] = _Backend(
            id=backend_id,
            client=client,
            accepted_mime_types=_parse_accepted_mime_types(entry),
        )
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


def _resolve_candidates(
    chain: list[str] | None, mime_type: str | None = None
) -> list[_Backend]:
    """把 chain（后端 id 有序列表）解析成 _Backend 列表；空则按配置顺序取全部。

    Args:
        chain: 后端 id 的有序优先级；空则按配置顺序取全部。
        mime_type: 待上传文件的 MIME 类型；给出时只保留声明接受该类型的后端。
    """
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
        seen.add(backend_id)
        if mime_type is not None and not backend.accepts(mime_type):
            # 未声明接受该类型的后端直接不试：失败会累加失败分并触发冷却，
            # 拿视频去试只吃图片的图床会把图片上传路径一起冻上。
            logger.debug(f"[ImageHost] 图床 {backend_id} 未声明接受 {mime_type}，跳过")
            continue
        result.append(backend)
    return result


async def upload_image(
    file_path: str | Path, chain: list[str] | None = None
) -> str | None:
    """把本地图片上传到图床，返回公网外链；全部失败返回 None。

    Args:
        file_path: 本地图片路径。
        chain: 后端 id 的有序优先级（对应 image_host 配置里的 id）；空则用全部已启用后端。
    """
    return await upload_media(file_path, chain)


async def upload_media(
    file_path: str | Path,
    chain: list[str] | None = None,
    mime_type: str | None = None,
) -> str | None:
    """把本地媒体文件上传到图床，返回公网外链；无可用后端或全部失败返回 None。

    候选后端按各自声明的 mime_types 筛选，其余行为（优先级、失败退避、后端切换）
    与图片上传完全一致。

    Args:
        file_path: 本地媒体路径。
        chain: 后端 id 的有序优先级（对应 image_host 配置里的 id）；空则用全部已启用后端。
        mime_type: 文件 MIME 类型；不给则按内容优先探测。
    """
    path = str(file_path)
    if not _get_backends():
        return None

    resolved_mime = normalize_mime_type(mime_type) if mime_type else ""
    if not resolved_mime:
        resolved_mime = await detect_file_mime_type_async(path)

    candidates = _resolve_candidates(chain, resolved_mime)
    if not candidates:
        logger.debug(f"[ImageHost] 无声明接受 {resolved_mime} 的图床后端")
        return None

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
