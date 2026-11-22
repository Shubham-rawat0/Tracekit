import { defaultDashboardFilters } from '../types';
import type { DashboardFilters, RangePreset, TimeBucket } from '../types';

const RANGES: RangePreset[] = ['any', '1h', '24h', '7d', '30d', 'custom'];
const BUCKETS: TimeBucket[] = ['hour', 'day'];

function hashParts(hash: string): { path: string; query: string } {
  const raw = hash.startsWith('#') ? hash.slice(1) : hash;
  const queryIndex = raw.indexOf('?');
  return {
    path: queryIndex === -1 ? raw || '/' : raw.slice(0, queryIndex) || '/',
    query: queryIndex === -1 ? '' : raw.slice(queryIndex + 1),
  };
}

function setFilterValue(filters: DashboardFilters, key: keyof DashboardFilters, raw: string): void {
  switch (key) {
    case 'range':
      filters.range = RANGES.includes(raw as RangePreset)
        ? (raw as RangePreset)
        : defaultDashboardFilters.range;
      break;
    case 'bucket':
      filters.bucket = BUCKETS.includes(raw as TimeBucket)
        ? (raw as TimeBucket)
        : defaultDashboardFilters.bucket;
      break;
    case 'errorsOnly':
    case 'sort_desc':
      filters[key] = raw === '1' || raw === 'true';
      break;
    case 'error':
      filters.error = raw === 'true' || raw === 'false' ? raw : '';
      break;
    case 'q':
    case 'from':
    case 'to':
    case 'project_id':
    case 'environment':
    case 'model_name':
    case 'status':
    case 'name_contains':
    case 'error_type':
    case 'span_type':
    case 'sort_by':
      filters[key] = raw;
      break;
  }
}

export function filtersFromHash(hash: string): DashboardFilters | null {
  const { query } = hashParts(hash);
  if (!query) return null;
  const params = new URLSearchParams(query);
  const filters: DashboardFilters = { ...defaultDashboardFilters };
  let found = false;
  for (const key of Object.keys(filters) as (keyof DashboardFilters)[]) {
    if (!params.has(key)) continue;
    found = true;
    setFilterValue(filters, key, params.get(key) ?? '');
  }
  return found ? filters : null;
}

export function filtersToQuery(filters: DashboardFilters): string {
  const params = new URLSearchParams();
  for (const key of Object.keys(filters) as (keyof DashboardFilters)[]) {
    const value = filters[key];
    if (value === defaultDashboardFilters[key]) continue;
    if (typeof value === 'boolean') {
      if (value) params.set(key, '1');
    } else if (value !== '') {
      params.set(key, String(value));
    }
  }
  return params.toString();
}

export function syncFiltersToHash(filters: DashboardFilters, hash?: string): void {
  const current = hash ?? window.location.hash ?? '#/';
  const { path } = hashParts(current);
  const query = filtersToQuery(filters);
  const next = `#${path}${query ? `?${query}` : ''}`;
  if (current === next) return;
  const base = `${window.location.pathname}${window.location.search}`;
  window.history.replaceState(window.history.state, '', `${base}${next}`);
}

export function hrefWithFilters(href: string, filters: DashboardFilters): string {
  const { path } = hashParts(href);
  const query = filtersToQuery(filters);
  return `#${path}${query ? `?${query}` : ''}`;
}
