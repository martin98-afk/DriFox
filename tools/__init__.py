# -*- coding: utf-8 -*-
"""tools 包标记（历史为散脚本目录；现为可 import 包，供 tools.ui_driver 等子包挂载）。

打包边界：tools/ 仅开发与测试使用，PyInstaller 入口（main.py → app/...）不引用
本包，不进产物（build.py 的 collect_submodules 只收 app.core，--add-data 不含 tools/）。
"""
