import { getAuthHeaders } from './utils/auth';
import type {
  AnalyticsFilters,
  AnalyticsResponse,
  ConfigResponse,
  CreateProjectPayload,
  ExportFormat,
  HealthResponse,
  Project,
  Replay,
  ReplayPayload,
  ReplayProvidersResponse,
  ReplayResult,
  TimeBucket,
  Trace,
  TraceFilters,
  TraceQueryResponse,
} from './types';

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

function errorMessage(payload: unknown, fallback: string): string {
  if (payload && typeof payload === 'object' && 'error' in payload) {
    const detail = (payload as { error?: unknown }).error;
    if (typeof detail === 'string' && detail.trim()) return detail;
  }
  return fallback;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    ...getAuthHeaders(),
    ...(init.body === undefined ? {} : { 'Content-Type': 'application/json' }),
    ...((init.headers as Record<string, string> | undefined) ?? {}),
  };
  let response: Response;
  try {
    response = await fetch(path, { ...init, headers });
  } catch {
    throw new ApiError(0, 'Could not reach the dashboard API. Check that the server is running.');
  }
  if (response.status === 403) {
    throw new ApiError(403, 'Token invalid or expired. Reopen the dashboard with a fresh token link.');
  }
  if (!response.ok) {
    let payload: unknown = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    throw new ApiError(response.status, errorMessage(payload, `Request failed (${response.status})`));
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const FILTER_KEYS = [
  'status',
  'name',
  'name_contains',
  'q',
  'trace_id',
  'project_id',
  'environment',
  'start_time',
  'end_time',
  'error',
  'error_type',
  'span_type',
  'model_name',
  'min_duration_ms',
  'max_duration_ms',
  'sort_by',
  'sort_desc',
  'limit',
  'offset',
] as const;

function filterParams(filters: TraceFilters): URLSearchParams {
  const params = new URLSearchParams();
  for (const key of FILTER_KEYS) {
    const value = filters[key];
    if (value !== undefined && value !== null && value !== '') {
      params.append(key, String(value));
    }
  }
  return params;
}

function queryString(params: URLSearchParams): string {
  const query = params.toString();
  return query ? `?${query}` : '';
}

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>('/api/health');
}

export function getConfig(): Promise<ConfigResponse> {
  return request<ConfigResponse>('/api/config');
}

export function getTraces(filters: TraceFilters = {}): Promise<Trace[]> {
  return request<Trace[]>(`/api/traces${queryString(filterParams(filters))}`);
}

export function queryTraces(filters: TraceFilters = {}): Promise<TraceQueryResponse> {
  return request<TraceQueryResponse>(`/api/traces/query${queryString(filterParams(filters))}`);
}

export function getAnalytics(
  filters: AnalyticsFilters = {},
  bucket: TimeBucket = 'hour',
): Promise<AnalyticsResponse> {
  const params = filterParams(filters);
  params.set('bucket', bucket);
  return request<AnalyticsResponse>(`/api/analytics${queryString(params)}`);
}

export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function exportTraces(
  filters: TraceFilters = {},
  format: ExportFormat = 'jsonl',
): Promise<void> {
  const params = filterParams(filters);
  params.set('format', format);
  let response: Response;
  try {
    response = await fetch(`/api/export${queryString(params)}`, {
      headers: getAuthHeaders(),
    });
  } catch {
    throw new ApiError(0, 'Could not reach the dashboard API. Check that the server is running.');
  }
  if (response.status === 403) {
    throw new ApiError(403, 'Token invalid or expired. Reopen the dashboard with a fresh token link.');
  }
  if (!response.ok) {
    let payload: unknown = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    throw new ApiError(response.status, errorMessage(payload, `Export failed (${response.status})`));
  }
  const disposition = response.headers.get('content-disposition') ?? '';
  const match = disposition.match(/filename="?([^";]+)"?/i);
  const fallback = format === 'csv' ? 'tracekit-traces.csv' : 'tracekit-traces.jsonl';
  downloadBlob(await response.blob(), match?.[1] ?? fallback);
}

export function getReplayProviders(): Promise<ReplayProvidersResponse> {
  return request<ReplayProvidersResponse>('/api/replay/providers');
}

export function executeReplay(payload: ReplayPayload): Promise<ReplayResult> {
  return request<ReplayResult>('/api/replay', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function getTrace(traceId: string): Promise<Trace> {
  return request<Trace>(`/api/traces/${encodeURIComponent(traceId)}`);
}

export function getReplay(traceId: string): Promise<Replay> {
  return request<Replay>(`/api/traces/${encodeURIComponent(traceId)}/replay`);
}

export function getProjects(): Promise<Project[]> {
  return request<Project[]>('/api/projects');
}

export function createProject(payload: CreateProjectPayload): Promise<Project> {
  return request<Project>('/api/projects', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function archiveProject(projectId: string): Promise<{ project_id: string; archived: boolean }> {
  return request<{ project_id: string; archived: boolean }>(
    `/api/projects/${encodeURIComponent(projectId)}/archive`,
    { method: 'POST', body: '{}' },
  );
}

export function unarchiveProject(projectId: string): Promise<{ project_id: string; archived: boolean }> {
  return request<{ project_id: string; archived: boolean }>(
    `/api/projects/${encodeURIComponent(projectId)}/unarchive`,
    { method: 'POST', body: '{}' },
  );
}
