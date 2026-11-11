# -*- coding: utf-8 -*-
"""运行面板的历史数据提供者（会话重载路径）

会话重载会把消息批次重渲成新卡，此时插件桥接层（``SubAgentRunBridge``）里没有
任何运行态：任务结束即出表，历史卡因此看不到子智能体行。运行面板要回看历史，
只能从落库的 tool 消息侧重建——形如成果栏（``ToolResult.artifacts`` 随消息落库，
``render_batch_to_assistant_card`` 灌回卡片）。

本模块即那个转换层：主程序按 ``CardInlinePanelInfo.history_loader`` 调用
``load_history_items(batch, host) -> items``，主程序不含任何子智能体业务。

数据链：``subagent_para`` 的 tool 结果 JSON 里带 ``task_ids`` → 分发给面板
``restore_items`` → 面板逐行 ``show_completed_task``。任务详情优先取插件表
（跨重启仍在），缺失时回退内存归档（同进程刚跑完、DB 未落的情况）。
"""

import json
import re
from typing import Any, Dict, List, Optional

from loguru import logger

# subagent_para 结果里的任务 id 形态：uuid4（含历史 f"agent_xxxxxxxx" 形式）
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
_AGENT_ID_RE = re.compile(r"\bagent_[0-9a-f]{8,}\b", re.IGNORECASE)

_SUBAGENT_TOOL_NAMES = frozenset({"subagent_para", "task", "taskbatch", "batch", "subagents-para", "subagent-para"})

def _is_subagent_tool(msg: Dict[str, Any]) -> bool:
    """该 tool 消息是否由子智能体派发工具产生（含历史别名）。"""
    try:
        from app.tools.registry import ToolRegistry
        from app.tools.tool_name_mapper import ToolNameMapper

        native = ToolNameMapper.to_native(str(msg.get("name") or ""))
        reg = ToolRegistry.get_instance().get(native)
        if reg is not None and reg.metadata and reg.metadata.get("subagent_task"):
            return True
    except Exception:  # noqa: BLE001 - 注册表不可用时回退名字白名单
        pass
    return str(msg.get("name") or "").lower() in _SUBAGENT_TOOL_NAMES

def _extract_task_ids(msg: Dict[str, Any]) -> List[str]:
    """从该轮 tool 结果里取 task_ids（JSON/repr 优先，退化到全文 id 扫描）。

    落库形态说明：``ToolResult`` 的消息 content 经 ``str(result)`` 落库，是
    ``{'task_ids': [...]}`` 这种 Python repr（单引号），``json.loads`` 解析不了，
    故要补 ``ast.literal_eval``；两者都失败（结果被截断/包了说明文字）时再全文扫 id。
    """
    content = msg.get("content")
    if isinstance(content, (dict, list)):
        data: Any = content
    else:
        text = str(content or "")
        if not text:
            return []
        data = None
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            try:
                import ast

                data = ast.literal_eval(text)
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                data = None
        if data is None:
            found = _UUID_RE.findall(text) + _AGENT_ID_RE.findall(text)
            return list(dict.fromkeys(found))

    ids: List[str] = []
    if isinstance(data, dict):
        raw = data.get("task_ids")
        if isinstance(raw, list):
            ids.extend(str(x) for x in raw if x)
        elif isinstance(raw, str) and raw.strip():
            ids.append(raw.strip())
    elif isinstance(data, list):
        ids.extend(str(x) for x in data if isinstance(x, str) and x)
    return list(dict.fromkeys(ids))

def _manager_for(host: Any) -> Optional[Any]:
    """取该窗口管理器（插件宿主未就绪返回 None）。

    host 优先（卡片所属窗口，多窗口隔离）；缺省退回活跃窗口——历史批次渲染时
    卡片可能还没挂进主窗口的父链，沿父链找不到宿主。
    """
    window_id = str(getattr(host, "_window_id", "") or "") if host is not None else ""
    if not window_id:
        try:
            from app.plugins.capabilities.resolve import active_main_widget

            mw = active_main_widget()
            window_id = str(getattr(mw, "_window_id", "") or "") if mw is not None else ""
        except Exception:  # noqa: BLE001 - 窗口解析不可用按无宿主
            window_id = ""
    if not window_id:
        return None
    try:
        from app.plugins.registries.window_services import get_window_services

        facade = get_window_services().get("subagent", window_id)
    except Exception as e:  # noqa: BLE001 - 服务表不可用按无数据
        logger.debug(f"[system-subagent] 历史回填取服务失败: {e}")
        return None
    return getattr(facade, "manager", None) if facade is not None else None

def _task_item(manager: Any, task_id: str) -> Optional[Dict[str, Any]]:
    """单个任务 → 面板行数据；查不到返回 None。"""
    data: Dict[str, Any] = {}
    try:
        data = manager.get_task_logs(task_id) or {}
    except Exception as e:  # noqa: BLE001 - 查询失败按无数据处理
        logger.debug(f"[system-subagent] 历史回填查询失败 {task_id}: {e}")
    if not data.get("found"):
        return None
    summary = dict(data.get("summary") or {})
    status = str(data.get("status") or summary.get("status") or "")
    error = str(summary.get("error") or data.get("error") or "")
    # 运行中/未知状态不补行：运行态由桥接层投影，历史路径只管已完成
    if status in ("running", "unknown", ""):
        return None
    return {
        "task_id": task_id,
        "agent_name": str(summary.get("agent_name") or data.get("agent_name") or ""),
        "description": str(summary.get("task_description") or data.get("task_description") or ""),
        "model_name": str(summary.get("model_name") or ""),
        "tool_call_count": int(summary.get("tool_call_count") or 0),
        "elapsed_seconds": int(summary.get("elapsed_seconds") or 0),
        "context_usage": str(summary.get("context_usage") or ""),
        "success": not error,
    }

def load_history_items(batch: Any, host: Any) -> List[Dict[str, Any]]:
    """卡片的 ``history_loader``：把该批次里派发的子智能体任务整理成面板行数据。

    Args:
        batch: 该 assistant 卡对应的消息批次（含 tool 消息）
        host: 宿主窗口（可取 window_id 反查窗口服务）

    Returns:
        ``restore_items`` 入参；空列表 = 本卡无历史任务（宿主不会创建空面板）。
    """
    if not batch or not isinstance(batch, (list, tuple)):
        return []
    task_ids: List[str] = []
    for msg in batch:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        if not _is_subagent_tool(msg):
            continue
        task_ids.extend(_extract_task_ids(msg))
    if not task_ids:
        return []
    manager = _manager_for(host)
    if manager is None:
        return []
    items: List[Dict[str, Any]] = []
    for task_id in dict.fromkeys(task_ids):
        item = _task_item(manager, task_id)
        if item is not None:
            items.append(item)
    return items
