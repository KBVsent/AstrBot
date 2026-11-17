"""SNS client dashboard API: listing, sending, images, events and auth."""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import jwt
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlmodel import col, select

from astrbot.core.db.po import ImMessage, ImMessageWrite
from astrbot.core.message.components import Image, Plain
from astrbot.core.sns_recorder import SnsMessageWriter
from astrbot.core.star.context import Context
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.dashboard.api import sns as sns_api
from astrbot.dashboard.api.auth import require_dashboard_user
from astrbot.dashboard.responses import ApiError, error
from astrbot.dashboard.services import sns_service as sns_service_module
from astrbot.dashboard.services.sns_service import (
    SnsBroadcaster,
    SnsService,
    SnsServiceError,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _write(umo: str, content: list, *, at: datetime, sender: str = "u1"):
    platform_id, message_type, _ = umo.split(":", 2)
    return ImMessageWrite(
        message=ImMessage(
            umo=umo,
            direction="in",
            sender_id=sender,
            content=content,
            created_at=at,
        ),
        platform_id=platform_id,
        message_type=message_type,
        preview="",
    )


def _platform(platform_id: str, name: str, sent: list, *, fail: bool = False):
    async def send_by_session(session, chain):
        if fail:
            raise RuntimeError("adapter rejected")
        sent.append((str(session), chain.get_plain_text(), chain.use_markdown_))

    platform = SimpleNamespace(
        meta=lambda: SimpleNamespace(id=platform_id, name=name),
        send_by_session=send_by_session,
        _session_scene={},
    )
    platform.remember_session_scene = platform._session_scene.__setitem__
    return platform


def _service(db, platforms: list, writer: SnsMessageWriter | None = None) -> Any:
    context: Any = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=platforms),
        get_config=lambda **kwargs: {},
    )
    context.send_message = lambda session, chain: Context.send_message(
        context, session, chain
    )
    lifecycle: Any = SimpleNamespace(
        astrbot_config={"platform": [{"id": "p1", "type": "aiocqhttp"}]},
        platform_manager=context.platform_manager,
        star_context=context,
        sns_writer=writer,
    )
    return SnsService(db, lifecycle)


async def _settle(service: SnsService) -> None:
    while service._deliveries or (
        service._flushing is not None and not service._flushing.done()
    ):
        await asyncio.gather(*service._deliveries)
        if service._flushing is not None:
            await service._flushing


@pytest.mark.asyncio
async def test_session_list_names_order_and_message_pages(temp_db):
    base = datetime(2026, 11, 17, 10, 0)
    group, alice, bob = (
        "p1:GroupMessage:g1",
        "p1:FriendMessage:u1",
        "p1:FriendMessage:u2",
    )
    await temp_db.insert_im_messages(
        [_write(group, [{"type": "Plain", "text": str(i)}], at=base) for i in range(5)]
        + [
            _write(
                alice, [{"type": "Plain", "text": "hi"}], at=base + timedelta(hours=1)
            ),
            _write(
                bob, [{"type": "Plain", "text": "yo"}], at=base + timedelta(hours=2)
            ),
            _write("p2:GroupMessage:g2", [], at=base - timedelta(hours=1)),
        ]
    )
    await temp_db.upsert_umo_alias(group, "u1", "Group One", "Team")
    await temp_db.upsert_umo_auto_name(alice, "u1", "Alice")
    service = _service(temp_db, [_platform("p1", "aiocqhttp", [])])
    await service.patch_session("p2:GroupMessage:g2", pinned=True, keep_forever=None)

    async def page(**kwargs):
        params = dict.fromkeys(("message_type", "platform_id", "query", "cursor"))
        result = await service.list_sessions(**{**params, "limit": 2, **kwargs})
        return [s["display_name"] for s in result["sessions"]], result

    first, result = await page()
    second, last = await page(cursor=result["next_cursor"])
    assert first + second == ["g2", "u2", "Alice", "Team"]
    assert last["next_cursor"] is None
    assert result["unread_sessions"] == 4
    assert result["platforms"] == [{"id": "p1", "type": "aiocqhttp"}]
    assert result["sessions"][1]["platform_type"] == "aiocqhttp"
    assert (await page(query="team"))[0] == ["Team"]
    # The manual alias replaces the automatic name in search as well as display.
    assert (await page(query="group one"))[0] == []
    assert (await page(query="u2"))[0] == ["u2"]
    assert (await page(message_type="FriendMessage", limit=5))[0] == ["u2", "Alice"]

    async def ids(**kwargs):
        result = await service.list_messages(group, limit=2, **kwargs)
        return [m["id"] for m in result["messages"]], result["has_more"]

    assert await ids(before_id=None, after_id=None) == ([4, 5], True)
    assert await ids(before_id=4, after_id=None) == ([2, 3], True)
    assert await ids(before_id=2, after_id=None) == ([1], False)
    assert await ids(before_id=None, after_id=1) == ([2, 3], True)
    assert await ids(before_id=None, after_id=3) == ([4, 5], False)
    with pytest.raises(SnsServiceError):
        await service.list_messages(group, before_id=4, after_id=1, limit=2)


