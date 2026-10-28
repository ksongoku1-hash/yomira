export const API_BASE_URL = "/api/v1";
export const API_VERSION = 1;

export interface AppUpdateStatus {
  state: "unsupported" | "idle" | "checking" | "up_to_date" | "downloading" | "ready" | "installing" | "error";
  current_version: string;
  latest_version: string | null;
  downloaded_bytes: number;
  total_bytes: number;
  message: string;
  releases_url: string;
}

export async function appUpdates(action?: "check" | "install"): Promise<AppUpdateStatus> {
  const { payload, response } = await requestJson(
    `/system/updates${action ? `/${action}` : ""}`,
    action ? { method: "POST", headers: { "X-Yomira-Update": "1" } } : {},
  );
  if (!isRecord(payload) ||
    typeof payload.state !== "string" ||
    !["unsupported", "idle", "checking", "up_to_date", "downloading", "ready", "installing", "error"].includes(payload.state) ||
    typeof payload.current_version !== "string" ||
    (payload.latest_version !== null && typeof payload.latest_version !== "string") ||
    typeof payload.downloaded_bytes !== "number" || !Number.isSafeInteger(payload.downloaded_bytes) || payload.downloaded_bytes < 0 ||
    typeof payload.total_bytes !== "number" || !Number.isSafeInteger(payload.total_bytes) || payload.total_bytes < payload.downloaded_bytes ||
    typeof payload.message !== "string" ||
    payload.releases_url !== "https://github.com/ksongoku1-hash/yomira/releases") {
    throw new ApiError({ code: null, message: "No se pudo leer el estado de actualizacion.",
      retryable: true, status: response.status, traceId: null });
  }
  return payload as unknown as AppUpdateStatus;
}

const errorFamilies = new Set([
  "API",
  "SYS",
  "DB",
  "SRC",
  "DL",
  "IMG",
  "CBZ",
  "LIB",
  "READ",
  "EXP",
  "AUTH",
  "NET",
]);

type ApiErrorCode = string;

interface ApiErrorDetails {
  code: ApiErrorCode | null;
  message: string;
  retryable: boolean;
  traceId: string | null;
}

interface ApiErrorOptions extends ApiErrorDetails {
  status: number | null;
  cause?: unknown;
}

export class ApiError extends Error {
  readonly code: ApiErrorCode | null;
  readonly retryable: boolean;
  readonly status: number | null;
  readonly traceId: string | null;

  constructor(options: ApiErrorOptions) {
    super(options.message, options.cause === undefined ? undefined : { cause: options.cause });
    this.name = "ApiError";
    this.code = options.code;
    this.retryable = options.retryable;
    this.status = options.status;
    this.traceId = options.traceId;
  }
}

interface LivenessResponse {
  status: "alive";
}

export interface SystemReadinessResponse {
  status: "ready" | "not_ready";
}

export type SystemDiagnosticId =
  | "backend"
  | "database"
  | "library"
  | "disk"
  | "image_processor"
  | "cbz_builder"
  | "connectors"
  | "lan";

export type SystemDiagnosticStatus = "ok" | "warning" | "error" | "unknown";

export interface SystemDiagnosticCheck {
  id: SystemDiagnosticId;
  status: SystemDiagnosticStatus;
  code: string | null;
  message: string;
  traceId: string | null;
}

export interface SystemDiagnosticsResponse {
  appVersion: string;
  apiVersion: number;
  schemaVersion: string | null;
  checks: SystemDiagnosticCheck[];
}

interface VersionResponse {
  app_version: string;
  api_version: number;
}

export interface LibrarySeriesSummary {
  seriesId: string;
  canonicalTitle: string;
  displayTitle: string;
  author: string | null;
  genres: string[] | null;
  description: string | null;
  status: string | null;
  language: string | null;
  coverAssetId: string | null;
  createdAt: string;
  updatedAt: string;
}

export type MetadataField =
  | "title"
  | "cover"
  | "author"
  | "description"
  | "genres"
  | "status"
  | "language";

export type MetadataAuthority = "USER_OVERRIDE" | "LOCAL_CANONICAL" | "SOURCE_METADATA";
export type MetadataValue = string | string[] | null;

export interface MetadataSourceValue {
  seriesSourceId: string;
  sourceId: string;
  sourceDisplayName: string | null;
  value: MetadataValue;
}

export interface MetadataResolution {
  field: MetadataField;
  value: MetadataValue;
  authority: MetadataAuthority | null;
  localValue: MetadataValue;
  sourceValues: MetadataSourceValue[];
  hasUserOverride: boolean;
  userOverrideValue: MetadataValue;
  sourceConflict: boolean;
  effectiveSourceIds: string[];
}

export interface LibrarySeriesDetail extends LibrarySeriesSummary {
  metadata: MetadataResolution[];
  readingSources: LibraryReadingSource[];
}

export interface LibraryReadingSource {
  seriesSourceId: string;
  connectorId: string;
  remoteSeriesId: string;
  displayName: string;
  author: string | null;
  description: string | null;
  genres: string[] | null;
  status: string | null;
  language: string | null;
}

export interface ExternalMetadataCandidate {
  remoteId: string;
  title: string;
  year: number | null;
  author: string | null;
}

export interface ExternalMetadataCandidates {
  data: ExternalMetadataCandidate[];
  meta: { provider: RecommendationProvider; total: number; limit: 10 };
}

export interface LibraryPage {
  data: LibrarySeriesSummary[];
  meta: { limit: number; offset: number; total: number };
}

export interface RecentLibraryPage {
  data: LibrarySeriesSummary[];
  meta: { limit: number };
}

export interface RecentReaderProgress {
  seriesId: string;
  seriesDisplayTitle: string;
  chapterId: string;
  chapterDisplayLabel: string;
  pageIndex: number;
  percentage: string;
  updatedAt: string;
}

export interface RecentReaderProgressPage {
  data: RecentReaderProgress[];
  meta: { limit: number };
}

export type LibraryReadingStatus = "reading" | "completed" | "incomplete";

export interface LibraryPageRequest {
  limit: number;
  offset: number;
  readingStatus?: LibraryReadingStatus;
  query?: string;
  sort?: "title" | "recent" | "updated";
}

export interface ConnectorSearchResult {
  remoteId: string | null;
  remoteUrl: string | null;
  remoteTitle: string | null;
  coverUrl: string | null;
  sourceLabel: string | null;
}

export type ConnectorCapability =
  | "SEARCH"
  | "METADATA"
  | "CHAPTER_LIST"
  | "PAGE_LIST"
  | "UPDATE_CHECK";

export interface ConnectorDescriptor {
  connectorId: string;
  connectorVersion: string;
  capabilities: ConnectorCapability[];
}

export interface ConnectorsResponse {
  data: ConnectorDescriptor[];
}

export interface ConnectorSearchResponse {
  data: ConnectorSearchResult[];
  meta: { connectorId: string; query: string };
}

export interface ConnectorChapter {
  remoteId: string | null;
  displayLabel: string;
  remoteLabel: string | null;
  chapterRawLabel: string;
  chapterSortKey: number[];
  language: string | null;
}

export interface ConnectorChaptersResponse {
  data: ConnectorChapter[];
  meta: { connectorId: string; remoteSeriesId: string };
}

export interface ConnectorCoverResponse {
  data: { coverUrl: string | null };
  meta: { connectorId: string; remoteSeriesId: string };
}

export interface ConnectorLibrarySeriesImport {
  data: { seriesId: string; seriesTitle: string; created: boolean };
  meta: { connectorId: string; remoteSeriesId: string };
}

export type DownloadJobStatus =
  | "QUEUED"
  | "FETCHING_METADATA"
  | "DOWNLOADING"
  | "VALIDATING"
  | "PROCESSING"
  | "PACKAGING"
  | "VERIFYING"
  | "INDEXING"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED"
  | "INTERRUPTED";

export type DownloadPriority = "HIGH" | "NORMAL" | "LOW";

export interface DownloadJobSummary {
  jobId: string;
  chapterSourceId: string;
  seriesId: string;
  chapterId: string;
  status: DownloadJobStatus;
  priority: DownloadPriority;
  seriesTitle: string;
  chapterLabel: string;
  createdAt: string;
  updatedAt: string;
  cancelRequestedAt: string | null;
}

export interface DownloadJobCreation extends DownloadJobSummary {
  created: boolean;
}

export interface DownloadJobsPage {
  data: DownloadJobSummary[];
  meta: { limit: number; offset: number; total: number;
    filteredTotal?: number;
    states?: Partial<Record<DownloadJobStatus, number>>;
    pageProgress?: {jobId: string; knownPages: number; validatedPages: number}[];
    waitingSources?: number;
    retryAfterSeconds?: number;
    recoveringJobId?: string | null;
  };
}

export type UsbExportStatus = "QUEUED" | "EXPORTING" | "EXPORTED" | "SKIPPED" | "FAILED" | "INTERRUPTED";

export interface UsbExportTarget {
  targetId: string;
  displayName: string;
  fileSystem: string;
  totalBytes: number;
  freeBytes: number;
}

export interface UsbExportJob {
  progress?: { stage: string; processedBytes: number; totalBytes: number } | null;
  exportId: string;
  assetId: string;
  targetId: string;
  targetLabel: string;
  fileSystem: string;
  destinationName: string;
  expectedSizeBytes: number;
  expectedSha256: string;
  status: UsbExportStatus;
  createdAt: string;
  updatedAt: string;
  startedAt: string | null;
  terminalAt: string | null;
  traceId: string;
  errorCode: string | null;
  failureStage: string | null;
  retryable: boolean | null;
  causeType: string | null;
}

export interface UsbExportCursorPage {
  data: UsbExportJob[];
  meta: { limit: number; total: number; nextCursor: string | null };
}

export interface CbrExportResult {
  assetId: string;
  filename: string;
  seriesTitle: string;
  chapterLabels: string[];
  pageCount: number;
  sizeBytes: number;
  sha256: string;
}

export interface CreatedDownloadJobs {
  data: DownloadJobCreation[];
  meta: { connectorId: string; remoteSeriesId: string };
}

export interface ReaderChapter {
  chapterId: string;
  seriesId: string;
  displayLabel: string;
  pageCount: number;
}

export interface ReaderChaptersPage {
  data: ReaderChapter[];
  meta: { limit: number; offset: number; total: number };
}

export interface ReaderProgress {
  seriesId: string;
  chapterId: string;
  pageIndex: number;
  percentage: string;
  updatedAt: string;
}

export type ReaderMode = "single" | "vertical" | "double";

export interface ReaderSettings {
  cache_limit_mb: number;
  preload_ahead: number;
  mode: ReaderMode;
  rtl: boolean;
  e_reader: boolean;
}

const MAX_READER_PAGE_COUNT = 10_000;
const MAX_HOME_RECENT_LIMIT = 20;
const MAX_READER_CHAPTERS_PAGE_SIZE = 100;
const MAX_READER_PAGE_BYTES = 67_108_864;
const MIN_READER_CACHE_LIMIT_MB = 16;
const MAX_READER_CACHE_LIMIT_MB = 500;
const MAX_READER_PRELOAD_AHEAD = 2;
const READER_IMAGE_MIMES = new Set(["image/jpeg", "image/png", "image/webp"]);
const READER_MODES = new Set<ReaderMode>(["single", "vertical", "double"]);

const UUID_V4_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actualKeys = Object.keys(value);
  return actualKeys.length === keys.length && keys.every((key) => key in value);
}

function isCataloguedErrorCode(value: unknown): value is ApiErrorCode {
  if (typeof value !== "string") return false;

  const separator = value.indexOf("_");
  const family = value.slice(0, separator);
  const suffix = value.slice(separator + 1);
  return (
    separator > 0 &&
    errorFamilies.has(family) &&
    /^[A-Z0-9_]+$/.test(suffix) &&
    suffix.length > 0
  );
}

