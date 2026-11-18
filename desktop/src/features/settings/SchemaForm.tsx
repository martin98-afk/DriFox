/**
 * SchemaForm（§三）：一组件吃全部 E1 config_schema 插件设置卡。
 * 8 类型映射：text/password(+显隐)/bool(Switch)/select(可搜索)/model(占位按钮)/number/link(外链非表单值)/action(mock 应答)/textarea。
 * 布局：grid 两列，label 列 max-content 自然对齐最长非 bool label；bool 行开关左置 label 右。
 * 校验三态：全填=就绪 / required 空(bool/link/action 豁免)=待配置黄徽章 / 无 schema=无配置。
 * 三级链：envOverridden 字段锁定 + 「环境变量」徽章；默认值走 placeholder。
 * 读写 mock 应答 150ms（live 走 GET/PUT /api/plugins/{name}/config，联调波次）。
 */

import { useMemo, useState } from "react";
import type { ConfigFieldDef, PluginDetail } from "../../stores/pluginStore";
import { useFloatingStore } from "../floating/floatingStore";

type SaveState = "idle" | "saving" | "saved";

function isFilled(f: ConfigFieldDef, v: unknown): boolean {
  if (f.type === "bool" || f.type === "link" || f.type === "action") return true; // 豁免
  if (f.type === "number") return v !== undefined && v !== null && String(v) !== "";
  return typeof v === "string" ? v.trim() !== "" : v !== undefined && v !== null;
}

function Switch({ checked, onChange, disabled }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      className={`schema-switch${checked ? " is-on" : ""}`}
      disabled={disabled}
      onClick={() => onChange(!checked)}
    >
      <span className="schema-switch-knob" />
    </button>
  );
}

/** select 可搜索下拉（轻量 details 实现） */
function SearchSelect({ value, options, onChange, disabled }: { value: string; options: string[]; onChange: (v: string) => void; disabled?: boolean }) {
  const [filter, setFilter] = useState("");
  const [open, setOpen] = useState(false);

  const filtered = options.filter((o) => o.toLowerCase().includes(filter.toLowerCase()));

  return (
    <details
      className="schema-select"
      open={open}
      onToggle={(e) => {
        const now = (e.target as HTMLDetailsElement).open;
        setOpen(now);
        if (now) setFilter("");
      }}
    >
      <summary className={`schema-select-summary${disabled ? " is-disabled" : ""}`}>{value || "选择…"}</summary>
      {!disabled && (
        <div className="schema-select-list">
          <input
            type="text"
            className="schema-select-filter"
            placeholder="搜索…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            autoFocus
          />
          {filtered.map((o) => (
            <button
              key={o}
              type="button"
              className={`schema-select-opt${o === value ? " is-active" : ""}`}
              onClick={() => {
                onChange(o);
                setOpen(false);
              }}
            >
              {o}
            </button>
          ))}
          {filtered.length === 0 && <div className="schema-select-empty">无匹配项</div>}
        </div>
      )}
    </details>
  );
}

