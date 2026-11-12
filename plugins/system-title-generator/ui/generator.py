# -*- coding: utf-8 -*-
"""会话标题生成控制器：订阅 title.request，纯生成并提交。

宿主已判完「该不该生成」，本模块只负责生成什么标题。从
app/core/workers/topic_summary.py 迁入两套 prompt、JSON 提取、失败回退。
提交前用 session.read 比对 session_id 实现取消（切会话 / 关页丢弃结果）。
"""
from __future__ import annotations

import re
import threading
from typing import Any, Optional

import orjson as json
from loguru import logger
from PyQt5.QtCore import QObject, pyqtSignal

_BRIDGE: Optional["_SignalBridge"] = None


class _SignalBridge(QObject):
    """后台线程 → 主线程提交桥。"""
    resultReady = pyqtSignal(str, str, str)  # window_id, session_id, title


class _TitleController:
    def __init__(self, pool: Any):
        self._pool = pool
        self._inflight: set[str] = set()
        self._lock = threading.Lock()

    # ── 生命周期 ──
    def start(self) -> None:
        global _BRIDGE
        if _BRIDGE is None:
            _BRIDGE = _SignalBridge()
        # 无条件重连：同进程内 destroy→create 不重新 import 时，旧桥已断连但
        # 模块级 _BRIDGE 仍非 None，只判 None 会漏连导致结果无人接收。
        try:
            _BRIDGE.resultReady.disconnect(self._on_result)
        except (TypeError, RuntimeError):
            pass
        _BRIDGE.resultReady.connect(self._on_result)
        events = self._pool["ui.events"]()
        events["subscribe"]("title.request", self._on_title_request)

    def stop(self) -> None:
        with self._lock:
            self._inflight.clear()
        if _BRIDGE is not None:
            try:
                _BRIDGE.resultReady.disconnect(self._on_result)
            except (TypeError, RuntimeError):
                pass

    # ── 纯生成 ──
    @staticmethod
    def _build_context(messages: list) -> str:
        lines = []
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant"):
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    str(b.get("text", "")) for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            truncated = content[:300] + ("..." if len(content) > 300 else "")
            lines.append(f"{'用户' if msg.get('role') == 'user' else '助手'}: {truncated}")
        return "\n".join(lines)

    @classmethod
    def build_prompt(cls, messages: list, previous_summary: str = "") -> str:
        context = cls._build_context(messages)
        head = (
            "你是一个对话标题助手。\n最近的用户对话历史：\n"
            f"{context}\n\n根据对话生成标题。\n\n"
            "【标题要求】\n- 不超过15字，体现用户意图\n"
            '- 如："生成PPT"、"调试bug"、"咨询问题"\n\n'
        )
        if previous_summary:
            # 预留分支：当前宿主守卫 topic_summary 非空即不发请求，本分支不可达；
            # 未来放开「主题变更时更新标题」时无需再补 prompt。
            safe = previous_summary.replace("\\", "\\\\").replace('"', '\\"')
            return (
                head
                + "【标题更新原则】\n你不应该仅根据最新一条消息就生成新标题。而是应该：\n"
                "1. 如果已有标题反映的是本次对话的核心主题，即使最新消息是简单问候，也要保留原标题\n"
                "2. 只有当对话主题发生实质性转变时才更新标题\n"
                "3. 如果主题没有实质性转变，topic_summary 填入【之前的标题】原样返回\n\n"
                f"【之前的标题】\n{previous_summary}\n\n"
                "输出JSON（注意：topic_summary 必须是非空字符串，主题不变则填入原标题）：\n"
                f'```json\n{{\n  "topic_summary": "{safe}"\n}}\n```'
            )
        return (
            head
            + "输出JSON（注意：topic_summary 必须是非空字符串，请生成合适的标题）：\n"
            '```json\n{\n  "topic_summary": ""\n}\n```'
        )

    @staticmethod
    def extract_json(text: str) -> Optional[dict]:
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                pass
        try:
            stripped = text.strip()
            if stripped.startswith("{"):
                return json.loads(stripped)
        except Exception:
            pass
        return None

    # ── 事件与线程 ──
    def _on_title_request(self, payload: dict) -> None:
        window_id = str(payload.get("window_id") or "")
        session_id = str(payload.get("session_id") or "")
        if not window_id:
            return
        with self._lock:
            if window_id in self._inflight:
                return
            self._inflight.add(window_id)
        model_query = str(self._pool["plugin.config.read"]("title_model", "") or "")
        threading.Thread(
            target=self._generate,
            args=(
                window_id,
                session_id,
                list(payload.get("messages") or []),
                str(payload.get("fallback") or ""),
                model_query,
            ),
            daemon=True,
        ).start()

    def _generate(self, window_id: str, session_id: str, messages: list,
                  fallback: str, model_query: str) -> None:
        title = ""
        try:
            raw = self._pool["model.complete"](
                self.build_prompt(messages),
                model_query=model_query,
                temperature=0.1,
                max_tokens=1500,
            )
            parsed = self.extract_json(str(raw or "").strip())
            title = str((parsed or {}).get("topic_summary", "") or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[system-title-generator] 生成失败: {e}")
        title = " ".join((title or fallback).split())
        if not title:
            return
        self._emit(window_id, session_id, title)

    def _emit(self, window_id: str, session_id: str, title: str) -> None:
        """信号桥包一层：单测 monkeypatch 本方法即可断言生成结果。"""
        if _BRIDGE is not None:
            _BRIDGE.resultReady.emit(window_id, session_id, title)

    # ── 提交前复查 ──
    def _on_result(self, window_id: str, session_id: str, title: str) -> None:
        with self._lock:
            self._inflight.discard(window_id)
        try:
            # 定向到发起窗口：无参会解析到全局活跃窗口，多窗口下切窗后误杀结果。
            current = self._pool["session.read"](window_id) or {}
        except Exception:
            return
        if str(current.get("session_id") or "") != session_id:
            return
        self._pool["session.title.commit"](window_id, title)


_controller: Optional[_TitleController] = None


def create_controller() -> _TitleController:
    global _controller
    if _controller is None:
        from app.plugins.capabilities.resolve import pool_for

        pool = pool_for("system-title-generator", surface="ui")
        if pool is None:
            raise RuntimeError("[system-title-generator] 能力池不可用")
        _controller = _TitleController(pool)
    return _controller


def get_controller() -> Optional[_TitleController]:
    return _controller


def destroy_controller() -> None:
    global _controller
    if _controller is not None:
        _controller.stop()
        _controller = None
