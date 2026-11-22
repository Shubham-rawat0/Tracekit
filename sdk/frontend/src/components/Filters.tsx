import type {
  AnalyticsFilters,
  DashboardFilters,
  Project,
  RangePreset,
  TimeBucket,
  TraceFilters,
} from '../types';

export type { RangePreset, TimeBucket } from '../types';

export interface AnalyticsFilterValues {
  range: RangePreset;
  from: string;
  to: string;
  project_id: string;
  environment: string;
  model_name: string;
  bucket: TimeBucket;
}

export const defaultAnalyticsFilters: AnalyticsFilterValues = {
  range: 'any',
  from: '',
  to: '',
  project_id: '',
  environment: '',
  model_name: '',
  bucket: 'hour',
};

export function analyticsTimeBounds(
  values: AnalyticsFilterValues,
): Pick<TraceFilters, 'start_time' | 'end_time'> {
  if (values.range === 'any') return {};
  if (values.range === 'custom') {
    const start = values.from ? new Date(values.from) : null;
    const end = values.to ? new Date(values.to) : null;
    return {
      start_time: start && !Number.isNaN(start.getTime()) ? start.toISOString() : undefined,
      end_time: end && !Number.isNaN(end.getTime()) ? end.toISOString() : undefined,
    };
  }
  const hours =
    values.range === '1h' ? 1 : values.range === '7d' ? 168 : values.range === '30d' ? 720 : 24;
  const now = Date.now();
  return {
    start_time: new Date(now - hours * 3_600_000).toISOString(),
    end_time: new Date(now).toISOString(),
  };
}

function optional(value: string): string | undefined {
  return value === '' ? undefined : value;
}

export function dashboardTimeBounds(
  filters: DashboardFilters,
): Pick<TraceFilters, 'start_time' | 'end_time'> {
  if (filters.range === 'any') return { start_time: '', end_time: '' };
  if (filters.range === 'custom') {
    const start = filters.from ? new Date(filters.from) : null;
    const end = filters.to ? new Date(filters.to) : null;
    return {
      start_time: start && !Number.isNaN(start.getTime()) ? start.toISOString() : '',
      end_time: end && !Number.isNaN(end.getTime()) ? end.toISOString() : '',
    };
  }
  const hours =
    filters.range === '1h' ? 1 : filters.range === '7d' ? 168 : filters.range === '30d' ? 720 : 24;
  const now = Date.now();
  return {
    start_time: new Date(now - hours * 3_600_000).toISOString(),
    end_time: new Date(now).toISOString(),
  };
}

export function toAnalyticsFilters(filters: DashboardFilters): AnalyticsFilters {
  return {
    ...dashboardTimeBounds(filters),
    project_id: optional(filters.project_id),
    environment: optional(filters.environment),
    model_name: optional(filters.model_name),
    bucket: filters.bucket,
    q: optional(filters.q),
    status: optional(filters.status),
    name_contains: optional(filters.name_contains),
    error: filters.errorsOnly || filters.error === 'true' ? 'true' : optional(filters.error),
    error_type: optional(filters.error_type),
    span_type: optional(filters.span_type),
    sort_by: filters.sort_by,
    sort_desc: filters.sort_desc,
    limit: -1,
    offset: 0,
  };
}

export function toTraceFilters(
  filters: DashboardFilters,
  limit: number,
  offset: number,
): TraceFilters {
  return {
    ...dashboardTimeBounds(filters),
    project_id: optional(filters.project_id),
    environment: optional(filters.environment),
    model_name: optional(filters.model_name),
    q: optional(filters.q),
    status: optional(filters.status),
    name_contains: optional(filters.name_contains),
    error: filters.errorsOnly || filters.error === 'true' ? 'true' : optional(filters.error),
    error_type: optional(filters.error_type),
    span_type: optional(filters.span_type),
    sort_by: filters.sort_by,
    sort_desc: filters.sort_desc,
    limit,
    offset,
  };
}

export function filterSummary(filters: DashboardFilters): string {
  const parts: string[] = [];
  if (filters.range === 'any') {
    parts.push('all time');
  } else if (filters.range === 'custom') {
    parts.push(`${filters.from || 'start'} → ${filters.to || 'now'}`);
  } else {
    parts.push(`last ${filters.range}`);
  }
  if (filters.project_id) parts.push(`project:${filters.project_id.slice(0, 12)}`);
  if (filters.environment) parts.push(`env:${filters.environment}`);
  if (filters.model_name) parts.push(`model:${filters.model_name}`);
  if (filters.q) parts.push(`q:${filters.q}`);
  if (filters.errorsOnly || filters.error === 'true') parts.push('errors only');
  else if (filters.error === 'false') parts.push('no errors');
  if (filters.status) parts.push(filters.status);
  if (filters.error_type) parts.push(filters.error_type);
  return parts.join(' · ');
}

