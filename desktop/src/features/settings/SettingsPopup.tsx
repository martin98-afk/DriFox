/**
 * SettingsPopup：设置容器（§二 TOP 覆盖层系统卡，一次一张互斥）。
 * 分类侧导航骨架：通用/模型/主题/插件/安全；v1 有内容的=插件（SchemaForm）/主题（ThemeMode）/安全（SecretMode）。
 * 落位理由：复用 FloatingLayer gate 仲裁（互斥/压制/让位）与动画通道，独立 Modal 需重写且与 question 置顶层协调复杂。
 */

import { useState } from "react";
import { useFloatingStore } from "../floating/floatingStore";
import { usePluginStore } from "../../stores/pluginStore";
import { SchemaForm } from "./SchemaForm";
import { ThemeModeCard } from "./ThemeModeCard";
import { SecretModeCard } from "./SecretModeCard";

type Section = "general" | "model" | "theme" | "plugins" | "security";

const SECTIONS: Array<{ id: Section; label: string; icon: string }> = [
  { id: "general", label: "通用", icon: "⚙" },
  { id: "model", label: "模型", icon: "Z" },
  { id: "theme", label: "主题", icon: "◐" },
  { id: "plugins", label: "插件", icon: "▣" },
  { id: "security", label: "安全", icon: "🔒" },
];

export function SettingsPopup() {
  const closeCard = useFloatingStore((st) => st.closeCard);
  const plugins = usePluginStore((st) => st.plugins);
  const [section, setSection] = useState<Section>("plugins");

  return (
    <div className="SettingsPopup" role="dialog" aria-label="设置">
      <aside className="settings-nav">
        <div className="settings-nav-title">设置</div>
        {SECTIONS.map((s) => (
          <button
            key={s.id}
            type="button"
            className={`settings-nav-item${section === s.id ? " is-active" : ""}`}
            onClick={() => setSection(s.id)}
          >
            <span className="settings-nav-icon" aria-hidden>{s.icon}</span>
            {s.label}
          </button>
        ))}
      </aside>

      <div className="settings-content">
        {section === "plugins" && (
          <>
            {plugins.length === 0 && <div className="settings-empty">插件清单为空（mock 未注入）</div>}
            {plugins.map((p) => (
              <details key={p.name} className="plugin-setting-block" open={p.name === "demo-core"}>
                <summary className="plugin-setting-head">
                  <span className="plugin-setting-name">{p.name}</span>
                  <span className="plugin-setting-ver">v{p.version ?? "?"}</span>
                  <span className={`plugin-setting-state${p.enabled ? " is-on" : ""}`}>{p.enabled ? "已启用" : "已禁用"}</span>
                </summary>
                <SchemaForm plugin={p} />
              </details>
            ))}
          </>
        )}
        {section === "theme" && <ThemeModeCard />}
        {section === "security" && <SecretModeCard />}
        {(section === "general" || section === "model") && (
          <div className="settings-placeholder">
            <div className="settings-ph-line" style={{ width: "38%" }} />
            <div className="settings-ph-line" style={{ width: "66%" }} />
            <div className="settings-ph-line" style={{ width: "52%" }} />
            <div className="settings-card-desc">该分类 v1 占位（模型分类等 LlmSettings 卡后续接入）。</div>
          </div>
        )}
      </div>

      <button type="button" className="settings-close" title="关闭设置" aria-label="关闭设置" onClick={() => closeCard("settings-main")}>
        ×
      </button>
    </div>
  );
}