function parseApiError(value: unknown): ApiErrorDetails | null {
  if (!isRecord(value) || !isRecord(value.error)) return null;

  const error = value.error;
  if (
    !isCataloguedErrorCode(error.code) ||
    typeof error.message !== "string" ||
    typeof error.trace_id !== "string" ||
    typeof error.retryable !== "boolean"
  ) {
    return null;
  }

  return {
    code: error.code,
    message: error.message,
    retryable: error.retryable,
    traceId: error.trace_id,
  };
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: string;
  acceptedStatuses?: readonly number[];
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

export async function requestJson(
  path: string,
  options: RequestOptions = {},
): Promise<{ payload: unknown; response: Response }> {
  let response: Response;
  try {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (options.headers !== undefined) Object.assign(headers, options.headers);
    const init: RequestInit = { headers };
    if (options.signal !== undefined) init.signal = options.signal;
    if (options.method !== undefined && options.method !== "GET") init.method = options.method;
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = options.body;
    }
    response = await fetch(API_BASE_URL + path, init);
  } catch (cause) {
    if (options.signal?.aborted) throw cause;
    throw new ApiError({
      code: null,
      message: "No se pudo conectar con la API local.",
      retryable: true,
      status: null,
      traceId: null,
      cause,
    });
  }

  if (response.status === 204) return { payload: null, response };

  let payload: unknown;
  try {
    payload = await response.json();
  } catch (cause) {
    if (options.signal?.aborted) throw cause;
    throw new ApiError({
      code: null,
      message: response.ok
        ? "La API devolvió una respuesta JSON no válida."
        : "La API devolvió un error sin el contrato esperado.",
      retryable: false,
      status: response.status,
      traceId: null,
      cause,
    });
  }

  if (!response.ok && !options.acceptedStatuses?.includes(response.status)) {
    const error = parseApiError(payload);
    throw new ApiError({
      code: error?.code ?? null,
      message: error?.message ?? "La API rechazó la solicitud.",
      retryable: error?.retryable ?? false,
      status: response.status,
      traceId: error?.traceId ?? null,
    });
  }

  return { payload, response };
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function isOptionalString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isOptionalRemoteReference(value: unknown): value is string | null {
  return value === null || (isNonEmptyString(value) && value.length <= 512);
}

const downloadJobStatuses = new Set<DownloadJobStatus>([
  "QUEUED",
  "FETCHING_METADATA",
  "DOWNLOADING",
  "VALIDATING",
  "PROCESSING",
  "PACKAGING",
  "VERIFYING",
  "INDEXING",
  "COMPLETED",
  "FAILED",
  "CANCELLED",
  "INTERRUPTED",
]);
const downloadPriorities = new Set<DownloadPriority>(["HIGH", "NORMAL", "LOW"]);
const systemDiagnosticIds = new Set<SystemDiagnosticId>([
  "backend",
  "database",
  "library",
  "disk",
  "image_processor",
  "cbz_builder",
  "connectors",
  "lan",
]);
const systemDiagnosticStatuses = new Set<SystemDiagnosticStatus>([
  "ok",
  "warning",
  "error",
  "unknown",
]);
const RFC3339_TIMESTAMP_PATTERN =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;

function isTimestamp(value: unknown): value is string {
  return (
    typeof value === "string" &&
    RFC3339_TIMESTAMP_PATTERN.test(value) &&
    !Number.isNaN(Date.parse(value))
  );
}

function isOptionalTimestamp(value: unknown): value is string | null {
  return value === null || isTimestamp(value);
}

const usbExportStatuses = new Set<UsbExportStatus>([
  "QUEUED",
  "EXPORTING",
  "EXPORTED",
  "SKIPPED",
  "FAILED",
  "INTERRUPTED",
]);
const MAX_USB_EXPORT_BYTES = 8_589_934_592;

function parseUsbExportTarget(value: unknown): UsbExportTarget | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "target_id",
      "display_name",
      "file_system",
      "total_bytes",
      "free_bytes",
    ]) ||
    !isNonEmptyString(value.target_id) ||
    value.target_id.length > 128 ||
    !isNonEmptyString(value.display_name) ||
    value.display_name.length > 128 ||
    value.display_name.includes("/") ||
    value.display_name.includes("\\") ||
    value.display_name.split("").some((character) => {
      const codePoint = character.charCodeAt(0);
      return codePoint < 32 || codePoint === 127;
    }) ||
    !isNonEmptyString(value.file_system) ||
    value.file_system.length > 32 ||
    typeof value.total_bytes !== "number" ||
    !Number.isSafeInteger(value.total_bytes) ||
    value.total_bytes < 0 ||
    typeof value.free_bytes !== "number" ||
    !Number.isSafeInteger(value.free_bytes) ||
    value.free_bytes < 0 ||
    value.free_bytes > value.total_bytes
  ) {
    return null;
  }
  return {
    targetId: value.target_id,
    displayName: value.display_name,
    fileSystem: value.file_system,
    totalBytes: value.total_bytes,
    freeBytes: value.free_bytes,
  };
}

function parseUsbExportJob(value: unknown): UsbExportJob | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "export_id",
      "asset_id",
      "target_id",
      "target_label",
      "file_system",
      "destination_name",
      "expected_size_bytes",
      "expected_sha256",
      "status",
      "created_at",
      "updated_at",
      "started_at",
      "terminal_at",
      "trace_id",
      "error_code",
      "failure_stage",
      "retryable",
      "cause_type",
      ...("progress" in value ? ["progress"] : []),
    ]) ||
    typeof value.export_id !== "string" ||
    !UUID_V4_PATTERN.test(value.export_id) ||
    typeof value.asset_id !== "string" ||
    !UUID_V4_PATTERN.test(value.asset_id) ||
    !isNonEmptyString(value.target_id) ||
    !isNonEmptyString(value.target_label) ||
    !isNonEmptyString(value.file_system) ||
    !isNonEmptyString(value.destination_name) ||
    value.destination_name.includes("/") ||
    value.destination_name.includes("\\") ||
    typeof value.expected_size_bytes !== "number" ||
    !Number.isSafeInteger(value.expected_size_bytes) ||
    value.expected_size_bytes < 1 ||
    value.expected_size_bytes > MAX_USB_EXPORT_BYTES ||
    typeof value.expected_sha256 !== "string" ||
    !/^[0-9a-f]{64}$/.test(value.expected_sha256) ||
    typeof value.status !== "string" ||
    !usbExportStatuses.has(value.status as UsbExportStatus) ||
    !isTimestamp(value.created_at) ||
    !isTimestamp(value.updated_at) ||
    Date.parse(value.updated_at) < Date.parse(value.created_at) ||
    !isOptionalTimestamp(value.started_at) ||
    !isOptionalTimestamp(value.terminal_at) ||
    typeof value.trace_id !== "string" ||
    !UUID_V4_PATTERN.test(value.trace_id) ||
    !isOptionalString(value.error_code) ||
    !isOptionalString(value.failure_stage) ||
    !(value.retryable === null || typeof value.retryable === "boolean") ||
    !isOptionalString(value.cause_type)
  ) {
    return null;
  }
  const progress = value.progress;
  if (progress != null && (!isRecord(progress) ||
      !hasExactKeys(progress, ["stage", "processed_bytes", "total_bytes"]) ||
      typeof progress.stage !== "string" ||
      !["verify_source", "copy", "verify_staging", "resume_staging",
        "destination_verify", "publish", "verify_final"].includes(progress.stage) ||
      typeof progress.processed_bytes !== "number" || !Number.isSafeInteger(progress.processed_bytes) ||
      progress.processed_bytes < 0 || progress.total_bytes !== value.expected_size_bytes ||
      progress.processed_bytes > value.expected_size_bytes)) return null;
  return {
    exportId: value.export_id,
    progress: isRecord(progress) ? { stage: progress.stage as string,
      processedBytes: progress.processed_bytes as number, totalBytes: progress.total_bytes as number } : null,
    assetId: value.asset_id,
    targetId: value.target_id,
    targetLabel: value.target_label,
    fileSystem: value.file_system,
    destinationName: value.destination_name,
    expectedSizeBytes: value.expected_size_bytes,
    expectedSha256: value.expected_sha256,
    status: value.status as UsbExportStatus,
    createdAt: value.created_at,
    updatedAt: value.updated_at,
    startedAt: value.started_at,
    terminalAt: value.terminal_at,
    traceId: value.trace_id,
    errorCode: value.error_code,
    failureStage: value.failure_stage,
    retryable: value.retryable,
    causeType: value.cause_type,
  };
}

function invalidUsbExportResponse(response: Response): ApiError {
  return new ApiError({
    code: null,
    message: "La API devolvió una respuesta de exportación USB no válida.",
    retryable: false,
    status: response.status,
    traceId: response.headers.get("X-Trace-ID"),
  });
}

function invalidCbrExportResponse(response: Response): ApiError {
  return new ApiError({
    code: null,
    message: "La API devolvió una respuesta de CBR no válida.",
    retryable: false,
    status: response.status,
    traceId: response.headers.get("X-Trace-ID"),
  });
}

function parseCbrExportResult(value: unknown): CbrExportResult | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "asset_id",
      "filename",
      "series_title",
      "chapter_labels",
      "page_count",
      "size_bytes",
      "sha256",
    ]) ||
    typeof value.asset_id !== "string" ||
    !UUID_V4_PATTERN.test(value.asset_id) ||
    !isNonEmptyString(value.filename) ||
    value.filename.length > 240 ||
    value.filename.includes("/") ||
    value.filename.includes("\\") ||
    !value.filename.toLowerCase().endsWith(".cbr") ||
    !isNonEmptyString(value.series_title) ||
    value.series_title.length > 512 ||
    !Array.isArray(value.chapter_labels) ||
    value.chapter_labels.length < 1 ||
    value.chapter_labels.length > 10_000 ||
    value.chapter_labels.some((label) => !isNonEmptyString(label) || label.length > 256) ||
    typeof value.page_count !== "number" ||
    !Number.isSafeInteger(value.page_count) ||
    value.page_count < 1 ||
    value.page_count > 10_000 ||
    typeof value.size_bytes !== "number" ||
    !Number.isSafeInteger(value.size_bytes) ||
    value.size_bytes < 1 ||
    value.size_bytes > MAX_USB_EXPORT_BYTES ||
    typeof value.sha256 !== "string" ||
    !/^[0-9a-f]{64}$/.test(value.sha256)
  ) {
    return null;
  }
  return {
    assetId: value.asset_id,
    filename: value.filename,
    seriesTitle: value.series_title,
    chapterLabels: value.chapter_labels,
    pageCount: value.page_count,
    sizeBytes: value.size_bytes,
    sha256: value.sha256,
  };
}

function parseDownloadJobSummary(value: unknown): DownloadJobSummary | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "job_id",
      "chapter_source_id",
      "series_id",
      "chapter_id",
      "status",
      "priority",
      "series_title",
      "chapter_label",
      "created_at",
      "updated_at",
      "cancel_requested_at",
    ]) ||
    typeof value.job_id !== "string" ||
    !UUID_V4_PATTERN.test(value.job_id) ||
    typeof value.chapter_source_id !== "string" ||
    !UUID_V4_PATTERN.test(value.chapter_source_id) ||
    typeof value.series_id !== "string" ||
    !UUID_V4_PATTERN.test(value.series_id) ||
    typeof value.chapter_id !== "string" ||
    !UUID_V4_PATTERN.test(value.chapter_id) ||
    typeof value.status !== "string" ||
    !downloadJobStatuses.has(value.status as DownloadJobStatus) ||
    typeof value.priority !== "string" ||
    !downloadPriorities.has(value.priority as DownloadPriority) ||
    !isNonEmptyString(value.series_title) ||
    !isNonEmptyString(value.chapter_label) ||
    !isTimestamp(value.created_at) ||
    !isTimestamp(value.updated_at) ||
    !isOptionalTimestamp(value.cancel_requested_at) ||
    Date.parse(value.updated_at) < Date.parse(value.created_at)
  ) {
    return null;
  }

  return {
    jobId: value.job_id,
    chapterSourceId: value.chapter_source_id,
    seriesId: value.series_id,
    chapterId: value.chapter_id,
    status: value.status as DownloadJobStatus,
    priority: value.priority as DownloadPriority,
    seriesTitle: value.series_title,
    chapterLabel: value.chapter_label,
    createdAt: value.created_at,
    updatedAt: value.updated_at,
    cancelRequestedAt: value.cancel_requested_at,
  };
}

function invalidDownloadJobsResponse(response: Response): ApiError {
  return new ApiError({
    code: null,
    message: "La API devolvió una respuesta de trabajos de descarga no válida.",
    retryable: false,
    status: response.status,
    traceId: response.headers.get("X-Trace-ID"),
  });
}

function parseDownloadJobCreation(value: unknown): DownloadJobCreation | null {
  if (!isRecord(value) || !hasExactKeys(value, [
    "job_id",
    "chapter_source_id",
    "series_id",
    "chapter_id",
    "status",
    "priority",
    "series_title",
    "chapter_label",
    "created",
    "created_at",
    "updated_at",
  ]) || typeof value.created !== "boolean") {
    return null;
  }
  const summary = parseDownloadJobSummary({
    job_id: value.job_id,
    chapter_source_id: value.chapter_source_id,
    series_id: value.series_id,
    chapter_id: value.chapter_id,
    status: value.status,
    priority: value.priority,
    series_title: value.series_title,
    chapter_label: value.chapter_label,
    created_at: value.created_at,
    updated_at: value.updated_at,
    cancel_requested_at: null,
  });
  return summary === null ? null : { ...summary, created: value.created };
}

export async function createConnectorDownloadJobs(
  connectorId: string,
  remoteSeriesId: string,
  selection: { chapterRemoteIds: string[]; priority?: DownloadPriority },
): Promise<CreatedDownloadJobs> {
  if (!isNonEmptyString(connectorId) || !isNonEmptyString(remoteSeriesId)) {
    throw new RangeError("connectorId and remoteSeriesId must not be empty");
  }
  if (
    selection.chapterRemoteIds.length < 1 ||
    selection.chapterRemoteIds.length > 100 ||
    selection.chapterRemoteIds.some((id) => !isNonEmptyString(id) || id.length > 512) ||
    new Set(selection.chapterRemoteIds).size !== selection.chapterRemoteIds.length
  ) {
    throw new RangeError("chapterRemoteIds must contain 1 to 100 unique nonempty IDs");
  }
  if (selection.priority !== undefined && !downloadPriorities.has(selection.priority)) {
    throw new RangeError("priority must be HIGH, NORMAL, or LOW");
  }

  const body: { chapter_remote_ids: string[]; priority?: DownloadPriority } = {
    chapter_remote_ids: selection.chapterRemoteIds,
  };
  if (selection.priority !== undefined) body.priority = selection.priority;

  const path = `/connectors/${encodeURIComponent(connectorId)}/series/${encodeURIComponent(remoteSeriesId)}/download-jobs`;
  const { payload, response } = await requestJson(path, {
    method: "POST",
    body: JSON.stringify(body),
  });
  if (
    !isRecord(payload) ||
    !hasExactKeys(payload, ["data", "meta"]) ||
    !Array.isArray(payload.data) ||
    !isRecord(payload.meta) ||
    !hasExactKeys(payload.meta, ["connector_id", "remote_series_id"]) ||
    payload.meta.connector_id !== connectorId ||
    payload.meta.remote_series_id !== remoteSeriesId ||
    payload.data.length !== selection.chapterRemoteIds.length
  ) {
    throw invalidDownloadJobsResponse(response);
  }

  const data: DownloadJobCreation[] = [];
  const jobIds = new Set<string>();
  const chapterSourceIds = new Set<string>();
  for (const record of payload.data) {
    const item = parseDownloadJobCreation(record);
    if (
      item === null ||
      jobIds.has(item.jobId) ||
      chapterSourceIds.has(item.chapterSourceId)
    ) {
      throw invalidDownloadJobsResponse(response);
    }
    jobIds.add(item.jobId);
    chapterSourceIds.add(item.chapterSourceId);
    data.push(item);
  }

  return { data, meta: { connectorId, remoteSeriesId } };
}

