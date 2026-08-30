# -*- coding: utf-8 -*-
"""工具 description 参数的共享约定（下划线前缀 → 工具 loader 会跳过本文件）

背景：shell / 写入 / 编辑这类工具的折叠预览只能展示原始参数（命令、文件路径），
用户扫一眼消息列表看不出"这一步到底在做什么"。约定由大模型在调用时额外传一个
`description`：一句自然语言说明本次调用的意图，preview 与 summarize 优先展示它。

两种形态（见 `prefer_description` 的 `tail_fn`）：
- 替换式（bash / bg_start）：命令串对用户没信息量，description 直接替换。
- 拼接式（write / edit / multi_edit）：**文件路径必须留在折叠头上**，拼成
  "description (路径)"。用户明确要求过这一点——光看意图不知道改了哪个文件。

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


def prefer_description(preview_fn=None, tail_fn=None):
    """包装 preview 闭包：有 description 时优先展示它，否则回退原闭包的预览

    两种形态：
    - 默认（tail_fn=None）：description **替换**原预览。适用于原始参数对用户
      没有信息量的工具（bash 命令串）。
    - tail_fn 非空：拼成 "description (tail)"。适用于**原始参数本身仍要留着**
      的工具——典型是编辑类工具的文件路径：光看"抽离 token 估算函数"不知道改了
      哪个文件，路径必须留在折叠头上。

    Args:
        preview_fn: 原 preview 闭包 (tool_args) -> str，作为无 description 时的兜底
        tail_fn: (tool_args) -> str，从参数里提取要保留的事实信息（如相对路径）
    """

    def _wrapped(tool_args: dict) -> str:
        args = tool_args or {}
        desc = get_description(args)
        if not desc:
            if preview_fn is None:
                return ""
            return preview_fn(args) or ""
        if tail_fn is None:
            return desc
        tail = ""
        try:
            tail = tail_fn(args) or ""
        except Exception:
            tail = ""
        return f"{desc} ({tail})" if tail else desc

    return _wrapped
