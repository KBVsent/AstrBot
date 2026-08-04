"""图床后端的 MIME 能力筛选与内容优先的 MIME 探测。

关键点：拿视频去试只吃图片的第三方图床必然失败，而失败会累加失败分并触发指数退避冷却，
把图片上传路径一起冻上。因此候选筛选必须发生在尝试上传之前。

同时锁定向后兼容：mime_types 缺失时只接受图片，旧配置行为完全不变；upload_image()
的签名与语义不变。
"""

import json

import pytest

from astrbot.core.utils import media_utils
from astrbot.core.utils.imagehost import uploader


class _FakeResult:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakeClient:
    """记录被真正调用过的图床后端。"""

    calls: list[tuple[str, str]] = []

    def __init__(self, backend_id: str) -> None:
        self.backend_id = backend_id

    def upload_file(self, path: str, public_url: bool = False):  # noqa: ARG002
        _FakeClient.calls.append((self.backend_id, path))
        return _FakeResult(f"https://cdn.test/{self.backend_id}")


@pytest.fixture
def image_host(monkeypatch):
    """按给定条目装配图床后端，并把上传替换成可观测的假客户端。"""
    _FakeClient.calls = []

    def _setup(entries: list[dict]):
        uploader.reset_backends()

        class _FakeAstrBotConfig:
            @staticmethod
            def get(key, default=None):
                if key == "image_host":
                    return json.dumps(entries)
                return default

        import astrbot.core as astrbot_core

        monkeypatch.setattr(astrbot_core, "astrbot_config", _FakeAstrBotConfig)
        monkeypatch.setitem(uploader._BACKEND_TYPES, "fake", _FakeBackendType)
        return entries

    yield _setup
    uploader.reset_backends()


class _FakeBackendType:
    @staticmethod
    def from_config(entry: dict) -> _FakeClient:
        return _FakeClient(str(entry.get("id")))


# ------------------------------------------------------- mime_types 声明语义


@pytest.mark.parametrize(
    ("entry", "mime_type", "accepted"),
    [
        # 字段缺失：默认只接受图片 —— 旧配置行为完全不变。
        ({}, "image/jpeg", True),
        ({}, "video/mp4", False),
        ({}, "audio/mpeg", False),
        # 显式空数组：不接受任何上传。
        ({"mime_types": []}, "image/jpeg", False),
        ({"mime_types": []}, "video/mp4", False),
        # 精确 MIME。
        ({"mime_types": ["image/jpeg"]}, "image/jpeg", True),
        ({"mime_types": ["image/jpeg"]}, "image/png", False),
        # 别名归一化（配置值与探测结果两侧都做）。
        ({"mime_types": ["image/jpg"]}, "image/jpeg", True),
        ({"mime_types": ["audio/mp3"]}, "audio/mpeg", True),
        ({"mime_types": ["audio/m4a"]}, "audio/mp4", True),
        # type/* 通配。
        ({"mime_types": ["image/*"]}, "image/png", True),
        ({"mime_types": ["image/*"]}, "video/mp4", False),
        ({"mime_types": ["video/*", "audio/*"]}, "audio/mp4", True),
        # */* 全收。
        ({"mime_types": ["*/*"]}, "video/mp4", True),
        ({"mime_types": ["*/*"]}, "application/octet-stream", True),
        # 识别不出具体类型的文件只匹配 */*。
        ({"mime_types": ["image/*"]}, "application/octet-stream", False),
        (
            {"mime_types": ["application/octet-stream"]},
            "application/octet-stream",
            False,
        ),
    ],
)
def test_backend_accepts_declared_mime_types(entry, mime_type, accepted):
    backend = uploader._Backend(
        id="b",
        client=None,  # type: ignore[arg-type]
        accepted_mime_types=uploader._parse_accepted_mime_types(entry),
    )
    assert backend.accepts(mime_type) is accepted


def test_malformed_mime_types_falls_back_to_images_only():
    backend = uploader._Backend(
        id="b",
        client=None,  # type: ignore[arg-type]
        accepted_mime_types=uploader._parse_accepted_mime_types({"mime_types": 123}),
    )
    assert backend.accepts("image/png") is True
    assert backend.accepts("video/mp4") is False


# ------------------------------------------------------------ 候选筛选与上传


@pytest.mark.asyncio
async def test_video_is_only_offered_to_backends_declaring_it(image_host, tmp_path):
    """声明只吃图片的后端不能被拿视频去试，否则它的冷却会波及图片上传。"""
    image_host(
        [
            {"id": "img-only", "type": "fake"},
            {"id": "anything", "type": "fake", "mime_types": ["*/*"]},
        ]
    )
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16)

    url = await uploader.upload_media(str(video))

    assert url == "https://cdn.test/anything"
    assert [call[0] for call in _FakeClient.calls] == ["anything"]


