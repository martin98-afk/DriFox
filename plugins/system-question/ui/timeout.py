# -*- coding: utf-8 -*-
"""system-question 超时控制器：按插件配置驱动提问卡超时策略。

职责边界
--------
宿主只提供 ``question.control`` 能力（订阅生命周期 / 读快照 / 代答 / 倒计时），
「多久算超时」「超时后跳过还是让模型代答」全部由本插件决定 —— 宿主不含任何
提问超时逻辑。

线程纪律
--------
- QTimer 只能在主线程创建/销毁（start/stop 由 register_ui/unload_ui 调用）；
- AI 代答的模型调用在后台线程执行，结果经 ``_aiResult`` 信号回主线程再提交答案；
- 订阅按窗口惰性建立（tick 每秒扫一次窗口表），窗口关闭即释放。

计时口径
--------
每题独立计时：``asked`` 与 ``page`` 事件都会重置当前窗口的截止时间，
与提问卡翻页语义一致。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from loguru import logger
from PyQt5.QtCore import QObject, QTimer, pyqtSignal

_TICK_MS = 1000
_CONFIG_PLUGIN = "system-question"


class _WindowSession:
    """单窗口的订阅与计时状态。"""

    def __init__(self, window_id: str, pool: Any):
        self.window_id = window_id
        self.pool = pool
        self.callback = None
        self.deadline: Optional[float] = None
        self.sec = 0
        self.action = "none"
        self.model_query = ""
        self.tool_call_id = ""


class QuestionTimeoutController(QObject):
    """提问超时控制器（模块级单例，见 get_controller/create_controller）。"""

    # (window_id, 答案文本)；答案文本为空表示 AI 代答失败（调用方按跳过处理）
    _aiResult = pyqtSignal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self._timer: Optional[QTimer] = None
        self._sessions: Dict[str, _WindowSession] = {}
        self._ai_pending: set = set()
        self._aiResult.connect(self._on_ai_result)

    # ── 生命周期（仅主线程） ──

    def start(self) -> None:
        if self._timer is not None:
            return
        self._timer = QTimer()
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()
        self._ensure_subscriptions()
        logger.info("[system-question] 超时控制器已启动（tick=1000ms）")

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        for session in list(self._sessions.values()):
            self._release_session(session)
        self._sessions.clear()
        self._ai_pending.clear()

    # ── 窗口订阅 ──

    def _windows(self) -> Dict[str, Any]:
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            registry = UIPluginRegistry.get_instance()
            if registry is None:
                return {}
            return dict(getattr(registry, "_window_main_widgets", {}) or {})
        except Exception as e:  # noqa: BLE001 - 窗口表不可用时跳过本轮
            logger.debug(f"[system-question] 窗口表不可用: {e}")
            return {}

    def _ensure_subscriptions(self) -> None:
        windows = self._windows()
        for window_id in list(self._sessions):
            if window_id not in windows:
                self._release_session(self._sessions.pop(window_id))
        for window_id, widget in windows.items():
            window_id = str(window_id)
            if window_id in self._sessions:
                continue
            # 全局卡片宿主（__global__ 容器等）不是对话窗口，没有提问控制能力。
            # 直接订阅会在 provider 抛 RuntimeError，且订阅失败不登记 session，
            # 于是下一秒重复尝试、WARNING 每秒刷屏。按能力方法预检，跳过这类宿主。
            # 析构中的 PyQt 包装对象 hasattr 可能抛 RuntimeError，一并吞掉跳过。
            try:
                capable = hasattr(widget, "question_control_subscribe")
            except Exception:  # noqa: BLE001 - 窗口析构中跳过本轮
                continue
            if not capable:
                continue
            pool = self._build_pool(widget)
            if pool is None or "question.control" not in pool:
                continue
            session = _WindowSession(window_id, pool)

            def _callback(event: str, payload: dict, _sid: str = window_id) -> None:
                self._on_question_event(_sid, event, payload)

            try:
                api = pool["question.control"]
                if not api["subscribe"](_callback):
                    continue
            except Exception as e:  # noqa: BLE001 - 单窗口订阅失败不影响其余
                logger.warning(f"[system-question] 订阅提问事件失败（{window_id}）: {e}")
                continue
            session.callback = _callback
            self._sessions[window_id] = session
            logger.debug(f"[system-question] 已订阅窗口 {window_id} 的提问事件")

    @staticmethod
    def _build_pool(widget: Any) -> Any:
        try:
            from app.plugins.sdk import pool_for

            return pool_for(_CONFIG_PLUGIN, surface="ui", host=widget)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[system-question] 能力池构建失败: {e}")
            return None

    def _release_session(self, session: Optional[_WindowSession]) -> None:
        if session is None:
            return
        try:
            api = session.pool["question.control"]
            api["stop_countdown"]()
            if session.callback is not None:
                api["unsubscribe"](session.callback)
        except Exception:  # noqa: BLE001 - 窗口销毁中静默
            pass
        session.deadline = None
        session.callback = None

    # ── 配置 ──

    @staticmethod
    def _read_policy() -> Dict[str, Any]:
        """读本插件自己的配置（action / sec / model）。"""
        try:
            from app.plugins.managers.plugin_config_store import PluginConfigStore

            store = PluginConfigStore()
            action = str(store.get(_CONFIG_PLUGIN, "question_timeout_action") or "none").strip()
            raw_sec = store.get(_CONFIG_PLUGIN, "question_timeout_sec")
            try:
                sec = int(float(raw_sec))
            except (TypeError, ValueError):
                sec = 120
            model = str(store.get(_CONFIG_PLUGIN, "question_timeout_model") or "").strip()
        except Exception as e:  # noqa: BLE001 - 配置读取失败按不超时处理
            logger.debug(f"[system-question] 配置读取失败，按不超时处理: {e}")
            return {"action": "none", "sec": 120, "model": ""}
        if action not in ("none", "skip", "ai"):
            action = "none"
        sec = min(max(sec, 10), 3600)
        return {"action": action, "sec": sec, "model": model}

    # ── 事件 ──

    def _on_question_event(self, window_id: str, event: str, payload: dict) -> None:
        session = self._sessions.get(window_id)
        if session is None:
            return
        if event == "asked":
            policy = self._read_policy()
            session.action = policy["action"]
            session.sec = policy["sec"]
            session.model_query = policy["model"]
            session.tool_call_id = str(payload.get("tool_call_id") or "")
            if session.action == "none":
                session.deadline = None
                self._safe_api(session, "stop_countdown")
                return
            self._arm(session)
            return
        if event == "page":
            # 每题独立计时：翻页重置
            if session.action != "none":
                self._arm(session)
            return
        if event == "closed":
            session.deadline = None
            session.tool_call_id = ""
            session.action = "none"  # 下一题的计时策略由新的 asked 事件重定
            self._safe_api(session, "stop_countdown")

    def _arm(self, session: _WindowSession) -> None:
        session.deadline = time.monotonic() + float(session.sec)
        self._safe_api(session, "countdown", session.sec)

    def _on_tick(self) -> None:
        self._ensure_subscriptions()
        now = time.monotonic()
        for session in list(self._sessions.values()):
            if session.deadline is None or now < session.deadline:
                continue
            session.deadline = None  # 先摘掉，防同一窗口重复触发
            self._fire_timeout(session)

    # ── 超时动作 ──

    def _fire_timeout(self, session: _WindowSession) -> None:
        try:
            api = session.pool["question.control"]
            snap = api["snapshot"]() or {}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[system-question] 读取提问快照失败（{session.window_id}）: {e}")
            return
        if not snap.get("tool_call_id"):
            return  # 问题已被用户处理
        if session.action == "ai":
            if session.window_id in self._ai_pending:
                return
            self._ai_pending.add(session.window_id)
            threading.Thread(
                target=self._run_ai_answer,
                args=(session.window_id, snap, session.model_query, session.sec, session.pool),
                daemon=True,
            ).start()
            return
        # skip（含未知 action）
        self._submit(session, self._build_skip_text(snap, session.sec))

    def _submit(self, session: _WindowSession, text: str) -> None:
        try:
            session.pool["question.control"]["answer"](text)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[system-question] 提交超时答案失败（{session.window_id}）: {e}")
            return
        session.tool_call_id = ""

    @staticmethod
    def _build_skip_text(snap: dict, sec: int) -> str:
        questions = snap.get("questions") or []
        answers = snap.get("answers") or {}
        parts = []
        for index, q in enumerate(questions):
            q_text = q.get("question", f"问题{index + 1}") if isinstance(q, dict) else f"问题{index + 1}"
            answered = answers.get(index)
            if answered:
                parts.append(f"问题「{q_text}」的回答：\n{answered}")
            else:
                parts.append(f"问题「{q_text}」的回答：\n（用户未在 {sec} 秒内回答，已跳过）")
        return "\n---\n".join(parts)

    # ── AI 代答 ──

    def _run_ai_answer(self, window_id: str, snap: dict, model_query: str, sec: int, pool: Any) -> None:
        text = ""
        try:
            text = self._ask_model(snap, model_query, pool)
        except Exception as e:  # noqa: BLE001 - 代答失败降级跳过
            logger.warning(f"[system-question] AI 代答失败（{window_id}）: {e}")
            text = ""
        self._aiResult.emit(window_id, text)

    def _on_ai_result(self, window_id: str, text: str) -> None:
        self._ai_pending.discard(window_id)
        session = self._sessions.get(window_id)
        if session is None or not session.tool_call_id:
            return  # 窗口已关 / 用户已答 / 已被其它路径收尾
        try:
            snap = session.pool["question.control"]["snapshot"]() or {}
        except Exception:  # noqa: BLE001
            return
        if not snap.get("tool_call_id"):
            return
        if not text:
            # 代答失败 → 退回跳过语义，保证主模型不会无限等
            self._submit(session, self._build_skip_text(snap, session.sec))
            return
        self._submit(session, self._build_ai_text(snap, text, session.sec))

    @staticmethod
    def _build_context_block(pool: Any) -> str:
        """会话上下文：最近一条用户消息（用户目标）+ 最近几条消息摘要。

        经 session.read 能力取（需 manifest 声明）；任何失败静默返回空串，
        prompt 退回无上下文形态——上下文是代答质量的增强项，不得阻断代答链。
        """
        try:
            if not (isinstance(pool, dict) or hasattr(pool, "get")) or "session.read" not in pool:
                return ""
            read = pool["session.read"]
            if not callable(read):
                return ""
            data = read() or {}
            msgs = [m for m in (data.get("messages") or []) if isinstance(m, dict)]

            def _text(m: dict) -> str:
                c = m.get("content")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    return " ".join(
                        str(b.get("text") or b.get("content") or "")
                        for b in c if isinstance(b, dict)
                    ).strip()
                return ""

            lines = []
            goal = ""
            for m in reversed(msgs):
                if m.get("role") == "user":
                    goal = _text(m).strip()
                    break
            if goal:
                lines.append("用户目标：" + goal[:500])
            parts = []
            for m in msgs[-6:]:
                role = "用户" if m.get("role") == "user" else "助手"
                t = _text(m).strip()[:80]
                if t:
                    parts.append(f"{role}：{t}")
            if parts:
                lines.append("最近对话：\n" + "\n".join(parts))
            if not lines:
                return ""
            return "任务上下文（据此判断用户最可能的意图）：\n" + "\n".join(lines) + "\n\n"
        except Exception:  # noqa: BLE001 - 上下文失败不阻断代答
            return ""

    def _ask_model(self, snap: dict, model_query: str, pool: Any) -> str:
        """调模型为未答问题作答，返回模型原始输出（JSON 文本）。"""
        from app.plugins.sdk import build_openai_client, chat_completion_text

        cfg = self._resolve_model_config(model_query, pool)
        if not cfg:
            return ""
        questions = snap.get("questions") or []
        answers = snap.get("answers") or {}
        lines = []
        for index, q in enumerate(questions):
            if index in answers:
                continue
            text = str(q.get("question") or "") if isinstance(q, dict) else str(q)
            labels = []
            for opt in (q.get("options") or []) if isinstance(q, dict) else []:
                if isinstance(opt, dict) and opt.get("label"):
                    labels.append(str(opt["label"]))
                elif isinstance(opt, str):
                    labels.append(opt)
            line = f"{index}. {text}"
            if labels:
                line += f"（可选：{' / '.join(labels[:8])}）"
            lines.append(line)
        if not lines:
            return ""

        prompt = (
            "你是桌面 AI 助手的代理。主 AI 向用户提了问题，用户超时未回答，请代用户给出最合理的回答。\n\n"
            + self._build_context_block(pool)
            + "待回答问题（index 为题目序号）：\n" + "\n".join(lines) + "\n\n"
            "输出格式（程序解析，必须严格遵守）：\n"
            '你的回复第一个字符必须是 {，最后一个字符必须是 }。\n'
            '格式：{"answers": [{"index": 0, "answer": "回答内容"}]}\n'
            "有选项时优先输出选项 label；answer 不超过 100 字；任何解释性文字都会导致解析失败。"
        )

        client = build_openai_client(
            api_key=cfg.get("API_KEY", ""),
            base_url=cfg.get("API_URL"),
            timeout=60,
        )
        return chat_completion_text(
            client,
            model=cfg.get("模型名称", "gpt-4o"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=2000,
        )

    def _resolve_model_config(self, model_query: str, pool: Any = None) -> dict:
        """配置指定了模型就走 model.config_for；否则跟随当前会话模型。"""
        if pool is None:
            for session in self._sessions.values():
                pool = session.pool
                break
        if pool is None:
            return {}
        try:
            if model_query and "model.config_for" in pool:
                resolved = pool["model.config_for"](model_query)
                if resolved:
                    return dict(resolved)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[system-question] 指定模型解析失败 {model_query!r}: {e}")
        try:
            if "model.config" in pool:
                return dict(pool["model.config"]() or {})
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[system-question] 当前模型配置读取失败: {e}")
        return {}

    @staticmethod
    def _build_ai_text(snap: dict, raw: str, sec: int = 0) -> str:
        """把模型输出的 answers JSON 渲染成提问卡提交格式。"""
        import json

        questions = snap.get("questions") or []
        answers = snap.get("answers") or {}
        ai_answers: Dict[int, str] = {}
        try:
            data = json.loads(raw.strip()[raw.strip().find("{"): raw.strip().rfind("}") + 1])
            for item in data.get("answers") or []:
                if not isinstance(item, dict):
                    continue
                try:
                    index = int(item.get("index"))
                except (TypeError, ValueError):
                    continue
                answer = str(item.get("answer") or "").strip()
                if answer:
                    ai_answers[index] = answer
        except Exception:  # noqa: BLE001 - 解析失败按全跳过处理
            ai_answers = {}

        parts: List[str] = []
        for index, q in enumerate(questions):
            q_text = q.get("question", f"问题{index + 1}") if isinstance(q, dict) else f"问题{index + 1}"
            answered = answers.get(index)
            if answered:
                parts.append(f"问题「{q_text}」的回答：\n{answered}")
            elif index in ai_answers:
                parts.append(f"问题「{q_text}」的回答：\n[AI 代答] {ai_answers[index]}")
            else:
                parts.append(f"问题「{q_text}」的回答：\n（用户未在 {sec} 秒内回答，已跳过）")
        return "\n---\n".join(parts)

    @staticmethod
    def _safe_api(session: _WindowSession, name: str, *args) -> None:
        try:
            api = session.pool["question.control"]
            api[name](*args)
        except Exception:  # noqa: BLE001
            pass


# ── 模块级单例（ui/__init__.py 管生命周期） ──

_controller: Optional[QuestionTimeoutController] = None


def get_controller() -> Optional[QuestionTimeoutController]:
    return _controller


def create_controller() -> QuestionTimeoutController:
    global _controller
    if _controller is None:
        _controller = QuestionTimeoutController()
    return _controller


def destroy_controller() -> None:
    global _controller
    if _controller is not None:
        _controller.stop()
        _controller = None
