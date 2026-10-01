# -*- coding: utf-8 -*-
"""插件 SDK — 插件层唯一允许依赖的主程序共享面。

边界约定（检查工具 tools/plugin_import_guard.py，批 4 落地）：
- 插件只允许 import：app.plugins.contracts / app.plugins.registries /
  app.plugins.managers.plugin_config_store / app.plugins.sdk
- 批 1（本文件）：集中 re-export 插件所需的共享纯函数与常量。
- 批 2：把下列符号本体搬迁至 sdk，app/core 原位置留 re-export，
  反转依赖方向（sdk 不再 import app/core）。
- 批 3：服务型对象（ToolRegistry / TeamManager / SessionStore 等）经
  服务槽（get_service/register_service）暴露，禁止插件直接 import。
"""

from app.constants import PARAM_SCHEMA
from app.constants import provider_quota_exclude_keys
from app.core.conversation.message_content import append_text_block
from app.core.modelmeta.model_capabilities import get_model_capabilities, normalize_reasoning_effort
from app.core.modelmeta.provider_profile import get_provider_profile
from app.core.tools.tool_arg_lines import (
    LINE_ESTIMATE_STEP,
    build_progress_payload,
    extract_partial_path,
    should_emit_progress,
)
from app.utils.http_client import build_openai_client

__all__ = [
    "LINE_ESTIMATE_STEP",
    "PARAM_SCHEMA",
    "append_text_block",
    "build_openai_client",
    "build_progress_payload",
    "extract_partial_path",
    "get_model_capabilities",
    "get_provider_profile",
    "normalize_reasoning_effort",
    "provider_quota_exclude_keys",
    "should_emit_progress",
]
