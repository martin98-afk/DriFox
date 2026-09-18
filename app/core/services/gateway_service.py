# -*- coding: utf-8 -*-
"""
GatewayService — 应用级 Gateway 服务（一个应用一个实例）

历史问题：Gateway 逻辑寄生在 ChatBackend（每个 ChatWindow/tab 一个）上——
PlatformManager 单例闭包绑定创建时那个 backend，GatewayEngine 也是它的全局
单例。关掉"第一个 tab"：引擎被 cleanup（is_active=False）、消息仍投给已关闭
backend → 永远报 "engine unavailable (window closed)"。

修复：Gateway 全部状态/生命周期上移到本服务（QObject 应用级单例）：
- 平台适配器（PlatformManager 单例）闭包绑定本服务实例
- GatewayEngine 依赖（get_model_config / tool_executor / agent_manager /
  session_store）全部为全局组件，不持任何窗口引用
- tab 开关完全不影响 Gateway 收发

模型配置：读全局设置（llm_selected_model / llm_saved_providers），
与窗口级模型选择解耦；会话级 /model 覆盖仍生效（存于 Gateway 会话 metadata）。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time as time_module
from typing import Any, Dict, Optional

from loguru import logger
from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from app.core.backend import _extract_markdown_images, _gw_str_platform

_AUTO_RESTART_COOLDOWN = 5.0  # 引擎不可用时的自愈重建最小间隔（秒）


class GatewayService(QObject):
    """Gateway 应用级服务（单例，主线程创建）"""

    _instance: Optional["GatewayService"] = None

    # 平台线程 → 主线程（与原 backend 相同的信号机制）
    gateway_input_received = pyqtSignal(object)  # dict: {text, chat_id, user_id, platform, future}

    @classmethod
    def get_instance(cls) -> "GatewayService":
        """获取全局单例（须在主线程首次调用；重复调用线程安全）"""
        if cls._instance is None:
            svc = cls()
            cls._instance = svc
        return cls._instance

    def __init__(self, parent=None):
        if GatewayService._instance is not None:
            raise RuntimeError("GatewayService is singleton, use GatewayService.get_instance()")
        super().__init__(parent)

        self._manager = None  # PlatformManager 单例
        self._engine = None  # GatewayEngine
        self._tool_executor = None
        self._agent_manager = None
        self._initialized = False
        self._last_selfheal_ts = 0.0
        # [PERF T34] 后台预热线程与主线程消息路径的 _ensure_components 竞争保护。
        # 不用 RLock：构造段在锁内是纯 CPU/import，无需重入。
        self._components_lock = threading.Lock()

        self.gateway_input_received.connect(self._on_gateway_input)
        # 创建/停止均由 TabManagerWindow（应用生命周期容器）驱动：
        # __init__ → ensure_started()；cleanup() → stop()

        logger.info("[GatewayService] 已创建（应用级单例）")

    # ==================== 依赖（全局，无窗口引用） ====================

    @staticmethod
    def _get_model_config() -> Dict[str, Any]:
        """全局模型配置（读 Settings，不依赖任何窗口/tab 的选择）"""
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        current = cfg.llm_selected_model.value or ""
        saved = cfg.llm_saved_providers.value or {}
        if current and current in saved:
            config = dict(saved[current])
            config.pop("备注", None)
            config.pop("获取地址", None)
            config.pop("模型列表", None)
            return config
        # 兜底：取第一个可用配置
        for _cid, info in saved.items():
            config = dict(info)
            config.pop("备注", None)
            config.pop("获取地址", None)
            config.pop("模型列表", None)
            return config
        return {}

    def _ensure_components(self) -> bool:
        """确保 gateway 专属 tool_executor / agent_manager 就绪（幂等，双检锁）

        [PERF T34] 后台预热线程（_prebuild_components_background）与主线程消息
        路径（_on_gateway_input → _ensure_engine）会并发进入本方法。锁内双检
        保证仅构造一次；已就绪时仍走无锁快路径（首行判断）不付锁代价。
        """
        if self._tool_executor is not None and self._agent_manager is not None:
            return True
        with self._components_lock:
            # 二次检查：可能在等锁期间已被另一线程构造完成
            if self._tool_executor is not None and self._agent_manager is not None:
                return True
            return self._build_components_locked()

    def _build_components_locked(self) -> bool:
        """实际构造段（调用方必须已持 self._components_lock）"""
        try:
            from app.core.tools.tool_executor import ToolExecutor

            if self._tool_executor is None:
                # backend=None：gateway 无 UI，不跑主对话 Hook；
                # 权限由 GatewayEngine 的 AGENT_CONFIG 策略裁决
                self._tool_executor = ToolExecutor(backend=None)
                self._tool_executor.set_llm_config_getter(self._get_model_config)
                # 团队上下文用固定 id，与窗口团队互不干扰
                if self._tool_executor._builtin_tools:
                    self._tool_executor._builtin_tools.set_team_context("gateway", "plan")

            if self._agent_manager is None:
                from app.core.agent import AgentManager

                # [PERF T34] 不再覆盖 _agent_manager._builtin_tools（T19 实证：
                # GatewayEngine 全链显式传表 engine.py:768/773，覆盖全局指针只造成
                # 窗口 fallback 污染与关闭泄漏，无功能依赖）。
                self._agent_manager = AgentManager.get_instance(None, None)
            return True
        except Exception as e:
            logger.error(f"[GatewayService] 组件创建失败: {e}", exc_info=True)
            return False

    def _ensure_engine(self) -> bool:
        """确保 GatewayEngine 就绪（幂等；引擎是全局单例）"""
        if self._engine is not None and getattr(self._engine, "is_active", True):
            return True
        if not self._ensure_components():
            return False
        try:
            from app.core.engines.gateway import GatewayEngine

            self._engine = GatewayEngine.get_instance(
                get_model_config=self._get_model_config,
                tool_executor=self._tool_executor,
                agent_manager=self._agent_manager,
                session_store=self._get_session_store(),
            )
            logger.info("[GatewayService] GatewayEngine 就绪")
        except Exception as e:
            logger.error(f"[GatewayService] GatewayEngine 创建失败: {e}", exc_info=True)
        return self._engine is not None and getattr(self._engine, "is_active", True)

    @staticmethod
    def _get_session_store():
        from app.core.store import SessionStore

        return SessionStore.get_instance()

    # ==================== 生命周期 ====================

    def ensure_started(self) -> None:
        """幂等启动：PlatformManager（全局单例）+ GatewayEngine（后台线程连接）"""
        if self._initialized:
            return
        try:
            from app.gateway.manager import create_platform_manager

            async def process_message(
                session_id: str, text: str, platform: Any, chat_id: str, user_id: str, **kwargs
            ) -> str:
                return await self._process_message(session_id, text, platform, chat_id, user_id, **kwargs)

            async def send_message(platform: Any, chat_id: str, content: str, **kwargs) -> Any:
                return await self._send_message(platform, chat_id, content, **kwargs)

            self._manager = create_platform_manager(process_message, send_message)
            self._initialized = True
            logger.info("[GatewayService] PlatformManager 就绪")

            # [PERF] 引擎构造不再阻塞启动关键路径：_ensure_engine 触发的懒加载
            # import 链（GatewayEngine → adapters → mcp SDK 3s + tool_executor
            # 1.4s + SessionStore DB 迁移）实测 ~8.5s，是 create_instance 的最大
            # 单项。时序安全：
            # - 首批外部平台消息早于引擎就绪 → _on_gateway_input 的
            #   _ensure_engine 自愈守卫兜底（幂等 + 5s 冷却）
            # - 应用退出早于引擎就绪 → stop() 只动 _manager，_engine=None 无害
            # [PERF 2026-09-14] singleShot(0) → 4000ms：首帧后立即预热仍落在启动
            # 高峰窗口（UI 插件装载/后端延迟创建并发期），实测 _ensure_engine 占
            # 主线程 ~370ms（含 ToolExecutor 重复初始化日志）。延后 4s 错峰执行，
            # 首条平台消息早到仍由自愈守卫兜底。
            # [PERF T34] 4s 检查点不再直接建引擎，改走 _maybe_prebuild_engine：
            # 无启用平台时彻底跳过（不用网关的用户省下 20-50MB 常驻 + 4s 尖峰），
            # 有启用平台时把重活（import 链 + ToolExecutor）挪到 daemon 线程，
            # 主线程只留 QObject 构造段。
            from PyQt5.QtCore import QTimer

            QTimer.singleShot(4000, self._maybe_prebuild_engine)

            self._manager.start_all_async()
        except Exception as e:
            logger.exception(f"[GatewayService] 启动失败: {e}", exc_info=True)

    # ==================== 按需预热（T34） ====================

    def _has_enabled_platform(self) -> bool:
        """是否存在「已注册 + 配置启用」的 gateway 平台（实时遍历，零缓存）。

        registry 异常（插件组件未就绪/加载失败）时返回 False —— 保守跳过预热，
        待首条平台消息经 _on_gateway_input 自愈路径建引擎（该路径不依赖本判断）。

        不缓存的原因：平台启用状态可由用户随时在设置卡切换，缓存会让
        「刚启用平台但引擎未建」的窗口期延长到下次判断；本方法只在 4s
        检查点调用一次，遍历成本可忽略。
        """
        try:
            from app.gateway.config import GatewayConfigHelper
            from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

            for d in GatewayPlatformRegistry.get_instance().list_platforms():
                if GatewayConfigHelper.is_platform_enabled(d.platform_id):
                    return True
            return False
        except Exception as e:
            logger.debug(f"[GatewayService] 平台启用状态检查失败（视为无启用平台）: {e}")
            return False

    def _maybe_prebuild_engine(self) -> None:
        """4s 检查点入口（主线程）：按需决定是否预热引擎。

        - 引擎已活 → 直接返回（幂等，可能是消息自愈路径先建好了）
        - 无启用平台 → 跳过（T34 目标：不用网关的用户零成本）
        - 有启用平台 → 转后台线程做重活
        """
        if self._engine is not None and getattr(self._engine, "is_active", True):
            return
        if not self._has_enabled_platform():
            logger.info("[GatewayService] 无启用平台，跳过引擎预热")
            return
        self._prebuild_components_background()

    def _prebuild_components_background(self) -> None:
        """后台线程预热组件（import 链 + ToolExecutor），完成后回主线程建引擎。

        T19 定案：把 ~370ms 主线程串行段（GatewayEngine import 链含 mcp SDK、
        ToolExecutor 构造 ~1.4s 级）移出主线程；QObject 构造段（GatewayEngine
        get_instance）必须回主线程执行，故用 QTimer.singleShot(0) 投递。
        """
        t0 = time_module.perf_counter()

        def _work():
            ok = False
            try:
                # GatewayEngine import 链预热（模块级 import 含 mcp SDK，冷启动最重）
                import app.core.engines.gateway  # noqa: F401

                # 组件构造（含 ToolExecutor）在后台线程完成；_ensure_components
                # 内部双检锁保证与主线程消息路径竞争安全
                ok = self._ensure_components()
                elapsed = (time_module.perf_counter() - t0) * 1000
                logger.info(f"[GatewayService] 后台组件预热 ok={ok} {elapsed:.0f}ms")
                if ok:
                    # 回主线程做 QObject 构造（GatewayEngine 是 QObject 单例）；
                    # QTimer.singleShot 可从任意线程投递到对象所属线程的事件循环
                    QTimer.singleShot(0, self._ensure_engine)
            except Exception as e:
                logger.error(f"[GatewayService] 后台组件预热失败: {e}", exc_info=True)

        threading.Thread(target=_work, name="gw-prebuild", daemon=True).start()

    def sync_platforms(self) -> None:
        """按 registry 当前注册 + 配置启用状态，补启未连接的启用平台（幂等，主线程调用）。

        修复启动时序缺陷：ensure_started() 的 start_all_async 执行时，gateway 插件
        组件尚未注册进 GatewayPlatformRegistry（warmup_runtime_components 经
        QTimer 延迟 ~2s 才跑），registry 为空 → 已启用平台漏启；且 start_all_async
        跑完后 _running=True，再次 start_all 直接 return（Already running），
        之后无人补偿 → 只能手动关闭/打开插件开关（走 start_platform_async 才活）。

        本方法在 warmup 完成后调用：对每个"已注册 + 配置启用 + 未连接"的平台
        补 start_platform_async（_ensure_adapter 动态加载 adapter；
        adapter.start() 幂等，已连接实例不会重复建连）。
        """
        if self._manager is None:
            return
        try:
            from app.plugins.registries.gateway_platform_registry import (
                GatewayPlatformRegistry,
            )

            for d in GatewayPlatformRegistry.get_instance().list_platforms():
                try:
                    cfg = self._manager._config.get_platform_config(d.platform_id)
                    if not cfg or not getattr(cfg, "enabled", False):
                        continue
                    adapter = self._manager._adapters.get(d.platform_id)
                    if adapter is not None and adapter.is_connected:
                        continue
                    logger.info(f"[GatewayService] sync: 补启平台 {d.platform_id}")
                    self._manager.start_platform_async(d.platform_id)
                except Exception as e:
                    logger.warning(f"[GatewayService] sync 平台 {d.platform_id} 失败: {e}")
        except Exception as e:
            logger.warning(f"[GatewayService] sync_platforms 失败: {e}")

    def stop(self) -> None:
        """停止平台连接（应用退出时调用）"""
        if self._manager:
            try:
                self._manager.stop_all()
                logger.info("[GatewayService] 已停止")
            except Exception as e:
                logger.warning(f"[GatewayService] 停止失败: {e}")

    # ==================== 平台消息入口 ====================

    async def _process_message(
        self, session_id: str, text: str, platform: Any, chat_id: str, user_id: str, **kwargs
    ) -> str:
        """处理平台消息（manager 事件循环线程）→ 转主线程"""
        _pid = _gw_str_platform(platform)
        logger.info(f"[Gateway] Processing message from {_pid}:{user_id}: {text[:50]}...")

        # 流式平台跳过"思考中"占位（打字机即实时反馈）
        try:
            adapter = self._manager.get_adapter(platform)
            use_stream = bool(adapter and adapter.supports_streaming(chat_id))
        except Exception:
            use_stream = False
        if not use_stream:
            await self._send_message(platform, chat_id, "🤔 正在思考，请稍候...")

        future = concurrent.futures.Future()
        self.gateway_input_received.emit(
            {
                "text": text,
                "chat_id": chat_id,
                "user_id": user_id,
                "platform": _pid,
                "future": future,
            }
        )
        try:
            # on_stream_finished 回调已推送最终回复，此处返回空避免重复发送
            return await asyncio.wrap_future(future)
        except Exception as e:
            import traceback

            logger.error(f"[Gateway] AI processing error: {e}\n{traceback.format_exc()}")
            return ""

    async def _send_message(self, platform: Any, chat_id: str, content: str, **kwargs) -> Any:
        from app.gateway.base import SendResult

        adapter = self._manager.get_adapter(platform) if self._manager else None
        if adapter:
            try:
                return await adapter.send(chat_id, content)
            except Exception as e:
                logger.error(f"[Gateway] Send failed: {e}")
                return SendResult(success=False, error=str(e))
        logger.warning(f"[Gateway] No adapter for platform {platform}")
        return SendResult(success=False, error="No adapter")

    def send_to_platform(self, platform: Any, chat_id: str, content: str, timeout: float = 30.0) -> Any:
        """同步向指定平台会话发送消息（供插件调用，任意线程安全）。

        与 `_send_message` 的区别：本方法是**公开的插件服务面**，
        在 PlatformManager 的持久事件循环上调度协程并等待结果，
        无需插件自行 import asyncio / 持有 manager 单例引用。

        Args:
            platform: 平台标识（`Platform` 枚举或 str 平台 id，如 "feishu"）
            chat_id: 目标会话 id（gateway 会话的 chat_id）
            content: 消息文本
            timeout: 等待发送结果的秒数

        Returns:
            SendResult（success/error 字段可判定结果）。服务未就绪时
            返回 success=False 的 SendResult，不抛异常。
        """
        from app.gateway.base import SendResult

        if self._manager is None:
            return SendResult(success=False, error="Gateway 未启动")

        async def _run() -> Any:
            return await self._send_message(platform, chat_id, content)

        try:
            coro = _run()
            loop = getattr(self._manager, "_loop", None)
            if loop is None or not loop.is_running():
                return SendResult(success=False, error="Gateway 事件循环未运行")
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            logger.warning(f"[Gateway] send_to_platform 超时: {platform}:{chat_id}")
            return SendResult(success=False, error="发送超时")
        except Exception as e:
            logger.warning(f"[Gateway] send_to_platform 失败: {e}")
            return SendResult(success=False, error=str(e))

    def list_platform_sessions(self) -> list:
        """已知 gateway 会话列表（供插件选择投递目标）。

        返回 GatewaySession 对象列表；服务未就绪时返回空列表。
        """
        if self._manager is None:
            return []
        try:
            return self._manager.get_sessions()
        except Exception as e:
            logger.warning(f"[Gateway] list_platform_sessions 失败: {e}")
            return []

    def list_platforms(self) -> list:
        """已注册通讯平台及其连接状态（供插件区分「没配平台」和「配了没会话」）。

        与会话列表无关：平台是连接通道，会话是入站消息产生的记录。
        平台已连接但从未收到消息时，会话列表为空而本列表非空。

        Returns:
            list[dict]，每项 {id, enabled, connected, available, error}；
            服务未就绪时返回空列表。
        """
        if self._manager is None:
            return []
        try:
            platforms = self._manager.get_status().get("platforms") or {}
            return [{"id": pid, **(info or {})} for pid, info in platforms.items()]
        except Exception as e:
            logger.warning(f"[Gateway] list_platforms 失败: {e}")
            return []

    async def _send_image(self, platform: Any, chat_id: str, image_path: str, **kwargs) -> Any:
        from app.gateway.base import SendResult

        adapter = self._manager.get_adapter(platform) if self._manager else None
        if adapter:
            try:
                return await adapter.send_image(chat_id, image_path)
            except Exception as e:
                logger.error(f"[Gateway] Send image failed: {e}")
                return SendResult(success=False, error=str(e))
        return SendResult(success=False, error="No adapter")

    # ==================== 主线程消息处理 ====================

    def _on_gateway_input(self, data: dict):
        """处理平台消息（主线程）——原 ChatBackend 逻辑，引擎不可用时自愈重建"""
        text = data["text"]
        chat_id = data["chat_id"]
        user_id = data["user_id"]
        platform = data["platform"]
        future = data["future"]

        logger.info(f"[Gateway] Main thread processing: {text[:50]}...")

        # ===== 入口守卫 + 自愈 =====
        # 服务永不销毁，但引擎可能因历史 cleanup 处于不可用状态：先尝试同步重建
        # （轻量、幂等），5s 冷却防风暴。仍失败才走"不可用"兜底回复。
        import time as _time

        if not self._ensure_engine():
            now = _time.monotonic()
            if now - self._last_selfheal_ts > _AUTO_RESTART_COOLDOWN:
                self._last_selfheal_ts = now
                logger.warning("[Gateway] engine unavailable, self-heal failed once more")
            try:
                if not future.done():
                    future.set_result("")
            except Exception:
                pass
            self._notify_unavailable(platform, chat_id)
            return

        try:
            from app.gateway.base import MessageEvent, MessageType

            gw_platform = _gw_str_platform(platform)

            # 1. 获取或创建 GatewaySession（平台用户映射）
            gw_session = self._manager.session_manager.get_or_create_session(
                MessageEvent(
                    text=text,
                    message_type=MessageType.TEXT,
                    message_id=user_id,
                    chat_id=chat_id,
                    user_id=user_id,
                    platform=gw_platform,
                )
            )

            # 2. 查找或创建 Gateway 自己的 ChatSession（完全独立于 UI）
            stored_chat_id = gw_session.metadata.get("chat_session_id")
            chat_session = None
            if stored_chat_id:
                chat_session = self._engine.find_session(stored_chat_id)

            if not chat_session:
                from app.core.chat_session import ChatSession

                user_name = gw_session.user_name or user_id[:8]
                chat_session = ChatSession(name=f"{gw_platform}对话")
                chat_session.set_topic_summary(f"[{gw_platform}] {user_name}")
                self._engine.add_session(chat_session)
                gw_session.metadata["chat_session_id"] = chat_session.session_id
                logger.debug(f"[Gateway] Created ChatSession: {chat_session.session_id} for {gw_platform}:{user_id}")

            _ev_loop = getattr(self._manager, "_loop", None)

            def _push_to_platform(content: str) -> None:
                """发送中间更新到平台"""
                if not _ev_loop or not content.strip():
                    return
                try:
                    asyncio.run_coroutine_threadsafe(self._send_message(gw_platform, chat_id, content), _ev_loop)
                except Exception as e:
                    logger.error(f"[Gateway] Stream push error: {e}")

            # 3.5 流式（打字机）通道：平台适配器支持时走 update_stream/finish_stream
            try:
                adapter = self._manager.get_adapter(platform)
                use_stream = bool(adapter and adapter.supports_streaming(chat_id) and _ev_loop)
            except Exception:
                adapter, use_stream = None, False

            if use_stream:
                try:
                    sf = asyncio.run_coroutine_threadsafe(adapter.start_stream(chat_id), _ev_loop)
                    use_stream = bool(sf.result(timeout=10))
                except Exception as e:
                    logger.warning(f"[Gateway] start_stream failed, fallback: {e}")
                    use_stream = False

            _visible: list = []
            _last_stream_push = [0.0]
            import time as _time

            def _stream_update(force: bool = False) -> None:
                """可见快照节流推给平台（300ms 粗节流）"""
                if not use_stream or not adapter:
                    return
                now = _time.monotonic()
                if not force and now - _last_stream_push[0] < 0.3:
                    return
                _last_stream_push[0] = now
                snapshot = "".join(_visible)
                if not snapshot.strip():
                    return
                try:
                    asyncio.run_coroutine_threadsafe(adapter.update_stream(chat_id, snapshot), _ev_loop)
                except Exception as e:
                    logger.debug(f"[Gateway] stream update skipped: {e}")

            gateway_chunks = []

            def on_content_received(chunk):
                """AI 流式输出——流式平台并入可见快照，否则累积待最终发送"""
                gateway_chunks.append(chunk)
                if use_stream:
                    _visible.append(chunk)
                    _stream_update()

            def on_tool_call(tool_data: dict):
                """工具调用进度——仅流式平台内联展示"""
                if not (use_stream and adapter):
                    return
                name = tool_data.get("tool_name", tool_data.get("name", "未知工具"))
                _visible.append(("\n\n" if _visible else "") + f"🔧 {name}…")
                _stream_update(force=True)

            def on_tool_result(tool_data: dict):
                """工具结果——仅流式平台简讯并入"""
                if not (use_stream and adapter):
                    return
                name = tool_data.get("tool_name", tool_data.get("name", "未知工具"))
                _visible.append(f"\n\n✅ {name} 完成")
                _stream_update(force=True)

            def on_stream_finished(response):
                """AI 完成 → 流式平台收尾打字机；其余平台发送最终完整回复"""
                content = response or "".join(gateway_chunks)
                final = content or "抱歉，我没有生成有效回复，请重试。"

                logger.info(f"[Gateway] AI completed, response_len={len(response)}, final_len={len(final)}")

                clean_content, image_paths = _extract_markdown_images(final)
                for img_path in image_paths:
                    try:
                        asyncio.run_coroutine_threadsafe(self._send_image(gw_platform, chat_id, img_path), _ev_loop)
                    except Exception as e:
                        logger.error(f"[Gateway] Image send error: {e}")

                text_to_send = clean_content.strip()
                if use_stream and adapter:
                    try:
                        fut = asyncio.run_coroutine_threadsafe(
                            adapter.finish_stream(chat_id, text_to_send or final), _ev_loop
                        )
                        result = fut.result(timeout=15)
                    except Exception as e:
                        logger.error(f"[Gateway] finish_stream error: {e}")
                        result = None
                    if not (result and result.success):
                        _push_to_platform(text_to_send or final)
                elif text_to_send:
                    _push_to_platform(text_to_send)
                elif not image_paths:
                    _push_to_platform(final)

                # 回复已在上方送达（流式卡片 finish_stream / 兜底 _push_to_platform），
                # future 仅作完成信号置空串——若回传 final，MessageHandler.handle
                # 会把同一份结果再发一遍（历史 bug：Gateway 结果发两次）。
                try:
                    if not future.done():
                        future.set_result("")
                except Exception:
                    pass

            def on_error(error):
                logger.error(f"[Gateway] AI error: {error}")
                _push_to_platform(f"❌ {error}")
                # 错误文案已推送，future 置空串防 handle 二次发送（同上）
                try:
                    if not future.done():
                        future.set_result("")
                except Exception:
                    pass

            callbacks = {
                "content_received": on_content_received,
                "tool_call_started": on_tool_call,
                "tool_result_received": on_tool_result,
                "stream_finished": on_stream_finished,
                "error": on_error,
            }

            self._engine.process(session=chat_session, text=text, callbacks=callbacks)

        except Exception as e:
            import traceback

            logger.error(f"[Gateway] Processing error: {e}\n{traceback.format_exc()}")
            try:
                if not future.done():
                    future.set_exception(e)
            except Exception:
                pass

    def _notify_unavailable(self, platform: Any, chat_id: str) -> None:
        """引擎不可用兜底提示"""
        _ev_loop = getattr(self._manager, "_loop", None) if self._manager else None
        if _ev_loop:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._send_message(
                        _gw_str_platform(platform),
                        chat_id,
                        "Gateway 当前不可用，请打开 DriFox 主窗口后重试。",
                    ),
                    _ev_loop,
                )
            except Exception as e:
                logger.warning(f"[Gateway] unavailable notice send failed: {e}")

    # ==================== 状态 ====================

    def get_status(self) -> dict:
        if self._manager:
            return self._manager.get_status()
        return {"running": False, "platforms": {}}
