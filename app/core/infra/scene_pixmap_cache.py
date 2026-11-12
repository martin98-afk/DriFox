# -*- coding: utf-8 -*-
"""壁纸 pixmap 跨窗口共享缓存（T43）。

动机（T40 实测核算）：高分辨率壁纸主题下，每个窗口的 SceneLayer 各自持有
5 份真副本（image source 31.64MB + 预乘 31.64MB + baked 8.37MB + fg source
31.64MB + fg 预乘 31.64MB ≈ 103-127MB），跨窗零共享；10 窗 = 1033MB，是
主进程 2.2GB 里最大的一块。本缓存让「同壁纸同参数」的副本全进程只建一份，
QLabel.setPixmap 走 Qt 隐式共享（引用计数），零复制。

key 设计（七元组，见 get_pixmap）：
- **必须用解析后的绝对路径**：项目已知教训——原串做 key 会命中假缓存，
  切主题背景不动（同 path 不同文件的旧 key 污染）。
- 文件 mtime：壁纸文件被替换后 key 自然变化，旧组被 LRU 淘汰。
- 副本类型/目标尺寸/opacity/blur/pad：任一渲染参数不同即不同条目，
  显示结果逐像素等同各自构建。

淘汰策略：按「壁纸组」整体 LRU（path+mtime 为组键），_MAX_SETS=1 组封顶
（仅当前壁纸），一套 ~103MB → 缓存上界 ~103-127MB；切主题时旧组淘汰、新组重解码。
不提供显式 invalidate：换壁纸 = path/mtime 变化 → 新组进、旧组出；
同壁纸换主题但渲染参数未变时命中缓存恰是正确行为（显示本就该一致）。

生命周期（引用计数）：每个活跃 SceneLayer 在构造时 acquire、析构时 release。
计数归零（全部窗口关闭）不立刻丢弃，起 _GRACE_SECONDS 宽限定时器，到点 clear；
宽限期内任一 acquire（重开窗口/切回主题）取消定时器并命中缓存，零重解码。
窗口仍打开时计数 >0，任何路径都不会提前淘汰。
"""

from __future__ import annotations

import os
from collections import OrderedDict
from typing import Callable, Optional

# 缓存组数上界：仅当前壁纸（T8 收紧，原 2 组 = 当前 + 上一主题）。
# 一套全副本 ≈ 103-127MB（T40 核算），4K baked 最坏再 +131MB；
# 1 组封顶 -103MB，代价仅切主题时重解码一次。
_MAX_SETS = 1

# 全部窗口关闭后的宽限秒数：到点才 clear。换来「快关快开」零重解码
# （重解码一份含 blur 烘焙的壁纸是主线程同步操作，秒级耗时不可接受）。
_GRACE_SECONDS = 30.0

# 每组 baked 条目上限（M2）：blur 壁纸拖拽 resize 按 (w,h,blur,pad) 逐帧写入，
# 实测 40 步拖拽堆 40 份/330MB（1080p）且相邻几何命中率 0/39。取 4 = 覆盖
# ≤3 窗活跃几何 + 1 份回弹缓冲；最坏驻留 1080p 约 34MB / 4K 约 131MB。
_MAX_BAKED_PER_GROUP = 4


