import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, exportTraces, queryTraces } from '../api';
import FilterControls from '../components/FilterControls';
import Icon from '../components/Icons';
import { findCost } from '../components/SpanInspector';
import { EmptyState, LoadingRows } from '../components/States';
import { CostValue, TokenGroup } from '../components/TokenValue';
import { useFilterOptions } from '../hooks/useFilterOptions';
import { toTraceFilters } from '../components/Filters';
import type { DashboardFilters, Trace, TraceQueryResponse } from '../types';
import { copyToClipboard } from '../utils/auth';
import { formatAxisTime, formatCount, formatDateTime, formatDuration, relativeTime, toFiniteNumber } from '../utils/format';
import { learnTokenHistory, resolveTokens } from '../utils/tokenEstimation';
import { hrefWithFilters } from '../utils/urlFilters';

interface HomeProps {
  filters: DashboardFilters;
  onFiltersChange: (patch: Partial<DashboardFilters>) => void;
  refreshSignal: number;
  onRefresh: () => void;
}

const TOKEN_ERROR = 'Token invalid or expired. Reopen the dashboard with a fresh token link.';

function modelsText(trace: Trace): string {
  const record = trace as Record<string, unknown>;
  for (const value of [record.model_name, record.model, record.provider]) {
    if (typeof value === 'string' && value.trim()) return value;
  }
  return '—';
}

function statusOf(status?: string | null): { label: string; tone: 'ok' | 'error' | 'neutral' } {
  const value = String(status ?? '').toLowerCase();
  if (['ok', 'success', 'completed'].includes(value)) return { label: 'OK', tone: 'ok' };
  if (['error', 'failed', 'failure'].includes(value)) return { label: 'Error', tone: 'error' };
  return { label: status || '—', tone: 'neutral' };
}

