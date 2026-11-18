/**
 * workbenchStore：工作台侧板开合与页签记忆（§三）。
 * 关闭侧板记录当前页签，重开恢复；越界保护：恢复页签已关→回退第一可用。
 * features 内自建（stores/ 边界：F6 仅允许 pluginStore fixture 扩展）。
 */

import { create } from "zustand";

export interface WorkbenchTab {
  id: string;
  label: string;
}

interface WorkbenchState {
  open: boolean;
  tabs: WorkbenchTab[];
  activeTabId: string;
  /** 关闭侧板前的页签记忆 */
  rememberedTabId: string;
  setOpen: (open: boolean) => void;
  toggle: () => void;
  closeTab: (id: string) => void;
  closeOthers: (id: string) => void;
  setActive: (id: string) => void;
}

export const useWorkbenchStore = create<WorkbenchState>((set, get) => ({
  open: false,
  tabs: [{ id: "workbench", label: "工作台" }],
  activeTabId: "workbench",
  rememberedTabId: "workbench",

  setOpen: (open) => {
    if (open) {
      // 重开恢复记忆页签；越界回退第一可用（:929）
      const tabs = get().tabs;
      const remembered = get().rememberedTabId;
      const active = tabs.some((t) => t.id === remembered) ? remembered : (tabs[0]?.id ?? "workbench");
      set({ open, activeTabId: active });
    } else {
      set({ open, rememberedTabId: get().activeTabId });
    }
  },

  toggle: () => get().setOpen(!get().open),

  closeTab: (id) => {
    const tabs = get().tabs.filter((t) => t.id !== id);
    if (tabs.length === 0) {
      // 全关 = 收起侧板
      get().setOpen(false);
      return;
    }
    const activeTabId = get().activeTabId === id ? (tabs[0]?.id ?? "") : get().activeTabId;
    set({ tabs, activeTabId });
  },

  closeOthers: (id) => {
    const keep = get().tabs.find((t) => t.id === id);
    if (!keep) return;
    set({ tabs: [keep], activeTabId: id });
  },

  setActive: (id) => set({ activeTabId: id }),
}));
