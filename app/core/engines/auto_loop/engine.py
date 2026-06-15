# -*- coding: utf-8 -*-
"""
AutoLoop 循环引擎 — 管理循环状态、迭代追踪、完成信号检测、预算控制、共享笔记

三阶段设计：
1. PLANNING 阶段：拆解任务为步骤，写入 SHARED_TASK_NOTES.md
2. EXECUTING 阶段：按步骤执行，每步必须验证
3. ARCHIVING 阶段：执行完成后清理文件、归档日志和笔记
"""
import re
import time
from pathlib import Path
from typing import Optional, List

from loguru import logger

from app.core.engines.base import BaseEngine
from app.core.engines.auto_loop.config import AutoLoopConfig


class LoopState:
    IDLE = "idle"
    RUNNING = "running"
    PLANNING = "planning"
    EXECUTING = "executing"
    ARCHIVING = "archiving"
    COMPLETED = "completed"
    STOPPED = "stopped"
    ERROR = "error"


class AutoLoopEngine(BaseEngine):
    """核心循环引擎，不依赖 Qt，纯逻辑层

    AutoLoopEngine 是一个状态机，管理 AutoLoop 的三阶段执行流程：
    1. PLANNING → 2. EXECUTING → 3. ARCHIVING → COMPLETED

    它不直接参与 LLM 对话（由 AutoLoopWorker 通过 ConversationCore 驱动），
    而是提供状态追踪、预算控制、完成检测等逻辑能力。

    注意：AutoLoopEngine 不直接使用 ConversationCore + ConversationExecutor，
    因此 pass None 到父类构造，子类通过重写相关方法提供实现。
    """

    def __init__(self, config: Optional[AutoLoopConfig] = None):
        self.config = config or AutoLoopConfig()
        self.state = LoopState.IDLE
        self.iteration = 0
        self._completion_count = 0
        self._start_time = 0.0
        self._total_tokens = 0
        self._consecutive_failures = 0

        # 规划状态
        self._is_planning_phase = True
        self._planning_count = 0
        # _current_step: 内部步骤追踪，只由 advance_to_step / enter_execution_phase 修改
        self._current_step = 0
        # _display_step: UI 显示的步骤值，由 set_step_progress 修改（与 _current_step 分离）
        self._display_step = 0
        self._total_steps = 0
        self._verified_steps: set[int] = set()
        self._step_verified = False
        self._verification_failures = 0

        # 归档阶段
        self._is_archiving_phase = False
        self._archiving_complete = False

        # BaseEngine 不持有 ConversationCore，传 None
        super().__init__(conversation_core=None, conversation_executor=None)

    # ========== BaseEngine 接口实现 ==========

    def get_current_session(self):
        """AutoLoopEngine 不直接管理会话，返回 None"""
        return None

    # ========== 公共属性（只读）==========

    @property
    def current_step(self) -> int:
        """内部追踪的当前步骤（由 advance_to_step 控制）"""
        return self._current_step

    @property
    def display_step(self) -> int:
        """UI 显示的当前步骤（由 set_step_progress 控制）"""
        return self._display_step

    @property
    def total_steps(self) -> int:
        return self._total_steps

    @property
    def total_tokens(self) -> int:
        return self._total_tokens

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def is_planning(self) -> bool:
        return self._is_planning_phase

    @property
    def is_archiving(self) -> bool:
        return self._is_archiving_phase

    @property
    def is_archiving_complete(self) -> bool:
        return self._archiving_complete

    # ========== 状态写入方法 ==========

    def set_step_progress(self, current: int, total: int):
        """设置步骤进度（仅用于 UI 显示，不影响步骤推进）
        
        ⚡ 只修改 _display_step 和 _total_steps
        ⚡ 不修改 _current_step（它只由 advance_to_step / enter_execution_phase 控制）
        """
        self._display_step = current
        self._total_steps = total

    def add_tokens(self, tokens: int):
        """累加 token 使用量"""
        self._total_tokens += tokens

    def increment_consecutive_failures(self):
        """递增连续失败计数"""
        self._consecutive_failures += 1

    def reset_consecutive_failures(self):
        """重置连续失败计数"""
        self._consecutive_failures = 0

    # ========== 状态管理 ==========

    def reset(self):
        self.state = LoopState.IDLE
        self.iteration = 0
        self._completion_count = 0
        self._start_time = 0.0
        self._total_tokens = 0
        self._consecutive_failures = 0
        self._is_planning_phase = True
        self._planning_count = 0
        self._current_step = 0
        self._display_step = 0
        self._total_steps = 0
        self._verified_steps = set()
        self._step_verified = False
        self._verification_failures = 0
        self._is_archiving_phase = False
        self._archiving_complete = False

    def start(self):
        self.state = LoopState.PLANNING
        self._start_time = time.time()
        self._is_planning_phase = True
        self._is_archiving_phase = False
        logger.info("[AutoLoop] Engine started in PLANNING phase")

    def enter_execution_phase(self):
        """进入执行阶段"""
        self.state = LoopState.EXECUTING
        self._is_planning_phase = False
        self._is_archiving_phase = False
        self._current_step = 1
        logger.info("[AutoLoop] Entering EXECUTION phase, step 1")

    def enter_archiving_phase(self):
        """进入归档阶段"""
        self.state = LoopState.ARCHIVING
        self._is_planning_phase = False
        self._is_archiving_phase = True
        logger.info("[AutoLoop] Entering ARCHIVING phase")

    def stop(self):
        self.state = LoopState.STOPPED
        logger.info("[AutoLoop] Engine stopped by user")

    def is_planning_phase(self) -> bool:
        return self._is_planning_phase and not self._is_archiving_phase

    def is_executing_phase(self) -> bool:
        return not self._is_planning_phase and not self._is_archiving_phase

    def is_archiving_phase(self) -> bool:
        return self._is_archiving_phase

    # ========== 规划阶段管理 ==========

    def on_planning_attempt(self):
        """每次规划尝试调用"""
        self._planning_count += 1
        if self._planning_count > 3:
            logger.warning(f"[AutoLoop] Too many planning attempts ({self._planning_count}), forcing execution")
            if self._total_steps == 0:
                self._total_steps = 1
            self.enter_execution_phase()

    def parse_steps_from_notes(self, notes: str) -> tuple[int, int]:
        """从笔记中解析当前步骤和总步骤数"""
        patterns = [
            r'[-*]\s*\[.*?\]\s*\[步骤\s*(\d+)\]',
            r'[-*]\s*\[.*?\]\s*步骤\s*(\d+)',
        ]
        steps = []
        for pattern in patterns:
            matches = re.findall(pattern, notes, re.IGNORECASE)
            if matches:
                steps = [int(m) for m in matches]
                break
        if steps:
            return max(steps), len(steps)
        return 0, 0

    def parse_current_and_next_step(self, notes: str) -> tuple[int, int, int]:
        """从笔记中解析当前步骤、已勾选完成的最大步骤、总步骤数"""
        # 同时匹配两种常见格式：
        # 1. - [ ] [步骤 1] <描述>  (嵌套方括号)
        # 2. - [ ] 步骤 1 <描述>      (直接)
        # 匹配三种格式：
        # 1. - [x] [步骤 1] (嵌套方括号)
        # 2. - [x] 步骤 1   (直接)
        # 3. - [步骤 1]     (无复选框)
        patterns = [
            r'[-*]\s*\[.*?\]\s*\[步骤\s*(\d+)\]',
            r'[-*]\s*\[.*?\]\s*步骤\s*(\d+)',
        ]
        all_steps = []
        for pattern in patterns:
            matches = re.findall(pattern, notes, re.IGNORECASE)
            if matches:
                all_steps = matches
                break

        total_steps = len(all_steps)
        max_step_num = max([int(s) for s in all_steps]) if all_steps else 0

        verified_steps = self.parse_checked_steps_from_notes(notes)
        max_verified = max(verified_steps) if verified_steps else 0

        current_step = max_verified + 1
        return current_step, max_verified, total_steps

    def parse_checked_steps_from_notes(self, notes: str) -> set[int]:
        """从笔记中解析已勾选完成的步骤 [x]
        
        只匹配 [x]（已勾选），不匹配 [ ]（未勾选）。
        支持 [x] 和 [X] 两种写法。
        """
        patterns = [
            r'[-*]\s*\[x\]\s*\[步骤\s*(\d+)\]',
            r'[-*]\s*\[X\]\s*\[步骤\s*(\d+)\]',
            r'[-*]\s*\[x\]\s*步骤\s*(\d+)',
            r'[-*]\s*\[X\]\s*步骤\s*(\d+)',
        ]
        checked = set()
        for pattern in patterns:
            for match in re.finditer(pattern, notes):
                checked.add(int(match.group(1)))
        return checked

    def get_verified_steps(self) -> set[int]:
        """获取已验证通过的步骤集合"""
        return self._verified_steps

    def sync_verified_steps_from_notes(self, notes: str):
        """从笔记同步已勾选步骤到缓存"""
        self._verified_steps = self.parse_checked_steps_from_notes(notes)
        logger.info(f"[AutoLoop] Synced {len(self._verified_steps)} verified steps from notes")

    def is_current_step_verified(self) -> bool:
        """检查当前步骤是否已验证通过"""
        return self._current_step in self._verified_steps

    def get_incremental_summary(self) -> str:
        """生成增量执行进度总结"""
        if self._is_planning_phase:
            return ""

        verified = sorted(self._verified_steps)
        total = self._total_steps
        # 展示给模型看的步骤用 display_step（反映笔记中的实际进度）
        display = self._display_step if self._display_step > 0 else self._current_step
        remaining = [s for s in range(1, total + 1) if s not in self._verified_steps]

        summary = [
            "\n\n📊 **增量执行进度总结**",
            f"- ✅ 已验证完成：{verified if verified else '无'}",
            f"- 🔄 当前需要处理：步骤 {display}",
            f"- ⏭️ 未开始：{remaining if remaining else '无'}",
            "",
            "⚠️ 强制要求：",
            f"- 你只需要处理**当前步骤 {display}**",
            "- 已完成步骤不需要重复验证或修改",
            "- **每轮结束必须追加**本轮操作记录到 SHARED_TASK_NOTES.md",
            "- 禁止覆盖原始执行计划，只能在文档末尾追加结果记录",
            "",
        ]
        return "\n".join(summary)

    # ========== 执行阶段管理 ==========

    def advance_to_step(self, step_num: int):
        """前进到指定步骤"""
        self._current_step = step_num
        self._step_verified = False
        logger.info(f"[AutoLoop] Advanced to step {step_num}/{self._total_steps}")

    def verify_current_step(self, success: bool):
        """验证当前步骤结果"""
        if success:
            self._verified_steps.add(self._current_step)
            self._step_verified = True
            self._verification_failures = 0
        else:
            self._verification_failures += 1
            if self._verification_failures >= 3:
                logger.warning(f"[AutoLoop] Step {self._current_step} failed 3 times, skipping")
                self._verified_steps.add(self._current_step)
                self._verification_failures = 0

    def is_task_completed(self) -> bool:
        """检查任务是否完成（所有步骤都已验证）"""
        if self._total_steps == 0:
            return False
        all_steps = set(range(1, self._total_steps + 1))
        return self._verified_steps == all_steps

    def all_steps_verified(self) -> bool:
        """检查所有步骤是否已验证完成

        优先使用 _verified_steps 缓存（从笔记同步），
        如果缓存为空则从笔记直接解析。
        """
        if self._total_steps == 0:
            return False
        if self._verified_steps:
            all_steps = set(range(1, self._total_steps + 1))
            return self._verified_steps == all_steps
        # 兜底：从笔记直接解析
        notes = self.read_shared_notes()
        checked = self.parse_checked_steps_from_notes(notes)
        if checked:
            all_steps = set(range(1, self._total_steps + 1))
            return checked == all_steps
        return False

    # ========== 完成检测 ==========

    def check_completion(self, response_text: str) -> bool:
        """检测响应中是否包含完成信号
        
        结束只由 completion_signal 连续出现次数决定，与步骤数无关。
        步骤数可动态变化，不参与结束判断。

        Returns:
            True 当信号出现次数达到阈值，False 否则
        """
        signal = self.config.completion_signal
        if not signal:
            return False
        # 规划阶段和归档阶段忽略完成信号，防止偶然匹配导致误判
        if not self.is_executing_phase():
            return False
        if signal in response_text:
            self._completion_count += 1
            if self._completion_count >= self.config.completion_threshold:
                logger.info(f"[AutoLoop] Completion signal detected ({self._completion_count} times)")
                return True
        else:
            self._completion_count = 0
        return False

    def get_completion_count(self) -> int:
        """获取当前的完成信号计数（用于反馈：还需 X 次确认）"""
        return self._completion_count

    def check_archive_complete(self, response_text: str) -> bool:
        """检测归档是否完成（检查 ARCHIVE_COMPLETE 信号）"""
        if not self._is_archiving_phase:
            return False
        if "ARCHIVE_COMPLETE" in response_text:
            self._archiving_complete = True
            self.state = LoopState.COMPLETED
            logger.info("[AutoLoop] Archive complete signal detected")
            return True
        return False

    def check_planning_complete(self, response_text: str, notes: str) -> bool:
        """检测规划是否完成"""
        if "PLANNING_COMPLETE" not in response_text.upper():
            return False
        return True

    # ========== 预算检查 ==========

    def check_budget(self) -> Optional[str]:
        """检查是否超预算，返回 None=正常，str=停止原因"""
        elapsed = time.time() - self._start_time
        elapsed_min = elapsed / 60

        if self.config.max_duration_minutes > 0 and elapsed_min >= self.config.max_duration_minutes:
            reason = f"超时: 已运行 {elapsed_min:.1f} 分钟，上限 {self.config.max_duration_minutes} 分钟"
            logger.warning(f"[AutoLoop] {reason}")
            return reason

        if self.config.max_tokens > 0 and self._total_tokens >= self.config.max_tokens:
            reason = f"Token 超限: 已用 {self._total_tokens}，上限 {self.config.max_tokens}"
            logger.warning(f"[AutoLoop] {reason}")
            return reason

        return None

    # ========== 共享笔记 ==========

    def get_notes_path(self) -> Optional[Path]:
        if not self.config.project_path:
            return None
        return Path(self.config.project_path) / self.config.notes_file

    def read_shared_notes(self) -> str:
        path = self.get_notes_path()
        if not path or not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"[AutoLoop] Failed to read notes: {e}")
            return ""

    def get_round_log_path(self, iteration: int) -> Optional[Path]:
        """获取当前轮次的独立日志文件路径"""
        if not self.config.project_path:
            return None
        project_dir = Path(self.config.project_path)
        logs_dir = project_dir / self.config.logs_dir
        logs_dir.mkdir(parents=True, exist_ok=True)
        return logs_dir / f"round_{iteration:03d}.md"

    def write_round_log(self, iteration: int, content: str) -> bool:
        """写入当前轮次的完整日志到独立文件"""
        path = self.get_round_log_path(iteration)
        if not path:
            return False
        try:
            path.write_text(content, encoding="utf-8")
            logger.info(f"[AutoLoop] Wrote round {iteration} log to {path}")
            return True
        except Exception as e:
            logger.warning(f"[AutoLoop] Failed to write round log: {e}")
            return False

    # ========== 归档路径 ==========

    def get_archive_latest_dir(self) -> Optional[Path]:
        """获取归档最近一次执行的目录路径"""
        if not self.config.project_path:
            return None
        return Path(self.config.project_path) / ".autoloop" / "archive" / "latest"

    def get_archive_timestamped_dir(self) -> Optional[Path]:
        """获取时间戳归档路径：.autoloop/archive/YYYYMMDD_HHMMSS-任务名

        每次归档创建一个独立的时间戳子目录，避免覆盖历史记录。
        """
        if not self.config.project_path:
            return None
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        task_name = (self.config.task_prompt or "untitled")[:30]
        # 清理文件名非法字符
        import re
        safe_name = re.sub(r'[^\w\-\u4e00-\u9fff]', '_', task_name)
        return Path(self.config.project_path) / ".autoloop" / "archive" / f"{timestamp}-{safe_name}"

    def get_archive_meta_path(self) -> Optional[Path]:
        """获取归档元信息文件路径（latest）"""
        d = self.get_archive_latest_dir()
        if d:
            return d / "META.md"
        return None

    def read_archive_notes(self) -> str:
        """读取最近的归档笔记（从 latest 或时间戳目录），用于规划阶段参考"""
        # 优先读 latest，再找最新的时间戳目录
        d = self.get_archive_latest_dir()
        if d and d.exists():
            notes_path = d / "SHARED_TASK_NOTES.md"
            if notes_path.exists():
                try:
                    return notes_path.read_text(encoding="utf-8")
                except Exception:
                    pass
        
        # 兜底：找最新的时间戳目录
        if self.config.project_path:
            archive_root = Path(self.config.project_path) / ".autoloop" / "archive"
            if archive_root.exists():
                dirs = sorted(archive_root.iterdir(), key=lambda p: p.name, reverse=True)
                for d in dirs:
                    if d.is_dir() and d.name != "latest":
                        notes_path = d / "SHARED_TASK_NOTES.md"
                        if notes_path.exists():
                            try:
                                return notes_path.read_text(encoding="utf-8")
                            except Exception:
                                continue
        return ""

    # ========== 获取进度信息 ==========

    def get_progress(self) -> dict:
        """获取当前进度信息，用于 UI 更新"""
        elapsed = time.time() - self._start_time
        return {
            "iteration": self.iteration,
            "max_iterations": self.config.max_iterations,
            "elapsed_seconds": int(elapsed),
            "elapsed_str": self._format_time(elapsed),
            "total_tokens": self._total_tokens,
            "max_tokens": self.config.max_tokens,
            "state": self.state,
            "phase": "archiving" if self._is_archiving_phase else ("planning" if self._is_planning_phase else "executing"),
            # UI 显示用 _display_step，内部追踪用 _current_step
            "current_step": self._display_step if not self._is_planning_phase else self._current_step,
            "total_steps": self._total_steps,
        }

    @staticmethod
    def _format_time(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}时{m}分{s}秒"
        return f"{m}分{s}秒"

    # ========== 步骤完成检测 ==========

    def check_step_completed(self, response: str, notes: str, step_num: int) -> bool:
        """检测当前步骤是否完成"""
        # 新增：STEP_X/N_COMPLETE 信号检测
        total = self._total_steps
        step_signal = f"STEP_{step_num}/{total}_COMPLETE" if total > 0 else None
        if step_signal and step_signal in response:
            return True

        patterns = [
            rf'步骤\s*{step_num}\s*(完成|已验证|验证成功)',
            rf'step\s*{step_num}\s*(complete|verified|done)',
            # 注意：必须绑定 step_num，防止"步骤 3 验证成功"被步骤2的检查误匹配
            rf'步骤\s*{step_num}.*?验证.*?成功',
            rf'step\s*{step_num}.*?verify.*?success',
        ]
        for p in patterns:
            if re.search(p, response, re.IGNORECASE):
                return True

        if notes:
            # 只匹配 [x]（已勾选），不匹配 [ ]
            pattern = rf'- \[x\]\s*步骤\s*{step_num}'
            if re.search(pattern, notes, re.IGNORECASE):
                return True
            pattern = rf'- \[X\]\s*步骤\s*{step_num}'
            if re.search(pattern, notes):
                return True
            if re.search(rf'步骤\s*{step_num}\s+结果', notes):
                return True
            if re.search(rf'步骤\s*{step_num}.*完成', notes, re.DOTALL):
                return True

        return False

    def get_next_step_preview(self, notes: str, step_num: int) -> str:
        """获取下一步骤的预览文本"""
        pattern = rf'- \[.?\]?\s*\[步骤\s*{step_num}\].*?(?=\n-|\Z)'
        match = re.search(pattern, notes, re.DOTALL)
        if match:
            step_text = match.group(0)
            preview = re.sub(r'^-\s*\[.?\]?\s*\[步骤\s*\d+\]\s*', '', step_text)
            if '|' in preview:
                preview = preview.split('|')[0].strip()
            return preview[:60].strip() + ('...' if len(preview) > 60 else '')
        return f"步骤 {step_num}"

    def check_relay_doc_updated(self, iteration: int) -> bool:
        """检查接力文档是否已更新"""
        notes = self.read_shared_notes()

        if not notes or len(notes.strip()) < 50:
            logger.warning(f"[AutoLoop] Iteration {iteration}: relay doc is empty or too short")
            return False

        if self._is_planning_phase:
            if "## 执行计划" not in notes and "- [步骤" not in notes:
                logger.warning(f"[AutoLoop] Iteration {iteration}: no execution plan in relay doc")
                return False
            return True

        total_steps = self._total_steps

        if total_steps > 0:
            # 使用最后已验证的步骤号，而不是 _current_step（它已在 advance_to_step 后前进到下一步）
            last_verified = max(self._verified_steps) if self._verified_steps else 0
            check_step = last_verified if last_verified > 0 else self._current_step
            result_pattern = rf'步骤\s*{check_step}\s+结果|## 步骤\s*{check_step}\s+结果'
            if not re.search(result_pattern, notes, re.IGNORECASE):
                if "## 当前状态" not in notes and "当前状态" not in notes:
                    logger.warning(f"[AutoLoop] Iteration {iteration}: no step {check_step} result recorded (current_step={self._current_step}, verified={self._verified_steps})")
                    return False

        return True
