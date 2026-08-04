"""LINE 出入站媒体的收敛与 URL 供给。

LINE 的一次 reply 请求里只要有一个消息对象不合法就会整批 400，同批其它消息一起发不出去。
因此进入批次前必须确认媒体的实际 MIME 与实际字节数满足平台约束 —— 不是「调用了某个
转换函数所以应该没问题」，而是转换完再实测一次。收敛不下去的组件一律跳过并 warning，
把「少一张图」控制在局部，不放大成整批失败。

两组上限不要混为一件事：

- LINE_MAX_*_BYTES 是平台合法性，决定这条消息能不能发；
- *_DOWNLOAD_LIMIT / OUTBOUND_UPLOAD_LIMIT 是本地资源保护（防 OOM），
  决定走哪条路径拿 URL。四个本地阈值各自独立取值，调整其一不影响其余三者。
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

import aiohttp
from PIL import Image as PILImage

from astrbot.api import logger
from astrbot.core import astrbot_config, file_token_service
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.media_utils import (
    MediaTooLargeError,
    convert_audio_format,
    convert_video_format,
    detect_file_mime_type,
    detect_file_mime_type_async,
    extract_video_cover,
    get_media_duration,
)

# —— 平台合法性（RUNTIME_CONSTRAINTS §6）——
LINE_MAX_IMAGE_BYTES = 10 * 1024 * 1024
LINE_MAX_PREVIEW_BYTES = 1 * 1024 * 1024
LINE_MAX_VIDEO_BYTES = 200 * 1024 * 1024
LINE_MAX_AUDIO_BYTES = 200 * 1024 * 1024
LINE_MAX_URL_LENGTH = 2000

LINE_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png"})
LINE_AUDIO_MIME_TYPES = frozenset({"audio/mpeg", "audio/mp4"})
LINE_VIDEO_MIME_TYPES = frozenset({"video/mp4"})

# —— 本地资源保护（防 OOM，四者互相独立）——
INBOUND_DOWNLOAD_LIMIT = 20 * 1024 * 1024
"""普通入站 Content API 下载上限；超限降级为占位文本组件。"""
OUTBOUND_UPLOAD_LIMIT = 20 * 1024 * 1024
"""出站进图床的上传上限；超限不走图床，转临时文件 URL 兜底。"""
QUOTE_LOOKUP_LIMIT = 10 * 1024 * 1024
"""引用内容回查上限；超限保留 Reply.id、内容留空。"""
EXTERNAL_IMAGE_LIMIT = 10 * 1024 * 1024
"""外链图片抓取上限；超限跳过该图片 + warning。"""

# 兜底临时 URL 的有效期。LINE 会从公网主动拉取、可能重试、可能对 original 与 preview
# 分别拉取，因此必须是可重复读取的令牌，且有效期显著长于一次 reply 的生命周期。
FALLBACK_URL_TTL_SECONDS = 3600


def _temp_dir() -> Path:
    temp_dir = Path(get_astrbot_temp_path())
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir


def _temp_path(kind: str, suffix: str) -> Path:
    return _temp_dir() / f"line_{kind}_{uuid.uuid4().hex}{suffix}"


def inbound_temp_path(message_id: str, suffix: str = "") -> Path:
    """入站媒体的落盘路径（按消息 id 命名，便于排查）。

    Args:
        message_id: 入站消息 id。
        suffix: 文件后缀（含点）；未知时留空。

    Returns:
        临时目录下的目标路径。
    """
    safe_id = "".join(ch for ch in message_id if ch.isalnum())[:32] or "unknown"
    return _temp_dir() / f"line_inbound_{safe_id}_{uuid.uuid4().hex[:6]}{suffix}"


def file_size(path: str | Path) -> int:
    """返回文件字节数；文件不存在或不可读时返回 -1。"""
    try:
        return Path(path).stat().st_size
    except OSError:
        return -1


# ---------------------------------------------------------------- 受限下载


async def stream_response_to_file(
    resp: aiohttp.ClientResponse,
    dest: Path,
    limit_bytes: int,
    *,
    chunk_size: int = 64 * 1024,
) -> int:
    """把响应体分块写入 dest，累计字节超过 limit_bytes 时中止。

    服务端不给 Content-Length 是常态，因此除了预检声明长度外，必须在累计字节
    达到上限时中止，否则「防 OOM」根本没做成。

    Args:
        resp: 已发起的 aiohttp 响应（状态码由调用方检查）。
        dest: 目标文件路径。
        limit_bytes: 允许写入的最大字节数。
        chunk_size: 单次读取的块大小。

    Returns:
        实际写入的字节数。

    Raises:
        MediaTooLargeError: 声明长度或实际字节数超过上限，下载已中止。
    """
    declared = resp.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > limit_bytes:
        raise MediaTooLargeError(
            f"declared size {declared} exceeds limit {limit_bytes}"
        )

    written = 0
    try:
        with dest.open("wb") as f:
            async for chunk in resp.content.iter_chunked(chunk_size):
                written += len(chunk)
                if written > limit_bytes:
                    raise MediaTooLargeError(f"actual size exceeds limit {limit_bytes}")
                f.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return written


# ---------------------------------------------------------------- 图片收敛


def _save_image(
    image: PILImage.Image,
    out_path: Path,
    output_format: str,
    save_kwargs: dict[str, object],
) -> bool:
    """保存图片；optimize 在高分辨率噪声图上会失败，失败则退一步不优化再存。

    Pillow 的 JPEG optimize 需要一次性容纳整段扫描数据（ImageFile.MAXBLOCK），
    高分辨率细节丰富的图片会抛 broken data stream。这条路径只影响文件体积，
    不影响合法性，退化保存比丢掉整张图划算。
    """
    try:
        image.save(out_path, output_format, **save_kwargs)
        return True
    except OSError as e:
        logger.debug("[LINE] optimized save failed (%s), retrying without optimize", e)
        out_path.unlink(missing_ok=True)
    fallback_kwargs = {k: v for k, v in save_kwargs.items() if k != "optimize"}
    try:
        image.save(out_path, output_format, **fallback_kwargs)
        return True
    except Exception as e:
        logger.warning("[LINE] image save failed: %s", e)
        out_path.unlink(missing_ok=True)
        return False


def _encode_image_within_budget(
    source: Path,
    max_bytes: int,
    max_edge: int | None,
) -> str | None:
    """把图片编码为 JPEG/PNG 并压到 max_bytes 以内（同步，供线程池调用）。

    动画取首帧，带透明通道走 PNG（LINE 同样接受），其余走 JPEG。先降质量再降分辨率，
    降到下限仍超标则放弃 —— 宁可少一张图，也不把必被拒的对象放进批次。

    Args:
        source: 源图片路径。
        max_bytes: 输出字节上限。
        max_edge: 初始最长边像素上限；None 表示保持原尺寸。

    Returns:
        产出文件路径，或收敛不下去时 None。
    """
    min_edge = 160
    min_quality = 40

    # 全部处理都留在 with 内：Pillow 的解码是惰性的，源文件句柄一关，
    # 后续 save() 会以「broken data stream」失败。
    with PILImage.open(source) as opened:
        if getattr(opened, "is_animated", False):
            opened.seek(0)
        has_alpha = opened.mode in {"RGBA", "LA"} or (
            opened.mode == "P" and "transparency" in opened.info
        )
        working = opened.convert("RGBA" if has_alpha else "RGB")

        output_format = "PNG" if has_alpha else "JPEG"
        suffix = ".png" if has_alpha else ".jpg"
        quality = 90

        try:
            if max_edge and max(working.size) > max_edge:
                working.thumbnail((max_edge, max_edge), PILImage.Resampling.LANCZOS)

            while True:
                out_path = _temp_path("image", suffix)
                save_kwargs: dict[str, object] = {"optimize": True}
                if output_format == "JPEG":
                    save_kwargs["quality"] = quality
                    save_kwargs["subsampling"] = 0 if quality >= 80 else 2
                if not _save_image(working, out_path, output_format, save_kwargs):
                    return None
                if out_path.stat().st_size <= max_bytes:
                    return str(out_path)
                out_path.unlink(missing_ok=True)

                if output_format == "JPEG" and quality > min_quality:
                    quality -= 15
                    continue
                if max(working.size) > min_edge:
                    target = max(int(max(working.size) * 0.7), min_edge)
                    working.thumbnail((target, target), PILImage.Resampling.LANCZOS)
                    quality = 90
                    continue
                return None
        finally:
            working.close()


async def prepare_line_image(path: str) -> str | None:
    """把本地图片收敛为 LINE 可接受的原图（JPEG/PNG 且 ≤ 10 MB）。

    Args:
        path: 本地图片路径。

    Returns:
        可直接交给 LINE 的本地图片路径，或收敛失败时 None。
    """
    mime = await detect_file_mime_type_async(path)
    size = file_size(path)
    if size < 0:
        logger.warning("[LINE] image not readable, skipped: %s", path)
        return None
    if mime in LINE_IMAGE_MIME_TYPES and size <= LINE_MAX_IMAGE_BYTES:
        return path

    try:
        converted = await asyncio.to_thread(
            _encode_image_within_budget, Path(path), LINE_MAX_IMAGE_BYTES, None
        )
    except Exception as e:
        logger.warning("[LINE] image convert failed, skipped: %s (%s)", path, e)
        return None
    if not converted:
        logger.warning(
            "[LINE] image cannot be reduced under %s bytes, skipped: %s",
            LINE_MAX_IMAGE_BYTES,
            path,
        )
        return None
    return _verify_prepared(converted, LINE_IMAGE_MIME_TYPES, LINE_MAX_IMAGE_BYTES)


async def prepare_line_preview(path: str) -> str | None:
    """由本地图片生成 LINE 预览图（JPEG/PNG 且 ≤ 1 MB，保持宽高比）。

    宽高比必须与原图/视频一致：不一致时客户端里预览图会露在视频后面。

    Args:
        path: 本地图片路径（原图或视频封面）。

    Returns:
        预览图本地路径，或收敛失败时 None。
    """
    mime = await detect_file_mime_type_async(path)
    size = file_size(path)
    if size < 0:
        logger.warning("[LINE] preview source not readable, skipped: %s", path)
        return None
    if mime in LINE_IMAGE_MIME_TYPES and size <= LINE_MAX_PREVIEW_BYTES:
        return path

    try:
        converted = await asyncio.to_thread(
            _encode_image_within_budget, Path(path), LINE_MAX_PREVIEW_BYTES, 1280
        )
    except Exception as e:
        logger.warning("[LINE] preview convert failed, skipped: %s (%s)", path, e)
        return None
    if not converted:
        logger.warning(
            "[LINE] preview cannot be reduced under %s bytes, skipped: %s",
            LINE_MAX_PREVIEW_BYTES,
            path,
        )
        return None
    return _verify_prepared(converted, LINE_IMAGE_MIME_TYPES, LINE_MAX_PREVIEW_BYTES)


# ---------------------------------------------------------------- 音视频收敛


def _conversion_source(path: str, target_suffix: str) -> str:
    """绕开转码函数「后缀已是目标格式就原样返回」的短路。

    实际 MIME 已经判定为非目标格式，但文件名后缀可能恰好是目标后缀（例如内容是 wav 的
    x.m4a）。此时直接调转码函数会原样返回，产出仍是非法对象。这里给它一个中性后缀
    的软链接（不可用则回退拷贝），ffmpeg 对输入本就按内容探测，不看后缀。

    Args:
        path: 源文件路径。
        target_suffix: 转码目标后缀（含点）。

    Returns:
        可安全传给转码函数的输入路径。
    """
    source = Path(path)
    if source.suffix.lower() != target_suffix.lower():
        return path
    link_path = _temp_path("src", ".src")
    try:
        link_path.symlink_to(source.resolve())
    except OSError:
        try:
            shutil.copyfile(source, link_path)
        except OSError as e:
            logger.debug("[LINE] prepare conversion source failed: %s", e)
            return path
    return str(link_path)


async def prepare_line_audio(path: str) -> str | None:
    """把本地音频收敛为 LINE 可接受的格式（MP3 / M4A 且 ≤ 200 MB）。

    Args:
        path: 本地音频路径。

    Returns:
        可交给 LINE 的音频路径，或收敛失败时 None。
    """
    mime = await detect_file_mime_type_async(path)
    size = file_size(path)
    if size < 0:
        logger.warning("[LINE] audio not readable, skipped: %s", path)
        return None
    if mime in LINE_AUDIO_MIME_TYPES:
        if size <= LINE_MAX_AUDIO_BYTES:
            return path
        logger.warning(
            "[LINE] audio exceeds %s bytes, skipped: %s", LINE_MAX_AUDIO_BYTES, path
        )
        return None

    try:
        # 输出 m4a（AAC in MP4 容器）：LINE 接受，且比 mp3 转码更快、体积更小。
        converted = await convert_audio_format(
            audio_path=_conversion_source(path, ".m4a"),
            output_format="m4a",
            output_path=str(_temp_path("audio", ".m4a")),
        )
    except Exception as e:
        logger.warning("[LINE] audio convert failed, skipped: %s (%s)", path, e)
        return None
    return _verify_prepared(converted, LINE_AUDIO_MIME_TYPES, LINE_MAX_AUDIO_BYTES)


async def prepare_line_video(path: str) -> str | None:
    """把本地视频收敛为 LINE 可接受的格式（MP4 且 ≤ 200 MB）。

    Args:
        path: 本地视频路径。

    Returns:
        可交给 LINE 的视频路径，或收敛失败时 None。
    """
    mime = await detect_file_mime_type_async(path)
    size = file_size(path)
    if size < 0:
        logger.warning("[LINE] video not readable, skipped: %s", path)
        return None
    if mime in LINE_VIDEO_MIME_TYPES:
        if size <= LINE_MAX_VIDEO_BYTES:
            return path
        logger.warning(
            "[LINE] video exceeds %s bytes, skipped: %s", LINE_MAX_VIDEO_BYTES, path
        )
        return None

    try:
        converted = await convert_video_format(
            video_path=_conversion_source(path, ".mp4"),
            output_format="mp4",
            output_path=str(_temp_path("video", ".mp4")),
        )
    except Exception as e:
        logger.warning("[LINE] video convert failed, skipped: %s (%s)", path, e)
        return None
    return _verify_prepared(converted, LINE_VIDEO_MIME_TYPES, LINE_MAX_VIDEO_BYTES)


async def extract_local_video_cover(video_path: str) -> str | None:
    """为本地视频抽一帧作封面，并收敛为合法预览图；失败返回 None。

    只用于本地视频 —— 外链视频缺预览图时按规格跳过，不下载抽帧。

    Args:
        video_path: 本地视频路径。

    Returns:
        预览图路径，或失败时 None。
    """
    try:
        cover = await extract_video_cover(video_path, str(_temp_path("cover", ".jpg")))
    except Exception as e:
        logger.warning("[LINE] extract video cover failed: %s (%s)", video_path, e)
        return None
    return await prepare_line_preview(cover)


async def resolve_audio_duration(path: str) -> int | None:
    """探测音频毫秒时长；取不到返回 None（不取保守值：错值是用户可见的错误）。

    Args:
        path: 本地音频路径。

    Returns:
        毫秒时长，或探测失败时 None。
    """
    duration = await get_media_duration(path)
    if isinstance(duration, int) and duration > 0:
        return duration
    return None


def _verify_prepared(
    path: str,
    allowed_mime_types: frozenset[str],
    max_bytes: int,
) -> str | None:
    """转换完成后实测 MIME 与字节数，未达后置条件则丢弃产物。"""
    mime = detect_file_mime_type(path)
    size = file_size(path)
    if mime not in allowed_mime_types or size < 0 or size > max_bytes:
        logger.warning(
            "[LINE] converted media still invalid (mime=%s size=%s), skipped: %s",
            mime,
            size,
            path,
        )
        return None
    return path


# ---------------------------------------------------------------- URL 供给


async def resolve_public_media_url(
    path: str,
    chain: list[str] | None,
    *,
    mime_type: str | None = None,
) -> str | None:
    """给本地媒体取一个 LINE 可重复拉取的 URL。

    两级：图床（长期外链，首选）→ 临时文件 URL 兜底（尽力而为，可能被 TempDirCleaner
    提前清掉）。

    责任边界：图床返回的 URL 直接信任，图床由使用者配置，适配器不检查、不探测；
    而兜底 URL 是适配器自己用 callback_api_base 拼出来的产物，必须自检 ——
    该配置项默认很可能是 http，拼出来直接就是整批 400。

    Args:
        path: 本地媒体路径。
        chain: 图床后端 id 优先链。
        mime_type: 已知的 MIME 类型；不给则按内容探测。

    Returns:
        HTTPS URL，或无法供给时 None。
    """
    size = file_size(path)
    if size < 0:
        logger.warning("[LINE] media not readable: %s", path)
        return None

    resolved_mime = mime_type or await detect_file_mime_type_async(path)
    if size <= OUTBOUND_UPLOAD_LIMIT:
        url = await _upload_to_image_host(path, chain, resolved_mime)
        if url:
            return url
    else:
        logger.debug(
            "[LINE] media larger than %s bytes, skipping image host: %s",
            OUTBOUND_UPLOAD_LIMIT,
            path,
        )

    url = await _register_reusable_temp_url(path)
    if url and _usable_fallback_url(url):
        return url
    return None


def _usable_fallback_url(url: str) -> bool:
    """兜底 URL 必须是 HTTPS 且不超过 2000 字符，否则 LINE 必拒。"""
    if not url.startswith("https://"):
        logger.warning("[LINE] media URL is not HTTPS, unusable: %s", url)
        return False
    if len(url) > LINE_MAX_URL_LENGTH:
        logger.warning("[LINE] media URL exceeds %s chars", LINE_MAX_URL_LENGTH)
        return False
    return True


async def _upload_to_image_host(
    path: str, chain: list[str] | None, mime_type: str
) -> str | None:
    """把媒体上传到共享图床，得到长期外链；失败返回 None。"""
    try:
        from astrbot.core.utils.imagehost import upload_media

        return await upload_media(path, chain, mime_type)
    except Exception as e:
        logger.debug("[LINE] imagehost upload failed: %s", e)
        return None


async def _register_reusable_temp_url(path: str) -> str | None:
    """把媒体拷进临时目录并注册可重复读取的回调 URL；不可用时返回 None。"""
    callback_host = str(astrbot_config.get("callback_api_base", "")).rstrip("/")
    if not callback_host:
        logger.warning("[LINE] callback_api_base 未配置，临时文件 URL 兜底不可用")
        return None

    source_path = Path(path)
    if not source_path.is_file():
        logger.warning("[LINE] media file does not exist: %s", path)
        return None

    outbound_dir = _temp_dir() / "line_outbound"
    outbound_dir.mkdir(parents=True, exist_ok=True)
    outbound_path = outbound_dir / f"{uuid.uuid4().hex}{source_path.suffix}"
    try:
        await asyncio.to_thread(shutil.copyfile, source_path, outbound_path)
        token = await file_token_service.register_file(
            str(outbound_path),
            timeout=FALLBACK_URL_TTL_SECONDS,
            reusable=True,
        )
    except Exception as e:
        logger.warning("[LINE] register fallback media URL failed: %s", e)
        return None
    return f"{callback_host}/api/file/{token}"
