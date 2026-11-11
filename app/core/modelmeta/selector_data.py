# -*- coding: utf-8 -*-
"""模型选择器数据源：只收 chat 实例，产出 (display_name, model_list, is_current) 行与两个反查映射。"""
from __future__ import annotations

import ast
from typing import Any, Dict, List, Tuple

from app.core.modelmeta.provider_save_plan import filter_disabled_models


def build_selector_provider_data(
    valid_configs: Dict[str, Dict[str, Any]],
    current_provider_name: str,
    merged_provider_models: Dict[str, List[str]] | None = None,
) -> Tuple[List[Tuple[str, List[str], bool]], Dict[str, str], Dict[str, str]]:
    """返回 (rows, display_to_config, display_to_provider_name)。
    display_to_config / display_to_provider_name 均以同一 display_name 口径为键
    （display_name → config_id / provider_name），两映射键集恒一致。
    """
    merged = merged_provider_models or {}
    rows: List[Tuple[str, List[str], bool]] = []
    display_to_config: Dict[str, str] = {}
    display_to_provider_name: Dict[str, str] = {}
    for config_id, config in (valid_configs or {}).items():
        # 生成类实例（图像/嵌入等）不进聊天模型选择器；字段缺失按 chat 兼容老配置
        if str(config.get("capability_type", "chat") or "chat") != "chat":
            continue
        display_name = config.get("display_name") or config.get("provider_name") or config_id
        pname = config.get("provider_name", config_id)
        display_to_config[display_name] = config_id
        display_to_provider_name[display_name] = pname

        model_list: List[str] = []
        if "模型列表" in config:
            saved_models = config["模型列表"]
            if isinstance(saved_models, str):
                # 兼容磁盘旧数据：字符串形态（ast 字面量）同样解析
                try:
                    saved_models = ast.literal_eval(saved_models)
                except Exception:
                    saved_models = []
            if isinstance(saved_models, list) and saved_models:
                model_list = list(saved_models)
            # 模型列表存在但为空（[]）→ 回退到 merged_provider_models，
            # 避免刚注入默认配置时模型选择器一片空白
            if not model_list and pname in merged:
                model_list = list(merged[pname])
        elif pname in merged:
            model_list = list(merged[pname])

        # 关闭的模型不在选择卡片显示（P1-9 OpenCode 风格清单；仍在「模型列表」）
        model_list = filter_disabled_models(model_list, config)
        cur_model = config.get("模型名称", "")
        if cur_model and cur_model not in model_list:
            model_list.insert(0, cur_model)
        if not model_list and cur_model:
            model_list = [cur_model]
        rows.append((display_name, model_list, config_id == current_provider_name))
    return rows, display_to_config, display_to_provider_name
