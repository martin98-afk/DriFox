# -*- coding: utf-8 -*-
"""子智能体日志按钮 → 紧凑卡片显示链路回归

Bug（L2 状态层重构 89398da1 引入）：auto_hide/手动关闭紧凑卡后 ``_batch_started=False``
且任务行仍在，点消息卡片日志按钮时 ``show_completed_task`` 因 ``task_id in _task_rows``
早退（不 setVisible），``refresh_layer("status")`` 的分层谓词
``_batch_started and _task_rows`` 恒假 → 没人负责显示 → 点击按钮无任何反应。

修复契约：按钮路径与 /subagents 命令路径一致 —— 先置 ``_batch_started=True``
再 refresh_layer。本文件同时锁定 CardManager 的机制行为作为陷阱文档。
谓词闭包绑定桩的字段名（_batch_started/_task_rows），与生产侧谓词表达式一致。
"""

from app.widgets.cards.card_manager import CardManager, ContainerType


class _FakeCompact:
    """SubAgentCompactFloatingWidget 状态桩：暴露分层谓词依赖的字段与显隐接口"""

    def __init__(self):
        self._batch_started = False
        self._task_rows = {}
        self.visible = False

    def setVisible(self, v):
        self.visible = bool(v)

    def show_completed_task(self, task_id, *_args, **_kwargs):
        """复刻生产 SubAgentCompactFloatingWidget.show_completed_task 的早退语义：
        行已存在 → return，不 setVisible（Bug 机制的关键一环）"""
        if task_id in self._task_rows:
            return
        self.setVisible(True)

    def show_card(self):
        self.visible = True

    def hide_card(self):
        self.visible = False

    def windowTitle(self):
        return "fake"


def _setup(cm):
    """注册与 main_widget 相同形状的 sub_agent_compact 分层卡（谓词绑定桩状态）"""
    compact = _FakeCompact()
    W = "w"
    cm.register_window(W)
    cm.register_card(
        W,
        ContainerType.BOTTOM,
        "sub_agent_compact",
        compact,
        layer="status",
        stackable=True,
        order_hint=10,
        visible_when=lambda: bool(compact._batch_started and compact._task_rows),
    )
    return W, compact


def test_refresh_layer_hides_button_shown_card_after_auto_hide():
    """Bug 场景文档：auto_hide 后点日志按钮，无人负责显示 → 卡片不出现

    真实序列（L2 状态层重构 89398da1 引入的回归）：
    1. 任务运行期谓词真，refresh_layer 把卡纳入 multi_visible 集合
    2. 全部任务完成 → _auto_hide → closed → _batch_started=False → 卡退出可见集
    3. 用户点消息卡片日志按钮 → 任务行仍在（auto_hide 不清行）→
       show_completed_task 因 task_id in _task_rows 早退（无 setVisible）
    4. refresh_layer 谓词假 → 不显示 → 按钮点击无任何反应
    """
    CardManager.reset_instance()
    try:
        cm = CardManager.get_instance()
        W, compact = _setup(cm)

        # 步骤 1：运行期谓词真 → 卡进可见集
        compact._task_rows["t1"] = object()
        compact._batch_started = True
        cm.refresh_layer(W, "status")
        assert compact.visible

        # 步骤 2a：_auto_hide → setVisible(False) + closed 信号（widget 侧）
        compact.setVisible(False)
        # 步骤 2b：_on_sub_agent_compact_closed 槽收到 closed → 置 _batch_started=False
        compact._batch_started = False
        cm.refresh_layer(W, "status")
        assert not compact.visible

        # 步骤 3：点按钮 → _on_subagent_log_requested → 行已存在 →
        # show_completed_task 早退，不 setVisible（生产 Bug 的关键一环）
        compact.show_completed_task("t1", "agent", "desc")
        assert not compact.visible

        # 步骤 4：_batch_started 仍为 False → refresh_layer 谓词假 → 不显示
        cm.refresh_layer(W, "status")
        assert not compact.visible
    finally:
        CardManager.reset_instance()


def test_log_button_contract_sets_batch_flag_then_refresh():
    """修复契约：按钮路径先置 _batch_started=True 再 refresh_layer → 卡片可见"""
    CardManager.reset_instance()
    try:
        cm = CardManager.get_instance()
        W, compact = _setup(cm)

        # 修复后的按钮路径序列（与 /subagents 命令路径一致）：
        # 任务行已存在（show_completed_task 会早退）也必须置位，否则无人显示
        compact._task_rows["t1"] = object()  # auto_hide 后行仍在
        compact.show_completed_task("t1", "agent", "desc")  # 早退，不改变可见性
        assert not compact.visible
        compact._batch_started = True
        cm.refresh_layer(W, "status")

        assert compact.visible

        # 用户关闭卡片 → _on_sub_agent_compact_closed 置 False → 谓词假 → 隐藏
        compact._batch_started = False
        cm.refresh_layer(W, "status")
        assert not compact.visible

        # 再次点击按钮 → 重新置位 → 恢复显示
        compact._batch_started = True
        cm.refresh_layer(W, "status")
        assert compact.visible
    finally:
        CardManager.reset_instance()
