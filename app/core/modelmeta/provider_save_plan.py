# -*- coding: utf-8 -*-
"""服务商保存计划（纯函数，无 Qt 依赖）。

从 `ProviderEditCard._on_save` 抽出的三段可测逻辑：
1. 清空模型列表的确认判定（`confirm_clear`）；
2. 套餐用量额外字段的提取（`collect_extra_fields`）；
3. 保存 payload 的组装（`build_provider_save_plan`）。

抽出的动机：`_on_save` 原先把「取值 / 判空 / 组字典 / 弹窗 / 发信号」混在一个方法里，
其中「清空语义」与「显式管理键」的规则最容易写错（P0-6 的 `if val:` 门槛就是把清空
静默变成「保留旧值」）。规则集中在此处后可用无 Qt 单测锁住。

与 `apply_provider_save` 的配合（P0-6）：
- 本模块产出的 payload 里，**显式管理的键恒存在（含空串）**；
- 未显式管理的键**不出现**，由 `apply_provider_save` 的 merge 语义保留磁盘旧值。
两者合起来 = 「用户能看到的字段随用户意图，用户看不到的字段不动」。
"""

from typing import Any, Callable, Dict, Iterable, List, Tuple

# payload 中恒写入的表单键（名称保持历史键名，勿改：它们是磁盘配置的实际字段）
FORM_KEYS = ("API_URL", "API_KEY", "模型名称", "认证方式", "name")


def build_provider_save_plan(
    form_values: Dict[str, Any],
    old_info: Dict[str, Any],
    extra_fields: Dict[str, Any],
) -> Dict[str, Any]:
    """组装一次保存动作的执行计划。

    Args:
        form_values: 表单当前值，键为 ``api_url`` / ``api_key`` / ``model`` /
            ``auth_type`` / ``name`` / ``models``（models 为模型名列表）/
            ``auto_refresh`` / ``health_check``（bool，双开关；缺省视作 False）/
            ``fetch_meta``（可选，``{"ts": str, "status": str}`` 手动刷新结果）
        old_info: 编辑前的 provider_info（用于取 ``config_id`` 与 ``模型列表``）
        extra_fields: 套餐用量等额外字段的当前值（**含空串**；空串是显式清空）

    Returns:
        ``{"confirm_clear": bool, "payload": dict}``

        - ``confirm_clear``：用户清空了模型列表而旧列表非空 → 调用方需弹确认框。
        - ``payload``：交给 ``apply_provider_save`` 的 provider_info。五键恒存在
          （含空串 = 显式清空）；``config_id`` 仅在旧配置有时写入；``模型列表``
          恒写入（空列表同样是显式值）；**两个开关恒写 bool（含 False = 显式关）**；
          ``fetch_meta`` 携带时写「上次模型刷新」+「模型刷新状态」；
          ``extra_fields`` 覆盖同键。
    """
    models: List[str] = list(form_values.get("models") or [])
    old_models = old_info.get("模型列表") or []
    confirm_clear = (not models) and bool(old_models)

    payload: Dict[str, Any] = {
        "API_URL": str(form_values.get("api_url", "") or "").strip(),
        "API_KEY": str(form_values.get("api_key", "") or "").strip(),
        "模型名称": str(form_values.get("model", "") or "").strip(),
        "认证方式": str(form_values.get("auth_type", "") or "").strip(),
        "name": str(form_values.get("name", "") or "").strip(),
    }

    # 编辑场景下保留旧 config_id，让 apply_provider_save 能据此判断 (URL, KEY) 是否被改过
    existing_config_id = old_info.get("config_id", "")
    if existing_config_id:
        payload["config_id"] = existing_config_id

    # 模型列表恒写入：空列表 = 用户在确认框里确认过的清空意图
    payload["模型列表"] = models

    # 双开关恒写 bool（显式 False 同样要写入，否则关不掉）
    payload["自动刷新模型"] = bool(form_values.get("auto_refresh", False))
    payload["健康检查"] = bool(form_values.get("health_check", False))

    # 手动刷新的状态元数据（有则随保存落盘，供列表行状态点 / 上次刷新时间显示）
    fetch_meta = form_values.get("fetch_meta") or {}
    if isinstance(fetch_meta, dict) and fetch_meta:
        ts = str(fetch_meta.get("ts", "") or "")
        status = str(fetch_meta.get("status", "") or "")
        if ts:
            payload["上次模型刷新"] = ts
        if status:
            payload["模型刷新状态"] = status

    # 额外字段覆盖同键（空串同样覆盖 → 清空生效）
    payload.update(extra_fields)
    return {"confirm_clear": confirm_clear, "payload": payload}


def collect_extra_fields(
    provider_name: str,
    extra_field_rows: Iterable[Tuple[Tuple[str, str], Tuple[Any, str]]],
    get_editor: Callable[[str], Any],
) -> Dict[str, Any]:
    """提取当前服务商的套餐用量额外字段值。

    规则（P0-6 核心）：判定依据是「该字段有无编辑器」，与值无关。
    - 有编辑器 → 恒写入，值可为空串（用户能看到它 → 当前值就是用户意图）
    - 无编辑器（非当前服务商的字段行）→ 不写入，交给 ``apply_provider_save``
      的 merge 语义保留既有值

    Args:
        provider_name: 当前编辑的服务商名
        extra_field_rows: ``{(provider_name, config_key): (row_widget, edit_attr)}``
        get_editor: ``edit_attr -> 编辑器控件 | None``（通常传 ``lambda a: getattr(card, a, None)``）

    Returns:
        ``{config_key: 值}``，只含当前服务商的行
    """
    result: Dict[str, Any] = {}
    for (pname, config_key), (_row, edit_attr) in extra_field_rows:
        if pname != provider_name:
            continue
        editor = get_editor(edit_attr)
        if editor is not None:
            result[config_key] = editor.text().strip()
    return result
