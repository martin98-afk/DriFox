# -*- coding: utf-8 -*-
"""外部存储 / 网络投递（Gitee 上传、EdgeOne 静态页部署）。

原 ``app/gateway/utils``，与 IM 无关（被分享卡、同步、附件回退共用），
故从 gateway 契约包里移出。
"""
from app.core.net.gitee_uploader import (
    GiteeUploader,
    get_gitee_uploader,
    upload_to_gitee,
)

__all__ = [
    "GiteeUploader",
    "get_gitee_uploader",
    "upload_to_gitee",
]
