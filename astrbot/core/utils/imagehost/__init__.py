"""共享图床模块：把本地图片上传到第三方 CDN 得到长期可访问的公网外链，供各适配器复用。"""

from .uploader import reset_backends, upload_image

__all__ = ["upload_image", "reset_backends"]
