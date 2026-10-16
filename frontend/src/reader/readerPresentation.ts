export interface VerticalAnchor { page: number; fraction: number }

export function fittedPageWidth(
  ratio: number | undefined, width: number, height: number,
  fit: "width" | "height" | null, zoom: number,
): number | undefined {
  if (ratio === undefined || width <= 0 || height <= 0) return undefined;
  if (fit === "width") return width;
  if (fit === "height") return height * ratio;
  return Math.min(width, height * ratio) * zoom;
}

export function verticalLayout(
  count: number, width: number, fallbackHeight: number,
  ratios: ReadonlyMap<number, number>, zoom: number, gap: number, fitHeight: boolean,
) {
  const offsets = [0];
  const heights: number[] = [];
  for (let page = 1; page <= count; page += 1) {
    const ratio = ratios.get(page);
    const height = ratio === undefined || fitHeight
      ? fallbackHeight * Math.max(1, zoom)
      : Math.max(1, width * zoom / ratio + gap);
    heights.push(height);
    offsets.push(offsets[page - 1]! + height);
  }
  return { offsets, heights };
}

export function anchorAt(offsets: readonly number[], scrollTop: number): VerticalAnchor {
  let low = 0;
  let high = offsets.length - 2;
  const top = Math.max(0, scrollTop);
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    // Browsers can round a programmed scroll position to the preceding pixel.
    if (offsets[middle]! <= top + 1) low = middle;
    else high = middle - 1;
  }
  const height = Math.max(1, offsets[low + 1]! - offsets[low]!);
  return { page: low + 1, fraction: Math.min(0.999, Math.max(0, (top - offsets[low]!) / height)) };
}

const POSITION_KEY = "yomira.reader.vertical-positions.v1";

export function readVerticalAnchor(chapterId: string, page: number): VerticalAnchor | null {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(POSITION_KEY) ?? "{}");
    if (typeof value !== "object" || value === null) return null;
    const anchor = (value as Record<string, unknown>)[chapterId];
    if (typeof anchor !== "object" || anchor === null) return null;
    const { page: savedPage, fraction } = anchor as Partial<VerticalAnchor>;
    return savedPage === page && typeof fraction === "number" && Number.isFinite(fraction)
      && fraction >= 0 && fraction < 1 ? { page, fraction } : null;
  } catch { return null; }
}

export function saveVerticalAnchor(chapterId: string, anchor: VerticalAnchor): void {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(POSITION_KEY) ?? "{}");
    const entries = typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
      ? Object.entries(parsed).filter(([key]) => key !== chapterId).slice(-49) : [];
    localStorage.setItem(POSITION_KEY, JSON.stringify(Object.fromEntries([...entries, [chapterId, anchor]])));
  } catch { /* The server still saves the page when browser storage is unavailable. */ }
}
