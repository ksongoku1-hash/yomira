export interface CachedReaderPage {
  chapterId: string;
  kind: "ready";
  url: string;
  blob: Blob;
  decoded: boolean;
  decodedBytes: number;
}

export interface EvictedReaderPage {
  pageNumber: number;
  resource: CachedReaderPage;
}

export class ReaderPageCache {
  private readonly pages = new Map<number, CachedReaderPage>();
  private byteLimit: number;
  private byteCount = 0;

  constructor(byteLimit: number) {
    this.byteLimit = Math.max(0, Math.floor(byteLimit));
  }

  get estimatedBytes(): number {
    return this.byteCount;
  }

  get size(): number {
    return this.pages.size;
  }

  has(pageNumber: number): boolean {
    return this.pages.has(pageNumber);
  }

  delete(pageNumber: number): EvictedReaderPage | undefined {
    const resource = this.pages.get(pageNumber);
    if (resource === undefined) return undefined;
    this.pages.delete(pageNumber);
    this.byteCount -= this.cost(resource);
    return { pageNumber, resource };
  }

  get(pageNumber: number): CachedReaderPage | undefined {
    const resource = this.pages.get(pageNumber);
    if (resource === undefined) return undefined;
    this.pages.delete(pageNumber);
    this.pages.set(pageNumber, resource);
    return resource;
  }

  set(
    pageNumber: number,
    resource: CachedReaderPage,
    pinnedPages: ReadonlySet<number>,
  ): EvictedReaderPage[] {
    const replaced = this.pages.get(pageNumber);
    if (replaced !== undefined) {
      this.byteCount -= this.cost(replaced);
      this.pages.delete(pageNumber);
    }
    this.pages.set(pageNumber, resource);
    this.byteCount += this.cost(resource);

    const evicted = this.trim(pinnedPages);
    if (replaced !== undefined && replaced.url !== resource.url) {
      evicted.unshift({ pageNumber, resource: replaced });
    }
    return evicted;
  }

  setLimit(byteLimit: number, pinnedPages: ReadonlySet<number>): EvictedReaderPage[] {
    this.byteLimit = Math.max(0, Math.floor(byteLimit));
    return this.trim(pinnedPages);
  }

  clear(): EvictedReaderPage[] {
    const entries = Array.from(this.pages, ([pageNumber, resource]) => ({
      pageNumber,
      resource,
    }));
    this.pages.clear();
    this.byteCount = 0;
    return entries;
  }

  private cost(resource: CachedReaderPage): number {
    const decodedBytes = Number.isFinite(resource.decodedBytes)
      ? Math.max(0, Math.floor(resource.decodedBytes))
      : 0;
    return resource.blob.size + decodedBytes;
  }

  private trim(pinnedPages: ReadonlySet<number>): EvictedReaderPage[] {
    const evicted: EvictedReaderPage[] = [];
    while (this.byteCount > this.byteLimit) {
      let candidate: [number, CachedReaderPage] | undefined;
      for (const entry of this.pages) {
        if (!pinnedPages.has(entry[0])) {
          candidate = entry;
          break;
        }
      }
      if (candidate === undefined) break;
      const [pageNumber, resource] = candidate;
      this.pages.delete(pageNumber);
      this.byteCount -= this.cost(resource);
      evicted.push({ pageNumber, resource });
    }
    return evicted;
  }
}
