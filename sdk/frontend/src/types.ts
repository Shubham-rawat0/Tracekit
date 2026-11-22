export interface TraceEvent {
  name?: string | null;
  timestamp?: string | null;
  time?: string | null;
  attributes?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface Trace {
  trace_id: string;
  name?: string | null;
  status?: string | null;
  project_id?: string | null;
  project_name?: string | null;
  environment?: string | null;
  start_time?: string | null;
  end_time?: string | null;
  duration_ms?: number | null;
  latency_ms?: number | null;
  error?: string | null;
  error_type?: string | null;
  model_name?: string | null;
  model?: string | null;
  provider?: string | null;
  span_count?: number | null;
  llm_calls?: number | null;
  token_count?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  token_usage_source?: string | null;
  prompt?: string | null;
  response?: string | null;
  attributes?: Record<string, unknown> | null;
  metadata?: Record<string, unknown> | null;
  events?: TraceEvent[] | null;
  [key: string]: unknown;
}

export interface SpanNode {
  span_id?: string | null;
  trace_id?: string | null;
  parent_span_id?: string | null;
  name?: string | null;
  span_type?: string | null;
  type?: string | null;
  start_time?: string | null;
  end_time?: string | null;
  duration_ms?: number | null;
  status?: string | null;
  error?: string | null;
  error_type?: string | null;
  input?: unknown;
  output?: unknown;
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  token_usage_source?: string | null;
  attributes?: Record<string, unknown> | null;
  children?: SpanNode[] | null;
  [key: string]: unknown;
}

export interface Replay {
  trace?: Trace | null;
  spans?: SpanNode[] | null;
  tree?: SpanNode[] | null;
  [key: string]: unknown;
}

export interface ReplayComparisonEntry {
  trace_id?: string | null;
  span_id?: string | null;
  input?: unknown;
  output?: unknown;
  model?: string | null;
  provider?: string | null;
  latency_ms?: number | null;
  parameters?: Record<string, unknown> | null;
  token_usage?: Record<string, unknown> | null;
  status?: string | null;
  [key: string]: unknown;
}

export interface ReplayResult {
  trace_id?: string | null;
  span_id?: string | null;
  replay_of_trace_id?: string | null;
  replay_of_span_id?: string | null;
  status?: string | null;
  error?: string | null;
  warning?: string | null;
  comparison?: {
    original?: ReplayComparisonEntry | null;
    replay?: ReplayComparisonEntry | null;
    [key: string]: unknown;
  } | null;
  [key: string]: unknown;
}

export interface ReplayPayload {
  trace_id: string;
  span_id?: string | null;
  prompt?: string | null;
  parameters?: Record<string, unknown> | null;
  model?: string | null;
  provider?: string | null;
  [key: string]: unknown;
}

export interface ReplayProvidersResponse {
  providers: string[];
  [key: string]: unknown;
}

export interface Project {
  project_id: string;
  name?: string | null;
  description?: string | null;
  archived?: boolean | null;
  trace_count?: number | null;
  created_at?: string | null;
  [key: string]: unknown;
}

export interface CreateProjectPayload {
  name: string;
  project_id?: string;
  description?: string;
}

export type ExportFormat = 'csv' | 'jsonl';
export type TimeBucket = 'hour' | 'day';
export type RangePreset = 'any' | '1h' | '24h' | '7d' | '30d' | 'custom';

export interface DashboardFilters {
  project_id: string;
  environment: string;
  range: RangePreset;
  from: string;
  to: string;
  model_name: string;
  bucket: TimeBucket;
  q: string;
  status: string;
  name_contains: string;
  error: '' | 'true' | 'false';
  error_type: string;
  span_type: string;
  errorsOnly: boolean;
  sort_by: string;
  sort_desc: boolean;
}

export const defaultDashboardFilters: DashboardFilters = {
  project_id: '',
  environment: '',
  range: 'any',
  from: '',
  to: '',
  model_name: '',
  bucket: 'hour',
  q: '',
  status: '',
  name_contains: '',
  error: '',
  error_type: '',
  span_type: '',
  errorsOnly: false,
  sort_by: 'start_time',
  sort_desc: true,
};

export interface TraceFilters {
  status?: string | null;
  name?: string | null;
  name_contains?: string | null;
  q?: string | null;
  trace_id?: string | null;
  project_id?: string | null;
  environment?: string | null;
  start_time?: string | null;
  end_time?: string | null;
  error?: string | boolean | null;
  error_type?: string | null;
  span_type?: string | null;
  model_name?: string | null;
  min_duration_ms?: string | number | null;
  max_duration_ms?: string | number | null;
  sort_by?: string | null;
  sort_desc?: string | boolean | null;
  limit?: number | null;
  offset?: number | null;
  [key: string]: unknown;
}

export interface TraceQueryResponse {
  items: Trace[];
  total: number;
  limit: number;
  offset: number;
  sort_by: string;
  sort_desc: boolean;
  [key: string]: unknown;
}

export interface AnalyticsFilters extends TraceFilters {
  bucket?: TimeBucket | null;
}

export interface AnalyticsPoint {
  timestamp?: string | null;
  traces?: number | null;
  failed_traces?: number | null;
  error_rate?: number | null;
  avg_latency_ms?: number | null;
  tokens?: number | null;
  cost_usd?: number | null;
  [key: string]: unknown;
}

export interface AnalyticsModel {
  model?: string | null;
  providers?: string[] | null;
  traces?: number | null;
  failed_traces?: number | null;
  avg_latency_ms?: number | null;
  tokens?: number | null;
  cost_usd?: number | null;
  [key: string]: unknown;
}

export interface AnalyticsErrorGroup {
  error_type?: string | null;
  message?: string | null;
  count?: number | null;
  sample_trace_id?: string | null;
  trace_ids?: string[] | null;
  span_ids?: string[] | null;
  projects?: string[] | null;
  environments?: string[] | null;
  models?: string[] | null;
  first_seen?: string | null;
  last_seen?: string | null;
  stacktrace?: string | null;
  [key: string]: unknown;
}

export interface SlowTrace {
  trace_id?: string | null;
  name?: string | null;
  duration_ms?: number | null;
  status?: string | null;
  project_id?: string | null;
  environment?: string | null;
  model?: string | null;
  [key: string]: unknown;
}

export interface SlowSpan {
  trace_id?: string | null;
  span_id?: string | null;
  name?: string | null;
  span_type?: string | null;
  duration_ms?: number | null;
  status?: string | null;
  error?: string | null;
  [key: string]: unknown;
}

export interface AnalyticsSlowest {
  traces?: SlowTrace[] | null;
  spans?: SlowSpan[] | null;
  [key: string]: unknown;
}

export interface AnalyticsSummary {
  total_traces?: number | null;
  total_spans?: number | null;
  total_llm_calls?: number | null;
  failed_traces?: number | null;
  error_rate?: number | null;
  avg_latency_ms?: number | null;
  p50_latency_ms?: number | null;
  p95_latency_ms?: number | null;
  p99_latency_ms?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  total_cost_usd?: number | null;
  tokens_available?: boolean | null;
  cost_available?: boolean | null;
  traces_with_model?: number | null;
  traces_with_tokens?: number | null;
  traces_with_cost?: number | null;
  traces_with_error_type?: number | null;
  [key: string]: unknown;
}

export interface AnalyticsResponse {
  summary?: AnalyticsSummary | null;
  timeseries?: AnalyticsPoint[] | null;
  models?: AnalyticsModel[] | null;
  errors?: AnalyticsErrorGroup[] | null;
  slowest?: AnalyticsSlowest | null;
  [key: string]: unknown;
}

export interface HealthResponse {
  status?: string | null;
  storage_type?: string | null;
  storage_detail?: string | null;
  [key: string]: unknown;
}

export interface ConfigResponse {
  db_path?: string | null;
  version?: string | null;
  [key: string]: unknown;
}

export type TraceTab =
  | 'waterfall'
  | 'overview'
  | 'input'
  | 'output'
  | 'events'
  | 'attributes'
  | 'error'
  | 'raw';
