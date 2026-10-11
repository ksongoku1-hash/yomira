import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

beforeEach(() => {
  localStorage.clear();
  vi.resetModules();
});

afterEach(() => vi.restoreAllMocks());

describe("appearance preferences", () => {
  it("restores older preferences and persists the performance mode", async () => {
    localStorage.setItem("yomira.appearance.v1", JSON.stringify({ enabled: false, intensity: 35 }));
    const { useAppearanceStore } = await import("./appearanceStore");
    expect(useAppearanceStore.getState()).toMatchObject({ enabled: false, intensity: 35, simpleGraphics: false });
    useAppearanceStore.getState().update({ simpleGraphics: true });
    vi.resetModules();
    const fresh = await import("./appearanceStore");
    expect(fresh.useAppearanceStore.getState()).toMatchObject({ enabled: false, intensity: 35, simpleGraphics: true });
  });
  it("persists the switch and intensity and restores them on a fresh load", async () => {
    const { useAppearanceStore } = await import("./appearanceStore");
    useAppearanceStore.getState().update({ intensity: 25, enabled: false });
    expect(JSON.parse(localStorage.getItem("yomira.appearance.v1")!))
      .toEqual({ intensity: 25, enabled: false, simpleGraphics: false });
    vi.resetModules();
    const fresh = await import("./appearanceStore");
    expect(fresh.useAppearanceStore.getState()).toMatchObject({ intensity: 25, enabled: false });
  });

  it.each(["not json", '{"enabled":"false","intensity":20}', '{"enabled":true,"intensity":101}',
    '{"enabled":true,"intensity":null}'])("ignores invalid saved data: %s", async (saved) => {
    localStorage.setItem("yomira.appearance.v1", saved);
    const { useAppearanceStore } = await import("./appearanceStore");
    expect(useAppearanceStore.getState()).toMatchObject({ enabled: true, intensity: 60 });
  });

  it("rejects out of range, fractional and nonfinite values", async () => {
    const { useAppearanceStore } = await import("./appearanceStore");
    for (const intensity of [-1, 101, 0.5, NaN, Infinity]) {
      useAppearanceStore.getState().update({ intensity });
      expect(useAppearanceStore.getState().intensity).toBe(60);
    }
    useAppearanceStore.getState().update({ intensity: 0 });
    expect(useAppearanceStore.getState().intensity).toBe(0);
  });

  it("keeps the controls usable if storage is blocked and reports failed saves", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("Blocked"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Quota"); });
    const { useAppearanceStore } = await import("./appearanceStore");
    useAppearanceStore.getState().update({ enabled: false });
    expect(useAppearanceStore.getState()).toMatchObject({ enabled: false, storageError: true });
  });
});
