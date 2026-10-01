# -*- coding: utf-8 -*-
"""工具执行运行时 — 执行器、调用解析、权限管控、结果落盘、MCP/LSP 安全。

与 app/tools/（工具注册框架：registry/loader/schema）的分工：
  app/tools      工具「是什么」（注册、schema、别名、分组）
  app/core/tools 工具「怎么跑」（执行、流式解析、权限、落盘、安全护栏）
"""
