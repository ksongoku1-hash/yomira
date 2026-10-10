import { describe, expect, it } from "vitest";

import { ReaderPageCache } from "./readerPageCache";
import type { CachedReaderPage } from "./readerPageCache";

function page(pageNumber: number, blobBytes: number, decodedBytes = 0): CachedReaderPage {
  return {
    chapterId: "chapter-a",
    kind: "ready",
    url: `blob:page-${pageNumber}`,
    blob: new Blob([new Uint8Array(blobBytes)]),
    decoded: decodedBytes > 0,
    decodedBytes,
  };
}

describe("ReaderPageCache", () => {
  it("evicts the least recently used pages until the byte budget is met", () => {
    const cache = new ReaderPageCache(8);
    cache.set(1, page(1, 4), new Set());
    cache.set(2, page(2, 4), new Set());
    expect(cache.get(1)?.url).toBe("blob:page-1");

    const evicted = cache.set(3, page(3, 4), new Set());

    expect(evicted.map((entry) => entry.pageNumber)).toEqual([2]);
    expect(cache.has(1)).toBe(true);
    expect(cache.has(2)).toBe(false);
    expect(cache.has(3)).toBe(true);
    expect(cache.estimatedBytes).toBe(8);
  });

  it("counts both compressed blob bytes and the decoded image estimate", () => {
    const cache = new ReaderPageCache(20);
    cache.set(1, page(1, 5, 12), new Set([1]));
    cache.set(2, page(2, 5, 12), new Set([2]));

    expect(cache.estimatedBytes).toBe(17);
    expect(cache.has(1)).toBe(false);
    expect(cache.has(2)).toBe(true);
  });

  it("keeps visible pages pinned even when they exceed the configured budget", () => {
    const cache = new ReaderPageCache(4);
    const pinned = new Set([1]);
    cache.set(1, page(1, 8, 4), pinned);
    const evicted = cache.set(2, page(2, 1), pinned);

    expect(evicted.map((entry) => entry.pageNumber)).toEqual([2]);
    expect(cache.has(1)).toBe(true);
    expect(cache.estimatedBytes).toBe(12);
  });

  it("trims immediately when the budget is reduced and returns retained entries on clear", () => {
    const cache = new ReaderPageCache(20);
    cache.set(1, page(1, 5), new Set());
    cache.set(2, page(2, 5), new Set());
    cache.set(3, page(3, 5), new Set());

    const evicted = cache.setLimit(6, new Set([3]));
    expect(evicted.map((entry) => entry.pageNumber)).toEqual([1, 2]);
    expect(cache.estimatedBytes).toBe(5);

    const cleared = cache.clear();
    expect(cleared.map((entry) => entry.pageNumber)).toEqual([3]);
    expect(cache.estimatedBytes).toBe(0);
    expect(cache.size).toBe(0);
  });

  it.each([100, 500, 2_000])(
    "keeps the LRU estimate and live page resources bounded across a %i-page journey",
    (pageCount) => {
      const byteLimit = 16_000_000;
      const pageCost = 4 * 1024 * 1024 + 3;
      const maxRetainedPages = Math.floor(byteLimit / pageCost);
      const cache = new ReaderPageCache(byteLimit);
      const liveUrls = new Set<string>();

      for (let pageNumber = 1; pageNumber <= pageCount; pageNumber += 1) {
        const resource = page(pageNumber, 3, 4 * 1024 * 1024);
        liveUrls.add(resource.url);
        const evicted = cache.set(pageNumber, resource, new Set([pageNumber]));
        for (const entry of evicted) liveUrls.delete(entry.resource.url);

        expect(cache.estimatedBytes).toBeLessThanOrEqual(byteLimit);
        expect(cache.size).toBeLessThanOrEqual(maxRetainedPages);
        expect(liveUrls.size).toBe(cache.size);
      }

      expect(cache.size).toBe(maxRetainedPages);
      expect(cache.estimatedBytes).toBe(maxRetainedPages * pageCost);

      for (const entry of cache.clear()) liveUrls.delete(entry.resource.url);
      expect(liveUrls.size).toBe(0);
      expect(cache.estimatedBytes).toBe(0);
    },
  );
});