class _ScenePixmapCache:
    """按壁纸组 LRU 的 pixmap 缓存（进程级单例，见模块尾）。"""

    def __init__(self, max_sets: int = _MAX_SETS) -> None:
        self._max_sets = max_sets
        # group_key -> OrderedDict[sub_key -> QPixmap]
        self._groups: "OrderedDict[tuple, OrderedDict[tuple, object]]" = OrderedDict()
        # 引用计数：活跃使用者（SceneLayer）数；归零后宽限期结束 clear
        self._users = 0
        self._evict_timer = None  # QTimer，归零时起，acquire 取消

    def acquire(self) -> None:
        """活跃使用者 +1（SceneLayer 构造时调用）。取消未到期的归零宽限。"""
        self._users += 1
        if self._evict_timer is not None:
            self._evict_timer.stop()
            self._evict_timer.deleteLater()
            self._evict_timer = None

    def release(self) -> None:
        """活跃使用者 -1（SceneLayer 析构时调用）；归零起宽限定时器，到点 clear。

        幂等：计数已归零时忽略（destroyed 信号最多发一次，此保护使重复
        release 也不会把计数打负）。
        """
        if self._users <= 0:
            return
        self._users -= 1
        if self._users == 0:
            self._schedule_eviction()

    def _schedule_eviction(self) -> None:
        """归零后起一次性宽限定时器（幂等：已有 pending 不重复起）。"""
        if self._evict_timer is not None:
            return
        try:
            from PyQt5.QtCore import QTimer

            timer = QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(self._evict)
            timer.start(int(_GRACE_SECONDS * 1000))
        except RuntimeError:
            # QApplication 已销毁（teardown/解释器关闭）：QTimer 无法创建，
            # 宽限释放丢失属可接受（进程退出由 OS 兜底），静默即可
            return
        self._evict_timer = timer

    def _evict(self) -> None:
        """宽限期到点：此时计数必为 0（acquire 会取消定时器），安全清空。"""
        self._evict_timer = None
        self._groups.clear()

    @staticmethod
    def _group_key(resolved_path: str):
        abs_path = os.path.abspath(resolved_path)
        try:
            mtime = os.path.getmtime(abs_path)
        except OSError:
            mtime = -1.0  # 文件缺失/特殊路径：仍可缓存（加载失败返回 null pixmap 不入缓存）
        return (abs_path, mtime)

    @staticmethod
    def _sub_key(kind, width, height, opacity_255, blur_radius, pad) -> tuple:
        return (
            kind,
            int(width),
            int(height),
            int(round(opacity_255)),  # opacity 取整到 1/255（Qt alpha 粒度）
            round(float(blur_radius), 1),
            int(pad),
        )

    def get_pixmap(
        self,
        kind: str,
        resolved_path: str,
        *,
        load: Callable[[str], object],
        width: int = 0,
        height: int = 0,
        opacity_255: int = 255,
        blur_radius: float = 0.0,
        pad: int = 0,
    ) -> object:
        """取副本：命中返回缓存对象（Qt 隐式共享，勿 .copy()），未命中建并写入。

        Args:
            kind: 副本类型（如 'source' / 'premult' / 'fg_source' / 'fg_premult' / 'baked'）
            resolved_path: **解析后的**图片路径（绝对路径化在 key 内完成）
            load: 未命中时的构建函数，入参 resolved_path，返回 QPixmap
            width/height/opacity_255/blur_radius/pad: 渲染参数，参与 key
        """
        group = self._group_key(resolved_path)
        sub = self._sub_key(kind, width, height, opacity_255, blur_radius, pad)
        bucket = self._groups.get(group)
        if bucket is not None and sub in bucket:
            self._groups.move_to_end(group)  # LRU 触碰
            if sub[0] == "baked":
                bucket.move_to_end(sub)  # baked 子键触碰：热几何不被淘汰（M2）
            return bucket[sub]
        pix = load(resolved_path)
        try:
            if pix is None or pix.isNull():
                return pix  # 加载失败不缓存（下次重试）
        except AttributeError:
            return pix
        if bucket is None:
            bucket = OrderedDict()
            self._groups[group] = bucket
            # 超 _MAX_SETS：整体淘汰最旧组
            while len(self._groups) > self._max_sets:
                self._groups.popitem(last=False)
        elif sub[0] == "baked":
            # baked 组内上限（M2）：只淘汰最旧 baked，跳过非 baked 条目
            # （source/premult 每组固定 ≤5 份，不能被 baked 挤掉）
            baked_count = 0
            for k in bucket:
                if k[0] == "baked":
                    baked_count += 1
            while baked_count >= _MAX_BAKED_PER_GROUP:
                for k in bucket:
                    if k[0] == "baked":
                        del bucket[k]
                        break
                baked_count -= 1
        bucket[sub] = pix
        return pix

    def clear(self) -> None:
        """清空全部缓存（当前无调用方：失效靠 key 语义；保留供调试/未来挂 EV）。"""
        self._groups.clear()

    def stats(self) -> dict:
        """调试用：组数与条目数。"""
        return {"groups": len(self._groups), "entries": sum(len(b) for b in self._groups.values())}


scene_pixmap_cache = _ScenePixmapCache()
