import { useEffect, useState } from 'react';
import { defaultDashboardFilters } from '../types';
import type { DashboardFilters, Project, RangePreset } from '../types';
import { getActiveChips } from '../utils/filterChips';
import Icon from './Icons';

interface DashboardFilterBarProps {
  filters: DashboardFilters;
  onChange: (patch: Partial<DashboardFilters>) => void;
  projects: Project[];
  environments: string[];
  models: string[];
}

const RANGE_OPTIONS: { value: RangePreset; label: string }[] = [
  { value: '1h', label: '1h' },
  { value: '24h', label: '24h' },
  { value: '7d', label: '7d' },
  { value: '30d', label: '30d' },
  { value: 'any', label: 'All time' },
];

const STATUS_OPTIONS = [
  { value: '', label: 'All' },
  { value: 'ok', label: 'OK' },
  { value: 'error', label: 'Error' },
];

const SORT_FIELDS = ['start_time', 'duration_ms', 'name', 'status'];

function withCurrentOption(options: string[], current: string): string[] {
  return current && !options.includes(current) ? [current, ...options] : options;
}

export default function DashboardFilterBar({
  filters,
  onChange,
  projects,
  environments,
  models,
}: DashboardFilterBarProps) {
  const chips = getActiveChips(filters, projects);
  const advancedCount = chips.filter((chip) => chip.advanced).length;
  const [panelOpen, setPanelOpen] = useState(() => advancedCount > 0);
  const [draft, setDraft] = useState<DashboardFilters>(filters);

  useEffect(() => {
    if (panelOpen) setDraft(filters);
  }, [filters, panelOpen]);

  const modelOptions = withCurrentOption(models, draft.model_name);
  const environmentOptions = withCurrentOption(environments, draft.environment);
  const sortFields = withCurrentOption(SORT_FIELDS, draft.sort_by);
  const sortValue = `${draft.sort_by}|${draft.sort_desc ? 'desc' : 'asc'}`;

  const updateDraft = (patch: Partial<DashboardFilters>) => {
    setDraft((current) => ({ ...current, ...patch }));
  };

  const applyDraft = () => {
    onChange({
      project_id: draft.project_id,
      model_name: draft.model_name,
      bucket: draft.bucket,
      environment: draft.environment,
      error_type: draft.error_type,
      span_type: draft.span_type,
      name_contains: draft.name_contains,
      error: draft.error,
      errorsOnly: draft.errorsOnly,
      sort_by: draft.sort_by,
      sort_desc: draft.sort_desc,
      from: draft.from,
      to: draft.to,
    });
    setPanelOpen(false);
  };

  const clearAll = () => {
    onChange({ ...defaultDashboardFilters });
    setDraft({ ...defaultDashboardFilters });
    setPanelOpen(false);
  };

  return (
    <section className="tk-card tk-filter-card" aria-label="Dashboard filters">
      <div className="tk-filter-primary">
        <label className="tk-search-field">
          <span className="tk-field-label">Search</span>
          <span className="tk-search-wrap">
            <Icon name="search" size={15} className="tk-search-icon" />
            <input
              type="search"
              placeholder="Search by trace ID, name or text"
              aria-label="Search by trace ID, name or text"
              value={filters.q}
              onChange={(event) => onChange({ q: event.target.value })}
            />
          </span>
        </label>

        <div className="tk-field-group">
          <span className="tk-field-label" id="tk-range-label">Time range</span>
          <div className="tk-segments" role="group" aria-labelledby="tk-range-label">
            {RANGE_OPTIONS.map((option) => (
              <button
                key={option.value}
                type="button"
                className={filters.range === option.value ? 'tk-segment active' : 'tk-segment'}
                aria-pressed={filters.range === option.value}
                onClick={() => onChange({ range: option.value, from: '', to: '' })}
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>

        <div className="tk-field-group">
          <span className="tk-field-label" id="tk-status-label">Status</span>
          <div className="tk-segments" role="group" aria-labelledby="tk-status-label">
            {STATUS_OPTIONS.map((option) => (
              <button
                key={option.value || 'all'}
                type="button"
                className={filters.status === option.value ? 'tk-segment active' : 'tk-segment'}
                aria-pressed={filters.status === option.value}
                onClick={() => onChange({ status: option.value })}
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>

        <button
          type="button"
          className={panelOpen ? 'tk-more-button is-open' : 'tk-more-button'}
          aria-expanded={panelOpen}
          onClick={() => setPanelOpen((open) => !open)}
        >
          <Icon name="sliders" size={15} />
          <span>More filters</span>
          {advancedCount > 0 && (
            <span className="tk-filter-badge" aria-label={`${advancedCount} advanced filters active`}>
              {advancedCount}
            </span>
          )}
        </button>
      </div>

      {chips.length > 0 && (
        <div className="tk-active-row">
          <span className="tk-active-label">Active:</span>
          <div className="tk-chips">
            {chips.map((chip) => (
              <span className="tk-chip" key={chip.key}>
                <span className="tk-chip-label">{chip.label}</span>
                <button
                  type="button"
                  className="tk-chip-remove"
                  aria-label={`Remove ${chip.label} filter`}
                  onClick={() => onChange(chip.patch)}
                >
                  <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true" focusable="false">
                    <path d="m2 2 6 6M8 2 2 8" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
                  </svg>
                </button>
              </span>
            ))}
          </div>
          <button type="button" className="tk-clear-link" onClick={clearAll}>
            Clear all
          </button>
        </div>
      )}

      {panelOpen && (
        <div className="tk-advanced-panel" id="tk-advanced-panel">
          <div className="tk-advanced-grid">
            <label className="tk-field">
              <span className="tk-field-label">Project</span>
              <select value={draft.project_id} onChange={(event) => updateDraft({ project_id: event.target.value })}>
                <option value="">All projects</option>
                {projects.map((project) => (
                  <option key={project.project_id} value={project.project_id}>
                    {project.name || project.project_id}
                  </option>
                ))}
              </select>
            </label>

            <label className="tk-field">
              <span className="tk-field-label">Model</span>
              <select value={draft.model_name} onChange={(event) => updateDraft({ model_name: event.target.value })}>
                <option value="">All models</option>
                {modelOptions.map((model) => (
                  <option key={model} value={model}>
                    {model}
                  </option>
                ))}
              </select>
            </label>

            <label className="tk-field">
              <span className="tk-field-label">Bucket</span>
              <select
                value={draft.bucket}
                onChange={(event) => updateDraft({ bucket: event.target.value as DashboardFilters['bucket'] })}
              >
                <option value="hour">Hourly</option>
                <option value="day">Daily</option>
              </select>
            </label>

            <label className="tk-field">
              <span className="tk-field-label">Sort by</span>
              <select
                value={sortValue}
                onChange={(event) => {
                  const [sort_by, direction] = event.target.value.split('|');
                  updateDraft({ sort_by, sort_desc: direction !== 'asc' });
                }}
              >
                {sortFields.flatMap((field) => [
                  <option key={`${field}|desc`} value={`${field}|desc`}>
                    {field} · descending
                  </option>,
                  <option key={`${field}|asc`} value={`${field}|asc`}>
                    {field} · ascending
                  </option>,
                ])}
              </select>
            </label>

            <label className="tk-field">
              <span className="tk-field-label">Environment</span>
              <input
                type="text"
                list="tk-environment-options"
                placeholder="All environments"
                value={draft.environment}
                onChange={(event) => updateDraft({ environment: event.target.value })}
              />
              <datalist id="tk-environment-options">
                {environmentOptions.map((environment) => (
                  <option key={environment} value={environment} />
                ))}
              </datalist>
            </label>

            <label className="tk-field">
              <span className="tk-field-label">Error type</span>
              <input
                type="text"
                placeholder="Any error type"
                value={draft.error_type}
                onChange={(event) => updateDraft({ error_type: event.target.value })}
              />
            </label>

            <label className="tk-field">
              <span className="tk-field-label">Span type</span>
              <input
                type="text"
                placeholder="Any span type"
                value={draft.span_type}
                onChange={(event) => updateDraft({ span_type: event.target.value })}
              />
            </label>

            {draft.range === 'custom' && (
              <>
                <label className="tk-field">
                  <span className="tk-field-label">From</span>
                  <input type="datetime-local" value={draft.from} onChange={(event) => updateDraft({ from: event.target.value })} />
                </label>
                <label className="tk-field">
                  <span className="tk-field-label">To</span>
                  <input type="datetime-local" value={draft.to} onChange={(event) => updateDraft({ to: event.target.value })} />
                </label>
              </>
            )}
          </div>

          <div className="tk-advanced-actions">
            <label className="tk-checkbox-field">
              <input
                type="checkbox"
                checked={draft.errorsOnly}
                onChange={(event) => updateDraft({ errorsOnly: event.target.checked })}
              />
              <span>Errors only</span>
            </label>
            <button type="button" className="tk-button tk-button-primary" onClick={applyDraft}>
              Apply
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