export async function queueConnectorChapters(
  connectorId: string, remoteSeriesId: string, chapterRemoteIds: readonly string[],
  priority: DownloadPriority = "NORMAL",
): Promise<number> {
  const ids = [...new Set(chapterRemoteIds)];
  if (!isNonEmptyString(connectorId) || !isNonEmptyString(remoteSeriesId) ||
    remoteSeriesId.length > 512 || ids.length < 1 || ids.length > 10_000 ||
    ids.some((id) => !isNonEmptyString(id) || id.length > 512) || !downloadPriorities.has(priority)) {
    throw new RangeError("Chapter selection exceeds the source contract");
  }
  for (let start = 0; start < ids.length; start += 100) {
    await createConnectorDownloadJobs(connectorId, remoteSeriesId, {
      chapterRemoteIds: ids.slice(start, start + 100), priority,
    });
  }
  return ids.length;
}

export async function getDownloadJobsPage({
  limit,
  offset,
  archived = false,
  statuses = [],
}: {
  limit: number;
  offset: number;
  archived?: boolean;
  statuses?: readonly DownloadJobStatus[];
}): Promise<DownloadJobsPage> {
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100) {
    throw new RangeError("limit must be between 1 and 100");
  }
  if (!Number.isSafeInteger(offset) || offset < 0 || offset > 10_000_000) {
    throw new RangeError("offset must be between 0 and 10000000");
  }
  if (statuses.length > downloadJobStatuses.size || statuses.some((status) => !downloadJobStatuses.has(status))) {
    throw new RangeError("statuses must contain supported job states");
  }

  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (archived) params.set("archived", "true");
  for (const status of new Set(statuses)) params.append("status", status);
  const { payload, response } = await requestJson(`/download-jobs?${params.toString()}`);
  if (
    !isRecord(payload) ||
    !hasExactKeys(payload, ["data", "meta"]) ||
    !Array.isArray(payload.data) ||
    !isRecord(payload.meta) ||
    !hasExactKeys(payload.meta, ["limit", "offset", "total",
      ...("filtered_total" in payload.meta ? ["filtered_total"] : []),
      ...("states" in payload.meta ? ["states"] : []),
      ...("page_progress" in payload.meta ? ["page_progress"] : []),
      ...("waiting_sources" in payload.meta ? ["waiting_sources"] : []),
      ...("retry_after_seconds" in payload.meta ? ["retry_after_seconds"] : []),
      ...("recovering_job_id" in payload.meta ? ["recovering_job_id"] : []),
    ]) ||
    payload.meta.limit !== limit ||
    payload.meta.offset !== offset ||
    typeof payload.meta.total !== "number" ||
    !Number.isSafeInteger(payload.meta.total) ||
    payload.meta.total < 0 ||
    payload.data.length > limit ||
    payload.data.length > payload.meta.total
  ) {
    throw invalidDownloadJobsResponse(response);
  }
  const filteredTotal = payload.meta.filtered_total;
  if ((statuses.length > 0 || filteredTotal !== undefined) &&
      (typeof filteredTotal !== "number" || !Number.isSafeInteger(filteredTotal) ||
        filteredTotal < 0 || filteredTotal > payload.meta.total || payload.data.length > filteredTotal)) {
    throw invalidDownloadJobsResponse(response);
  }

  const data: DownloadJobSummary[] = [];
  const jobIds = new Set<string>();
  for (const record of payload.data) {
    const item = parseDownloadJobSummary(record);
    if (item === null || jobIds.has(item.jobId) || (statuses.length > 0 && !statuses.includes(item.status))) {
      throw invalidDownloadJobsResponse(response);
    }
    jobIds.add(item.jobId);
    data.push(item);
  }

  if (payload.meta.recovering_job_id !== undefined && payload.meta.recovering_job_id !== null &&
    (typeof payload.meta.recovering_job_id !== "string" || !UUID_V4_PATTERN.test(payload.meta.recovering_job_id))) {
    throw invalidDownloadJobsResponse(response);
  }
  const states = payload.meta.states;
  for (const key of ["waiting_sources", "retry_after_seconds"]) {
    const value = payload.meta[key];
    if (value !== undefined && (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0 || value > 10_000)) {
      throw invalidDownloadJobsResponse(response);
    }
  }
  if (states !== undefined && (!isRecord(states) ||
    Object.entries(states).some(([status, count]) => !downloadJobStatuses.has(status as DownloadJobStatus) ||
      typeof count !== "number" || !Number.isSafeInteger(count) || count < 0) ||
    Object.values(states).reduce<number>((sum, count) => sum + (count as number), 0) !== payload.meta.total)) {
    throw invalidDownloadJobsResponse(response);
  }
  if (states !== undefined && statuses.length > 0 &&
    [...new Set(statuses)].reduce((sum, status) => sum + ((states as Record<string, number>)[status] ?? 0), 0) !== filteredTotal) {
    throw invalidDownloadJobsResponse(response);
  }
  const pageProgress: NonNullable<DownloadJobsPage["meta"]["pageProgress"]> = [];
  if (payload.meta.page_progress !== undefined) {
    if (!Array.isArray(payload.meta.page_progress)) throw invalidDownloadJobsResponse(response);
    const seen = new Set<string>();
    for (const item of payload.meta.page_progress) {
      if (!isRecord(item) || !hasExactKeys(item, ["job_id", "known_pages", "validated_pages"]) ||
        typeof item.job_id !== "string" || !jobIds.has(item.job_id) || seen.has(item.job_id) ||
        typeof item.known_pages !== "number" || !Number.isSafeInteger(item.known_pages) || item.known_pages < 1 || item.known_pages > 10_000 ||
        typeof item.validated_pages !== "number" || !Number.isSafeInteger(item.validated_pages) || item.validated_pages < 0 || item.validated_pages > item.known_pages) {
        throw invalidDownloadJobsResponse(response);
      }
      seen.add(item.job_id);
      pageProgress.push({jobId:item.job_id, knownPages:item.known_pages, validatedPages:item.validated_pages});
    }
  }
  return { data, meta: { limit, offset, total: payload.meta.total,
    ...(filteredTotal === undefined ? {} : {filteredTotal: filteredTotal as number}),
    ...(payload.meta.recovering_job_id === undefined ? {} : {recoveringJobId:payload.meta.recovering_job_id as string | null}),
    ...(states === undefined ? {} : {states:states as Partial<Record<DownloadJobStatus, number>>}),
    ...(payload.meta.page_progress === undefined ? {} : {pageProgress}),
    ...(payload.meta.waiting_sources === undefined ? {} : {waitingSources:payload.meta.waiting_sources as number}),
    ...(payload.meta.retry_after_seconds === undefined ? {} : {retryAfterSeconds:payload.meta.retry_after_seconds as number}),
  } };
}

export interface DownloadGroup {
  seriesId: string;
  seriesTitle: string;
  queued: number;
  active: number;
  paused: boolean;
}

export async function getDownloadGroupsPage({limit = 50, offset = 0} = {}): Promise<{
  data: DownloadGroup[]; meta: {limit: number; offset: number; total: number};
}> {
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100 ||
      !Number.isSafeInteger(offset) || offset < 0 || offset > 10_000_000) {
    throw new RangeError("Invalid group pagination");
  }
  const {payload, response} = await requestJson(`/download-jobs/groups?limit=${limit}&offset=${offset}`);
  if (!isRecord(payload) || !hasExactKeys(payload, ["data", "meta"]) ||
      !Array.isArray(payload.data) || !isRecord(payload.meta) ||
      !hasExactKeys(payload.meta, ["limit", "offset", "total"]) ||
      payload.meta.limit !== limit || payload.meta.offset !== offset ||
      typeof payload.meta.total !== "number" || !Number.isSafeInteger(payload.meta.total) ||
      payload.meta.total < 0 || payload.data.length > limit ||
      payload.data.length !== Math.min(limit, Math.max(0, payload.meta.total - offset))) {
    throw invalidDownloadJobsResponse(response);
  }
  const seen = new Set<string>();
  const data = payload.data.map((item: unknown) => {
    if (!isRecord(item) || !hasExactKeys(item, ["series_id", "series_title", "queued", "active", "paused"]) ||
        typeof item.series_id !== "string" || !UUID_V4_PATTERN.test(item.series_id) || seen.has(item.series_id) ||
        !isNonEmptyString(item.series_title) || typeof item.paused !== "boolean" ||
        typeof item.queued !== "number" || !Number.isSafeInteger(item.queued) || item.queued < 0 ||
        typeof item.active !== "number" || !Number.isSafeInteger(item.active) || item.active < 0 ||
        item.queued + item.active < 1) throw invalidDownloadJobsResponse(response);
    seen.add(item.series_id);
    return {seriesId: item.series_id, seriesTitle: item.series_title, queued: item.queued,
      active: item.active, paused: item.paused};
  });
  return {data, meta: {limit, offset, total: payload.meta.total}};
}

export async function setDownloadGroupPaused(seriesId: string, paused: boolean): Promise<void> {
  if (!UUID_V4_PATTERN.test(seriesId) || typeof paused !== "boolean") {
    throw new RangeError("Invalid download group pause");
  }
  const {payload, response} = await requestJson(`/download-jobs/groups/${encodeURIComponent(seriesId)}/pause`, {
    method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({paused}),
  });
  if (response.status !== 204 || payload !== null) throw invalidDownloadJobsResponse(response);
}

export interface ChapterAvailability {
  linked: boolean;
  readyRemoteIds: string[];
  readyLabels: string[];
  busyRemoteIds: string[];
}

export async function getChapterAvailability(connectorId: string, remoteSeriesId: string): Promise<ChapterAvailability> {
  const {payload, response} = await requestJson(`/connectors/${encodeURIComponent(connectorId)}/series/${encodeURIComponent(remoteSeriesId)}/availability`);
  if (!isRecord(payload) || !hasExactKeys(payload, ["data"]) || !isRecord(payload.data) ||
    !hasExactKeys(payload.data, ["linked", "ready_remote_ids", "ready_labels", "busy_remote_ids"]) ||
    typeof payload.data.linked !== "boolean" ||
    [payload.data.ready_remote_ids, payload.data.ready_labels, payload.data.busy_remote_ids].some(
      (value) => !Array.isArray(value) || value.length > 10_000 || !value.every(isNonEmptyString),
    )) throw invalidDownloadJobsResponse(response);
  return {linked:payload.data.linked, readyRemoteIds:payload.data.ready_remote_ids as string[],
    readyLabels:payload.data.ready_labels as string[], busyRemoteIds:payload.data.busy_remote_ids as string[]};
}

export async function archiveDownloadJob(jobId: string): Promise<void> {
  if (!UUID_V4_PATTERN.test(jobId)) throw new RangeError("jobId must be a UUID v4");
  const { payload, response } = await requestJson(
    `/download-jobs/${encodeURIComponent(jobId)}/archive`,
    { method: "POST" },
  );
  if (response.status !== 204 || payload !== null) throw invalidDownloadJobsResponse(response);
}

export async function restoreDownloadJob(jobId: string): Promise<void> {
  if (!UUID_V4_PATTERN.test(jobId)) throw new RangeError("jobId must be a UUID v4");
  const { payload, response } = await requestJson(
    `/download-jobs/${encodeURIComponent(jobId)}/restore`,
    { method: "POST" },
  );
  if (response.status !== 204 || payload !== null) throw invalidDownloadJobsResponse(response);
}

export async function getUsbExportTargets(): Promise<UsbExportTarget[]> {
  const { payload, response } = await requestJson("/usb-exports/targets");
  if (
    !isRecord(payload) ||
    !hasExactKeys(payload, ["data"]) ||
    !Array.isArray(payload.data)
  ) {
    throw invalidUsbExportResponse(response);
  }
  const targets = payload.data.map(parseUsbExportTarget);
  if (targets.some((target) => target === null)) throw invalidUsbExportResponse(response);
  const targetIds = new Set<string>();
  for (const target of targets) {
    if (target === null || targetIds.has(target.targetId)) throw invalidUsbExportResponse(response);
    targetIds.add(target.targetId);
  }
  return targets.filter((target): target is UsbExportTarget => target !== null);
}

