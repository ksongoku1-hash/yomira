import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { queryPollingInterval } from "./queryPolling";

afterEach(() => vi.restoreAllMocks());
describe("polling while idle", () => {
  it("uses fifteen seconds while idle and two seconds while a download is active", () => {
    const client = new QueryClient();
    expect(queryPollingInterval(client)).toBe(15_000);
    client.setQueryData(["download-jobs", "completed"], {data:[], meta:{states:{COMPLETED:20, DOWNLOADING:1}}});
    expect(queryPollingInterval(client)).toBe(2_000);
    client.setQueryData(["download-jobs", "completed"], {data:[], meta:{states:{COMPLETED:21}}});
    expect(queryPollingInterval(client)).toBe(15_000);
    client.clear();
  });
  it("does not poll while the tab is hidden", () => {
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    expect(queryPollingInterval(new QueryClient())).toBe(false);
  });
});
