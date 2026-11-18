/**
 * SecretModeCard：隐私模式开关专卡（开关态；mock 语义=隐藏会话内容预览，v1 仅状态与反馈）。
 */

import { useState } from "react";
import { useFloatingStore } from "../floating/floatingStore";

export function SecretModeCard() {
  const [on, setOn] = useState(false);
  const pushToast = useFloatingStore((st) => st.pushToast);

  const toggle = () => {
    const next = !on;
    setOn(next);
    pushToast({
      title: "隐私模式",
      message: next ? "已开启：会话内容预览将脱敏显示（示例语义）" : "已关闭",
      level: next ? "warn" : "info",
    });
  };

  return (
    <div className="SecretModeCard">
      <div className="settings-card-title">隐私模式</div>
      <div className="secret-row">
        <span className="secret-label">{on ? "已开启" : "已关闭"}</span>
        <button type="button" role="switch" aria-checked={on} className={`schema-switch${on ? " is-on" : ""}`} onClick={toggle}>
          <span className="schema-switch-knob" />
        </button>
      </div>
      <div className="settings-card-desc">开启后敏感内容在预览中脱敏（v1 占位语义）。</div>
    </div>
  );
}