export async function getUsbExportHistoryPage({
  limit,
  cursor,
}: {
  limit: number;
  cursor?: string | null;
}): Promise<UsbExportCursorPage> {
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100) {
    throw new RangeError("limit must be between 1 and 100");
  }
  if (
    cursor !== undefined &&
    cursor !== null &&
    (cursor.length < 1 || cursor.length > 256 || !/^[A-Za-z0-9_-]+$/.test(cursor))
  ) {
    throw new RangeError("cursor must be bounded base64url text");
  }
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor !== undefined && cursor !== null) params.set("cursor", cursor);
  const { payload, response } = await requestJson(`/usb-exports/pages?${params.toString()}`);
  if (
    !isRecord(payload) ||
    !hasExactKeys(payload, ["data", "meta"]) ||
    !Array.isArray(payload.data) ||
    !isRecord(payload.meta) ||
    !hasExactKeys(payload.meta, ["limit", "total", "next_cursor"]) ||
    payload.meta.limit !== limit ||
    typeof payload.meta.total !== "number" ||
    !Number.isSafeInteger(payload.meta.total) ||
    payload.meta.total < 0 ||
    !(payload.meta.next_cursor === null ||
      (typeof payload.meta.next_cursor === "string" &&
        payload.meta.next_cursor.length > 0 &&
        payload.meta.next_cursor.length <= 256 &&
        /^[A-Za-z0-9_-]+$/.test(payload.meta.next_cursor))) ||
    payload.data.length > limit ||
    payload.data.length > payload.meta.total
  ) {
    throw invalidUsbExportResponse(response);
  }
  const data: UsbExportJob[] = [];
  const exportIds = new Set<string>();
  for (const record of payload.data) {
    const item = parseUsbExportJob(record);
    if (item === null || exportIds.has(item.exportId)) throw invalidUsbExportResponse(response);
    exportIds.add(item.exportId);
    data.push(item);
  }
  return {
    data,
    meta: { limit, total: payload.meta.total, nextCursor: payload.meta.next_cursor },
  };
}

export async function createChapterUsbExport(
  chapterId: string,
  targetId: string,
  idempotencyKey: string,
): Promise<UsbExportJob> {
  if (!UUID_V4_PATTERN.test(chapterId)) throw new RangeError("chapterId must be a UUID v4");
  if (!isNonEmptyString(targetId) || targetId.length > 128) {
    throw new RangeError("targetId must contain between 1 and 128 characters");
  }
  if (
    !isNonEmptyString(idempotencyKey) ||
    idempotencyKey.length > 128 ||
    /[^\x21-\x7e]/.test(idempotencyKey)
  ) {
    throw new RangeError("idempotencyKey must contain 1 to 128 visible ASCII characters");
  }
  const { payload, response } = await requestJson(
    `/usb-exports/chapters/${encodeURIComponent(chapterId)}`,
    {
      method: "POST",
      body: JSON.stringify({ target_id: targetId }),
      headers: { "Idempotency-Key": idempotencyKey },
    },
  );
  if (
    response.status !== 200 ||
    !isRecord(payload) ||
    !hasExactKeys(payload, ["data"])
  ) {
    throw invalidUsbExportResponse(response);
  }
  const job = parseUsbExportJob(payload.data);
  if (job === null) throw invalidUsbExportResponse(response);
  return job;
}

export async function createCbrExport(
  seriesId: string,
  chapterIds: string[],
): Promise<CbrExportResult> {
  if (!UUID_V4_PATTERN.test(seriesId)) throw new RangeError("seriesId must be a UUID v4");
  if (
    chapterIds.length < 1 ||
    chapterIds.length > 10_000 ||
    chapterIds.some((chapterId) => !UUID_V4_PATTERN.test(chapterId)) ||
    new Set(chapterIds).size !== chapterIds.length
  ) {
    throw new RangeError("chapterIds must contain up to 10000 unique UUID v4 values");
  }
  const { payload, response } = await requestJson("/cbr-exports", {
    method: "POST",
    body: JSON.stringify({ series_id: seriesId, chapter_ids: chapterIds }),
  });
  if (
    response.status !== 200 ||
    !isRecord(payload) ||
    !hasExactKeys(payload, ["data"])
  ) {
    throw invalidCbrExportResponse(response);
  }
  const result = parseCbrExportResult(payload.data);
  if (result === null || result.chapterLabels.length !== chapterIds.length) {
    throw invalidCbrExportResponse(response);
  }
  return result;
}

export async function createUsbAssetExport(
  assetId: string,
  targetId: string,
  idempotencyKey: string,
): Promise<UsbExportJob> {
  if (!UUID_V4_PATTERN.test(assetId)) throw new RangeError("assetId must be a UUID v4");
  if (!isNonEmptyString(targetId) || targetId.length > 128) {
    throw new RangeError("targetId must contain between 1 and 128 characters");
  }
  if (
    !isNonEmptyString(idempotencyKey) ||
    idempotencyKey.length > 128 ||
    /[^\x21-\x7e]/.test(idempotencyKey)
  ) {
    throw new RangeError("idempotencyKey must contain 1 to 128 visible ASCII characters");
  }
  const { payload, response } = await requestJson("/usb-exports", {
    method: "POST",
    body: JSON.stringify({ asset_id: assetId, target_id: targetId }),
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (
    response.status !== 200 ||
    !isRecord(payload) ||
    !hasExactKeys(payload, ["data"])
  ) {
    throw invalidUsbExportResponse(response);
  }
  const job = parseUsbExportJob(payload.data);
  if (job === null) throw invalidUsbExportResponse(response);
  return job;
}

function invalidDownloadCancellationResponse(response: Response): ApiError {
  return new ApiError({
    code: null,
    message: "La API devolvió una respuesta de cancelación no válida.",
    retryable: false,
    status: response.status,
    traceId: response.headers.get("X-Trace-ID"),
  });
}

const cancellableDownloadJobStatuses = new Set<DownloadJobStatus>([
  "QUEUED",
  "FETCHING_METADATA",
  "DOWNLOADING",
  "VALIDATING",
  "PROCESSING",
  "PACKAGING",
  "VERIFYING",
  "INDEXING",
]);
const cancellationIntentResultStatuses = new Set<DownloadJobStatus>([
  ...cancellableDownloadJobStatuses,
  "FAILED",
  "INTERRUPTED",
]);

export async function cancelDownloadJob(jobId: string): Promise<DownloadJobSummary> {
  if (!UUID_V4_PATTERN.test(jobId)) {
    throw new RangeError("jobId must be a UUID v4");
  }

  const { payload, response } = await requestJson(
    `/downloads/${encodeURIComponent(jobId)}/cancel`,
    { method: "POST" },
  );
  if (
    response.status !== 200 ||
    !isRecord(payload) ||
    !hasExactKeys(payload, ["data"])
  ) {
    throw invalidDownloadCancellationResponse(response);
  }

  const job = parseDownloadJobSummary(payload.data);
  if (
    job === null ||
    job.jobId !== jobId ||
    (job.status !== "CANCELLED" && job.cancelRequestedAt === null) ||
    (job.status !== "CANCELLED" && !cancellationIntentResultStatuses.has(job.status))
  ) {
    throw invalidDownloadCancellationResponse(response);
  }
  return job;
}

export async function retryDownloadJob(jobId: string): Promise<DownloadJobSummary> {
  if (!UUID_V4_PATTERN.test(jobId)) {
    throw new RangeError("jobId must be a UUID v4");
  }
  const { payload, response } = await requestJson(
    `/downloads/${encodeURIComponent(jobId)}/retry`,
    { method: "POST" },
  );
  if (response.status !== 200 || !isRecord(payload) || !hasExactKeys(payload, ["data"])) {
    throw invalidDownloadJobsResponse(response);
  }
  const job = parseDownloadJobSummary(payload.data);
  if (job === null || job.jobId === jobId || job.status === "FAILED" || job.status === "CANCELLED") {
    throw invalidDownloadJobsResponse(response);
  }
  return job;
}

function invalidReaderResponse(response: Response): ApiError {
  return new ApiError({
    code: null,
    message: "La API devolvió una respuesta del lector no válida.",
    retryable: false,
    status: response.status,
    traceId: response.headers.get("X-Trace-ID"),
  });
}

function parseReaderChapter(value: unknown): ReaderChapter | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["chapter_id", "series_id", "display_label", "page_count"]) ||
    typeof value.chapter_id !== "string" ||
    !UUID_V4_PATTERN.test(value.chapter_id) ||
    typeof value.series_id !== "string" ||
    !UUID_V4_PATTERN.test(value.series_id) ||
    !isNonEmptyString(value.display_label) ||
    typeof value.page_count !== "number" ||
    !Number.isSafeInteger(value.page_count) ||
    value.page_count < 1 ||
    value.page_count > MAX_READER_PAGE_COUNT
  ) {
    return null;
  }

  return {
    chapterId: value.chapter_id,
    seriesId: value.series_id,
    displayLabel: value.display_label,
    pageCount: value.page_count,
  };
}

export interface ReaderDownloadSource {
  connectorId: string;
  remoteSeriesId: string;
  chapterRemoteId: string;
}

export async function getReaderDownloadSource(chapterId: string): Promise<ReaderDownloadSource | null> {
  if (!UUID_V4_PATTERN.test(chapterId)) throw new RangeError("chapterId must be a UUID v4");
  const { payload, response } = await requestJson(`/reader/${chapterId}/download-source`);
  if (!isRecord(payload) || !hasExactKeys(payload, ["data", "meta"]) ||
    !isRecord(payload.meta) || Object.keys(payload.meta).length !== 0) {
    throw invalidReaderResponse(response);
  }
  if (payload.data === null) return null;
  const data = payload.data;
  if (!isRecord(data) || !hasExactKeys(data, [
    "chapter_id", "connector_id", "remote_series_id", "chapter_remote_id",
  ]) || data.chapter_id !== chapterId ||
    !isNonEmptyString(data.connector_id) || data.connector_id.length > 512 ||
    !isNonEmptyString(data.remote_series_id) || data.remote_series_id.length > 512 ||
    !isNonEmptyString(data.chapter_remote_id) || data.chapter_remote_id.length > 512) {
    throw invalidReaderResponse(response);
  }
  return { connectorId: data.connector_id, remoteSeriesId: data.remote_series_id,
    chapterRemoteId: data.chapter_remote_id };
}

export async function registerOnlineReaderChapter(
  connectorId: string, remoteSeriesId: string, chapterRemoteId: string,
): Promise<{ chapterId: string; seriesId: string }> {
  if ([connectorId, remoteSeriesId, chapterRemoteId].some(
    (value) => !isNonEmptyString(value) || value.length > 512,
  )) throw new RangeError("Online chapter references must be bounded nonempty strings");
  const { payload, response } = await requestJson(
    `/connectors/${encodeURIComponent(connectorId)}/series/${encodeURIComponent(remoteSeriesId)}/reader-chapter`,
    { method: "POST", body: JSON.stringify({ chapter_remote_id: chapterRemoteId }) },
  );
  if (
    !isRecord(payload) || !hasExactKeys(payload, ["data", "meta"]) ||
    !isRecord(payload.data) || !hasExactKeys(payload.data, ["chapter_id", "series_id", "display_label"]) ||
    typeof payload.data.chapter_id !== "string" || !UUID_V4_PATTERN.test(payload.data.chapter_id) ||
    typeof payload.data.series_id !== "string" || !UUID_V4_PATTERN.test(payload.data.series_id) ||
    !isNonEmptyString(payload.data.display_label) || !isRecord(payload.meta) ||
    !hasExactKeys(payload.meta, ["connector_id", "remote_series_id"]) ||
    payload.meta.connector_id !== connectorId || payload.meta.remote_series_id !== remoteSeriesId
  ) throw invalidReaderResponse(response);
  return { chapterId: payload.data.chapter_id, seriesId: payload.data.series_id };
}

export interface ReaderOpen {
  chapter: ReaderChapter;
  progress: ReaderProgress | null;
}

export async function openOnlineReader(chapterId: string, restart = false, prepare = false): Promise<ReaderOpen> {
  if (!UUID_V4_PATTERN.test(chapterId)) throw new RangeError("chapterId must be a UUID v4");
  const { payload, response } = await requestJson(`/reader/${encodeURIComponent(chapterId)}/open`, {
    method: "POST", body: JSON.stringify({ restart, ...(prepare ? { prepare: true } : {}) }),
  });
  if (!isRecord(payload) || !hasExactKeys(payload, ["data", "meta"]) ||
    !isRecord(payload.meta) || Object.keys(payload.meta).length !== 0 ||
    !isRecord(payload.data) || !hasExactKeys(payload.data, ["chapter", "progress"])) {
    throw invalidReaderResponse(response);
  }
  const chapter = parseReaderChapter(payload.data.chapter);
  const progress = payload.data.progress === null ? null : parseReaderProgress(payload.data.progress, chapterId);
  if (chapter === null || chapter.chapterId !== chapterId ||
    (payload.data.progress !== null && (progress === null || progress.seriesId !== chapter.seriesId))) {
    throw invalidReaderResponse(response);
  }
  return { chapter, progress };
}

