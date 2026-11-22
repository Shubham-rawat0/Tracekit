import { defaultDashboardFilters } from '../types';
import type { DashboardFilters, Project } from '../types';

export interface ActiveChip {
  key: string;
  label: string;
  patch: Partial<DashboardFilters>;
  advanced: boolean;
}

function shorten(value: string, max = 32): string {
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

export function getActiveChips(filters: DashboardFilters, projects: Project[]): ActiveChip[] {
  const chips: ActiveChip[] = [];
  if (filters.q) {
    chips.push({ key: 'q', label: `Search: ${shorten(filters.q)}`, patch: { q: '' }, advanced: false });
  }
  if (filters.range !== defaultDashboardFilters.range) {
    const label =
      filters.range === 'any'
        ? 'All time'
        : filters.range === 'custom'
          ? 'Custom range'
          : `Last ${filters.range}`;
    chips.push({
      key: 'range',
      label: `Range: ${label}`,
      patch: { range: defaultDashboardFilters.range, from: '', to: '' },
      advanced: false,
    });
  }
  if (filters.status) {
    const label = filters.status.toLowerCase() === 'ok' ? 'OK' : 'Error';
    chips.push({
      key: 'status',
      label: `Status: ${label}`,
      patch: { status: '' },
      advanced: false,
    });
  }
  if (filters.project_id) {
    const project = projects.find((item) => item.project_id === filters.project_id);
    chips.push({
      key: 'project_id',
      label: `Project: ${shorten(project?.name || filters.project_id)}`,
      patch: { project_id: '' },
      advanced: true,
    });
  }
  if (filters.model_name) {
    chips.push({
      key: 'model_name',
      label: `Model: ${shorten(filters.model_name)}`,
      patch: { model_name: '' },
      advanced: true,
    });
  }
  if (filters.bucket !== defaultDashboardFilters.bucket) {
    chips.push({
      key: 'bucket',
      label: `Bucket: ${filters.bucket === 'day' ? 'Daily' : 'Hourly'}`,
      patch: { bucket: defaultDashboardFilters.bucket },
      advanced: true,
    });
  }
  if (filters.environment) {
    chips.push({
      key: 'environment',
      label: `Environment: ${shorten(filters.environment)}`,
      patch: { environment: '' },
      advanced: true,
    });
  }
  if (filters.error_type) {
    chips.push({
      key: 'error_type',
      label: `Error type: ${shorten(filters.error_type)}`,
      patch: { error_type: '' },
      advanced: true,
    });
  }
  if (filters.span_type) {
    chips.push({
      key: 'span_type',
      label: `Span type: ${shorten(filters.span_type)}`,
      patch: { span_type: '' },
      advanced: true,
    });
  }
  if (filters.name_contains) {
    chips.push({
      key: 'name_contains',
      label: `Name: ${shorten(filters.name_contains)}`,
      patch: { name_contains: '' },
      advanced: true,
    });
  }
  if (filters.error) {
    chips.push({
      key: 'error',
      label: `Error: ${filters.error === 'true' ? 'with errors' : 'without errors'}`,
      patch: { error: '' },
      advanced: true,
    });
  }
  if (filters.errorsOnly) {
    chips.push({
      key: 'errorsOnly',
      label: 'Errors only',
      patch: { errorsOnly: false },
      advanced: true,
    });
  }
  if (filters.sort_by !== defaultDashboardFilters.sort_by || !filters.sort_desc) {
    chips.push({
      key: 'sort',
      label: `Sort: ${shorten(filters.sort_by)} ${filters.sort_desc ? '↓' : '↑'}`,
      patch: { sort_by: defaultDashboardFilters.sort_by, sort_desc: defaultDashboardFilters.sort_desc },
      advanced: true,
    });
  }
  if (filters.range === 'custom') {
    if (filters.from) {
      chips.push({
        key: 'from',
        label: `From: ${shorten(filters.from)}`,
        patch: { from: '' },
        advanced: true,
      });
    }
    if (filters.to) {
      chips.push({
        key: 'to',
        label: `To: ${shorten(filters.to)}`,
        patch: { to: '' },
        advanced: true,
      });
    }
  }
  return chips;
}
