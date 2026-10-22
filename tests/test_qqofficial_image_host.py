"""QQ 官方富媒体上传作为图床：四步上传流程、错误处理、默认优先链，以及发送时按会话挑选目标。"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import botpy.interaction
import botpy.message
import httpx
import pytest

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.qqofficial import (
    _image_host,
    _markdown_media,
    qqofficial_message_event,
    qqofficial_platform_adapter,
)
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
    QQOfficialMessageEvent,
)
from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
    QQOfficialPlatformAdapter,
)

BASE_URL = "https://api.sgroup.qq.com/v2/groups/g1"
HEADERS = {"Authorization": "QQBot token-1", "X-Union-Appid": "app-1"}


class _FakeQQ:
    """模拟开放平台接口与 COS 预签名 PUT，记录收到的每个请求。"""

    def __init__(
        self,
        *,
        block_size: int | None = None,
        finish_codes: list[int] | None = None,
        raw_url: str = "https://cos.test/robot_upload/part_1?sign=1",
    ) -> None:
        self.block_size = block_size
        self.finish_codes = list(finish_codes or [])
        self.raw_url = raw_url
        self.calls: list[tuple[str, str]] = []
        self.bodies: dict[str, list[dict]] = {}
        self.put_chunks: list[bytes] = []
        self.put_types: list[str | None] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        step = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((request.method, step))
        if request.method == "PUT":
            self.put_chunks.append(request.content)
            self.put_types.append(request.headers.get("content-type"))
            return httpx.Response(200)

        body = json.loads(request.content)
        self.bodies.setdefault(step, []).append(body)
        if step == "upload_prepare":
            size = int(body["file_size"])
            block = self.block_size or size
            count = -(-size // block)
            return httpx.Response(
                200,
                json={
                    "upload_id": "up-1",
                    "block_size": block,
                    "parts": [
                        {"index": i, "presigned_url": f"https://cos.test/part_{i}"}
                        for i in range(1, count + 1)
                    ],
                },
            )
        if step == "upload_part_finish":
            if self.finish_codes:
                code = self.finish_codes.pop(0)
                return httpx.Response(500, json={"code": code, "message": "bdh"})
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"file_info": "info", "raw_url": self.raw_url})


@pytest.fixture
def fake_qq(monkeypatch):
    def _install(**kwargs) -> _FakeQQ:
        server = _FakeQQ(**kwargs)
        client = httpx.Client(transport=httpx.MockTransport(server.handler))
        monkeypatch.setattr(_image_host, "HTTP_CLIENT", client)
        monkeypatch.setattr(_image_host, "time", SimpleNamespace(sleep=lambda _s: None))
        return server

    return _install


@pytest.fixture
def png(tmp_path):
    from PIL import Image as PILImage

    path = tmp_path / "pic.png"
    PILImage.new("RGB", (8, 8), (1, 2, 3)).save(path)
    return path


# ------------------------------------------------------------------ 上传流程


def test_upload_runs_four_steps_and_returns_raw_url(fake_qq, png):
    server = fake_qq()
    data = png.read_bytes()

    url = _image_host._upload(png, BASE_URL, HEADERS)

    assert url == server.raw_url
    assert server.calls == [
        ("POST", "upload_prepare"),
        ("PUT", "part_1"),
        ("POST", "upload_part_finish"),
        ("POST", "files"),
    ]
    # 不带 Content-Type 时 raw_url 会回 application/octet-stream
    assert server.put_types == ["image/png"]
    assert server.put_chunks == [data]
    assert server.bodies["upload_part_finish"] == [
        {
            "upload_id": "up-1",
            "part_index": 1,
            "block_size": str(len(data)),
            "md5": hashlib.md5(data).hexdigest(),
        }
    ]
    assert server.bodies["files"] == [
        {
            "file_type": 1,
            "srv_send_msg": False,
            "file_name": "pic.png",
            "upload_id": "up-1",
        }
    ]


def test_multi_part_upload_splits_by_block_size(fake_qq, png):
    """实测服务端总是只给一片，这条路径只有测试能覆盖；切错了图片会被悄悄损坏。"""
    data = png.read_bytes()
    server = fake_qq(block_size=len(data) // 3 + 1)

    _image_host._upload(png, BASE_URL, HEADERS)

    assert b"".join(server.put_chunks) == data
    assert [b["block_size"] for b in server.bodies["upload_part_finish"]] == [
        str(len(chunk)) for chunk in server.put_chunks
    ]
    assert [b["part_index"] for b in server.bodies["upload_part_finish"]] == [1, 2, 3]


def test_bdh_channel_error_is_retried_at_most_three_times(fake_qq, png):
    server = fake_qq(finish_codes=[40093001] * 3)

    with pytest.raises(RuntimeError, match="40093001"):
        _image_host._upload(png, BASE_URL, HEADERS)
    assert server.calls.count(("POST", "upload_part_finish")) == 3
    assert ("POST", "files") not in server.calls


def test_missing_raw_url_is_an_error(fake_qq, png):
    """空字符串若当成功返回，会拼出空链接的 markdown 图片，也不会回退到其他后端。"""
    fake_qq(raw_url="")

    with pytest.raises(RuntimeError, match="raw_url"):
        _image_host._upload(png, BASE_URL, HEADERS)


# ------------------------------------------------------------ 后端构建


class _FakeBotHttp:
    is_sandbox = False

    def __init__(self) -> None:
        self._headers: dict | None = None

    async def check_session(self) -> None:
        self._headers = dict(HEADERS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "expected_base"),
    [
        ({"group_openid": "g1"}, "https://api.sgroup.qq.com/v2/groups/g1"),
        ({"user_openid": "u1"}, "https://api.sgroup.qq.com/v2/users/u1"),
    ],
)
async def test_backend_upload_uses_bot_token_and_session_scope(
    monkeypatch, png, target, expected_base
):
    captured = {}

    def fake_upload(path, base_url, headers):
        captured.update(path=path, base_url=base_url, headers=headers)
        return "https://cos.test/raw"

    monkeypatch.setattr(_image_host, "_upload", fake_upload)
    bot = SimpleNamespace(api=SimpleNamespace(_http=_FakeBotHttp()))
    backends = _image_host.image_host_backends(bot, "bot-a", **target)

    assert await backends["qqofficial"].upload(str(png)) == "https://cos.test/raw"
    assert captured == {"path": png, "base_url": expected_base, "headers": HEADERS}


# ------------------------------------------------------- 发送时挑选目标


class _Stop(Exception):
    pass


def _bare(cls, **attrs):
    obj = cls.__new__(cls)
    for key, value in attrs.items():
        setattr(obj, key, value)
    return obj


def _capture_targets(monkeypatch, module) -> dict:
    captured: dict = {}

    def fake_backends(bot, platform_id, **target):  # noqa: ARG001
        captured.update(platform_id=platform_id, **target)
        return {}

    async def stop(*_args, **_kwargs):
        raise _Stop

    monkeypatch.setattr(module, "image_host_backends", fake_backends)
    monkeypatch.setattr(
        QQOfficialMessageEvent, "_parse_to_qqofficial", staticmethod(stop)
    )
    return captured


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            _bare(botpy.message.GroupMessage, group_openid="g1"),
            {"group_openid": "g1", "user_openid": None},
        ),
        (
            _bare(botpy.message.C2CMessage, author=SimpleNamespace(user_openid="u1")),
            {"group_openid": None, "user_openid": "u1"},
        ),
        (
            _bare(
                botpy.interaction.Interaction,
                chat_type=1,
                group_openid="g1",
                user_openid="member",
            ),
            {"group_openid": "g1", "user_openid": None},
        ),
        (
            _bare(
                botpy.interaction.Interaction,
                chat_type=2,
                group_openid=None,
                user_openid="u1",
            ),
            {"group_openid": None, "user_openid": "u1"},
        ),
    ],
)
async def test_reply_targets_current_session(monkeypatch, source, expected):
    captured = _capture_targets(monkeypatch, qqofficial_message_event)
    event = _bare(
        QQOfficialMessageEvent,
        message_obj=SimpleNamespace(raw_message=source),
        send_buffer=None,
        bot=object(),
        platform_meta=SimpleNamespace(id="bot-a"),
        _image_host_chain=None,
    )

    with pytest.raises(_Stop):
        await event._post_send_one(MessageChain([Plain("hi")]))
    assert captured == {"platform_id": "bot-a", **expected}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message_type", "scene", "expected"),
    [
        (
            MessageType.GROUP_MESSAGE,
            "group",
            {"group_openid": "s1", "user_openid": None},
        ),
        # 场景未知的群会话按频道发送，没有目标
        (MessageType.GROUP_MESSAGE, None, {"group_openid": None, "user_openid": None}),
        (
            MessageType.FRIEND_MESSAGE,
            None,
            {"group_openid": None, "user_openid": "s1"},
        ),
    ],
)
async def test_proactive_send_targets_session(
    monkeypatch, message_type, scene, expected
):
    captured = _capture_targets(monkeypatch, qqofficial_platform_adapter)
    adapter = _bare(
        QQOfficialPlatformAdapter,
        config={},
        client=object(),
        _session_scene={"s1": scene} if scene else {},
    )

    with pytest.raises(_Stop):
        await adapter._send_by_session_common(
            MessageSession("bot-a", message_type, "s1"),
            MessageChain([Plain("hi")]),
        )
    assert captured == {"platform_id": "bot-a", **expected}


# ------------------------------------------------------------ 默认优先链


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chain", "local", "expected_chain"),
    [
        # 没配优先链时只用机器人自身的上传，不拿免凭证的全局后端做默认
        (None, {"qqofficial": object()}, ["qqofficial"]),
        (
            ["yuanbao", "qqofficial"],
            {"qqofficial": object()},
            ["yuanbao", "qqofficial"],
        ),
        # 频道拿不到目标、没有自带后端，仍按全局后端走
        (None, {}, None),
    ],
)
async def test_markdown_image_defaults_to_own_upload(
    monkeypatch, png, chain, local, expected_chain
):
    captured = {}

    async def fake_upload_image(path, chain_arg, local_arg):  # noqa: ARG001
        captured["chain"] = chain_arg
        return "https://cos.test/raw"

    monkeypatch.setattr(_markdown_media, "upload_image", fake_upload_image)

    fragment = await _markdown_media.image_to_markdown_fragment(
        Image.fromFileSystem(str(png)), chain, local
    )

    assert captured["chain"] == expected_chain
    assert fragment == "\n![img #8px #8px](https://cos.test/raw)\n"