export async function openOnlineReaderChapter(chapterId: string, restart = false): Promise<ReaderChapter> {
  if (!UUID_V4_PATTERN.test(chapterId)) throw new RangeError("chapterId must be a UUID v4");
  const { payload, response } = await requestJson(`/reader/${encodeURIComponent(chapterId)}/session`, {
    method: "POST", body: JSON.stringify({ restart }),
  });
  if (
    !isRecord(payload) || !hasExactKeys(payload, ["data", "meta"]) ||
    !isRecord(payload.meta) || Object.keys(payload.meta).length !== 0 ||
    !isRecord(payload.data) || !hasExactKeys(payload.data, [
      "session_id", "chapter_id", "state", "page_count", "ready_pages", "error_code",
    ]) || typeof payload.data.session_id !== "string" || !UUID_V4_PATTERN.test(payload.data.session_id) ||
    payload.data.chapter_id !== chapterId || payload.data.state !== "complete" ||
    typeof payload.data.page_count !== "number" || !Number.isSafeInteger(payload.data.page_count) ||
    payload.data.page_count < 1 || payload.data.page_count > MAX_READER_PAGE_COUNT ||
    !Array.isArray(payload.data.ready_pages) || payload.data.ready_pages.length > payload.data.page_count ||
    payload.data.ready_pages.some((page: unknown) => typeof page !== "number" || !Number.isSafeInteger(page) ||
      page < 1 || page > MAX_READER_PAGE_COUNT) ||
    new Set(payload.data.ready_pages).size !== payload.data.ready_pages.length ||
    payload.data.error_code !== null
  ) throw invalidReaderResponse(response);
  const pageCount = payload.data.page_count;
  if (payload.data.ready_pages.some((page: number) => page > pageCount)) throw invalidReaderResponse(response);
  return getReaderChapter(chapterId);
}

export async function getReaderChapter(chapterId: string): Promise<ReaderChapter> {
  if (!UUID_V4_PATTERN.test(chapterId)) {
    throw new RangeError("chapterId must be a UUID v4");
  }

  const { payload, response } = await requestJson(`/reader/${encodeURIComponent(chapterId)}`);
  if (
    !isRecord(payload) ||
    !hasExactKeys(payload, ["data", "meta"]) ||
    !isRecord(payload.meta) ||
    !Object.values(payload.meta).every((value) => typeof value === "string")
  ) {
    throw invalidReaderResponse(response);
  }

  const chapter = parseReaderChapter(payload.data);
  if (chapter === null || chapter.chapterId !== chapterId) {
    throw invalidReaderResponse(response);
  }
  return chapter;
}

export async function getReaderChaptersPage({
  seriesId,
  limit,
  offset,
  order = "asc",
  query = "",
}: {
  seriesId: string;
  limit: number;
  offset: number;
  order?: "asc" | "desc";
  query?: string;
}): Promise<ReaderChaptersPage> {
  if (!UUID_V4_PATTERN.test(seriesId)) {
    throw new RangeError("seriesId must be a UUID v4");
  }
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > MAX_READER_CHAPTERS_PAGE_SIZE) {
    throw new RangeError("limit must be between 1 and 100");
  }
  if (!Number.isSafeInteger(offset) || offset < 0) {
    throw new RangeError("offset must be a non-negative safe integer");
  }

  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (order !== "asc" && order !== "desc") throw new RangeError("Invalid chapter order");
  if (order === "desc") params.set("order", order);
  if (typeof query !== "string" || query.length > 200) throw new RangeError("Invalid chapter search");
  if (query.trim()) params.set("q", query.trim());
  const { payload, response } = await requestJson(
    `/reader/series/${encodeURIComponent(seriesId)}/chapters?${params.toString()}`,
  );
  if (
    !isRecord(payload) ||
    !hasExactKeys(payload, ["data", "meta"]) ||
    !Array.isArray(payload.data) ||
    !isRecord(payload.meta) ||
    !hasExactKeys(payload.meta, ["limit", "offset", "total"]) ||
    payload.meta.limit !== limit ||
    payload.meta.offset !== offset ||
    typeof payload.meta.total !== "number" ||
    !Number.isSafeInteger(payload.meta.total) ||
    payload.meta.total < 0
  ) {
    throw invalidReaderResponse(response);
  }

  const data: ReaderChapter[] = [];
  const chapterIds = new Set<string>();
  for (const record of payload.data) {
    const chapter = parseReaderChapter(record);
    if (
      chapter === null ||
      chapter.seriesId !== seriesId ||
      chapterIds.has(chapter.chapterId)
    ) {
      throw invalidReaderResponse(response);
    }
    chapterIds.add(chapter.chapterId);
    data.push(chapter);
  }
  if (data.length > limit || (data.length > 0 && offset + data.length > payload.meta.total)) {
    throw invalidReaderResponse(response);
  }

  return { data, meta: { limit, offset, total: payload.meta.total } };
}

function parseReaderProgress(value: unknown, chapterId: string): ReaderProgress | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "series_id",
      "chapter_id",
      "page_index",
      "percentage",
      "updated_at",
    ]) ||
    typeof value.series_id !== "string" ||
    !UUID_V4_PATTERN.test(value.series_id) ||
    value.chapter_id !== chapterId ||
    typeof value.page_index !== "number" ||
    !Number.isSafeInteger(value.page_index) ||
    value.page_index < 0 ||
    value.page_index >= MAX_READER_PAGE_COUNT ||
    typeof value.percentage !== "string" ||
    !/^(?:0|[1-9]\d?|100)\.\d{2}$/.test(value.percentage) ||
    (value.percentage.startsWith("100.") && value.percentage !== "100.00") ||
    typeof value.updated_at !== "string" ||
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/.test(value.updated_at) ||
    Number.isNaN(Date.parse(value.updated_at))
  ) {
    return null;
  }

  return {
    seriesId: value.series_id,
    chapterId: value.chapter_id,
    pageIndex: value.page_index,
    percentage: value.percentage,
    updatedAt: value.updated_at,
  };
}

function parseReaderProgressEnvelope(
  payload: unknown,
  response: Response,
  chapterId: string,
): ReaderProgress | null {
  if (
    !isRecord(payload) ||
    !hasExactKeys(payload, ["data", "meta"]) ||
    !isRecord(payload.meta) ||
    Object.keys(payload.meta).length !== 0
  ) {
    throw invalidReaderResponse(response);
  }
  if (payload.data === null) return null;
  const progress = parseReaderProgress(payload.data, chapterId);
  if (progress === null) throw invalidReaderResponse(response);
  return progress;
}

export async function getReaderProgress(chapterId: string): Promise<ReaderProgress | null> {
  if (!UUID_V4_PATTERN.test(chapterId)) {
    throw new RangeError("chapterId must be a UUID v4");
  }
  const { payload, response } = await requestJson(
    `/reader/${encodeURIComponent(chapterId)}/progress`,
  );
  return parseReaderProgressEnvelope(payload, response, chapterId);
}

export async function putReaderProgress(
  chapterId: string,
  pageIndex: number,
): Promise<ReaderProgress> {
  if (!UUID_V4_PATTERN.test(chapterId)) {
    throw new RangeError("chapterId must be a UUID v4");
  }
  if (!Number.isSafeInteger(pageIndex) || pageIndex < 0 || pageIndex >= MAX_READER_PAGE_COUNT) {
    throw new RangeError("pageIndex must be between 0 and 9999");
  }

  const { payload, response } = await requestJson(
    `/reader/${encodeURIComponent(chapterId)}/progress`,
    { method: "PUT", body: JSON.stringify({ page_index: pageIndex }) },
  );
  const progress = parseReaderProgressEnvelope(payload, response, chapterId);
  if (progress === null || progress.pageIndex !== pageIndex) {
    throw invalidReaderResponse(response);
  }
  return progress;
}

function parseReaderSettings(value: unknown): ReaderSettings | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["cache_limit_mb", "preload_ahead", "mode", "rtl", "e_reader"]) ||
    typeof value.cache_limit_mb !== "number" ||
    !Number.isSafeInteger(value.cache_limit_mb) ||
    value.cache_limit_mb < MIN_READER_CACHE_LIMIT_MB ||
    value.cache_limit_mb > MAX_READER_CACHE_LIMIT_MB ||
    typeof value.preload_ahead !== "number" ||
    !Number.isSafeInteger(value.preload_ahead) ||
    value.preload_ahead < 0 ||
    value.preload_ahead > MAX_READER_PRELOAD_AHEAD ||
    typeof value.mode !== "string" ||
    !READER_MODES.has(value.mode as ReaderMode) ||
    typeof value.rtl !== "boolean" ||
    typeof value.e_reader !== "boolean"
  ) {
    return null;
  }

  return {
    cache_limit_mb: value.cache_limit_mb,
    preload_ahead: value.preload_ahead,
    mode: value.mode as ReaderMode,
    rtl: value.rtl,
    e_reader: value.e_reader,
  };
}

function invalidReaderSettingsResponse(response: Response): ApiError {
  return new ApiError({
    code: null,
    message: "La API devolvió una configuración del lector no válida.",
    retryable: false,
    status: response.status,
    traceId: response.headers.get("X-Trace-ID"),
  });
}

function parseReaderSettingsEnvelope(payload: unknown, response: Response): ReaderSettings {
  if (
    !isRecord(payload) ||
    !hasExactKeys(payload, ["data", "meta"]) ||
    !isRecord(payload.meta) ||
    Object.keys(payload.meta).length !== 0
  ) {
    throw invalidReaderSettingsResponse(response);
  }
  const settings = parseReaderSettings(payload.data);
  if (settings === null) throw invalidReaderSettingsResponse(response);
  return settings;
}

export async function getReaderSettings(): Promise<ReaderSettings> {
  const { payload, response } = await requestJson("/settings/reader");
  return parseReaderSettingsEnvelope(payload, response);
}

export async function putReaderSettings(settings: ReaderSettings): Promise<ReaderSettings> {
  if (
    !Number.isSafeInteger(settings.cache_limit_mb) ||
    settings.cache_limit_mb < MIN_READER_CACHE_LIMIT_MB ||
    settings.cache_limit_mb > MAX_READER_CACHE_LIMIT_MB ||
    !Number.isSafeInteger(settings.preload_ahead) ||
    settings.preload_ahead < 0 ||
    settings.preload_ahead > MAX_READER_PRELOAD_AHEAD ||
    !READER_MODES.has(settings.mode) ||
    typeof settings.rtl !== "boolean" ||
    typeof settings.e_reader !== "boolean"
  ) {
    throw new RangeError("Reader settings are outside the supported limits");
  }

  const body = {
    cache_limit_mb: settings.cache_limit_mb,
    preload_ahead: settings.preload_ahead,
    mode: settings.mode,
    rtl: settings.rtl,
    e_reader: settings.e_reader,
  };
  const { payload, response } = await requestJson("/settings/reader", {
    method: "PUT",
    body: JSON.stringify(body),
  });
  return parseReaderSettingsEnvelope(payload, response);
}

function readerPageHttpError(response: Response, payload: unknown): ApiError {
  const error = parseApiError(payload);
  return new ApiError({
    code: error?.code ?? null,
    message: error?.message ?? "La API no pudo entregar la página solicitada.",
    retryable: error?.retryable ?? false,
    status: response.status,
    traceId: error?.traceId ?? response.headers.get("X-Trace-ID"),
  });
}

function invalidReaderPage(response: Response, message: string): ApiError {
  return new ApiError({
    code: null,
    message,
    retryable: false,
    status: response.status,
    traceId: response.headers.get("X-Trace-ID"),
  });
}

export async function getReaderPageBlob(
  chapterId: string,
  page: number,
  signal?: AbortSignal,
): Promise<Blob> {
  if (!UUID_V4_PATTERN.test(chapterId)) {
    throw new RangeError("chapterId must be a UUID v4");
  }
  if (!Number.isSafeInteger(page) || page < 1 || page > MAX_READER_PAGE_COUNT) {
    throw new RangeError("page must be between 1 and 10000");
  }

  const headers = { Accept: "image/jpeg, image/png, image/webp" };
  const init: RequestInit = { headers, cache: "no-store" };
  if (signal !== undefined) init.signal = signal;

  let response: Response;
  try {
    response = await fetch(
      `${API_BASE_URL}/reader/${encodeURIComponent(chapterId)}/pages/${page}`,
      init,
    );
  } catch (cause) {
    if (signal?.aborted) throw cause;
    throw new ApiError({
      code: null,
      message: "No se pudo conectar con la API local para cargar la página.",
      retryable: true,
      status: null,
      traceId: null,
      cause,
    });
  }

  if (!response.ok) {
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    throw readerPageHttpError(response, payload);
  }

  const contentType = response.headers.get("Content-Type")?.split(";", 1)[0]?.trim().toLowerCase();
  if (contentType === undefined || !READER_IMAGE_MIMES.has(contentType)) {
    throw invalidReaderPage(response, "La API devolvió un tipo de imagen no permitido.");
  }

  const contentLength = response.headers.get("Content-Length");
  if (contentLength !== null) {
    if (!/^(0|[1-9]\d*)$/.test(contentLength)) {
      throw invalidReaderPage(response, "La API devolvió un tamaño de página no válido.");
    }
    const declaredBytes = Number(contentLength);
    if (!Number.isSafeInteger(declaredBytes) || declaredBytes > MAX_READER_PAGE_BYTES) {
      throw invalidReaderPage(response, "La página supera el límite permitido de 64 MiB.");
    }
  }

  let blob: Blob;
  if (response.body === null) {
    try {
      blob = await response.blob();
    } catch (cause) {
      if (signal?.aborted) throw cause;
      throw invalidReaderPage(response, "La página no se pudo leer completamente.");
    }
  } else {
    const reader = response.body.getReader();
    const chunks: ArrayBuffer[] = [];
    let totalBytes = 0;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        totalBytes += value.byteLength;
        if (totalBytes > MAX_READER_PAGE_BYTES) {
          await reader.cancel();
          throw invalidReaderPage(response, "La página supera el límite permitido de 64 MiB.");
        }
        const copy = new Uint8Array(value.byteLength);
        copy.set(value);
        chunks.push(copy.buffer);
      }
      blob = new Blob(chunks, { type: contentType });
    } catch (cause) {
      if (cause instanceof ApiError || signal?.aborted) throw cause;
      throw invalidReaderPage(response, "La página no se pudo leer completamente.");
    } finally {
      reader.releaseLock();
    }
  }

  if (blob.size < 1 || blob.size > MAX_READER_PAGE_BYTES) {
    throw invalidReaderPage(response, "El tamaño de la página no coincide con el límite permitido.");
  }
  return blob.type === contentType ? blob : blob.slice(0, blob.size, contentType);
}

