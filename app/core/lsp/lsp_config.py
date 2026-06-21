# -*- coding: utf-8 -*-
"""
LSP 配置模型 — 解析 .lsp.json 文件

格式与 Claude Code 兼容，字段说明：
  command:              LSP 服务器二进制名（必须在 PATH）
  args:                 启动参数列表
  extensionToLanguage:  文件扩展名 → LSP 语言标识映射
  initializationOptions: 初始化选项
  settings:             服务器设置
  startupTimeout:       启动超时（毫秒）
  maxRestarts:          崩溃最大重启次数
  env:                  环境变量
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


@dataclass
class LspServerConfig:
    """单个 LSP 服务器的配置"""
    name: str                           # "pyright", "clangd" 等
    command: str                        # 二进制名或路径
    args: List[str] = field(default_factory=list)
    extension_to_language: Dict[str, str] = field(default_factory=dict)  # {".py": "python"}
    initialization_options: Dict[str, Any] = field(default_factory=dict)
    settings: Dict[str, Any] = field(default_factory=dict)
    env: Dict[str, str] = field(default_factory=dict)
    startup_timeout: int = 10000        # 毫秒
    max_restarts: int = 3
    transport: str = "stdio"            # stdio | socket
    plugin_name: str = ""               # 来源插件名
    install_hint: str = ""              # 安装提示命令，如 "npm install -g pyright"
    # ── 高级字段：诊断触发（可选） ──
    trigger_diagnostics: Optional[Dict[str, Any]] = None
    # 通过 .lsp.json 的 ``triggerDiagnostics`` 声明如何主动触发诊断
    # （如 typescript-language-server 需要 workspace/executeCommand + geterr）
    cli_fallback: Optional[Dict[str, Any]] = None
    # 通过 .lsp.json 的 ``cliFallback`` 声明 LSP 失败时的 CLI 兜底
    # 字段：command、argsTemplate（支持 ${file}）、parser（tsc/pyright/raw）

    @classmethod
    def from_dict(cls, name: str, data: Dict[str, Any], plugin_name: str = "") -> "LspServerConfig":
        """从 .lsp.json 的一个 key 解析配置

        Args:
            name: 服务器名（json key）
            data: 服务器配置 dict
            plugin_name: 来源插件名
        """
        # 扩展名映射处理：.lsp.json 的 key 是字符串，需要确保带点
        ext_map_raw = data.get("extensionToLanguage", {})
        ext_map: Dict[str, str] = {}
        for k, v in ext_map_raw.items():
            key = k if k.startswith(".") else f".{k}"
            ext_map[key.lower()] = v

        return cls(
            name=name,
            command=data.get("command", name),
            args=data.get("args", []),
            extension_to_language=ext_map,
            initialization_options=data.get("initializationOptions", {}),
            settings=data.get("settings", {}),
            env=data.get("env", {}),
            startup_timeout=data.get("startupTimeout", 10000),
            max_restarts=data.get("maxRestarts", 3),
            transport=data.get("transport", "stdio"),
            plugin_name=plugin_name,
            install_hint=data.get("installHint", ""),
            trigger_diagnostics=data.get("triggerDiagnostics"),
            cli_fallback=data.get("cliFallback"),
        )


def load_lsp_configs(plugin_dir: Path) -> Dict[str, LspServerConfig]:
    """从插件目录加载 .lsp.json

    Args:
        plugin_dir: 插件根目录

    Returns:
        {server_name: LspServerConfig}，文件不存在则返回空
    """
    lsp_file = plugin_dir / ".lsp.json"
    if not lsp_file.exists():
        return {}

    try:
        with open(lsp_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        configs: Dict[str, LspServerConfig] = {}
        for server_name, server_data in data.items():
            configs[server_name] = LspServerConfig.from_dict(
                name=server_name, data=server_data, plugin_name=plugin_dir.name
            )

        logger.debug(f"[LSP] 从 {plugin_dir.name} 加载了 {len(configs)} 个 LSP 服务器")
        return configs

    except Exception as e:
        logger.error(f"[LSP] 解析 {lsp_file} 失败: {e}")
        return {}
