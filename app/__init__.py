# -*- coding: utf-8 -*-

# 🛡️ 全局 QThread 安全守卫：在一切 QThread 创建前安装
from app.utils.thread_guard import install_guard

install_guard()