function parseLibrarySeries(value: unknown): LibrarySeriesSummary | null {
  if (!isRecord(value)) return null;
  const author = "author" in value ? value.author : null;
  const genres = "genres" in value ? value.genres : null;
  if (
    typeof value.series_id !== "string" ||
    !UUID_V4_PATTERN.test(value.series_id) ||
    !isNonEmptyString(value.canonical_title) ||
    !isNonEmptyString(value.display_title) ||
    !isOptionalString(author) ||
    !(genres === null ||
      (Array.isArray(genres) && genres.every((item): item is string => typeof item === "string"))) ||
    !isOptionalString(value.description) ||
    !isOptionalString(value.status) ||
    !isOptionalString(value.language) ||
    !(value.cover_asset_id === null ||
      (typeof value.cover_asset_id === "string" && UUID_V4_PATTERN.test(value.cover_asset_id))) ||
    typeof value.created_at !== "string" ||
    Number.isNaN(Date.parse(value.created_at)) ||
    typeof value.updated_at !== "string" ||
    Number.isNaN(Date.parse(value.updated_at))
  ) {
    return null;
  }

  return {
    seriesId: value.series_id,
    canonicalTitle: value.canonical_title,
    displayTitle: value.display_title,
    author,
    genres,
    description: value.description,
    status: value.status,
    language: value.language,
    coverAssetId: value.cover_asset_id,
    createdAt: value.created_at,
    updatedAt: value.updated_at,
  };
}

const metadataFields: MetadataField[] = [
  "title",
  "cover",
  "author",
  "description",
  "genres",
  "status",
  "language",
];
const metadataAuthorities = new Set<MetadataAuthority>([
  "USER_OVERRIDE",
  "LOCAL_CANONICAL",
  "SOURCE_METADATA",
]);

function isMetadataValue(field: MetadataField, value: unknown): value is MetadataValue {
  if (field === "genres") {
    return (
      value === null ||
      (Array.isArray(value) && value.every((item): item is string => typeof item === "string"))
    );
  }
  return value === null || typeof value === "string";
}

function parseMetadataResolution(value: unknown): MetadataResolution | null {
  if (!isRecord(value) || !metadataFields.includes(value.field as MetadataField)) return null;
  const field = value.field as MetadataField;
  if (
    !isMetadataValue(field, value.value) ||
    !isMetadataValue(field, value.local_value) ||
    !isMetadataValue(field, value.user_override_value) ||
    !(
      value.authority === null ||
      (typeof value.authority === "string" &&
        metadataAuthorities.has(value.authority as MetadataAuthority))
    ) ||
    typeof value.has_user_override !== "boolean" ||
    typeof value.source_conflict !== "boolean" ||
    !Array.isArray(value.source_values) ||
    !Array.isArray(value.effective_source_ids) ||
    !value.effective_source_ids.every(
      (sourceId): sourceId is string =>
        typeof sourceId === "string" && UUID_V4_PATTERN.test(sourceId),
    )
  ) {
    return null;
  }

  const sourceValues: MetadataSourceValue[] = [];
  for (const sourceValue of value.source_values) {
    if (
      !isRecord(sourceValue) ||
      typeof sourceValue.series_source_id !== "string" ||
      !UUID_V4_PATTERN.test(sourceValue.series_source_id) ||
      typeof sourceValue.source_id !== "string" ||
      !UUID_V4_PATTERN.test(sourceValue.source_id) ||
      !(sourceValue.source_display_name === null || typeof sourceValue.source_display_name === "string") ||
      !isMetadataValue(field, sourceValue.value) ||
      sourceValue.value === null
    ) {
      return null;
    }
    sourceValues.push({
      seriesSourceId: sourceValue.series_source_id,
      sourceId: sourceValue.source_id,
      sourceDisplayName: sourceValue.source_display_name,
      value: sourceValue.value,
    });
  }

  return {
    field,
    value: value.value,
    authority: value.authority as MetadataAuthority | null,
    localValue: value.local_value,
    sourceValues,
    hasUserOverride: value.has_user_override,
    userOverrideValue: value.user_override_value,
    sourceConflict: value.source_conflict,
    effectiveSourceIds: value.effective_source_ids,
  };
}

function parseLibrarySeriesDetail(value: unknown): LibrarySeriesDetail | null {
  const series = parseLibrarySeries(value);
  if (series === null || !isRecord(value) || !Array.isArray(value.metadata)) return null;

  const metadata: MetadataResolution[] = [];
  for (const item of value.metadata) {
    const resolution = parseMetadataResolution(item);
    if (resolution === null || metadata.some((existing) => existing.field === resolution.field)) {
      return null;
    }
    metadata.push(resolution);
  }
  if (metadata.length !== metadataFields.length) return null;

  const readingSources: LibraryReadingSource[] = [];
  if (value.reading_sources !== undefined) {
    if (!Array.isArray(value.reading_sources)) return null;
    for (const source of value.reading_sources) {
      if (!isRecord(source) || typeof source.series_source_id !== "string" ||
        !UUID_V4_PATTERN.test(source.series_source_id) || !isNonEmptyString(source.connector_id) ||
        !isNonEmptyString(source.remote_series_id) || !isNonEmptyString(source.display_name) ||
        !isOptionalString(source.author) || !isOptionalString(source.description) ||
        !isOptionalString(source.status) || !isOptionalString(source.language) ||
        !(source.genres === null || (Array.isArray(source.genres) && source.genres.every(isNonEmptyString))) ||
        readingSources.some((item) => item.seriesSourceId === source.series_source_id)) return null;
      readingSources.push({seriesSourceId: source.series_source_id, connectorId: source.connector_id,
        remoteSeriesId: source.remote_series_id, displayName: source.display_name,
        author: source.author, description: source.description, genres: source.genres,
        status: source.status, language: source.language});
    }
  }
  return { ...series, metadata, readingSources };
}

function invalidLibraryResponse(response: Response): ApiError {
  return new ApiError({
    code: null,
    message: "La API devolvió una respuesta de biblioteca no válida.",
    retryable: false,
    status: response.status,
    traceId: response.headers.get("X-Trace-ID"),
  });
}

function invalidSeriesResponse(response: Response): ApiError {
  return new ApiError({
    code: null,
    message: "La API devolvió una respuesta de serie no válida.",
    retryable: false,
    status: response.status,
    traceId: response.headers.get("X-Trace-ID"),
  });
}

function invalidConnectorSearchResponse(response: Response): ApiError {
  return new ApiError({
    code: null,
    message: "La API devolvió una respuesta de búsqueda no válida.",
    retryable: false,
    status: response.status,
    traceId: response.headers.get("X-Trace-ID"),
  });
}

function invalidConnectorsResponse(response: Response): ApiError {
  return new ApiError({
    code: null,
    message: "La API devolvió una lista de conectores no válida.",
    retryable: false,
    status: response.status,
    traceId: response.headers.get("X-Trace-ID"),
  });
}

function invalidConnectorChaptersResponse(response: Response): ApiError {
  return new ApiError({
    code: null,
    message: "La API devolvió una respuesta de capítulos no válida.",
    retryable: false,
    status: response.status,
    traceId: response.headers.get("X-Trace-ID"),
  });
}

function invalidConnectorCoverResponse(response: Response): ApiError {
  return new ApiError({
    code: null,
    message: "La fuente devolvió una portada no válida.",
    retryable: false,
    status: response.status,
    traceId: response.headers.get("X-Trace-ID"),
  });
}

function parseConnectorSearchResult(value: unknown): ConnectorSearchResult | null {
  const coverUrl = isRecord(value) ? value.cover_url ?? null : null;
  const sourceLabel = isRecord(value) ? value.source_label ?? null : null;
  if (
    !isRecord(value) ||
    !isOptionalRemoteReference(value.remote_id) ||
    !isOptionalString(value.remote_url) ||
    !isOptionalString(value.remote_title) ||
    !isOptionalString(coverUrl) ||
    !isOptionalString(sourceLabel) ||
    (sourceLabel !== null && sourceLabel.length > 200) ||
    (value.remote_id === null && value.remote_url === null)
  ) {
    return null;
  }

  return {
    remoteId: value.remote_id,
    remoteUrl: value.remote_url,
    remoteTitle: value.remote_title,
    coverUrl,
    sourceLabel,
  };
}

const connectorCapabilities = new Set<ConnectorCapability>([
  "SEARCH",
  "METADATA",
  "CHAPTER_LIST",
  "PAGE_LIST",
  "UPDATE_CHECK",
]);

function parseConnectorDescriptor(value: unknown): ConnectorDescriptor | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["connector_id", "connector_version", "capabilities"]) ||
    !isNonEmptyString(value.connector_id) ||
    !isNonEmptyString(value.connector_version) ||
    !Array.isArray(value.capabilities)
  ) {
    return null;
  }

  const capabilities: ConnectorCapability[] = [];
  const seen = new Set<ConnectorCapability>();
  for (const valueCapability of value.capabilities) {
    if (
      typeof valueCapability !== "string" ||
      !connectorCapabilities.has(valueCapability as ConnectorCapability) ||
      seen.has(valueCapability as ConnectorCapability)
    ) {
      return null;
    }
    const capability = valueCapability as ConnectorCapability;
    seen.add(capability);
    capabilities.push(capability);
  }

  return {
    connectorId: value.connector_id,
    connectorVersion: value.connector_version,
    capabilities,
  };
}

export async function getConnectors(): Promise<ConnectorsResponse> {
  const { payload, response } = await requestJson("/connectors");
  if (
    !isRecord(payload) ||
    !hasExactKeys(payload, ["data"]) ||
    !Array.isArray(payload.data)
  ) {
    throw invalidConnectorsResponse(response);
  }

  const data: ConnectorDescriptor[] = [];
  const connectorIds = new Set<string>();
  for (const record of payload.data) {
    const descriptor = parseConnectorDescriptor(record);
    if (descriptor === null || connectorIds.has(descriptor.connectorId)) {
      throw invalidConnectorsResponse(response);
    }
    connectorIds.add(descriptor.connectorId);
    data.push(descriptor);
  }
  return { data };
}

function parseConnectorChapter(value: unknown): ConnectorChapter | null {
  if (
    !isRecord(value) ||
    !isOptionalRemoteReference(value.remote_id) ||
    !isNonEmptyString(value.display_label) ||
    !isOptionalString(value.remote_label) ||
    !isNonEmptyString(value.chapter_raw_label) ||
    !Array.isArray(value.chapter_sort_key) ||
    !value.chapter_sort_key.every(
      (part): part is number => typeof part === "number" && Number.isSafeInteger(part),
    ) ||
    !isOptionalString(value.language)
  ) {
    return null;
  }

  return {
    remoteId: value.remote_id,
    displayLabel: value.display_label,
    remoteLabel: value.remote_label,
    chapterRawLabel: value.chapter_raw_label,
    chapterSortKey: value.chapter_sort_key,
    language: value.language,
  };
}

export async function getConnectorSearch(
  connectorId: string,
  query: string,
  pageBudget = 3,
  signal?: AbortSignal,
): Promise<ConnectorSearchResponse> {
  if (!isNonEmptyString(connectorId)) {
    throw new RangeError("connectorId must not be empty");
  }
  if (!Number.isInteger(pageBudget) || pageBudget < 1 || pageBudget > 20) {
    throw new RangeError("pageBudget must be an integer from 1 to 20");
  }
  const params = new URLSearchParams({ query });
  if (pageBudget !== 3) params.set("page_budget", String(pageBudget));
  const requestOptions: RequestOptions = {};
  if (signal !== undefined) requestOptions.signal = signal;
  const { payload, response } = await requestJson(
    `/connectors/${encodeURIComponent(connectorId)}/search?${params.toString()}`,
    requestOptions,
  );
  if (
    !isRecord(payload) ||
    !Array.isArray(payload.data) ||
    !isRecord(payload.meta) ||
    payload.meta.connector_id !== connectorId ||
    payload.meta.query !== query
  ) {
    throw invalidConnectorSearchResponse(response);
  }

  const data: ConnectorSearchResult[] = [];
  for (const record of payload.data) {
    const result = parseConnectorSearchResult(record);
    if (result === null) throw invalidConnectorSearchResponse(response);
    data.push(result);
  }

  return { data, meta: { connectorId, query } };
}

