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

淘汰策略：按「壁纸组」整体 LRU（path+mtime 为组键），_MAX_SETS=2 组封顶
（I4：scene 层与 window/sidebar 层接入后允许「场景图 + 区块图」两组共存，
共图主题仍合为 1 组；_MAX_SETS=1 时 scene 层 resize 烘焙热路径会被
area 冷组挤掉反复重解码）。一套 ~103MB → 缓存上界 ~206MB（仅异图双壁纸
主题触顶）；切主题时旧组淘汰、新组重解码。
不提供显式 invalidate：换壁纸 = path/mtime 变化 → 新组进、旧组出；
同壁纸换主题但渲染参数未变时命中缓存恰是正确行为（显示本就该一致）。

生命周期（引用计数）：每个活跃 SceneLayer 在构造时 acquire、析构时 release。
计数归零（全部窗口关闭）立即 clear，不等宽限：托盘驻留（setQuitOnLastWindow-
Closed(False)，关全部窗进程仍在）下缓存 103-127MB 不再滞留；代价是重开窗
需重解码一次。窗口仍打开时计数 >0，任何路径都不会提前淘汰。
"""

from __future__ import annotations

import os
from collections import OrderedDict
from typing import Callable, Optional

# 缓存组数上界：2（I4 放开；T8 曾收紧为 1=仅当前壁纸，但 window/sidebar
# 区块层接入共享缓存后，异图双壁纸主题会形成「场景图 + 区块图」两组，
# 1 组封顶会让 scene 层 resize 烘焙热路径被 area 冷组反复挤掉重解码。
# 共图主题两组 key 相同仍合为 1 组；一套全副本 ≈ 103-127MB（T40 核算），
# 4K baked 由 _BAKED_BUDGET_BYTES 降为 1 份（31.64 MiB）；上界 ~254MB
# 仅在「异图双壁纸 + 4K」极端主题触顶，实际内置主题均为共图或区块小图。
_MAX_SETS = 2

# 每组 baked 条目上限（M2）：blur 壁纸拖拽 resize 按 (w,h,blur,pad) 逐帧写入，
# 实测 40 步拖拽堆 40 份/330MB（1080p）且相邻几何命中率 0/39。取 4 = 覆盖
# ≤3 窗活跃几何 + 1 份回弹缓冲。
_MAX_BAKED_PER_GROUP = 4

# baked 组内驻留字节预算（M-1c）：硬上限 4 份只对 1080p 合理（31.6 MiB），
# 4K 全幅单份 31.64 MiB × 4 = 126.6 MiB。按单份实际像素面积反算条数：
# 1080p→4（现状零变化）/ 1440p→3（42.2 MiB）/ 4K→1（31.64 MiB）。
# 不用 dpr 判据：baked 尺寸取逻辑像素，4K 物理屏 dpr=1 才是 +131MB 那档，
# dpr 越高逻辑尺寸越小、单份越小，按 dpr 降档恰好放过真凶。
_BAKED_BUDGET_BYTES = 48 * 1024 * 1024


def baked_cap(width: int, height: int, budget: int = _BAKED_BUDGET_BYTES) -> int:
    """按单份 baked 像素面积反算组内条目上限（纯函数，便于单测）。

    ARGB32 单份字节 = w×h×4；非法/非正尺寸按硬上限处理（不因脏参数缩容）。
    """
    try:
        per = int(width) * int(height) * 4
    except (TypeError, ValueError):
        return _MAX_BAKED_PER_GROUP
    if per <= 0:
        return _MAX_BAKED_PER_GROUP
    return max(1, min(_MAX_BAKED_PER_GROUP, int(budget // per)))


class _ScenePixmapCache:
    """按壁纸组 LRU 的 pixmap 缓存（进程级单例，见模块尾）。"""

    def __init__(self, max_sets: int = _MAX_SETS) -> None:
        self._max_sets = max_sets
        # group_key -> OrderedDict[sub_key -> QPixmap]
        self._groups: "OrderedDict[tuple, OrderedDict[tuple, object]]" = OrderedDict()
        # 引用计数：活跃使用者（SceneLayer）数；归零即 clear
        self._users = 0

    def acquire(self) -> None:
        """活跃使用者 +1（SceneLayer 构造时调用）。"""
        self._users += 1

    def release(self) -> None:
        """活跃使用者 -1（SceneLayer 析构时调用）；归零立即 clear。

        幂等：计数已归零时忽略（destroyed 信号最多发一次，此保护使重复
        release 也不会把计数打负）。
        """
        if self._users <= 0:
            return
        self._users -= 1
        if self._users == 0:
            try:
                self._groups.clear()
            except RuntimeError:
                # QApplication 已销毁（teardown/解释器关闭）：QPixmap 析构不可用，
                # 进程退出由 OS 兜底，静默即可
                pass

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
            # baked 组内上限（M2 + M-1c 面积预算）：只淘汰最旧 baked，跳过非
            # baked 条目（source/premult 每组固定 ≤5 份，不能被 baked 挤掉）
            cap = baked_cap(width, height)
            baked_count = 0
            for k in bucket:
                if k[0] == "baked":
                    baked_count += 1
            while baked_count >= cap:
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