@pytest.mark.asyncio
async def test_no_backend_accepts_type_returns_none_without_uploading(
    image_host, tmp_path
):
    image_host([{"id": "img-only", "type": "fake"}])
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 16)

    assert await uploader.upload_media(str(audio)) is None
    assert _FakeClient.calls == []


@pytest.mark.asyncio
async def test_upload_image_stays_backward_compatible(image_host, tmp_path):
    """旧调用方（不声明 mime_types、直接 upload_image）行为不变。"""
    from PIL import Image as PILImage

    image_host([{"id": "legacy", "type": "fake"}])
    image = tmp_path / "a.png"
    PILImage.new("RGB", (2, 2), (1, 2, 3)).save(image)

    assert await uploader.upload_image(str(image)) == "https://cdn.test/legacy"
    assert [call[0] for call in _FakeClient.calls] == ["legacy"]


@pytest.mark.asyncio
async def test_content_wins_over_extension(image_host, tmp_path):
    """出站图片经压缩/转码后扩展名可能已不符实际，MIME 必须按内容判定。"""
    from PIL import Image as PILImage

    image_host(
        [
            {"id": "img-only", "type": "fake", "mime_types": ["image/*"]},
            {"id": "video-only", "type": "fake", "mime_types": ["video/*"]},
        ]
    )
    # 内容是 PNG，扩展名却是 .mp4。
    disguised = tmp_path / "actually_png.mp4"
    PILImage.new("RGB", (2, 2), (4, 5, 6)).save(disguised, format="PNG")

    url = await uploader.upload_media(str(disguised))

    assert url == "https://cdn.test/img-only"
    assert [call[0] for call in _FakeClient.calls] == ["img-only"]


@pytest.mark.asyncio
async def test_unknown_content_only_matches_wildcard(image_host, tmp_path):
    image_host([{"id": "img-only", "type": "fake", "mime_types": ["image/*"]}])
    unknown = tmp_path / "blob.bin"
    unknown.write_bytes(b"\x01\x02\x03\x04not-a-known-container")

    assert media_utils.detect_file_mime_type(unknown) == "application/octet-stream"
    assert await uploader.upload_media(str(unknown)) is None
    assert _FakeClient.calls == []

    image_host([{"id": "anything", "type": "fake", "mime_types": ["*/*"]}])
    assert await uploader.upload_media(str(unknown)) == "https://cdn.test/anything"


@pytest.mark.asyncio
async def test_explicit_mime_type_overrides_detection(image_host, tmp_path):
    image_host([{"id": "audio-only", "type": "fake", "mime_types": ["audio/m4a"]}])
    blob = tmp_path / "clip.bin"
    blob.write_bytes(b"\x00" * 32)

    # 别名归一化对显式传入的 MIME 同样生效（audio/m4a -> audio/mp4）。
    assert (
        await uploader.upload_media(str(blob), None, "audio/x-m4a")
        == "https://cdn.test/audio-only"
    )


# ------------------------------------------------------------- MIME 探测本身


def test_detect_file_mime_type_by_content(tmp_path):
    from PIL import Image as PILImage

    png = tmp_path / "a.bin"
    PILImage.new("RGB", (1, 1)).save(png, format="PNG")
    assert media_utils.detect_file_mime_type(png) == "image/png"

    mp4 = tmp_path / "b.txt"
    mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16)
    assert media_utils.detect_file_mime_type(mp4) == "video/mp4"

    m4a = tmp_path / "c.txt"
    m4a.write_bytes(b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 16)
    assert media_utils.detect_file_mime_type(m4a) == "audio/mp4"

    wav = tmp_path / "d.txt"
    wav.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 16)
    assert media_utils.detect_file_mime_type(wav) == "audio/wav"

    mp3 = tmp_path / "e.txt"
    mp3.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 16)
    assert media_utils.detect_file_mime_type(mp3) == "audio/mpeg"

    webm = tmp_path / "f.txt"
    webm.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 32)
    assert media_utils.detect_file_mime_type(webm) == "video/webm"


def test_detect_file_mime_type_falls_back_to_extension(tmp_path):
    unknown = tmp_path / "notes.txt"
    unknown.write_bytes(b"plain text, no magic bytes here")
    assert media_utils.detect_file_mime_type(unknown) == "text/plain"


def test_normalize_mime_type_aliases():
    normalize = media_utils.normalize_mime_type
    assert normalize("IMAGE/JPG; charset=binary") == "image/jpeg"
    assert normalize("audio/mp3") == "audio/mpeg"
    assert normalize("audio/x-m4a") == "audio/mp4"
    assert normalize("audio/x-wav") == "audio/wav"
    assert normalize(None) == ""