interface GlobalFilterBarProps {
  filters: DashboardFilters;
  onChange: (patch: Partial<DashboardFilters>) => void;
  projects: Project[];
  environments: string[];
  models: string[];
  route: 'traces' | 'overview' | 'analytics' | 'errors' | 'projects';
}

export default function GlobalFilterBar({
  filters,
  onChange,
  projects,
  environments,
  models,
  route,
}: GlobalFilterBarProps) {
  const modelOptions = filters.model_name && !models.includes(filters.model_name)
    ? [filters.model_name, ...models]
    : models;
  const environmentOptions = filters.environment && !environments.includes(filters.environment)
    ? [filters.environment, ...environments]
    : environments;
  const sortOptions = ['start_time', 'duration_ms', 'name', 'status'];
  const sortOptionsWithCurrent = filters.sort_by && !sortOptions.includes(filters.sort_by)
    ? [filters.sort_by, ...sortOptions]
    : sortOptions;

  return (
    <div className="filter-panel">
      <div className="filter-grid">
        <label className="toolbar-field">
          <span>Project</span>
          <select
            value={filters.project_id}
            onChange={(event) => onChange({ project_id: event.target.value })}
          >
            <option value="">All projects</option>
            {projects.map((project) => (
              <option key={project.project_id} value={project.project_id}>
                {project.name || project.project_id}
              </option>
            ))}
          </select>
        </label>
        <label className="toolbar-field">
          <span>Environment</span>
          <input
            type="text"
            list="global-environment-options"
            placeholder="All environments"
            value={filters.environment}
            onChange={(event) => onChange({ environment: event.target.value })}
          />
          <datalist id="global-environment-options">
            {environmentOptions.map((environment) => (
              <option key={environment} value={environment} />
            ))}
          </datalist>
        </label>
        <label className="toolbar-field">
          <span>Time range</span>
          <select
            value={filters.range}
            onChange={(event) => onChange({ range: event.target.value as RangePreset })}
          >
            <option value="1h">Last 1h</option>
            <option value="24h">Last 24h</option>
            <option value="7d">Last 7d</option>
            <option value="30d">Last 30d</option>
            <option value="custom">Custom</option>
            <option value="any">All time</option>
          </select>
        </label>
        {filters.range === 'custom' && (
          <>
            <label className="toolbar-field">
              <span>From</span>
              <input
                type="datetime-local"
                value={filters.from}
                onChange={(event) => onChange({ from: event.target.value })}
              />
            </label>
            <label className="toolbar-field">
              <span>To</span>
              <input
                type="datetime-local"
                value={filters.to}
                onChange={(event) => onChange({ to: event.target.value })}
              />
            </label>
          </>
        )}
        <label className="toolbar-field">
          <span>Model</span>
          <select
            value={filters.model_name}
            onChange={(event) => onChange({ model_name: event.target.value })}
          >
            <option value="">All models</option>
            {modelOptions.map((model) => (
              <option key={model} value={model}>
                {model}
              </option>
            ))}
          </select>
        </label>
        <label className="toolbar-field">
          <span>Bucket</span>
          <select
            value={filters.bucket}
            onChange={(event) => onChange({ bucket: event.target.value as TimeBucket })}
          >
            <option value="hour">Hourly</option>
            <option value="day">Daily</option>
          </select>
        </label>
        {route === 'traces' && (
          <>
            <label className="toolbar-field">
              <span>Search</span>
              <input
                type="text"
                placeholder="Trace ID or text"
                value={filters.q}
                onChange={(event) => onChange({ q: event.target.value })}
              />
            </label>
            <label className="toolbar-field">
              <span>Status</span>
              <input
                type="text"
                placeholder="Any status"
                value={filters.status}
                onChange={(event) => onChange({ status: event.target.value })}
              />
            </label>
            <label className="toolbar-field">
              <span>Name contains</span>
              <input
                type="text"
                placeholder="Any name"
                value={filters.name_contains}
                onChange={(event) => onChange({ name_contains: event.target.value })}
              />
            </label>
            <label className="toolbar-field">
              <span>Error</span>
              <select
                value={filters.error}
                onChange={(event) =>
                  onChange({ error: event.target.value as DashboardFilters['error'] })
                }
              >
                <option value="">Any</option>
                <option value="true">With errors</option>
                <option value="false">Without errors</option>
              </select>
            </label>
            <label className="toolbar-field">
              <span>Error type</span>
              <input
                type="text"
                placeholder="Any error type"
                value={filters.error_type}
                onChange={(event) => onChange({ error_type: event.target.value })}
              />
            </label>
            <label className="toolbar-field">
              <span>Span type</span>
              <input
                type="text"
                placeholder="Any span type"
                value={filters.span_type}
                onChange={(event) => onChange({ span_type: event.target.value })}
              />
            </label>
            <label className="toolbar-field">
              <span>Sort by</span>
              <select
                value={filters.sort_by}
                onChange={(event) => onChange({ sort_by: event.target.value })}
              >
                {sortOptionsWithCurrent.map((option) => (
                  <option key={option} value={option}>
                    {option}
                  </option>
                ))}
              </select>
            </label>
            <label className="checkbox-field filter-check">
              <input
                type="checkbox"
                checked={filters.sort_desc}
                onChange={(event) => onChange({ sort_desc: event.target.checked })}
              />
              Descending
            </label>
            <label className="checkbox-field filter-check">
              <input
                type="checkbox"
                checked={filters.errorsOnly}
                onChange={(event) => onChange({ errorsOnly: event.target.checked })}
              />
              Errors only
            </label>
          </>
        )}
      </div>
    </div>
  );
}