export function SchemaForm({ plugin }: { plugin: PluginDetail }) {
  const pushToast = useFloatingStore((st) => st.pushToast);
  const schema = plugin.config_schema;
  const fields = schema?.fields ?? [];

  const [values, setValues] = useState<Record<string, unknown>>(() => {
    const v: Record<string, unknown> = {};
    for (const f of fields) {
      if (!f.envOverridden) v[f.key] = f.default ?? (f.type === "bool" ? false : "");
    }
    return v;
  });
  const [showPw, setShowPw] = useState<Record<string, boolean>>({});
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [actingKey, setActingKey] = useState<string | null>(null);

  const status = useMemo<"none" | "ready" | "pending">(() => {
    if (fields.length === 0) return "none";
    const missing = fields.some((f) => f.required && !isFilled(f, values[f.key]));
    return missing ? "pending" : "ready";
  }, [fields, values]);

  const setValue = (key: string, v: unknown) => setValues((prev) => ({ ...prev, [key]: v }));

  const save = async () => {
    if (saveState !== "idle") return;
    setSaveState("saving");
    await new Promise((r) => setTimeout(r, 150)); // mock 应答（live：PUT /api/plugins/{name}/config）
    setSaveState("saved");
    pushToast({ title: plugin.name, message: "配置已保存（mock 应答）", level: "info" });
    setTimeout(() => setSaveState("idle"), 2000);
  };

  const runAction = async (f: ConfigFieldDef) => {
    if (actingKey) return;
    setActingKey(f.key);
    await new Promise((r) => setTimeout(r, 150)); // mock 应答（live：POST /api/plugins/{name}/action）
    setActingKey(null);
    pushToast({ title: plugin.name, message: `「${f.label}」已执行（mock 应答）`, level: "info" });
  };

  if (fields.length === 0) {
    return <div className="schema-none">无配置项（无 config_schema）</div>;
  }

  const renderControl = (f: ConfigFieldDef) => {
    const locked = f.envOverridden === true;
    const v = values[f.key];
    switch (f.type) {
      case "text":
        return (
          <input
            type="text"
            className="schema-input"
            value={String(v ?? "")}
            placeholder={locked ? "由环境变量提供" : f.placeholder ?? ""}
            disabled={locked}
            onChange={(e) => setValue(f.key, e.target.value)}
          />
        );
      case "password":
        return (
          <span className="schema-pw-wrap">
            <input
              type={showPw[f.key] ? "text" : "password"}
              className="schema-input"
              value={String(v ?? "")}
              placeholder={locked ? "由环境变量提供" : f.placeholder ?? ""}
              disabled={locked}
              onChange={(e) => setValue(f.key, e.target.value)}
            />
            <button
              type="button"
              className="schema-pw-eye"
              title={showPw[f.key] ? "隐藏" : "显示"}
              onClick={() => setShowPw((prev) => ({ ...prev, [f.key]: !prev[f.key] }))}
            >
              {showPw[f.key] ? "🙈" : "👁"}
            </button>
          </span>
        );
      case "bool":
        return <Switch checked={v === true} onChange={(b) => setValue(f.key, b)} disabled={locked} />;
      case "select":
        return <SearchSelect value={String(v ?? "")} options={f.options ?? []} onChange={(x) => setValue(f.key, x)} disabled={locked} />;
      case "model":
        return (
          <button
            type="button"
            className="schema-model-btn"
            disabled={locked}
            onClick={() => pushToast({ title: "模型选择器", message: "弹层选型 M3 接入", level: "info" })}
          >
            <span className="model-z">Z</span>
            {String(v ?? "选择模型")}
          </button>
        );
      case "number":
        return (
          <input
            type="number"
            className="schema-input"
            value={v === undefined || v === null ? "" : String(v)}
            placeholder={f.placeholder ?? ""}
            disabled={locked}
            onChange={(e) => setValue(f.key, e.target.value === "" ? undefined : Number(e.target.value))}
          />
        );
      case "link":
        return (
          <button type="button" className="schema-link-btn" onClick={() => window.open(f.url ?? String(v ?? ""), "_blank")}>
            打开链接 ↗
          </button>
        );
      case "action":
        return (
          <button type="button" className="schema-action-btn" disabled={actingKey === f.key} onClick={() => void runAction(f)}>
            {actingKey === f.key ? "执行中…" : f.label}
          </button>
        );
      case "textarea":
        return (
          <textarea
            className="schema-textarea"
            rows={3}
            value={String(v ?? "")}
            placeholder={locked ? "由环境变量提供" : f.placeholder ?? ""}
            disabled={locked}
            onChange={(e) => setValue(f.key, e.target.value)}
          />
        );
    }
  };

  return (
    <div className="SchemaForm">
      <div className="schema-status-row">
        <span className={`schema-status is-${status}`}>
          {status === "ready" ? "就绪" : status === "pending" ? "待配置" : "无配置"}
        </span>
        <button type="button" className="schema-save" onClick={() => void save()} disabled={saveState !== "idle"}>
          {saveState === "saving" ? "保存中…" : saveState === "saved" ? "已保存 ✓" : "保存"}
        </button>
      </div>

      <div className="schema-grid">
        {fields.map((f) => {
          const isBool = f.type === "bool";
          const isAction = f.type === "action";
          const isWide = f.type === "textarea";
          return (
            <div key={f.key} className={`schema-row${isWide ? " is-wide" : ""}${isBool ? " is-bool" : ""}`}>
              {!isBool && !isAction && (
                <label className="schema-label" title={f.env ? `环境变量 ${f.env}` : undefined}>
                  {f.label}
                  {f.required && <span className="schema-req" aria-hidden>*</span>}
                  {f.envOverridden && (
                    <span className="schema-env-badge" title={`环境变量 ${f.env} 覆盖，锁定编辑`}>
                      环境变量
                    </span>
                  )}
                </label>
              )}
              <div className="schema-control">
                {isBool ? (
                  <span className="schema-bool-row">
                    {renderControl(f)}
                    <span className="schema-label">
                      {f.label}
                      {f.envOverridden && <span className="schema-env-badge">环境变量</span>}
                    </span>
                  </span>
                ) : (
                  renderControl(f)
                )}
                {f.description && <div className="schema-desc">{f.description}</div>}
              </div>
            </div>
          );
        })}
      </div>

      {plugin.enabled === false && <div className="schema-disabled-hint">插件已禁用，配置仅展示</div>}
    </div>
  );
}