export async function getConnectorChapters(
  connectorId: string,
  remoteSeriesId: string,
  refresh = false,
): Promise<ConnectorChaptersResponse> {
  if (!isNonEmptyString(connectorId) || !isNonEmptyString(remoteSeriesId)) {
    throw new RangeError("connectorId and remoteSeriesId must not be empty");
  }
  const { payload, response } = await requestJson(
    `/connectors/${encodeURIComponent(connectorId)}/series/${encodeURIComponent(remoteSeriesId)}/chapters${refresh ? "?refresh=true" : ""}`,
  );
  if (
    !isRecord(payload) ||
    !Array.isArray(payload.data) ||
    !isRecord(payload.meta) ||
    payload.meta.connector_id !== connectorId ||
    payload.meta.remote_series_id !== remoteSeriesId
  ) {
    throw invalidConnectorChaptersResponse(response);
  }

  const data: ConnectorChapter[] = [];
  const remoteIds = new Set<string>();
  for (const record of payload.data) {
    const chapter = parseConnectorChapter(record);
    if (
      chapter === null ||
      (chapter.remoteId !== null && remoteIds.has(chapter.remoteId))
    ) {
      throw invalidConnectorChaptersResponse(response);
    }
    if (chapter.remoteId !== null) remoteIds.add(chapter.remoteId);
    data.push(chapter);
  }

  return { data, meta: { connectorId, remoteSeriesId } };
}

export async function getConnectorCover(
  connectorId: string,
  remoteSeriesId: string,
  title?: string,
): Promise<ConnectorCoverResponse> {
  if (!isNonEmptyString(connectorId) || !isNonEmptyString(remoteSeriesId) || remoteSeriesId.length > 512) {
    throw new RangeError("connectorId and remoteSeriesId must be nonempty valid references");
  }
  if (title !== undefined && (!isNonEmptyString(title) || title.length > 200)) {
    throw new RangeError("cover title must be nonempty and at most 200 characters");
  }
  const query = title === undefined ? "" : `?title=${encodeURIComponent(title)}`;
  const { payload, response } = await requestJson(
    `/connectors/${encodeURIComponent(connectorId)}/series/${encodeURIComponent(remoteSeriesId)}/cover${query}`,
  );
  if (
    !isRecord(payload) ||
    !hasExactKeys(payload, ["data", "meta"]) ||
    !isRecord(payload.data) ||
    !hasExactKeys(payload.data, ["cover_url"]) ||
    !isOptionalString(payload.data.cover_url) ||
    !isRecord(payload.meta) ||
    !hasExactKeys(payload.meta, ["connector_id", "remote_series_id"]) ||
    payload.meta.connector_id !== connectorId ||
    payload.meta.remote_series_id !== remoteSeriesId
  ) {
    throw invalidConnectorCoverResponse(response);
  }

  return {
    data: { coverUrl: payload.data.cover_url },
    meta: { connectorId, remoteSeriesId },
  };
}

export async function importConnectorLibrarySeries(
  connectorId: string,
  remoteSeriesId: string,
): Promise<ConnectorLibrarySeriesImport> {
  if (!isNonEmptyString(connectorId) || !isNonEmptyString(remoteSeriesId) || remoteSeriesId.length > 512) {
    throw new RangeError("connectorId and remoteSeriesId must be nonempty valid references");
  }
  const { payload, response } = await requestJson(
    `/connectors/${encodeURIComponent(connectorId)}/series/${encodeURIComponent(remoteSeriesId)}/library-series`,
    { method: "POST" },
  );
  if (
    response.status !== 200 ||
    !isRecord(payload) ||
    !hasExactKeys(payload, ["data", "meta"]) ||
    !isRecord(payload.data) ||
    !hasExactKeys(payload.data, ["series_id", "series_title", "created"]) ||
    typeof payload.data.series_id !== "string" ||
    !UUID_V4_PATTERN.test(payload.data.series_id) ||
    !isNonEmptyString(payload.data.series_title) ||
    payload.data.series_title.trim().length === 0 ||
    typeof payload.data.created !== "boolean" ||
    !isRecord(payload.meta) ||
    !hasExactKeys(payload.meta, ["connector_id", "remote_series_id"]) ||
    payload.meta.connector_id !== connectorId ||
    payload.meta.remote_series_id !== remoteSeriesId
  ) throw invalidSeriesResponse(response);

  return {
    data: {
      seriesId: payload.data.series_id,
      seriesTitle: payload.data.series_title,
      created: payload.data.created,
    },
    meta: { connectorId, remoteSeriesId },
  };
}

export interface LocalImportStatus {
  folder: string;
  folders: { kind: "library" | "inbox" | "downloads" | "legacy"; path: string; problem: string | null }[];
  scanning: boolean;
  importedChapters: number;
  pendingMetadata: number;
  metadataReview: number;
  errors: { file: string; message: string; chapterId?: string }[];
}

export async function getLocalImportStatus(retry = false): Promise<LocalImportStatus> {
  const { payload, response } = await requestJson(
    retry ? "/library/local-imports/retry" : "/library/local-imports",
    retry ? { method: "POST" } : undefined,
  );
  if (!isRecord(payload) || typeof payload.folder !== "string" ||
      typeof payload.scanning !== "boolean" ||
      typeof payload.imported_chapters !== "number" || !Number.isSafeInteger(payload.imported_chapters) || payload.imported_chapters < 0 ||
      typeof payload.pending_metadata !== "number" || !Number.isSafeInteger(payload.pending_metadata) || payload.pending_metadata < 0 ||
      (payload.metadata_review !== undefined && (typeof payload.metadata_review !== "number" || !Number.isSafeInteger(payload.metadata_review) || payload.metadata_review < 0)) ||
      !Array.isArray(payload.errors)) throw invalidLibraryResponse(response);
  const errors: LocalImportStatus["errors"] = [];
  if (payload.folders !== undefined && !Array.isArray(payload.folders)) {
    throw invalidLibraryResponse(response);
  }
  const folders: LocalImportStatus["folders"] = [];
  for (const item of (payload.folders as unknown[] | undefined) ?? []) {
    if (!isRecord(item) || !["library", "inbox", "downloads", "legacy"].includes(String(item.kind)) ||
        typeof item.path !== "string" || (item.problem !== null && typeof item.problem !== "string")) {
      throw invalidLibraryResponse(response);
    }
    folders.push({ kind: item.kind as LocalImportStatus["folders"][number]["kind"],
      path: item.path, problem: item.problem });
  }
  for (const item of payload.errors) {
    if (!isRecord(item) || typeof item.file !== "string" || typeof item.message !== "string" ||
        (item.chapter_id !== undefined && (typeof item.chapter_id !== "string" || !UUID_V4_PATTERN.test(item.chapter_id)))) {
      throw invalidLibraryResponse(response);
    }
    errors.push({ file: item.file, message: item.message,
      ...(typeof item.chapter_id === "string" ? { chapterId: item.chapter_id } : {}) });
  }
  return { folder: payload.folder, folders,
    scanning: payload.scanning,
    importedChapters: payload.imported_chapters, pendingMetadata: payload.pending_metadata,
    metadataReview: payload.metadata_review as number | undefined ?? 0, errors };
}

export async function openLibraryFolder(kind: "library" | "inbox" | "downloads"): Promise<void> {
  const { payload, response } = await requestJson(`/library/folders/${kind}/open`, { method: "POST" });
  if (response.status !== 204 || payload !== null) throw invalidLibraryResponse(response);
}

export async function getLibraryPage({
  limit,
  offset,
  readingStatus,
  query = "",
  sort,
}: LibraryPageRequest): Promise<LibraryPage> {
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 500) {
    throw new RangeError("limit must be between 1 and 500");
  }
  if (!Number.isSafeInteger(offset) || offset < 0) {
    throw new RangeError("offset must be a non-negative safe integer");
  }
  if (query.length > 200) throw new RangeError("query must not exceed 200 characters");
  if (sort !== undefined && !["title", "recent", "updated"].includes(sort)) {
    throw new RangeError("sort is not supported");
  }

  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (readingStatus !== undefined) params.set("reading_status", readingStatus);
  if (query.trim()) params.set("q", query.trim());
  if (sort !== undefined) params.set("sort", sort);
  const { payload, response } = await requestJson(`/library?${params.toString()}`);
  const meta = isRecord(payload) && isRecord(payload.meta) ? payload.meta : null;
  if (
    !isRecord(payload) ||
    !Array.isArray(payload.data) ||
    meta === null ||
    meta.limit !== limit ||
    meta.offset !== offset ||
    typeof meta.total !== "number" ||
    !Number.isSafeInteger(meta.total) ||
    meta.total < 0
  ) {
    throw invalidLibraryResponse(response);
  }

  const data: LibrarySeriesSummary[] = [];
  for (const record of payload.data) {
    const series = parseLibrarySeries(record);
    if (series === null) throw invalidLibraryResponse(response);
    data.push(series);
  }
  if (data.length > limit || data.length > meta.total) {
    throw invalidLibraryResponse(response);
  }

  return {
    data,
    meta: { limit, offset, total: meta.total },
  };
}

export async function getRecentLibrary(limit = 4): Promise<RecentLibraryPage> {
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > MAX_HOME_RECENT_LIMIT) {
    throw new RangeError("limit must be between 1 and 20");
  }

  const params = new URLSearchParams({ limit: String(limit) });
  const { payload, response } = await requestJson(`/library/recent?${params.toString()}`);
  if (
    !isRecord(payload) ||
    !hasExactKeys(payload, ["data", "meta"]) ||
    !Array.isArray(payload.data) ||
    !isRecord(payload.meta) ||
    !hasExactKeys(payload.meta, ["limit"]) ||
    payload.meta.limit !== limit ||
    payload.data.length > limit
  ) {
    throw invalidLibraryResponse(response);
  }

  const data: LibrarySeriesSummary[] = [];
  const seriesIds = new Set<string>();
  for (const record of payload.data) {
    const item = parseLibrarySeries(record);
    if (item === null || seriesIds.has(item.seriesId)) {
      throw invalidLibraryResponse(response);
    }
    seriesIds.add(item.seriesId);
    data.push(item);
  }

  return { data, meta: { limit } };
}

export async function getRecentReaderProgress(limit = 4, perSeries = false): Promise<RecentReaderProgressPage> {
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > MAX_HOME_RECENT_LIMIT) {
    throw new RangeError("limit must be between 1 and 20");
  }

  const params = new URLSearchParams({ limit: String(limit) });
  if (perSeries) params.set("per_series", "true");
  const { payload, response } = await requestJson(
    `/reader/progress/recent?${params.toString()}`,
  );
  if (
    !isRecord(payload) ||
    !hasExactKeys(payload, ["data", "meta"]) ||
    !Array.isArray(payload.data) ||
    !isRecord(payload.meta) ||
    !hasExactKeys(payload.meta, ["limit"]) ||
    payload.meta.limit !== limit ||
    payload.data.length > limit
  ) {
    throw invalidReaderResponse(response);
  }

  const data: RecentReaderProgress[] = [];
  const chapterIds = new Set<string>();
  const seriesIds = new Set<string>();
  for (const record of payload.data) {
    if (
      !isRecord(record) ||
      !hasExactKeys(record, [
        "series_id",
        "display_title",
        "chapter_id",
        "display_label",
        "page_index",
        "percentage",
        "updated_at",
      ]) ||
      typeof record.series_id !== "string" ||
      !UUID_V4_PATTERN.test(record.series_id) ||
      !isNonEmptyString(record.display_title) ||
      typeof record.chapter_id !== "string" ||
      !UUID_V4_PATTERN.test(record.chapter_id) ||
      !isNonEmptyString(record.display_label) ||
      typeof record.page_index !== "number" ||
      !Number.isSafeInteger(record.page_index) ||
      record.page_index < 0 ||
      record.page_index >= MAX_READER_PAGE_COUNT ||
      typeof record.percentage !== "string" ||
      !/^(?:0|[1-9]\d?|100)\.\d{2}$/.test(record.percentage) ||
      (record.percentage.startsWith("100.") && record.percentage !== "100.00") ||
      !isTimestamp(record.updated_at) ||
      chapterIds.has(record.chapter_id) ||
      (perSeries && seriesIds.has(record.series_id))
    ) {
      throw invalidReaderResponse(response);
    }
    chapterIds.add(record.chapter_id);
    seriesIds.add(record.series_id);
    data.push({
      seriesId: record.series_id,
      seriesDisplayTitle: record.display_title,
      chapterId: record.chapter_id,
      chapterDisplayLabel: record.display_label,
      pageIndex: record.page_index,
      percentage: record.percentage,
      updatedAt: record.updated_at,
    });
  }

  return { data, meta: { limit } };
}

export async function getSeriesDetail(seriesId: string): Promise<LibrarySeriesDetail> {
  const { payload, response } = await requestJson(`/series/${encodeURIComponent(seriesId)}`);
  if (!isRecord(payload) || !isRecord(payload.meta)) {
    throw invalidSeriesResponse(response);
  }

  const series = parseLibrarySeriesDetail(payload.data);
  if (series === null) throw invalidSeriesResponse(response);
  return series;
}

export type SeriesCoverVariant = "small" | "medium" | "large";

