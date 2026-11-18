/**
 * 插件域 store：清单 / 启停 / UI 槽注册物占位。
 *
 * 槽位类型对齐主程序 register_ui 扩展点（sidebar/floating_card/welcome_tab 等），
 * remote-ui 通道打通后由插件注册物填充，当前仅占位建模。
 */

import { create } from "zustand";
import { restApi } from "../api/rest";
import type { PluginInfo } from "../api/rest";

export type PluginSlotKind =
  | "sidebar"
  | "floating_card"
  | "welcome_tab"
  | "input_button"
  | "content_renderer"
  | "footer_stat"
  | "footer_action"
  | "settings_card"
  | "window";

export interface PluginSlotEntry {
  id: string;
  plugin: string;
  kind: PluginSlotKind;
  meta?: Record<string, unknown>;
}

// ---------- SchemaForm 契约（E1 config_schema，F5） ----------

export type ConfigFieldType =
  | "text"
  | "password"
  | "bool"
  | "select"
  | "model"
  | "number"
  | "link"
  | "action"
  | "textarea";

export interface ConfigFieldDef {
  key: string;
  label: string;
  type: ConfigFieldType;
  default?: unknown;
  /** 环境变量名（三级链展示用） */
  env?: string;
  /** mock：env 已覆盖该字段（锁定态 + 来源徽章） */
  envOverridden?: boolean;
  placeholder?: string;
  description?: string;
  options?: string[];
  /** link 类型外链地址 */
  url?: string;
  required?: boolean;
}

export interface ConfigSchema {
  title?: string;
  fields: ConfigFieldDef[];
}

export interface PluginDetail extends PluginInfo {
  config_schema?: ConfigSchema;
}

/** 右键菜单插件项 fixture（§四合并规则：内置前/分隔线/插件项后） */
export interface ContextMenuItemDef {
  name: string;
  targets: string[];
  icon?: string;
}

interface PluginState {
  plugins: PluginDetail[];
  loading: boolean;
  slots: PluginSlotEntry[];
  /** 右键菜单插件项（登录后一次性拉取缓存语义，mock 注入） */
  contextMenu: ContextMenuItemDef[];
  loadPlugins: () => Promise<void>;
  /** mock fixture 注入（3 插件覆盖 8 类型 + action + 右键菜单清单），F5/F6 */
  loadMockPlugins: () => void;
  /** REST 启停 + 本地同步 */
  setEnabled: (name: string, enabled: boolean) => Promise<void>;
  /** 同 id 覆盖注册（热更新语义） */
  registerSlot: (entry: PluginSlotEntry) => void;
  unregisterSlot: (id: string) => void;
}

export const usePluginStore = create<PluginState>((set) => ({
  plugins: [],
  loading: false,
  slots: [],
  contextMenu: [],

  loadPlugins: async () => {
    set({ loading: true });
    try {
      set({ plugins: (await restApi.listPlugins()) as PluginDetail[], loading: false });
    } catch (e) {
      set({ loading: false });
      throw e;
    }
  },

  loadMockPlugins: () => {
    set({ plugins: buildMockPlugins(), contextMenu: buildMockContextMenu() });
  },

  setEnabled: async (name, enabled) => {
    if (enabled) {
      await restApi.enablePlugin(name);
    } else {
      await restApi.disablePlugin(name);
    }
    set((st) => ({ plugins: st.plugins.map((p) => (p.name === name ? { ...p, enabled } : p)) }));
  },

  registerSlot: (entry) =>
    set((st) => ({ slots: [...st.slots.filter((s) => s.id !== entry.id), entry] })),

  unregisterSlot: (id) => set((st) => ({ slots: st.slots.filter((s) => s.id !== id) })),
}));

// ---------- mock fixture：右键菜单插件项（按 target 过滤合并） ----------

function buildMockContextMenu(): ContextMenuItemDef[] {
  return [
    { name: "用助手解释", targets: ["message"], icon: "💡" },
    { name: "翻译为英文", targets: ["message", "editor"], icon: "译" },
    { name: "同步分享到云", targets: ["session"], icon: "☁" },
  ];
}

function buildMockPlugins(): PluginDetail[] {
  return [
    {
      name: "demo-core",
      enabled: true,
      version: "1.4.0",
      description: "演示插件：覆盖全部 8 类型字段",
      config_schema: {
        title: "演示插件设置",
        fields: [
          { key: "plugin_name", label: "插件实例名称", type: "text", default: "my-demo", required: true, placeholder: "实例名", description: "用于日志与遥测标识" },
          { key: "api_key", label: "API 密钥", type: "password", default: "", env: "DEMO_API_KEY", envOverridden: true, required: true, description: "已被环境变量覆盖，锁定编辑" },
          { key: "model", label: "默认模型", type: "model", default: "glm-5.3-flash", description: "点击打开模型选择器（M3 接入）" },
          { key: "endpoint", label: "服务端点", type: "select", options: ["https://api.example.com", "https://mirror.example.com", "http://127.0.0.1:8765"], default: "https://api.example.com", description: "下拉可搜索" },
          { key: "max_tokens", label: "最大 Token", type: "number", default: 4096, placeholder: "1024-32768" },
          { key: "homepage", label: "项目主页", type: "link", url: "https://example.com", description: "外链按钮，非表单值" },
          { key: "debug_mode", label: "调试模式", type: "bool", default: false, description: "开启后输出详细日志" },
          { key: "prompt_tpl", label: "提示词模板", type: "textarea", default: "", placeholder: "多行模板…", description: "支持变量插值" },
          { key: "reindex", label: "重建索引", type: "action", description: "动作按钮，POST /action mock 应答" },
        ],
      },
    },
    {
      name: "theme-lite",
      enabled: true,
      version: "0.9.2",
      description: "轻量主题扩展",
      config_schema: {
        title: "轻量主题扩展",
        fields: [
          { key: "accent", label: "强调色名", type: "select", options: ["blue", "green", "purple"], default: "blue", required: true },
          { key: "mono_icons", label: "单色图标", type: "bool", default: true },
        ],
      },
    },
    {
      name: "gateway-relay",
      enabled: false,
      version: "2.0.1",
      description: "网关中继（禁用态示例）",
      config_schema: {
        title: "网关中继设置",
        fields: [
          { key: "relay_token", label: "中继令牌", type: "password", default: "", required: true, placeholder: "必填", description: "清空后可见待配置黄徽章" },
          { key: "relay_host", label: "中继主机", type: "text", default: "relay.example.com", env: "RELAY_HOST" },
          { key: "heartbeat_sec", label: "心跳间隔秒", type: "number", default: 30 },
        ],
      },
    },
  ];
}
