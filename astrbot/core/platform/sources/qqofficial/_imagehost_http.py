"""图床上传共享的 HTTP 配置：可选 SOCKS5 / HTTP 代理。

部分图床（chatglm / bilibili / nature）是第三方外部服务，某些部署环境下需要
经代理才能访问或获得更稳定的速度。代理 URL 按以下优先级解析（均为环境变量，
可写在 ``data/.env``）：

    QQOFFICIAL_IMAGE_PROXY > SOCKS5_PROXY > ALL_PROXY > HTTPS_PROXY > HTTP_PROXY

- 支持 ``socks5://[user:pass@]host:port`` 与 ``http://host:port``。
- 把 ``QQOFFICIAL_IMAGE_PROXY`` 显式设为 ``off`` / ``none`` / ``disable`` 可强制直连，
  忽略其余变量（便于临时关闭而无需清空系统级 *_PROXY）。
- 均未设置时返回 None，即直连。

注意：代理仅用于外部图床；qqchannel / cos 走腾讯自家 API，不经此处。
"""

from __future__ import annotations

import os

import httpx

# 解析顺序：专用变量优先，其后回退到通用代理变量。
_PROXY_ENV_KEYS = (
    "QQOFFICIAL_IMAGE_PROXY",
    "SOCKS5_PROXY",
    "ALL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
)
_DISABLE_TOKENS = {"off", "none", "disable", "disabled", "false", "0"}


def resolve_proxy() -> str | None:
    """返回当前生效的代理 URL；未配置或被显式禁用时返回 None。"""
    primary = os.getenv("QQOFFICIAL_IMAGE_PROXY", "").strip()
    if primary.lower() in _DISABLE_TOKENS:
        return None
    for key in _PROXY_ENV_KEYS:
        val = os.getenv(key, "").strip()
        if val:
            return val
    return None


def http_kwargs(timeout: httpx.Timeout | float, **extra: object) -> dict:
    """构造 httpx 请求的 kwargs，按需注入 ``proxy=``。

    每次调用都重新解析环境变量，便于运行时修改 ``data/.env`` 后即时生效。
    """
    kw: dict = {"timeout": timeout, **extra}
    proxy = resolve_proxy()
    if proxy:
        kw["proxy"] = proxy
    return kw
