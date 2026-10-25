import { afterEach, describe, expect, it, vi } from "vitest";

import { appUpdates } from "../api/client";

const status = { state: "ready", current_version: "0.1.14", latest_version: "0.1.15",
  downloaded_bytes: 100, total_bytes: 100, message: "Lista",
  releases_url: "https://github.com/ksongoku1-hash/yomira/releases" };

afterEach(() => vi.unstubAllGlobals());

describe("updates API", () => {
  it("uses the local header for mutations and parses the verified state", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => status });
    vi.stubGlobal("fetch", fetchMock);
    expect(await appUpdates()).toEqual(status);
    await appUpdates("check");
    await appUpdates("install");
    expect(fetchMock).toHaveBeenLastCalledWith("/api/v1/system/updates/install", {
      method: "POST", headers: { Accept: "application/json", "X-Yomira-Update": "1" },
    });
  });
  it.each([{ state: ["ready"] }, { releases_url: "https://evil.example" },
    { total_bytes: -1 }, { downloaded_bytes: 101 }])("rejects malformed server state %j", async (change) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ ...status, ...change }) }));
    await expect(appUpdates()).rejects.toThrow("No se pudo leer el estado");
  });
});