export function getSeriesCoverUrl(
  seriesId: string,
  variant: SeriesCoverVariant = "medium",
  version?: string,
): string {
  if (!UUID_V4_PATTERN.test(seriesId)) throw new TypeError("seriesId must be a UUIDv4");
  if (!["small", "medium", "large"].includes(variant)) {
    throw new TypeError("variant must be small, medium or large");
  }
  const params = new URLSearchParams({ variant });
  if (version !== undefined) params.set("v", version);
  return `${API_BASE_URL}/series/${seriesId}/cover?${params.toString()}`;
}

export async function refreshSeriesMetadata(seriesId: string): Promise<LibrarySeriesDetail> {
  const { payload, response } = await requestJson(
    `/series/${encodeURIComponent(seriesId)}/metadata/refresh`,
    { method: "POST" },
  );
  if (!isRecord(payload) || !isRecord(payload.meta)) throw invalidSeriesResponse(response);
  const series = parseLibrarySeriesDetail(payload.data);
  if (series === null || series.seriesId !== seriesId) throw invalidSeriesResponse(response);
  return series;
}

export async function getExternalMetadataCandidates(
  seriesId: string,
): Promise<ExternalMetadataCandidates> {
  if (!UUID_V4_PATTERN.test(seriesId)) throw new RangeError("seriesId must be a UUID v4");
  const { payload, response } = await requestJson(`/series/${seriesId}/metadata/external-candidates`);
  if (
    response.status !== 200 ||
    !isRecord(payload) ||
    !hasExactKeys(payload, ["data", "meta"]) ||
    !Array.isArray(payload.data) ||
    !isRecord(payload.meta) ||
    !hasExactKeys(payload.meta, ["provider", "total", "limit"]) ||
    payload.meta.provider !== "MangaDex" ||
    payload.meta.limit !== 10 ||
    typeof payload.meta.total !== "number" ||
    !Number.isSafeInteger(payload.meta.total) ||
    payload.meta.total < 0 ||
    payload.data.length > 10 ||
    payload.data.length > payload.meta.total
  ) throw invalidSeriesResponse(response);

  const data: ExternalMetadataCandidate[] = [];
  const remoteIds = new Set<string>();
  for (const item of payload.data) {
    if (
      !isRecord(item) ||
      !hasExactKeys(item, ["remote_id", "title", "year", "author"]) ||
      typeof item.remote_id !== "string" ||
      !UUID_V4_PATTERN.test(item.remote_id) ||
      remoteIds.has(item.remote_id) ||
      !isNonEmptyString(item.title) ||
      item.title.trim().length === 0 ||
      !(item.year === null || (
        typeof item.year === "number" && Number.isSafeInteger(item.year) && item.year >= 1 && item.year <= 9999
      )) ||
      !isOptionalString(item.author)
    ) throw invalidSeriesResponse(response);
    remoteIds.add(item.remote_id);
    data.push({ remoteId: item.remote_id, title: item.title, year: item.year, author: item.author });
  }
  return { data, meta: { provider: "MangaDex", total: payload.meta.total, limit: 10 } };
}

export async function applyExternalSeriesMetadata(
  seriesId: string,
  remoteId: string,
): Promise<LibrarySeriesDetail> {
  if (!UUID_V4_PATTERN.test(seriesId) || !UUID_V4_PATTERN.test(remoteId)) {
    throw new RangeError("seriesId and remoteId must be UUID v4");
  }
  const { payload, response } = await requestJson(`/series/${seriesId}/metadata/external`, {
    method: "POST", body: JSON.stringify({ remote_id: remoteId }),
  });
  if (
    response.status !== 200 || !isRecord(payload) ||
    !hasExactKeys(payload, ["data", "meta"]) || !isRecord(payload.meta)
  ) throw invalidSeriesResponse(response);
  const series = parseLibrarySeriesDetail(payload.data);
  if (series === null || series.seriesId !== seriesId) throw invalidSeriesResponse(response);
  return series;
}

export async function setMetadataOverride(
  seriesId: string,
  field: MetadataField,
  value: MetadataValue,
): Promise<void> {
  const { payload, response } = await requestJson(
    `/series/${encodeURIComponent(seriesId)}/metadata-overrides`,
    {
      method: "PUT",
      body: JSON.stringify({ field, value }),
    },
  );
  if (response.status !== 204 || payload !== null) throw invalidSeriesResponse(response);
}

export async function clearMetadataOverride(
  seriesId: string,
  field: MetadataField,
): Promise<void> {
  const { payload, response } = await requestJson(
    `/series/${encodeURIComponent(seriesId)}/metadata-overrides/${field}`,
    { method: "DELETE" },
  );
  if (response.status !== 204 || payload !== null) throw invalidSeriesResponse(response);
}

export async function getSystemLiveness(): Promise<LivenessResponse> {
  const { payload, response } = await requestJson("/system/liveness");
  if (!isRecord(payload) || payload.status !== "alive") {
    throw new ApiError({
      code: null,
      message: "La API devolvió un estado de disponibilidad no válido.",
      retryable: false,
      status: response.status,
      traceId: null,
    });
  }

  return { status: "alive" };
}

export async function getSystemReadiness(): Promise<SystemReadinessResponse> {
  const { payload, response } = await requestJson("/system/readiness", {
    acceptedStatuses: [503],
  });
  const isReadyPayload = isRecord(payload) && payload.status === "ready";
  const isNotReadyPayload = isRecord(payload) && payload.status === "not_ready";
  if (
    (!isReadyPayload && !isNotReadyPayload) ||
    (response.status !== 200 && response.status !== 503) ||
    (response.status === 200 && !isReadyPayload) ||
    (response.status === 503 && !isNotReadyPayload)
  ) {
    throw new ApiError({
      code: null,
      message: "La API devolvió un estado de preparación no válido.",
      retryable: false,
      status: response.status,
      traceId: response.headers.get("X-Trace-ID"),
    });
  }

  return { status: isReadyPayload ? "ready" : "not_ready" };
}

export async function getSystemDiagnostics(): Promise<SystemDiagnosticsResponse> {
  const { payload, response } = await requestJson("/system/diagnostics");
  if (
    !isRecord(payload) ||
    !hasExactKeys(payload, ["app_version", "api_version", "schema_version", "checks"]) ||
    typeof payload.app_version !== "string" ||
    typeof payload.api_version !== "number" ||
    !Number.isFinite(payload.api_version) ||
    !isOptionalString(payload.schema_version) ||
    !Array.isArray(payload.checks) ||
    payload.checks.length !== systemDiagnosticIds.size
  ) {
    throw invalidSystemDiagnosticsResponse(response);
  }

  const checks: SystemDiagnosticCheck[] = [];
  const seenDiagnosticIds = new Set<SystemDiagnosticId>();
  for (const value of payload.checks) {
    if (
      !isRecord(value) ||
      !hasExactKeys(value, ["id", "status", "code", "message", "trace_id"]) ||
      typeof value.id !== "string" ||
      !systemDiagnosticIds.has(value.id as SystemDiagnosticId) ||
      seenDiagnosticIds.has(value.id as SystemDiagnosticId) ||
      typeof value.status !== "string" ||
      !systemDiagnosticStatuses.has(value.status as SystemDiagnosticStatus) ||
      !isOptionalString(value.code) ||
      typeof value.message !== "string" ||
      !isOptionalString(value.trace_id)
    ) {
      throw invalidSystemDiagnosticsResponse(response);
    }

    checks.push({
      id: value.id as SystemDiagnosticId,
      status: value.status as SystemDiagnosticStatus,
      code: value.code,
      message: value.message,
      traceId: value.trace_id,
    });
    seenDiagnosticIds.add(value.id as SystemDiagnosticId);
  }

  if (seenDiagnosticIds.size !== systemDiagnosticIds.size) {
    throw invalidSystemDiagnosticsResponse(response);
  }

  return {
    appVersion: payload.app_version,
    apiVersion: payload.api_version,
    schemaVersion: payload.schema_version,
    checks,
  };
}

function invalidSystemDiagnosticsResponse(response: Response): ApiError {
  return new ApiError({
    code: null,
    message: "La API devolvió una respuesta de diagnóstico no válida.",
    retryable: false,
    status: response.status,
    traceId: response.headers.get("X-Trace-ID"),
  });
}

export async function getSystemVersion(): Promise<VersionResponse> {
  const { payload, response } = await requestJson("/system/version");
  if (
    !isRecord(payload) ||
    typeof payload.app_version !== "string" ||
    payload.app_version.length === 0 ||
    typeof payload.api_version !== "number"
  ) {
    throw new ApiError({
      code: null,
      message: "La API devolvió información de versión no válida.",
      retryable: false,
      status: response.status,
      traceId: null,
    });
  }

  if (payload.api_version !== API_VERSION) {
    throw new ApiError({
      code: null,
      message: "La versión de la API no es compatible con esta interfaz.",
      retryable: false,
      status: response.status,
      traceId: null,
    });
  }

  return { app_version: payload.app_version, api_version: payload.api_version };
}

export type RecommendationProvider = "MangaDex" | "AniList" | "MyAnimeList";
const recommendationFormats = {
  MangaDex: { criterion: "followed_count", id: /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    cover: /^https:\/\/uploads\.mangadex\.org\/covers\/[0-9a-f-]{36}\/[0-9a-f-]{36}\.(?:jpg|jpeg|png|webp)$/ },
  AniList: { criterion: "trending", id: /^anilist:[1-9][0-9]{0,9}$/,
    cover: /^https:\/\/s4\.anilist\.co\/file\/anilistcdn\/media\/manga\/cover\/(?:medium|large)\/[A-Za-z0-9_.-]+$/ },
  MyAnimeList: { criterion: "popularity", id: /^jikan:[1-9][0-9]{0,9}$/,
    cover: /^https:\/\/cdn\.myanimelist\.net\/images\/manga\/[0-9]+\/[0-9]+\.(?:jpg|jpeg|png|webp)$/ },
};
export interface Recommendation {
  remoteId: string; title: string; aliases: string[]; genres: string[]; coverUrl: string | null;
}

export interface PublicSource { sourceId:string; name:string; language:string }
export async function getPublicSources(): Promise<PublicSource[]> {
  const {payload,response}=await requestJson("/connectors/public-sources");
  const invalid=()=>new ApiError({code:null,message:"No se pudo consultar la lista de fuentes.",retryable:true,status:response.status,traceId:response.headers.get("X-Trace-ID")});
  if(!isRecord(payload)||!Array.isArray(payload.data)||payload.data.length>32) throw invalid();
  const seen=new Set<string>();
  return payload.data.map((item:unknown)=>{
    if(!isRecord(item)||typeof item.source_id!=="string"||!/^[A-Za-z0-9._-]{1,80}$/.test(item.source_id)||seen.has(item.source_id)||
      typeof item.name!=="string"||!item.name.trim()||item.name.length>80||typeof item.language!=="string"||!['es','en'].includes(item.language)) throw invalid();
    seen.add(item.source_id);
    return {sourceId:item.source_id,name:item.name,language:item.language as string};
  });
}
export interface RecommendationFeed {
  data: Recommendation[];
  meta: { provider: RecommendationProvider; status: "ready" | "stale" | "unavailable"; updatedAt: string | null; retryAt: string | null };
}
export async function getRecommendations(provider: RecommendationProvider = "MangaDex"): Promise<RecommendationFeed> {
  const format = recommendationFormats[provider];
  if (!format) throw new RangeError("Unknown recommendation provider");
  const {payload, response} = await requestJson(provider === "MangaDex" ? "/recommendations" : `/recommendations?provider=${provider}`);
  const invalid = () => new ApiError({code: null, message: "No se pudo leer la lista de recomendaciones.",
    retryable: true, status: response.status, traceId: response.headers.get("X-Trace-ID")});
  const shortText = (value: unknown): value is string => typeof value === "string" && value.trim().length > 0 && value.length <= 200 && !Array.from(value).some((char)=>char.charCodeAt(0)<32);
  const texts = (value: unknown, limit: number): value is string[] => Array.isArray(value) && value.length <= limit && value.every(shortText);
  const timestamp = (value: unknown): value is string | null => value === null || (typeof value === "string" && value.length <= 40 && Number.isFinite(Date.parse(value)));
  if (!isRecord(payload) || !Array.isArray(payload.data) || payload.data.length > 24 || !isRecord(payload.meta) ||
      payload.meta.provider !== provider || payload.meta.criterion !== format.criterion ||
      typeof payload.meta.status !== "string" || !["ready", "stale", "unavailable"].includes(payload.meta.status) ||
      !timestamp(payload.meta.updated_at) || !timestamp(payload.meta.retry_at)) throw invalid();
  const data: Recommendation[] = [];
  const seen = new Set<string>();
  for (const item of payload.data) {
    if (!isRecord(item) || typeof item.remote_id !== "string" || !format.id.test(item.remote_id) ||
        seen.has(item.remote_id) || !shortText(item.title) || !texts(item.aliases, 6) || !texts(item.genres, 64) ||
        !(item.cover_url === null || (typeof item.cover_url === "string" && format.cover.test(item.cover_url)))) throw invalid();
    seen.add(item.remote_id);
    data.push({remoteId:item.remote_id, title:item.title, aliases:item.aliases, genres:item.genres, coverUrl:item.cover_url});
  }
  if (payload.meta.status === "unavailable" && data.length || payload.meta.status !== "unavailable" && payload.meta.updated_at === null) throw invalid();
  return {data, meta:{provider, status:payload.meta.status as RecommendationFeed["meta"]["status"], updatedAt:payload.meta.updated_at, retryAt:payload.meta.retry_at}};
}
