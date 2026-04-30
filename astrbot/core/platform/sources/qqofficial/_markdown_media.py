"""将 Image 组件转换为 QQ markdown 图片语法的工具。

QQ markdown 支持 `![alt #WIDTHpx #HEIGHTpx](public_url)` 语法内嵌图片，
开放平台会下载转存。这样图片就可以与 keyboard 共存于同一条 msg_type=2 消息。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot.api import logger
from astrbot.api.message_components import Image

from .qcloudcos import CosNoSdkClient

_cos_client: CosNoSdkClient | None = None
_cos_init_failed = False
_ENV_FILE = Path("data/.env")


def _get_cos_client() -> CosNoSdkClient | None:
    """懒加载 COS 客户端；环境变量缺失或初始化失败时返回 None，由调用方回退。"""
    global _cos_client, _cos_init_failed
    if _cos_client is not None or _cos_init_failed:
        return _cos_client
    try:
        env_file = _ENV_FILE if _ENV_FILE.is_file() else None
        _cos_client = CosNoSdkClient.from_env(env_file=env_file)
    except Exception as e:
        _cos_init_failed = True
        logger.debug(f"[QQOfficial] COS 未启用（环境变量未配置）：{e}")
        return None
    return _cos_client


async def _upload_to_cos(image: Image) -> str | None:
    client = _get_cos_client()
    if client is None:
        return None
    try:
        path = await image.convert_to_file_path()
        result = await asyncio.to_thread(client.upload_file, path, public_url=False)
        return result.url
    except Exception as e:
        logger.warning(f"[QQOfficial] COS 上传失败，回退到默认文件服务：{e}")
        return None


async def image_to_markdown_fragment(image: Image) -> str | None:
    """将 Image 组件转成 markdown 图片片段。

    Returns:
        形如 "\n![img #WIDTHpx #HEIGHTpx](url)\n" 的字符串；
        若文件服务不可用或尺寸读取失败，返回 None（调用方应回退到 msg_type=7）。
    """
    url = await _upload_to_cos(image)
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