interface AnalyticsFilterBarProps {
  values: AnalyticsFilterValues;
  onChange: (patch: Partial<AnalyticsFilterValues>) => void;
  projects: Project[];
  models: string[];
  environments?: string[];
  showBucket?: boolean;
}

export function AnalyticsFilterBar({
  values,
  onChange,
  projects,
  models,
  environments = [],
  showBucket = true,
}: AnalyticsFilterBarProps) {
  const modelOptions = values.model_name && !models.includes(values.model_name)
    ? [values.model_name, ...models]
    : models;
  const environmentOptions = values.environment && !environments.includes(values.environment)
    ? [values.environment, ...environments]
    : environments;

  return (
    <div className="filter-panel">
      <div className="filter-grid">
        <label className="toolbar-field">
          <span>Time</span>
          <select
            value={values.range}
            onChange={(event) => onChange({ range: event.target.value as RangePreset })}
          >
            <option value="1h">Last 1h</option>
            <option value="24h">Last 24h</option>
            <option value="7d">Last 7d</option>
            <option value="30d">Last 30d</option>
            <option value="custom">Custom</option>
            <option value="any">All time</option>
          </select>
        </label>
        <label className="toolbar-field">
          <span>Project</span>
          <select
            value={values.project_id}
            onChange={(event) => onChange({ project_id: event.target.value })}
          >
            <option value="">All projects</option>
            {projects.map((project) => (
              <option key={project.project_id} value={project.project_id}>
                {project.name || project.project_id}
              </option>
            ))}
          </select>
        </label>
        <label className="toolbar-field">
          <span>Environment</span>
          <input
            type="text"
            list="analytics-environment-options"
            placeholder="All environments"
            value={values.environment}
            onChange={(event) => onChange({ environment: event.target.value })}
          />
          <datalist id="analytics-environment-options">
            {environmentOptions.map((environment) => (
              <option key={environment} value={environment} />
            ))}
          </datalist>
        </label>
        <label className="toolbar-field">
          <span>Model</span>
          <select
            value={values.model_name}
            onChange={(event) => onChange({ model_name: event.target.value })}
          >
            <option value="">All models</option>
            {modelOptions.map((model) => (
              <option key={model} value={model}>
                {model}
              </option>
            ))}
          </select>
        </label>
        {values.range === 'custom' && (
          <>
            <label className="toolbar-field">
              <span>From</span>
              <input
                type="datetime-local"
                value={values.from}
                onChange={(event) => onChange({ from: event.target.value })}
              />
            </label>
            <label className="toolbar-field">
              <span>To</span>
              <input
                type="datetime-local"
                value={values.to}
                onChange={(event) => onChange({ to: event.target.value })}
              />
            </label>
          </>
        )}
        {showBucket && (
          <label className="toolbar-field">
            <span>Bucket</span>
            <select
              value={values.bucket}
              onChange={(event) => onChange({ bucket: event.target.value as TimeBucket })}
            >
              <option value="hour">Hourly</option>
              <option value="day">Daily</option>
            </select>
          </label>
        )}
      </div>
    </div>
  );
}
