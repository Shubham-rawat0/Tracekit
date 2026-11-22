import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { getAnalytics } from '../api';
import { toAnalyticsFilters } from '../components/Filters';
import FilterControls from '../components/FilterControls';
import { KpiStrip } from '../components/Metrics';
import { EmptyState, ErrorState, LoadingRows, Section } from '../components/States';
import { useFilterOptions } from '../hooks/useFilterOptions';
import type { AnalyticsErrorGroup, AnalyticsResponse, DashboardFilters } from '../types';
import { copyToClipboard } from '../utils/auth';
import { formatCount, formatDateTime, toFiniteNumber } from '../utils/format';

interface ErrorsProps {
  filters: DashboardFilters;
  onFiltersChange: (patch: Partial<DashboardFilters>) => void;
  refreshSignal: number;
}

function joined(values?: string[] | null): string {
  if (!Array.isArray(values) || values.length === 0) return '—';
  return values.join(', ');
}

function countText(value: unknown): string {
  const number = toFiniteNumber(value);
  return number === null ? '—' : Math.round(number).toLocaleString();
}

function SampleTrace({ traceId }: { traceId?: string | null }) {
  const [copied, setCopied] = useState(false);
  if (!traceId) return <>—</>;
  const id = String(traceId);
  const copy = async () => {
    if (!(await copyToClipboard(id))) return;
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };
  return (
    <div className="table-actions">
      <a className="mono link" href={`#/traces/${encodeURIComponent(id)}`}>{id}</a>
      <button type="button" className="copy-text" onClick={() => void copy()}>
        {copied ? 'Copied' : 'Copy'}
      </button>
    </div>
  );
}

export default function Errors({ filters, onFiltersChange, refreshSignal }: ErrorsProps) {
  const [data, setData] = useState<AnalyticsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { projects, environments, models } = useFilterOptions();
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const requestRef = useRef(0);
  const refreshRef = useRef(refreshSignal);

  const load = useCallback(async () => {
    const requestId = ++requestRef.current;
    setLoading(true);
    setError(null);
    try {
      const response = await getAnalytics(toAnalyticsFilters(filters), filters.bucket);
      if (requestId !== requestRef.current) return;
      setData(response);
    } catch (err) {
      if (requestId !== requestRef.current) return;
      setData(null);
      setError(err instanceof Error ? err.message : 'Failed to load error groups');
    } finally {
      if (requestId === requestRef.current) setLoading(false);
    }
  }, [filters, filters.bucket]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (refreshSignal === refreshRef.current) return;
    refreshRef.current = refreshSignal;
    void load();
  }, [refreshSignal, load]);

  const summary = data?.summary ?? {};
  const errors = useMemo(() => data?.errors ?? [], [data]);
  const occurrences = errors.reduce(
    (total, group) => total + (toFiniteNumber(group.count) ?? 0),
    0,
  );

  const toggle = (key: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const rowKey = (group: AnalyticsErrorGroup, index: number) =>
    `${group.error_type ?? 'error'}-${group.message ?? ''}-${index}`;

  const kpis = [
    {
      label: 'Failed traces',
      value: formatCount(summary.failed_traces),
      tone: (toFiniteNumber(summary.failed_traces) ?? 0) > 0 ? ('err' as const) : ('default' as const),
    },
    { label: 'Error groups', value: formatCount(errors.length) },
    { label: 'Occurrences', value: formatCount(occurrences) },
    { label: 'Traces with error type', value: formatCount(summary.traces_with_error_type) },
  ];

  return (
    <FilterControls
      tabKey="errors"
      title="Errors"
      subtitle="Grouped failures and stack traces"
      filters={filters}
      onFiltersChange={onFiltersChange}
      projects={projects}
      environments={environments}
      models={models}
    >
      {error && <ErrorState message={error} onRetry={() => void load()} />}
      {!error && loading && <LoadingRows rows={6} columns={8} label="Loading error groups" />}
      {!error && !loading && data && errors.length === 0 && (
        <EmptyState
          title="No errors in this window"
          message="Extend the time range or remove global filters to inspect other failures."
        />
      )}
      {!error && !loading && data && errors.length > 0 && (
        <>
          <KpiStrip items={kpis} />
          <Section label="Error groups" title={`${errors.length} groups`}>
            <div className="table-scroll">
              <table className="data-table dense-table">
                <thead>
                  <tr>
                    <th>Count</th>
                    <th>Error type</th>
                    <th>First seen</th>
                    <th>Last seen</th>
                    <th>Affected traces</th>
                    <th>Projects</th>
                    <th>Sample trace</th>
                    <th>Sample message</th>
                  </tr>
                </thead>
                <tbody>
                  {errors.map((group, index) => {
                    const key = rowKey(group, index);
                    const isOpen = expanded.has(key);
                    return (
                      <FragmentRow
                        key={key}
                        group={group}
                        rowIndex={index}
                        isOpen={isOpen}
                        onToggle={() => toggle(key)}
                      />
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Section>
        </>
      )}
    </FilterControls>
  );
}

interface FragmentRowProps {
  group: AnalyticsErrorGroup;
  rowIndex: number;
  isOpen: boolean;
  onToggle: () => void;
}

function FragmentRow({ group, rowIndex, isOpen, onToggle }: FragmentRowProps) {
  return (
    <>
      <tr className="clickable-row" onClick={onToggle}>
        <td className="mono">
          <div className="table-actions">
            <span>{formatCount(group.count)}</span>
            <button
              type="button"
              className="copy-text"
              aria-expanded={isOpen}
              aria-controls={`stack-${rowIndex}`}
              onClick={(event) => {
                event.stopPropagation();
                onToggle();
              }}
            >
              {isOpen ? 'Hide' : 'Stack'}
            </button>
          </div>
        </td>
        <td className="mono">{group.error_type || '—'}</td>
        <td className="muted-line">{formatDateTime(group.first_seen)}</td>
        <td className="muted-line">{formatDateTime(group.last_seen)}</td>
        <td className="mono">{countText(group.trace_ids?.length)}</td>
        <td className="muted-line">{joined(group.projects)}</td>
        <td><SampleTrace traceId={group.sample_trace_id} /></td>
        <td className="muted-line truncate-cell" title={group.message ?? ''}>
          {group.message || '—'}
        </td>
      </tr>
      {isOpen && (
        <tr id={`stack-${rowIndex}`}>
          <td colSpan={8}>
            <pre className="mono">{group.stacktrace || 'n/a'}</pre>
          </td>
        </tr>
      )}
    </>
  );
}
