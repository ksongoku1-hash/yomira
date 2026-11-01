import { vi } from "vitest";
import * as legacy from "./readerLibrary";

// Reader UI tests use the legacy fixture store; API migration has its own boundary tests.
export function readerLibraryFixture() {
  const snapshot = () => ({ ...legacy.readReaderLibrary(), libraryId: "123e4567-e89b-42d3-a456-426614174099", migrationWarning: null });
  const saved = (ok: boolean) => {
    if (!ok) throw new Error("No se pudieron guardar los datos del lector.");
    return snapshot();
  };
  return {
    READER_LIBRARY_QUERY_KEY: ["reader-library"],
    loadReaderLibrary: vi.fn(async () => snapshot()),
    putReaderWorkPreferences: vi.fn(async (id: string, value: legacy.ReaderWorkPreferences) => saved(legacy.saveReaderWorkPreferences(id, value))),
    putReaderBookmark: vi.fn(async (value: legacy.ReaderBookmark) => saved(legacy.saveReaderBookmark(value))),
    deleteReaderBookmark: vi.fn(async (id: string, page: number) => {
      const marker = snapshot().bookmarks.find((item) => item.chapterId === id && item.pageIndex === page);
      return saved(!marker || legacy.removeReaderBookmark(marker.seriesId, id, page));
    }),
    readerLibraryError: (error: unknown) => error instanceof Error ? error.message : "No se pudieron guardar los datos del lector.",
  };
}
