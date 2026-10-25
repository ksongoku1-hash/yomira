import type { ConnectorChapter } from "./client";

export function compareConnectorChapters(left: ConnectorChapter, right: ConnectorChapter): number {
  for (let index = 0; index < Math.max(left.chapterSortKey.length, right.chapterSortKey.length); index++) {
    const difference = (left.chapterSortKey[index] ?? -1) - (right.chapterSortKey[index] ?? -1);
    if (difference) return difference;
  }
  return left.displayLabel.localeCompare(right.displayLabel, "es", { numeric: true });
}

export function currentConnectorChapter(catalog: readonly ConnectorChapter[], remoteId: string | undefined,
  displayLabel: string | undefined): ConnectorChapter | undefined {
  const exact = catalog.find((chapter) => chapter.remoteId === remoteId);
  if (exact !== undefined) return exact;
  const matches = catalog.filter((chapter) => chapter.remoteId !== null && chapter.displayLabel === displayLabel);
  return matches.length === 1 ? matches[0] : undefined;
}
