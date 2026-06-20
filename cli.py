#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DriFox CLI — 命令行入口

使用方式：
    python cli.py --version
    python cli.py doctor
    python cli.py status
    python cli.py chat -z "帮我写个二分查找"
    python cli.py chat  # 交互模式
"""
import sys
import os

# 添加项目根到 path
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

from app.cli import main

if __name__ == "__main__":
    main()
