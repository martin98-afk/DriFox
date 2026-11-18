/**
 * contextMenuStore：右键菜单状态（跟随鼠标的临时浮层，独立于四向卡片体系）。
 * 菜单项由调用方构造（内置项）+ 插件项合并（pluginStore.contextMenu 按 target 过滤）。
 */

import { create } from "zustand";

export type CtxTarget = "message" | "editor" | "session" | "tab";

export interface CtxMenuItem {
  key: string;
  label: string;
  danger?: boolean;
  icon?: string;
  action: () => void;
}

interface CtxMenuOpen {
  x: number;
  y: number;
  target: CtxTarget;
  targetId: string;
}

interface CtxMenuState {
  open: CtxMenuOpen | null;
  /** 内置项（调用方构造） */
  items: CtxMenuItem[];
  /** 打开菜单：坐标记录原始鼠标位，渲染时二次钳制防溢出 */
  openMenu: (o: CtxMenuOpen, items: CtxMenuItem[]) => void;
  close: () => void;
}

export const useContextMenuStore = create<CtxMenuState>((set) => ({
  open: null,
  items: [],
  openMenu: (o, items) => set({ open: o, items }),
  close: () => set({ open: null, items: [] }),
}));

/** 便捷：元素 onContextMenu 处理器构造 */
export function ctxOpenHandler(target: CtxTarget, targetId: string, items: CtxMenuItem[]) {
  return (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    useContextMenuStore.getState().openMenu({ x: e.clientX, y: e.clientY, target, targetId }, items);
  };
}
