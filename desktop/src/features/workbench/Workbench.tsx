/**
 * Workbench（§三）：主区右侧并列侧板（宽窗分栏 / 窄窗降级覆盖抽屉，CSS media query）。
 * 页签条即头部（无额外标题行）；v1 单「工作台」页签 + WorkspaceTree 骨架
 * （项目→工作树两层 mock 树、行 hover「管理工作树」入口占位、点击切 workdir mock）。
 * 页签右键：关闭/关闭其他/弹出独立窗口（mock toast，真实 multiwindow M4）。
 */

import { useState } from "react";
import { useFloatingStore } from "../floating/floatingStore";
import { useWorkbenchStore } from "./workbenchStore";
import { ctxOpenHandler } from "../shell/contextMenuStore";
import type { CtxMenuItem } from "../shell/contextMenuStore";

/** mock 两层树：项目 → 工作树 */
const MOCK_TREES: Array<{ project: string; trees: string[] }> = [
  { project: "DriFox", trees: ["main", "tauri"] },
  { project: "助手中心", trees: ["main", "persona-lab"] },
];

function toast(msg: string): void {
  useFloatingStore.getState().pushToast({ title: "工作台", message: msg, level: "info" });
}

export function Workbench() {
  const open = useWorkbenchStore((st) => st.open);
  const tabs = useWorkbenchStore((st) => st.tabs);
  const activeTabId = useWorkbenchStore((st) => st.activeTabId);
  const setActive = useWorkbenchStore((st) => st.setActive);
  const closeTab = useWorkbenchStore((st) => st.closeTab);
  const closeOthers = useWorkbenchStore((st) => st.closeOthers);
  const setOpen = useWorkbenchStore((st) => st.setOpen);

  if (!open) return null;

  // tab 页签右键内置项（§四 tab target）
  const tabMenuItems = (tabId: string): CtxMenuItem[] => [
    { key: "close", label: "关闭", action: () => closeTab(tabId) },
    { key: "close-others", label: "关闭其他", action: () => closeOthers(tabId) },
    {
      key: "popout",
      label: "弹出独立窗口",
      action: () => toast("弹出独立窗口（Tauri multiwindow M4 接入，mock）"),
    },
  ];

  return (
    <aside className="Workbench" aria-label="工作台侧板">
      <div className="workbench-tabs">
        {tabs.map((t) => (
          <div
            key={t.id}
            className={`workbench-tab${activeTabId === t.id ? " is-active" : ""}`}
            role="tab"
            tabIndex={0}
            aria-selected={activeTabId === t.id}
            onClick={() => setActive(t.id)}
            onKeyDown={(e) => {
              if (e.key === "Enter") setActive(t.id);
            }}
            onContextMenu={ctxOpenHandler("tab", t.id, tabMenuItems(t.id))}
          >
            {t.label}
            {tabs.length > 1 && (
              <button
                type="button"
                className="workbench-tab-close"
                title="关闭页签"
                aria-label={`关闭 ${t.label}`}
                onClick={(e) => {
                  e.stopPropagation();
                  closeTab(t.id);
                }}
              >
                ×
              </button>
            )}
          </div>
        ))}
        <button type="button" className="workbench-collapse" title="收起侧板" onClick={() => setOpen(false)} aria-label="收起工作台">
          »
        </button>
      </div>

      <div className="workbench-body">{activeTabId === "workbench" && <WorkspaceTree />}</div>
    </aside>
  );
}

/** WorkspaceTree 骨架：项目→工作树两层 mock；行 hover「管理工作树」入口占位；点击切 workdir mock */
function WorkspaceTree() {
  const [managed, setManaged] = useState<string | null>(null);

  const switchWorkdir = (project: string, tree: string) => {
    toast(`工作目录已切换：${project} @ ${tree}（mock）`);
  };

  return (
    <div className="WorkspaceTree">
      {MOCK_TREES.map((p) => (
        <div key={p.project} className="wt-project">
          <div className="wt-project-head">
            <span className="group-logo" aria-hidden>DF</span>
            <span className="wt-project-name">{p.project}</span>
          </div>
          {p.trees.map((tree) => (
            <div
              key={tree}
              className="wt-tree-row"
              role="button"
              tabIndex={0}
              onClick={() => switchWorkdir(p.project, tree)}
              onKeyDown={(e) => {
                if (e.key === "Enter") switchWorkdir(p.project, tree);
              }}
            >
              <span className="wt-tree-icon" aria-hidden>⑂</span>
              <span className="wt-tree-name">{tree}</span>
              <button
                type="button"
                className="wt-manage-btn"
                title="管理工作树"
                onClick={(e) => {
                  e.stopPropagation();
                  setManaged(`${p.project}/${tree}`);
                  toast(`管理工作树入口（${p.project}/${tree}，占位）`);
                }}
              >
                管理
              </button>
            </div>
          ))}
        </div>
      ))}
      <div className="settings-card-desc">
        工作树只读展示；管理操作归右键菜单（真实数据 M3 接入）。{managed && `最近管理：${managed}`}
      </div>
    </div>
  );
}
