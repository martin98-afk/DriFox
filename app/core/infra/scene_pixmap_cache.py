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
（当前主题 + 上一主题），一套 ~103MB → 缓存上界 ~206MB。
不提供显式 invalidate：换壁纸 = path/mtime 变化 → 新组进、旧组出；
同壁纸换主题但渲染参数未变时命中缓存恰是正确行为（显示本就该一致）。
"""

from __future__ import annotations

import os
from collections import OrderedDict
from typing import Callable, Optional

# 缓存组数上界：当前主题 + 上一主题。一套全副本 ≈ 103-127MB（T40 核算），
# 2 组 ≈ 206-254MB 封顶——用 ~25% 的常驻上限换掉 10 窗 ×103MB 的线性膨胀。
_MAX_SETS = 2


class _ScenePixmapCache:
    """按壁纸组 LRU 的 pixmap 缓存（进程级单例，见模块尾）。"""

    def __init__(self, max_sets: int = _MAX_SETS) -> None:
        self._max_sets = max_sets
        # group_key -> OrderedDict[sub_key -> QPixmap]
        self._groups: "OrderedDict[tuple, OrderedDict[tuple, object]]" = OrderedDict()

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
        bucket[sub] = pix
        return pix

    def clear(self) -> None:
        """清空全部缓存（当前无调用方：失效靠 key 语义；保留供调试/未来挂 EV）。"""
        self._groups.clear()

    def stats(self) -> dict:
        """调试用：组数与条目数。"""
        return {"groups": len(self._groups), "entries": sum(len(b) for b in self._groups.values())}


scene_pixmap_cache = _ScenePixmapCache()
