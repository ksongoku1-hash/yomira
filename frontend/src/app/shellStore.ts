import { create } from "zustand";

interface ShellState {
  isNavigationOpen: boolean;
  toggleNavigation: () => void;
  closeNavigation: () => void;
}

export const useShellStore = create<ShellState>((set) => ({
  isNavigationOpen: false,
  toggleNavigation: () => set((state) => ({ isNavigationOpen: !state.isNavigationOpen })),
  closeNavigation: () => set({ isNavigationOpen: false }),
}));
