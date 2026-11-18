/**
 * ContextMenu（§四）：portal 渲染防父容器裁剪；内置项在前/分隔线/插件项在后带 icon；
 * item 高 32px、hover --surface-hover、danger --danger、宽自适应 max 280px；
 * 全局点击/Esc/滚轮关闭；边界内二次钳制防溢出屏幕。
 */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useContextMenuStore } from "./contextMenuStore";
import { usePluginStore } from "../../stores/pluginStore";
import { useFloatingStore } from "../floating/floatingStore";
import type { CtxMenuItem } from "./contextMenuStore";

export function ContextMenu() {
  const open = useContextMenuStore((st) => st.open);
  const items = useContextMenuStore((st) => st.items);
  const close = useContextMenuStore((st) => st.close);
  const pluginMenu = usePluginStore((st) => st.contextMenu);

  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ x: 0, y: 0 });

  // 全局关闭：左/右键点击、Esc、滚轮
  useEffect(() => {
    if (!open) return;
    const onDown = () => close();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    const onWheel = () => close();
    window.addEventListener("mousedown", onDown);
    window.addEventListener("contextmenu", onDown);
    window.addEventListener("keydown", onKey);
    window.addEventListener("wheel", onWheel, { passive: true });
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("contextmenu", onDown);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("wheel", onWheel);
    };
  }, [open, close]);

  // 边界内二次钳制（渲染后测量）
  useLayoutEffect(() => {
    if (!open || !ref.current) return;
    const el = ref.current;
    setPos({
      x: Math.min(open.x, window.innerWidth - el.offsetWidth - 8),
      y: Math.min(open.y, window.innerHeight - el.offsetHeight - 8),
    });
  }, [open, items]);

  if (!open) return null;

  // 插件项合并（§四）：内置项在前、分隔线、插件项在后带 icon；点击 mock 上行 context.action
  const pluginItems: CtxMenuItem[] = pluginMenu
    .filter((def) => def.targets.includes(open.target))
    .map((def) => ({
      key: `plugin:${def.name}`,
      label: def.name,
      icon: def.icon,
      action: () => {
        useFloatingStore.getState().pushToast({
          title: "context.action",
          message: `已上行 ${def.name}（target=${open.target}:${open.targetId}，mock）`,
          level: "info",
        });
      },
    }));

  const menu = (
    <div
      ref={ref}
      className="ContextMenu"
      style={{ left: pos.x, top: pos.y }}
      role="menu"
      onContextMenu={(e) => e.preventDefault()}
      onMouseDown={(e) => e.stopPropagation()}
    >
      {items.map((it) => (
        <button
          key={it.key}
          type="button"
          role="menuitem"
          className={`ctx-item${it.danger ? " is-danger" : ""}`}
          onClick={() => {
            close();
            it.action();
          }}
        >
          {it.icon && <span className="ctx-item-icon" aria-hidden>{it.icon}</span>}
          {it.label}
        </button>
      ))}
      {items.length > 0 && pluginItems.length > 0 && <div className="ctx-sep" role="separator" />}
      {pluginItems.map((it) => (
        <button
          key={it.key}
          type="button"
          role="menuitem"
          className="ctx-item is-plugin"
          onClick={() => {
            close();
            it.action();
          }}
        >
          {it.icon && <span className="ctx-item-icon" aria-hidden>{it.icon}</span>}
          {it.label}
        </button>
      ))}
    </div>
  );

  return createPortal(menu, document.body);
}
