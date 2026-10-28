import { requestJson } from "./client";

export interface ReaderDiskCache { limitMb: number; usedBytes: number; fileBytes: number; entries: number }
export async function readerDiskCache(limitMb?: number): Promise<ReaderDiskCache> {
  if (limitMb !== undefined && (!Number.isSafeInteger(limitMb) || limitMb < 128 || limitMb > 8192)) throw new RangeError("Límite de caché no válido");
  const { payload } = await requestJson("/settings/reader-cache", limitMb === undefined ? {} : { method: "PUT", body: JSON.stringify({ limit_mb: limitMb }) });
  if (typeof payload !== "object" || payload === null || !("data" in payload) || typeof payload.data !== "object" || payload.data === null) throw new Error("Estado de caché no válido");
  const data = payload.data as Record<string, unknown>;
  if (![data.limit_mb, data.used_bytes, data.file_bytes, data.entries].every((value) => typeof value === "number" && Number.isSafeInteger(value) && value >= 0) ||
    (data.limit_mb as number) < 128 || (data.limit_mb as number) > 8192) throw new Error("Estado de caché no válido");
  return { limitMb: data.limit_mb as number, usedBytes: data.used_bytes as number, fileBytes: data.file_bytes as number, entries: data.entries as number };
}
