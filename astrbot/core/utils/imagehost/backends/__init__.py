"""图床后端客户端集合。

每个后端提供统一接口：``from_config(entry: dict) -> Self``（缺凭据抛 ``RuntimeError``）、
``upload_file(path, **_) -> UploadResult``（结果对象带 ``.url`` 公网外链）。
"""

from .bilibili import BilibiliImageHost
from .chatglm import ChatGLMImageHost
from .cos import CosNoSdkClient
from .qqchannel import QQChannelImageHost
from .s3 import S3NoSdkClient
from .yuanbao import YuanbaoImageHost

__all__ = [
    "BilibiliImageHost",
    "ChatGLMImageHost",
    "CosNoSdkClient",
    "QQChannelImageHost",
    "S3NoSdkClient",
    "YuanbaoImageHost",
]
