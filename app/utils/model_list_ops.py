# -*- coding: utf-8 -*-
"""模型列表纯函数操作。

服务商配置里与模型列表相关的三个键：
    "模型列表"  : list[str]  全部已知模型 id（不变的既有键）
    "模型隐藏"  : list[str]  "模型列表" 的子集，被用户隐藏的 id
    "模型别名"  : dict        id -> 显示名

这些函数只做数据变换，不碰 Qt，供 UI 与主程序共用。
"""

from __future__ import annotations


def _norm(value: str) -> str:
    """归一化比较键：去空白 + 小写。"""
    return str(value or "").strip().lower()


def merge_fetched(current: list[str], fetched: list[str]) -> list[str]:
    """增量合并拉取结果：保留 current 原样，仅追加 fetched 中不存在的新 id。

    拉取不再覆盖用户手改的内容（旧实现直接 clear + addItems）。
    大小写不敏感去重，沿用 models.dev 的合并口径。
    返回新列表，不改入参。
    """
    result: list[str] = []
    seen: set[str] = set()
    for item in current or []:
        text = str(item or "").strip()
        if not text:
            continue
        key = _norm(text)
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    for item in fetched or []:
        text = str(item or "").strip()
        if not text:
            continue
        key = _norm(text)
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def filter_visible(models: list[str], hidden: list[str] | None) -> list[str]:
    """从 models 中剔除 hidden 里的 id（大小写不敏感）。"""
    hidden_keys = {_norm(h) for h in (hidden or []) if _norm(h)}
    if not hidden_keys:
        return list(models or [])
    return [m for m in (models or []) if _norm(m) not in hidden_keys]


def display_name(model_id: str, aliases: dict | None) -> str:
    """返回模型显示名：别名优先，无别名（或别名为空串）返回 id 本身。"""
    if not aliases:
        return model_id
    alias = aliases.get(model_id)
    if alias is None:
        return model_id
    text = str(alias).strip()
    return text or model_id


def normalize_hidden(models: list[str], hidden: list[str] | None) -> list[str]:
    """把 hidden 收敛为 models 的子集：剔除幽灵条目、去重、按 models 顺序输出。

    幽灵条目 = 隐藏集中已不在模型列表里的 id（用户手工删过该模型）。
    按 models 顺序输出保证结果稳定，便于比较与落盘。
    """
    models_list = [str(m or "").strip() for m in (models or [])]
    models_list = [m for m in models_list if m]
    model_keys = {_norm(m) for m in models_list}
    hidden_keys = {_norm(h) for h in (hidden or []) if _norm(h)}
    keep = hidden_keys & model_keys
    return [m for m in models_list if _norm(m) in keep]
