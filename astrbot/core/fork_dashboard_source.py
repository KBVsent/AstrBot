"""本 fork 自建 WebUI 的下载源"""

from __future__ import annotations

import os

# 环境变量 ASTRBOT_FORK_DASHBOARD_REPO 可覆盖仓库；置为空字符串则完全禁用 fork 源，
# 回到上游的下载行为。
_DEFAULT_REPO = "KBVsent/AstrBot"
_ENV_REPO = "ASTRBOT_FORK_DASHBOARD_REPO"


def fork_dashboard_url(version: str) -> str | None:
    """给定核心版本，返回本 fork 的 WebUI 包地址；不适用时返回 None。

    Args:
        version: 目标版本，形如 v4.26.8。40 位提交哈希不适用 —— fork 不按提交发包。

    Returns:
        下载地址，或 None 表示应改用上游源。
    """
    repo = os.environ.get(_ENV_REPO, _DEFAULT_REPO).strip()
    if not repo or len(version) == 40:
        return None
    tag = version if version.startswith("v") else f"v{version}"
    return f"https://github.com/{repo}/releases/download/webui-{tag}/dist.zip"