@pytest.mark.asyncio
async def test_send_records_one_message_and_reports_status(temp_db):
    sent: list = []
    writer = SnsMessageWriter(temp_db, {})
    qq = _platform("qq", "qq_official", sent)
    qq._session_scene["ch1"] = "channel"
    service = _service(
        temp_db, [qq, _platform("broken", "discord", sent, fail=True)], writer
    )
    events = service.broadcaster.subscribe()
    umos = [
        "qq:GroupMessage:g1",
        "qq:GroupMessage:ch1",
        "gone:FriendMessage:u1",
        "broken:FriendMessage:u1",
    ]
    await temp_db.insert_im_messages(
        [
            _write(umo, [{"type": "Plain", "text": "in"}], at=datetime.now())
            for umo in umos
        ]
    )
    writer.install()
    try:
        replies = [
            await service.send_text(umo, f"to {umo}", f"c{index}")
            for index, umo in enumerate(umos)
        ]
        repeat = await service.send_text(umos[0], "again", "c0")
        await _settle(service)
        await writer.drain()
    finally:
        await writer.close()

    assert [reply["status"] for reply in replies] == ["sending"] * 4
    assert repeat["id"] == replies[0]["id"]
    assert sent == [
        (umos[0], f"to {umos[0]}", False),
        (umos[1], f"to {umos[1]}", False),
    ]
    assert qq._session_scene == {"g1": "group", "ch1": "channel"}
    async with temp_db.get_db() as session:
        rows = (
            (
                await session.execute(
                    select(ImMessage)
                    .where(col(ImMessage.direction) == "out")
                    .order_by(col(ImMessage.id))
                )
            )
            .scalars()
            .all()
        )
    # Context.send_message would record a second copy without the suppression.
    assert [(row.status, row.error) for row in rows] == [
        ("ok", None),
        ("ok", None),
        ("failed", "Platform not running: gone"),
        ("failed", "adapter rejected"),
    ]
    received = []
    while not events.empty():
        received.append(events.get_nowait())
    created = [data for name, data in received if name == "message.created"]
    updated = {
        data["message"]["client_id"]: data["message"]["status"]
        for name, data in received
        if name == "message.updated"
    }
    assert len(created) == 4
    assert updated == {"c0": "ok", "c1": "ok", "c2": "failed", "c3": "failed"}
    assert created[0]["session"]["last_message_id"] == replies[0]["id"]
    assert created[0]["session"]["display_name"] == "g1"