export default function Home({ filters, onFiltersChange, refreshSignal, onRefresh }: HomeProps) {
  const { projects, environments, models } = useFilterOptions();
  const [result, setResult] = useState<TraceQueryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tokenError, setTokenError] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [limit, setLimit] = useState(25);
  const [offset, setOffset] = useState(0);
  const [updatedAt, setUpdatedAt] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());
  const requestRef = useRef(0);
  const copyTimerRef = useRef<number | null>(null);

  useEffect(() => {
    setOffset(0);
  }, [
    filters.project_id,
    filters.environment,
    filters.range,
    filters.from,
    filters.to,
    filters.model_name,
    filters.q,
    filters.status,
    filters.name_contains,
    filters.error,
    filters.error_type,
    filters.span_type,
    filters.errorsOnly,
    filters.sort_by,
    filters.sort_desc,
  ]);

  const reportError = useCallback((err: unknown, fallback: string) => {
    const message = err instanceof Error ? err.message : fallback;
    setError(message);
    setTokenError(
      (err instanceof ApiError && err.status === 403) ||
        /token invalid|expired/i.test(message),
    );
  }, []);

  const load = useCallback(async () => {
    const requestId = ++requestRef.current;
    setLoading(true);
    setError(null);
    setTokenError(false);
    try {
      const data = await queryTraces(toTraceFilters(filters, limit, offset));
      if (requestId !== requestRef.current) return;
      setResult(data);
      setUpdatedAt(Date.now());
    } catch (err) {
      if (requestId !== requestRef.current) return;
      setResult(null);
      reportError(err, 'Failed to load traces');
    } finally {
      if (requestId === requestRef.current) setLoading(false);
    }
  }, [filters, limit, offset, reportError]);

  useEffect(() => {
    void load();
  }, [load, refreshSignal]);

  useEffect(() => {
    const interval = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(interval);
  }, []);

  useEffect(() => {
    return () => {
      if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current);
    };
  }, []);

  const handleExport = async () => {
    setExporting(true);
    try {
      await exportTraces(toTraceFilters(filters, 10000, 0), 'csv');
    } catch (err) {
      reportError(err, 'Export failed');
    } finally {
      setExporting(false);
    }
  };

  const handleCopy = async (traceId: string) => {
    const ok = await copyToClipboard(traceId);
    if (!ok) return;
    setCopiedId(traceId);
    if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current);
    copyTimerRef.current = window.setTimeout(() => setCopiedId(null), 1200);
  };

  const sortState = (field: string) => {
    if (filters.sort_by !== field) return 'none' as const;
    return filters.sort_desc ? ('descending' as const) : ('ascending' as const);
  };

  const toggleSort = (field: string) => {
    const desc = filters.sort_by === field ? !filters.sort_desc : true;
    onFiltersChange({ sort_by: field, sort_desc: desc });
  };

  const sortIndicator = (field: string) => {
    const state = sortState(field);
    return state === 'none' ? '' : state === 'descending' ? ' ↓' : ' ↑';
  };

  const items = Array.isArray(result?.items) ? result.items : [];
  const tokenRows = useMemo(() => {
    learnTokenHistory(items);
    return items.map((trace) => ({
      trace,
      tokens: resolveTokens(trace, trace.trace_id),
      cost: findCost(trace),
    }));
  }, [items]);
  const total = toFiniteNumber(result?.total) ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / limit));
  const page = Math.min(pageCount, Math.floor(offset / limit) + 1);
  const selectedProject = projects.find((project) => project.project_id === filters.project_id);
  const subtitle = selectedProject
    ? `${selectedProject.name || 'Project'} · ${selectedProject.project_id}`
    : 'Project hjnc · proj_47c1f29';

  return (
    <FilterControls
      tabKey="traces"
      title="Traces"
      subtitle={subtitle}
      filters={filters}
      onFiltersChange={onFiltersChange}
      projects={projects}
      environments={environments}
      models={models}
      actions={
        <>
          <button
            type="button"
            className="tk-button tk-button-outline"
            disabled={exporting}
            onClick={() => void handleExport()}
          >
            {exporting ? 'Exporting…' : 'Export CSV'}
          </button>
          <button type="button" className="tk-button tk-button-primary" onClick={onRefresh}>
            Refresh
          </button>
        </>
      }
    >
      <section className="tk-card tk-log-card" aria-label="Trace log">
        <div className="tk-log-header">
          <h2>
            Trace log <span className="tk-result-count">· {formatCount(total)} results</span>
          </h2>
          <span className="tk-updated">Updated {relativeTime(updatedAt, now)}</span>
        </div>

        {error && (
          <div className="tk-error-state" role="alert">
            <Icon name="errors" size={20} />
            <span>{tokenError ? TOKEN_ERROR : error}</span>
            <button type="button" className="tk-button tk-button-outline" onClick={() => void load()}>
              Retry
            </button>
          </div>
        )}

        {!error && loading && <LoadingRows rows={8} columns={8} label="Loading traces" />}

        {!error && !loading && items.length === 0 && (
          <EmptyState
            title="No traces found"
            message="Adjust the filters above to broaden the search."
          />
        )}

        {!error && !loading && items.length > 0 && (
          <>
            <div className="tk-table-scroll">
              <table className="data-table tk-trace-table">
                <thead>
                  <tr>
                    <th scope="col">Trace ID</th>
                    <th scope="col" aria-sort={sortState('name')}>
                      <button type="button" className="tk-sort-button" onClick={() => toggleSort('name')}>
                        Name{sortIndicator('name')}
                      </button>
                    </th>
                    <th scope="col" aria-sort={sortState('start_time')}>
                      <button type="button" className="tk-sort-button" onClick={() => toggleSort('start_time')}>
                        Started{sortIndicator('start_time')}
                      </button>
                    </th>
                    <th scope="col">Model</th>
                    <th scope="col">Tokens</th>
                    <th scope="col">Cost</th>
                    <th scope="col" aria-sort={sortState('status')}>
                      <button type="button" className="tk-sort-button" onClick={() => toggleSort('status')}>
                        Status{sortIndicator('status')}
                      </button>
                    </th>
                    <th scope="col" aria-sort={sortState('duration_ms')}>
                      <button type="button" className="tk-sort-button" onClick={() => toggleSort('duration_ms')}>
                        Duration{sortIndicator('duration_ms')}
                      </button>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {tokenRows.map(({ trace, tokens, cost }) => {
                    const status = statusOf(trace.status);
                    return (
                      <tr key={trace.trace_id}>
                        <td className="tk-mono">
                          <div className="tk-trace-id-cell">
                            <a
                              className="tk-trace-link"
                              href={hrefWithFilters(`#/traces/${encodeURIComponent(trace.trace_id)}`, filters)}
                              title={trace.trace_id}
                            >
                              {trace.trace_id}
                            </a>
                            <button
                              type="button"
                              className="tk-icon-button"
                              aria-label={`Copy trace ID ${trace.trace_id}`}
                              title={copiedId === trace.trace_id ? 'Copied' : 'Copy trace ID'}
                              onClick={() => void handleCopy(trace.trace_id)}
                            >
                              <Icon name="copy" size={13} />
                            </button>
                          </div>
                        </td>
                        <td className="tk-truncate" title={trace.name ?? ''}>{trace.name || '—'}</td>
                        <td className="tk-mono" title={formatDateTime(trace.start_time)}>{formatAxisTime(trace.start_time)}</td>
                        <td>{modelsText(trace)}</td>
                        <td className="tk-mono"><TokenGroup tokens={tokens} compact /></td>
                        <td className="tk-mono"><CostValue value={cost} estimated={tokens.estimated} /></td>
                        <td>
                          <span className={`tk-status tk-status-${status.tone}`}>{status.label}</span>
                        </td>
                        <td className="tk-mono">{formatDuration(trace.duration_ms ?? trace.latency_ms)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div className="tk-pagination">
              <span>Page {page} of {pageCount}</span>
              <div className="tk-pagination-controls">
                <label>
                  Rows
                  <select
                    value={limit}
                    onChange={(event) => {
                      setLimit(Number(event.target.value));
                      setOffset(0);
                    }}
                  >
                    <option value={25}>25</option>
                    <option value={50}>50</option>
                    <option value={100}>100</option>
                  </select>
                </label>
                <button
                  type="button"
                  className="tk-button tk-button-outline"
                  disabled={page <= 1}
                  onClick={() => setOffset(Math.max(0, offset - limit))}
                >
                  Previous
                </button>
                <button
                  type="button"
                  className="tk-button tk-button-outline"
                  disabled={page >= pageCount}
                  onClick={() => setOffset(offset + limit)}
                >
                  Next
                </button>
              </div>
            </div>
          </>
        )}
      </section>
    </FilterControls>
  );
}
