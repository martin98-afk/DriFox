# -*- coding: utf-8 -*-
"""工具 description 参数的共享约定（下划线前缀 → 工具 loader 会跳过本文件）

背景：shell / 写入 / 编辑这类工具的折叠预览只能展示原始参数（命令、文件路径），
用户扫一眼消息列表看不出"这一步到底在做什么"。约定由大模型在调用时额外传一个
`description`：一句自然语言说明本次调用的意图，preview 与 summarize 优先展示它，
原始参数退到展开后的详情里（bash 的终端头、write/edit 的 diff 仍保留事实原文）。

用法（各工具插件）：

    _WRITE_SCHEMA = {..., "properties": {..., **description_param("新增 token 缓存")}}
    # 并把 "description" 加进 required
    registry.register(..., preview=prefer_description(_make_file_preview("write")))

加载方式与同目录 `_path_utils.py` 一致：消费方用 importlib 从插件目录读取本文件，
加载失败时回退到原预览（绝不因共享模块缺失而拖垮渲染）。
"""

DESCRIPTION_KEY = "description"


def description_param(example: str) -> dict:
    """生成 description 参数的 schema 片段（展开进 parameters.properties）

    Args:
        example: 给大模型的示例描述，用于校准粒度（应是"做什么"而非"怎么做"）
    """
    return {
        DESCRIPTION_KEY: {
            "type": "string",
            "description": (
                "必填。一句话自然语言描述这次调用要做什么（展示给用户看，替代原始参数），"
                f"例如 {example}。写意图，不要复述参数本身。"
            ),
        }
    }


def get_description(tool_args) -> str:
    """取出大模型填写的自然语言描述（缺失或非法时返回空串）"""
    if not isinstance(tool_args, dict):
        return ""
    return str(tool_args.get(DESCRIPTION_KEY) or "").strip()


def prefer_description(preview_fn=None):
    """包装 preview 闭包：有 description 时直接展示它，否则回退原闭包的预览

    Args:
        preview_fn: 原 preview 闭包 (tool_args) -> str，作为无 description 时的兜底
    """

    def _wrapped(tool_args: dict) -> str:
        desc = get_description(tool_args)
        if desc:
            return desc
        if preview_fn is None:
            return ""
        return preview_fn(tool_args or {}) or ""

    return _wrapped