@pytest.mark.asyncio
async def test_delete_session_commits_queued_messages_first(temp_db):
    umo = "p1:FriendMessage:u1"
    writer = SnsMessageWriter(temp_db, {})
    service = _service(temp_db, [_platform("p1", "aiocqhttp", [])], writer)
    await temp_db.insert_im_messages(
        [_write(umo, [{"type": "Plain", "text": "old"}], at=datetime.now())]
    )
    writer.record_outbound(umo, "aiocqhttp", "bot", [Plain("late")])

    await service.delete_session(umo)
    # Whatever is still queued gets committed by the writer task after the delete.
    await writer.drain()

    assert await temp_db.get_im_session(umo) is None
    async with temp_db.get_db() as session:
        rows = (await session.execute(select(ImMessage))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_image_sources_stay_on_server_and_are_checked(
    temp_db, tmp_path, monkeypatch
):
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    (temp_root / "in.png").write_bytes(PNG)
    (tmp_path / "out.png").write_bytes(PNG)
    (temp_root / "link.png").symlink_to(tmp_path / "out.png")
    monkeypatch.setattr(
        sns_service_module, "get_astrbot_temp_path", lambda: str(temp_root)
    )
    secret = "https://api.telegram.org/file/botSECRET/"

    def image(kind: str, value: str) -> dict:
        return {"type": "Image", "src": {"kind": kind, "value": value}}

    await temp_db.insert_im_messages(
        [
            _write(
                "p1:FriendMessage:u1",
                [
                    {"type": "Plain", "text": "x"},
                    image("file", str(temp_root / "in.png")),
                    image("file", str(tmp_path / "out.png")),
                    image("file", str(temp_root / "link.png")),
                    image("url", secret + "ok.png"),
                    image("url", secret + "page.html"),
                    image("url", secret + "huge.png"),
                    image("none", ""),
                    image("url", "http://intranet.test/a.png"),
                    image("url", secret + "redirect.png"),
                    image("url", "file:///etc/passwd"),
                    image("url", secret + "expired.png"),
                ],
                at=datetime.now(),
            )
        ]
    )

    async def resolve(host, _port):
        address = "10.0.0.5" if host == "intranet.test" else "149.154.167.220"
        return [(2, 1, 6, "", (address, 0))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)

    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.host == "intranet.test":
            return httpx.Response(
                200, headers={"content-type": "image/png"}, content=PNG
            )
        if request.url.path.endswith("redirect.png"):
            return httpx.Response(302, headers={"location": "http://intranet.test/a"})
        if request.url.path.endswith("ok.png"):
            # The bytes decide the type, whatever the host declares.
            headers = {"content-type": "application/octet-stream"}
            return httpx.Response(200, headers=headers, content=PNG)
        if request.url.path.endswith("expired.png"):
            return httpx.Response(400, json={"retmsg": "download url has expired"})
        if request.url.path.endswith("huge.png"):
            headers = {"content-type": "image/png", "content-length": str(30 << 20)}
            return httpx.Response(200, headers=headers, content=b"")
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<p>"
        )

    logged: dict[str, list[str]] = {"debug": [], "warning": []}
    monkeypatch.setattr(
        sns_service_module,
        "logger",
        SimpleNamespace(debug=logged["debug"].append, warning=logged["warning"].append),
    )
    service = _service(temp_db, [])
    service._http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    listed = await service.list_messages(
        "p1:FriendMessage:u1", before_id=None, after_id=None, limit=30
    )
    assert "SECRET" not in json.dumps(listed) and "png" not in json.dumps(listed)
    assert [s.get("available") for s in listed["messages"][0]["content"][1:]] == [
        True,
        True,
        True,
        True,
        True,
        True,
        False,
        True,
        True,
        True,
        True,
    ]

    local = await service.open_image(1, 1)
    assert (local.media_type, local.path) == (
        "image/png",
        (temp_root / "in.png").resolve(),
    )
    proxied = await service.open_image(1, 4)
    assert proxied.chunks is not None
    assert b"".join([chunk async for chunk in proxied.chunks]) == PNG
    for index in (0, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12):
        with pytest.raises(SnsServiceError) as rejected:
            await service.open_image(1, index)
        assert rejected.value.status_code == 404
    await service._http.aclose()
    # An expired link is routine; content that is not an image still warns.
    assert any("status 400" in line for line in logged["debug"])
    assert not any("status 400" in line for line in logged["warning"])
    assert any("type text/html" in line for line in logged["warning"])


