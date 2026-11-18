/**
 * 配置域 store：模型配置 / agents / 输入历史。
 */

import { create } from "zustand";
import { restApi } from "../api/rest";
import type { AgentInfo, InputHistoryItem, ModelConfig } from "../api/rest";

interface ConfigState {
  modelConfig: ModelConfig | null;
  agents: AgentInfo[];
  inputHistory: InputHistoryItem[];
  loadModelConfig: () => Promise<void>;
  /** 保存并回写服务端确认后的配置 */
  saveModelConfig: (cfg: ModelConfig) => Promise<void>;
  loadAgents: () => Promise<void>;
  loadInputHistory: () => Promise<void>;
}

export const useConfigStore = create<ConfigState>((set) => ({
  modelConfig: null,
  agents: [],
  inputHistory: [],
  loadModelConfig: async () => {
    set({ modelConfig: await restApi.getModelConfig() });
  },
  saveModelConfig: async (cfg) => {
    set({ modelConfig: await restApi.putModelConfig(cfg) });
  },
  loadAgents: async () => {
    set({ agents: await restApi.listAgents() });
  },
  loadInputHistory: async () => {
    set({ inputHistory: await restApi.getInputHistory() });
  },
}));
