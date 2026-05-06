import { create } from "zustand";

export const useImportAssistantStore = create((set) => ({
  batchId: null,
  activeTab: "plan",
  prompt: "",
  setPrompt: (prompt) => set({ prompt }),
  setBatchId: (batchId) => set({ batchId }),
  setActiveTab: (activeTab) => set({ activeTab }),
  resetFlow: () =>
    set({
      batchId: null,
      activeTab: "plan",
    }),
}));
