import asyncio
import os
import time
import uuid


class FileTokenService:
    """维护一个简单的基于令牌的文件下载服务，支持超时和懒清除。

    令牌默认是一次性的（读取即失效）。注册时传 reusable=True 可得到在有效期内
    可被重复读取的令牌，供需要被外部服务多次拉取的场景使用
    """

    def __init__(self, default_timeout: float = 300) -> None:
        self.lock = asyncio.Lock()
        self.staged_files = {}  # token: (file_path, expire_time, reusable)
        self.default_timeout = default_timeout

    async def _cleanup_expired_tokens(self) -> None:
        """清理过期的令牌"""
        now = time.time()
        expired_tokens = [
            token for token, (_, expire, _) in self.staged_files.items() if expire < now
        ]
        for token in expired_tokens:
            self.staged_files.pop(token, None)

    async def check_token_expired(self, file_token: str) -> bool:
        async with self.lock:
            await self._cleanup_expired_tokens()
            return file_token not in self.staged_files

    async def register_file(
        self,
        file_path: str,
        timeout: float | None = None,
        reusable: bool = False,
    ) -> str:
        """向令牌服务注册一个文件。

        Args:
            file_path(str): 文件路径
            timeout(float): 超时时间，单位秒（可选）
            reusable(bool): 为 True 时令牌在有效期内可被重复读取；默认一次性

        Returns:
            str: 一个文件令牌（默认单次有效）

        Raises:
            FileNotFoundError: 当路径不存在时抛出

        """
        try:
            from astrbot.core.utils.media_utils import file_uri_to_path, is_file_uri

            local_path = (
                file_uri_to_path(file_path) if is_file_uri(file_path) else file_path
            )
        except Exception:
            # Fall back to the original path if URL parsing fails.
            local_path = file_path

        async with self.lock:
            await self._cleanup_expired_tokens()

            if not os.path.exists(local_path):
                raise FileNotFoundError(
                    f"File does not exist: {local_path} (original input: {file_path})",
                )

            file_token = str(uuid.uuid4())
            expire_time = time.time() + (
                timeout if timeout is not None else self.default_timeout
            )
            # 存储转换后的真实路径
            self.staged_files[file_token] = (local_path, expire_time, reusable)
            return file_token

    async def handle_file(self, file_token: str) -> str:
        """根据令牌获取文件路径。一次性令牌使用后失效，可重复令牌保留至过期。

        Args:
            file_token(str): 注册时返回的令牌

        Returns:
            str: 文件路径

        Raises:
            KeyError: 当令牌不存在或已过期时抛出
            FileNotFoundError: 当文件本身已被删除时抛出

        """
        async with self.lock:
            await self._cleanup_expired_tokens()

            if file_token not in self.staged_files:
                raise KeyError(f"Invalid or expired file token: {file_token}")

            file_path, _, reusable = self.staged_files[file_token]
            if not reusable:
                self.staged_files.pop(file_token, None)
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"File does not exist: {file_path}")
            return file_path
