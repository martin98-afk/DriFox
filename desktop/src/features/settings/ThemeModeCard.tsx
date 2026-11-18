/**
 * ThemeModeCard：auto/light/dark 三态专卡，接 uiStore 主题机制实时生效（App applyTheme effect）。
 */

import { useUiStore } from "../../stores/uiStore";

const MODES: Array<{ id: "auto" | "light" | "dark"; label: string; hint: string }> = [
  { id: "auto", label: "跟随系统", hint: "auto" },
  { id: "light", label: "浅色 lumia", hint: "light" },
  { id: "dark", label: "深色 true-black", hint: "dark" },
];

export function ThemeModeCard() {
  const theme = useUiStore((st) => st.theme);
  const setTheme = useUiStore((st) => st.setTheme);

  return (
    <div className="ThemeModeCard">
      <div className="settings-card-title">主题模式</div>
      <div className="theme-mode-row">
        {MODES.map((m) => (
          <button
            key={m.id}
            type="button"
            className={`theme-mode-btn${theme === m.id ? " is-active" : ""}`}
            onClick={() => setTheme(m.id)}
          >
            <span className="theme-mode-preview" data-mode={m.id} aria-hidden />
            <span className="theme-mode-label">{m.label}</span>
            <span className="theme-mode-hint">{m.hint}</span>
          </button>
        ))}
      </div>
      <div className="settings-card-desc">切换实时生效；auto 跟随系统 prefers-color-scheme。</div>
    </div>
  );
}
