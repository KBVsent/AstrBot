"""图床后端共用的 HTTP 客户端。

各后端的上传都跑在 uploader 的 asyncio.to_thread 线程里，所以用同步客户端；
httpcore 的连接池自带线程锁，跨线程共用是安全的。
"""

from __future__ import annotations

import http.cookiejar

import httpx

# 对象直传的超时。httpx 的超时按单次读写计，不是整个请求的总时长，
# 大文件只要数据还在流动就不会被掐断。
OBJECT_PUT_TIMEOUT = 120.0

# 全进程共用一个连接池，池按域名分开管理。省下的是相邻两次上传之间的 TLS 握手，
# 单次上传内部基本复用不到（元宝的三个请求也分属三个域名）。
# 空闲时限取 30s：腾讯的几个域名实测空闲 30s 仍可复用、65s 时已被服务端断开
# （2026-10-22 实测），再长就可能拿到服务端正要关闭的连接。
# cookie 罐拒收一切：客户端在所有后端、所有账号间共用，鉴权只能来自各自显式带上的请求头，
# 服务端回写的 cookie（如元宝 genUploadInfo 的 hy_user）不能变成共享状态。
HTTP_CLIENT = httpx.Client(
    # 连接数上限沿用 httpx 默认值，只传 keepalive_expiry 会把它们变成不限
    limits=httpx.Limits(
        max_connections=100, max_keepalive_connections=20, keepalive_expiry=30.0
    ),
    cookies=http.cookiejar.CookieJar(
        policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[])
    ),
)
