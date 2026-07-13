"""将 Image 组件转换为 QQ markdown 图片语法的工具。

QQ markdown 支持 `![alt #WIDTHpx #HEIGHTpx](public_url)` 语法内嵌图片，
开放平台会下载转存。这样图片就可以与 keyboard 共存于同一条 msg_type=2 消息。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger
from astrbot.api.message_components import Image

from .bilibilihosting import BilibiliImageHost
from .chatglmhosting import ChatGLMImageHost
from .naturehosting import NatureImageHost
from .qcloudcos import CosNoSdkClient
from .qqchannelhosting import QQChannelImageHost
from .yuanbaohosting import YuanbaoImageHost

# 各图床客户端接口对齐：upload_file(path, public_url=...) -> 带 .url 的结果对象
_ImageUploader = (
    CosNoSdkClient
    | QQChannelImageHost
    | ChatGLMImageHost
    | BilibiliImageHost
    | NatureImageHost
    | YuanbaoImageHost
)

# provider 名 -> 工厂。from_env 在缺配置时抛错，组链时仅保留初始化成功的后端。
_FACTORIES: dict[str, Callable[[], _ImageUploader]] = {
    "nature": NatureImageHost.from_env,
    "chatglm": ChatGLMImageHost.from_env,
    "bilibili": BilibiliImageHost.from_env,
    "yuanbao": YuanbaoImageHost.from_env,
    "qqchannel": QQChannelImageHost.from_env,
    "cos": CosNoSdkClient.from_env,
}
_ALIASES = {
    "glm": "chatglm",
    "bili": "bilibili",
    "b23": "bilibili",
    "yb": "yuanbao",
    "qq": "qqchannel",
    "channel": "qqchannel",
}
# 默认优先级：免登录的 nature 优先（容量大、走腾讯 CDN），chatglm 次之，
# bilibili、yuanbao 再次，QQ 频道兜底，其后才是需密钥的 COS。
_DEFAULT_CHAIN = ["nature", "chatglm", "bilibili", "yuanbao", "qqchannel", "cos"]

# 动态冷却（指数退避）：上传失败累加 failure_score，冷却时长随之指数增长；
# 上传成功递减 failure_score，逐步恢复优先级。这样偶发抖动只短暂跳过，
# 而持续不稳定的后端会被越冻越久，避免每个周期都让用户白等一次超时。
_BASE_COOLDOWN = 60.0  # failure_score=1 时的冷却秒数
_MAX_COOLDOWN = 600.0  # 冷却时长上限（10 分钟）
# failure_score 上限：60 * 2**4 = 960 已超过 _MAX_COOLDOWN，再大也会被 min 截断，
# 故封顶在恢复仍可接受的范围内（满分后需对应次数的成功才能完全恢复）。
_MAX_FAILURE_SCORE = 5

_ENV_FILE = Path("data/.env")


def _cooldown_for(failure_score: int) -> float:
    """根据失败累计分计算冷却秒数（指数退避，封顶 ``_MAX_COOLDOWN``）。"""
    if failure_score <= 0:
        return 0.0
    return min(_BASE_COOLDOWN * (2 ** (failure_score - 1)), _MAX_COOLDOWN)


@dataclass
class _Backend:
    name: str
    client: _ImageUploader
    cooldown_until: float = 0.0  # time.monotonic() 时间戳；<= now 表示可用
    failure_score: int = 0  # 失败累计分：失败 +1、成功 -1，决定冷却时长


_backends: list[_Backend] | None = None


def _load_env_file() -> None:
    if not _ENV_FILE.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(_ENV_FILE)
    except Exception as e:
        logger.debug(f"[QQOfficial] 加载 {_ENV_FILE} 失败：{e}")


def _resolve_chain() -> list[str]:
    """解析 ``QQOFFICIAL_IMAGE_HOST``（逗号分隔的优先级列表）；未设置则用默认链。"""
    raw = os.getenv("QQOFFICIAL_IMAGE_HOST", "").strip()
    if not raw:
        return list(_DEFAULT_CHAIN)
    names: list[str] = []
    for token in raw.replace(" ", ",").split(","):
        name = _ALIASES.get(token.strip().lower(), token.strip().lower())
        if not name:
            continue
        if name not in _FACTORIES:
            logger.debug(f"[QQOfficial] 忽略未知图床后端：{name}")
            continue
        if name not in names:  # 去重保序
            names.append(name)
    return names or list(_DEFAULT_CHAIN)


def _get_backends() -> list[_Backend]:
    """懒加载图床上传链。

    通过 ``QQOFFICIAL_IMAGE_HOST`` 配置优先级（逗号分隔），按序尝试直到成功；
    可选后端：

    - ``nature``：免登录图床（默认首选，内置密钥、容量大、走腾讯 CDN）
    - ``chatglm``：免登录图床（质量好、开箱即用）
    - ``bilibili``：B 站图床（需 BILI_SESSDATA/BILI_CSRF_TOKEN 登录 Cookie）
    - ``yuanbao``：腾讯元宝图床（需 YUANBAO_COOKIE 登录 Cookie）
    - ``qqchannel``：QQ 频道图床（需 appid/secret/channel_id，作为兜底）
    - ``cos``：腾讯云 COS（需密钥）

    未配置时默认链为 ``nature,chatglm,bilibili,yuanbao,qqchannel,cos``，仅初始化成功的后端入链。
    外部图床（nature/chatglm/bilibili/yuanbao）可经代理访问，见 :mod:`._imagehost_http`。
    """
    global _backends
    if _backends is not None:
        return _backends

    _load_env_file()
    backends: list[_Backend] = []
    for name in _resolve_chain():
        try:
            backends.append(_Backend(name=name, client=_FACTORIES[name]()))
        except Exception as e:
            logger.debug(f"[QQOfficial] 图床后端 {name} 未启用：{e}")

    if not backends:
        logger.debug("[QQOfficial] 无可用图床后端")
    _backends = backends
    return _backends


async def _upload_image(image: Image) -> str | None:
    backends = _get_backends()
    if not backends:
        return None

    path = await image.convert_to_file_path()
    now = time.monotonic()
    # 优先用未在冷却中的后端；若全部冷却中则仍然全试一遍，不直接放弃。
    candidates = [b for b in backends if b.cooldown_until <= now] or backends

    for backend in candidates:
        try:
            result = await asyncio.to_thread(
                backend.client.upload_file, path, public_url=False
            )
            # 成功：失败分递减、解除冷却，逐步恢复优先级（chatglm 越快越值得回流）。
            backend.failure_score = max(0, backend.failure_score - 1)
            backend.cooldown_until = 0.0
            return result.url
        except Exception as e:
            # 失败：累加失败分，冷却时长按指数退避增长；持续不稳定者被冻得越来越久。
            backend.failure_score = min(backend.failure_score + 1, _MAX_FAILURE_SCORE)
            cooldown = _cooldown_for(backend.failure_score)
            backend.cooldown_until = time.monotonic() + cooldown
            logger.warning(
                f"[QQOfficial] 图床 {backend.name} 上传失败（累计 "
                f"{backend.failure_score} 次），冷却 {cooldown:.0f}s 后再试，"
                f"期间自动切换其它后端：{e}"
            )

    logger.warning("[QQOfficial] 所有图床后端均失败，回退到默认文件服务")
    return None


async def image_to_markdown_fragment(image: Image) -> str | None:
    """将 Image 组件转成 markdown 图片片段。

    Returns:
        形如 "\n![img #WIDTHpx #HEIGHTpx](url)\n" 的字符串；
        若文件服务不可用或尺寸读取失败，返回 None（调用方应回退到 msg_type=7）。
    """
    url = await _upload_image(image)
    if url is None:
        try:
            url = await image.register_to_file_service()
        except Exception as e:
            logger.warning(f"[QQOfficial] 注册图片到文件服务失败，无法转 markdown: {e}")
            return None

    width, height = await _read_image_size(image)
    if width is None or height is None:
        logger.warning(
            "[QQOfficial] 读取图片尺寸失败；不附尺寸的 markdown 图片在 QQ 客户端无法渲染，"
            "回退到 msg_type=7 富媒体路径。"
        )
        return None

    return f"\n![img #{width}px #{height}px]({url})\n"


async def _read_image_size(image: Image) -> tuple[int | None, int | None]:
    try:
        from PIL import Image as PILImage  # noqa: PLC0415
    except ImportError:
        return None, None

    try:
        path = await image.convert_to_file_path()
        with PILImage.open(path) as im:
            return im.width, im.height
    except Exception as e:
        logger.debug(f"[QQOfficial] 读取图片尺寸失败: {e}")
        return None, None
