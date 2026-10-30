# -*- coding: utf-8 -*-
"""T20 回归：replay_theme_qss 遍历线性化（O(n²) → O(N)）

背景
----
原实现 `stack.extend(obj.findChildren(QWidget))` 对每个 pop 出的节点都递归拉
**全部后代**入栈 → 同一 widget 被重复入栈、重复 apply（O(n²) 访问 + 多次冗余
setStyleSheet）。改为 `obj.children()` 直接子级 + isinstance(QWidget) 过滤后
恰为一次线性 BFS。

本文件锁定：
1. 全程不调用 findChildren（防将来改回去）；
2. 遍历语义不变：root 及其全部后代各 apply 恰一次（以登记工厂的执行计数为证）；
   QLayout/QAction 等非 QWidget 子对象被过滤、不参与。

2026-10-30：apply_theme_qss 新增同串短路（Qt5 对相同串也会 repolish 整棵
子树）。因此 factory 执行次数与 setStyleSheet 次数不再相等：前者=遍历触达数，
后者仅统计串真变化的控件。断言据此分开。
"""

from PyQt5.QtCore import QObject
from PyQt5.QtWidgets import QLabel, QWidget

from app.utils.theme_style import bind_theme_qss, replay_theme_qss

_PLACEHOLDER_QSS = "QWidget { background: transparent; }"  # 非空串：空串会被 apply 跳过


def test_replay_never_calls_find_children(qapp, monkeypatch):
    """防回归：replay_theme_qss 全程不得调用 findChildren（应走 children() 线性遍历）"""

    def _forbidden(*a, **k):
        raise AssertionError("replay_theme_qss 不得调用 findChildren（T20 线性化回归）")

    monkeypatch.setattr(QWidget, "findChildren", _forbidden)

    root = QWidget()
    child = QLabel(root)
    grandchild = QLabel(child)
    probe = QObject(root)  # 非 QWidget 直接子级：应被 isinstance 过滤、不炸遍历

    applied = []
    bind_theme_qss(root, lambda colors=None: (applied.append(root), _PLACEHOLDER_QSS)[1])
    applied.clear()  # bind_theme_qss 登记时会立即应用一次，与 replay 无关，清掉

    count = replay_theme_qss(root)
    assert applied == [root], "遍历必须触达 root（factory 执行即触达证据）"
    assert count == 0, f"串与现状相同→同串短路不重写，实际 count={count}"
    assert probe.parent() is root  # 哨兵仍挂在树上，证明过滤发生在遍历侧


def test_replay_visits_each_descendant_exactly_once(qapp):
    """遍历语义：深嵌套子树每个登记过的 QWidget 恰 apply 一次"""
    applied = []

    root = QWidget()
    a = QLabel(root)
    b = QLabel(a)
    c = QLabel(b)

    for w in (root, a, b, c):
        # 登记工厂：被 apply 时记录（apply_theme_qss 执行 factory 并 setStyleSheet）；
        # 默认参数绑定防循环变量晚绑定
        bind_theme_qss(w, lambda colors=None, _w=w: (applied.append(_w), _PLACEHOLDER_QSS)[1])

    applied.clear()
    count = replay_theme_qss(root)
    assert len(applied) == 4, f"每个 widget 恰被 factory 触达一次，实际 {len(applied)} 次: {applied}"
    assert set(applied) == {root, a, b, c}, "深嵌套后代必须全部触达"
    assert count == 0, f"串未变→同串短路不重写任何控件，实际 {count}"
