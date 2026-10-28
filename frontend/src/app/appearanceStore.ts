import { create } from "zustand";

const STORAGE_KEY = "yomira.appearance.v1";
export type InterfaceTheme = "tinta" | "papel";
const defaults = { enabled: true, intensity: 60, simpleGraphics: false, theme: "tinta" as InterfaceTheme };
type Preferences = typeof defaults;

interface AppearanceState extends Preferences {
  storageError: boolean;
  update: (preferences: Partial<Preferences>) => void;
}

function valid(preferences: Preferences): boolean {
  return typeof preferences.enabled === "boolean" &&
    (preferences.theme === "tinta" || preferences.theme === "papel") &&
    typeof preferences.simpleGraphics === "boolean" &&
    Number.isInteger(preferences.intensity) &&
    preferences.intensity >= 0 && preferences.intensity <= 100;
}

function readPreferences(): Preferences {
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "null");
    if (saved && typeof saved === "object" && "enabled" in saved && "intensity" in saved) {
      if (typeof saved.enabled === "boolean" && typeof saved.intensity === "number") {
        const preferences = { enabled: saved.enabled, intensity: saved.intensity,
          theme: "theme" in saved && saved.theme === "papel" ? "papel" as const : "tinta" as const,
          simpleGraphics: "simpleGraphics" in saved && typeof saved.simpleGraphics === "boolean"
            ? saved.simpleGraphics : false };
        if (valid(preferences)) return preferences;
      }
    }
  } catch {
    // Storage can be unavailable; appearance must never block reading.
  }
  return defaults;
}

export const useAppearanceStore = create<AppearanceState>((set, get) => ({
  ...readPreferences(),
  storageError: false,
  update: (update) => {
    const { enabled, intensity, simpleGraphics, theme } = get();
    const preferences = { enabled, intensity, simpleGraphics, theme, ...update };
    if (!valid(preferences)) return;
    let storageError = false;
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(preferences));
    } catch {
      storageError = true;
    }
    set({ ...preferences, storageError });
  },
}));
