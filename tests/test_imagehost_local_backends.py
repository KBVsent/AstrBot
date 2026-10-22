"""调用方自带图床后端（LocalBackend）在优先链里的语义。

关键点：自带后端和全局后端共用排序与冷却；后端对象每次调用都是新的，
冷却必须按 state_key 跨调用保留，且不同 key 互不牵连。
"""

import json

import pytest

from astrbot.core.utils.imagehost import uploader
from astrbot.core.utils.imagehost.uploader import LocalBackend


class _FakeResult:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakeClient:
    def __init__(self, backend_id: str, calls: list[str]) -> None:
        self.backend_id = backend_id
        self.calls = calls

    def upload_file(self, path: str, public_url: bool = False):  # noqa: ARG002
        self.calls.append(self.backend_id)
        return _FakeResult(f"https://cdn.test/{self.backend_id}")


@pytest.fixture
def calls() -> list[str]:
    return []


@pytest.fixture
def global_hosts(monkeypatch, calls):
    """按给定 id 装配全局图床后端（全部用记录调用的假客户端）。"""

    def _setup(ids: list[str]) -> None:
        uploader.reset_backends()
        entries = json.dumps([{"id": i, "type": "fake"} for i in ids])

        class _FakeAstrBotConfig:
            @staticmethod
            def get(key, default=None):
                return entries if key == "image_host" else default

        class _FakeBackendType:
            @staticmethod
            def from_config(entry: dict) -> _FakeClient:
                return _FakeClient(str(entry["id"]), calls)

        import astrbot.core as astrbot_core

        monkeypatch.setattr(astrbot_core, "astrbot_config", _FakeAstrBotConfig)
        monkeypatch.setitem(uploader._BACKEND_TYPES, "fake", _FakeBackendType)

    yield _setup
    uploader.reset_backends()


@pytest.fixture
def image(tmp_path):
    from PIL import Image as PILImage

    path = tmp_path / "a.png"
    PILImage.new("RGB", (2, 2), (1, 2, 3)).save(path)
    return str(path)


def _local(calls: list[str], name: str, state_key: str, *, fail: bool = False):
    async def upload(path: str) -> str:  # noqa: ARG001
        calls.append(name)
        if fail:
            raise RuntimeError("boom")
        return f"https://local.test/{name}"

    return LocalBackend(upload=upload, state_key=state_key)


@pytest.mark.asyncio
async def test_local_backend_follows_chain_order(global_hosts, calls, image):
    global_hosts(["yuanbao"])
    local = {"qqofficial": _local(calls, "qqofficial", "k")}

    url = await uploader.upload_image(image, ["qqofficial", "yuanbao"], local)
    assert url == "https://local.test/qqofficial"
    assert calls == ["qqofficial"]

    calls.clear()
    url = await uploader.upload_image(image, ["yuanbao", "qqofficial"], local)
    assert url == "https://cdn.test/yuanbao"
    assert calls == ["yuanbao"]


@pytest.mark.asyncio
async def test_local_backend_works_without_global_backends(global_hosts, calls, image):
    """QQ 官方未配图床时默认只用自带后端，全局一个后端都没有也必须能上传。"""
    global_hosts([])
    local = {"qqofficial": _local(calls, "qqofficial", "k")}

    url = await uploader.upload_image(image, ["qqofficial"], local)
    assert url == "https://local.test/qqofficial"


@pytest.mark.asyncio
async def test_local_backend_cooldown_is_scoped_by_state_key(
    global_hosts, calls, image
):
    global_hosts(["yuanbao"])
    chain = ["qqofficial", "yuanbao"]

    failing = {"qqofficial": _local(calls, "qq-a", "qqofficial@a", fail=True)}
    assert (
        await uploader.upload_image(image, chain, failing) == "https://cdn.test/yuanbao"
    )
    assert calls == ["qq-a", "yuanbao"]

    # 同一个 key 在冷却中：即使这次传入的是新的后端对象，也直接跳过
    calls.clear()
    retry = {"qqofficial": _local(calls, "qq-a", "qqofficial@a")}
    assert (
        await uploader.upload_image(image, chain, retry) == "https://cdn.test/yuanbao"
    )
    assert calls == ["yuanbao"]

    # 另一个机器人的 key 不受影响
    calls.clear()
    other = {"qqofficial": _local(calls, "qq-b", "qqofficial@b")}
    assert await uploader.upload_image(image, chain, other) == "https://local.test/qq-b"
    assert calls == ["qq-b"]