@pytest.mark.asyncio
async def test_sent_images_are_served_from_memory_copies_and_the_data_dir(temp_db):
    umo = "p1:FriendMessage:u1"
    temp_root = Path(get_astrbot_temp_path())
    in_data = temp_root.parent / "sns_test_sent.png"
    in_data.write_bytes(PNG)
    writer = SnsMessageWriter(temp_db, {})
    service = _service(temp_db, [], writer)
    try:
        writer.record_outbound(
            umo,
            "aiocqhttp",
            "bot",
            [Image.fromBytes(PNG), Image.fromFileSystem(str(in_data))],
        )
        await writer.drain()
        # The same path in a received message stays out of reach.
        await temp_db.insert_im_messages(
            [
                _write(
                    umo,
                    [{"type": "Image", "src": {"kind": "file", "value": str(in_data)}}],
                    at=datetime.now(),
                )
            ]
        )

        copied = await service.open_image(1, 0)
        assert copied.path is not None and copied.path.read_bytes() == PNG
        assert copied.path.is_relative_to(temp_root)
        assert (await service.open_image(1, 1)).path == in_data.resolve()
        with pytest.raises(SnsServiceError):
            await service.open_image(2, 0)
    finally:
        await writer.close()
        in_data.unlink()


@pytest.mark.asyncio
async def test_broadcaster_overflow_replaces_backlog_with_resync():
    broadcaster = SnsBroadcaster(queue_size=2)
    slow, fresh = broadcaster.subscribe(), broadcaster.subscribe()
    broadcaster.publish("session.deleted", {"umo": "a"})
    broadcaster.publish("session.deleted", {"umo": "b"})
    fresh.get_nowait()
    broadcaster.publish("session.deleted", {"umo": "c"})

    assert [slow.get_nowait()] == [("resync", {})] and slow.empty()
    assert [fresh.get_nowait(), fresh.get_nowait()] == [
        ("session.deleted", {"umo": "b"}),
        ("session.deleted", {"umo": "c"}),
    ]

    service = _service(None, [])
    stream = service.stream_events()
    assert await anext(stream) == ": connected\n\n"
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    service.broadcaster.publish("session.deleted", {"umo": "a"})
    assert await pending == 'event: session.deleted\ndata: {"umo": "a"}\n\n'
    await stream.aclose()
    assert not service.broadcaster.has_subscribers


@pytest.mark.asyncio
async def test_every_sns_route_requires_dashboard_login(temp_db):
    app = FastAPI()
    app.state.jwt_secret = "secret"
    app.state.services = SimpleNamespace(sns=_service(temp_db, []))
    app.include_router(sns_api.router, prefix="/api/v1")

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError):
        return JSONResponse(error(exc.message), status_code=exc.status_code)

    routes = [route for route in sns_api.router.routes if isinstance(route, APIRoute)]
    assert len(routes) == 8
    token = jwt.encode({"username": "admin"}, "secret", algorithm="HS256")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        for route in routes:
            assert any(
                dep.call is require_dashboard_user
                for dep in route.dependant.dependencies
            )
            assert not route.include_in_schema
            for method in route.methods or ():
                for headers in ({}, {"X-API-Key": "abk_any"}):
                    response = await client.request(
                        method, f"/api/v1{route.path}", headers=headers
                    )
                    assert response.status_code == 401, (method, route.path)
        response = await client.get(
            "/api/v1/sns/sessions", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200
