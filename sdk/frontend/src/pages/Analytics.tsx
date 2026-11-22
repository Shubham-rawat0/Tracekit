import { useCallback, useEffect, useMemo, useState } from 'react';
import { getAnalytics } from '../api';
import { BarChart } from '../components/Charts';
import { toAnalyticsFilters } from '../components/Filters';
import FilterControls from '../components/FilterControls';
import { KpiStrip } from '../components/Metrics';
import ModelDistributionTable from '../components/ModelDistributionTable';
import SpanCompositionCard from '../components/SpanCompositionCard';
import { EmptyState, ErrorState, LoadingRows, Section } from '../components/States';
import { useFilterOptions } from '../hooks/useFilterOptions';
import type { AnalyticsResponse, DashboardFilters } from '../types';
import { copyToClipboard } from '../utils/auth';
import { formatCount, formatDuration, formatPercent, toFiniteNumber } from '../utils/format';

interface AnalyticsProps {
  filters: DashboardFilters;
  onFiltersChange: (patch: Partial<DashboardFilters>) => void;
  refreshSignal: number;
}

function statusTone(status?: string | null): string {
  const normalized = String(status ?? '').toLowerCase();
  if (normalized === 'ok' || normalized === 'success' || normalized === 'completed') return 'ok';
  if (normalized === 'error' || normalized === 'failed' || normalized === 'failure') return 'err';
  if (['pending', 'running', 'cancelled', 'canceled'].includes(normalized)) return 'warn';
  return '';
}

function StatusTag({ status }: { status?: string | null }) {
  return (
    <span className={`status-tag ${statusTone(status)}`.trim()}>{status || 'unknown'}</span>
  );
}

function CopyId({ value, trace }: { value?: string | null; trace?: boolean }) {
  const [copied, setCopied] = useState(false);
  if (!value) return <>—</>;
  const text = String(value);
  const copy = async () => {
    if (!(await copyToClipboard(text))) return;
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };
  return (
    <div className="table-actions">
      {trace ? (
        <a className="mono link" href={`#/traces/${encodeURIComponent(text)}`}>{text}</a>
      ) : (
        <span className="mono">{text}</span>
      )}
      <button type="button" className="copy-text" onClick={() => void copy()}>
        {copied ? 'Copied' : 'Copy'}
      </button>
    </div>
  );
}

export default function Analytics({ filters, onFiltersChange, refreshSignal }: AnalyticsProps) {
  const [data, setData] = useState<AnalyticsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { projects, environments, models: modelOptions } = useFilterOptions();

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await getAnalytics(toAnalyticsFilters(filters), filters.bucket);
      setData(response);
    } catch (err) {
      setData(null);
      setError(err instanceof Error ? err.message : 'Failed to load analytics');
    } finally {
      setLoading(false);
    }
  }, [filters, filters.bucket]);

  useEffect(() => {
    void load();
  }, [load, refreshSignal]);

  const summary = data?.summary ?? {};
  const points = useMemo(
    () =>
      (data?.timeseries ?? [])
        .slice()
        .sort((a, b) => (a.timestamp ? Date.parse(a.timestamp) : 0) - (b.timestamp ? Date.parse(b.timestamp) : 0)),
    [data],
  );
  const labels = useMemo(() => points.map((point) => point.timestamp ?? ''), [points]);
  const models = useMemo(() => data?.models ?? [], [data]);
  const totalSpans = toFiniteNumber(summary.total_spans);
  const llmCalls = toFiniteNumber(summary.total_llm_calls) ?? 0;
  const slowTraces = Array.isArray(data?.slowest?.traces) ? data?.slowest?.traces ?? [] : [];
  const slowSpans = Array.isArray(data?.slowest?.spans) ? data?.slowest?.spans ?? [] : [];
  const hasData = (toFiniteNumber(summary.total_traces) ?? 0) > 0 || points.length > 0;

  const percentiles = [
    { label: 'p50', value: toFiniteNumber(summary.p50_latency_ms) },
    { label: 'p95', value: toFiniteNumber(summary.p95_latency_ms) },
    { label: 'p99', value: toFiniteNumber(summary.p99_latency_ms) },
  ];

  const kpis = [
    { label: 'Traces', value: formatCount(summary.total_traces) },
    {
      label: 'Failed',
      value: formatCount(summary.failed_traces),
      tone: (toFiniteNumber(summary.failed_traces) ?? 0) > 0 ? ('err' as const) : ('default' as const),
    },
    { label: 'Error rate', value: formatPercent(summary.error_rate) },
    { label: 'p50 latency', value: formatDuration(summary.p50_latency_ms) },
    { label: 'p95 latency', value: formatDuration(summary.p95_latency_ms) },
    { label: 'p99 latency', value: formatDuration(summary.p99_latency_ms) },
  ];

  return (
    <FilterControls
      tabKey="analytics"
      title="Analytics"
      subtitle="Latency, traffic and model usage"
      filters={filters}
      onFiltersChange={onFiltersChange}
      projects={projects}
      environments={environments}
      models={modelOptions}
    >
      {error && <ErrorState message={error} onRetry={() => void load()} />}
      {!error && loading && <LoadingRows rows={6} columns={6} label="Loading analytics" />}
      {!error && !loading && !hasData && (
        <EmptyState title="No traces in this window" message="Adjust the global filters to see analytics." />
      )}
      {!error && !loading && hasData && (
        <>
          <KpiStrip items={kpis} />

          <div className="chart-grid tk-chart-grid analytics-chart-layout">
            <BarChart
              label="Traffic over time"
              title={filters.bucket === 'day' ? 'Daily' : 'Hourly'}
              labels={labels}
              ariaLabel="Traffic over time"
              countAxis
              formatValue={(value) => Math.round(value).toLocaleString()}
              series={[
                {
                  label: 'Traces',
                  values: points.map((point) => point.traces),
                  color: 'var(--chart-primary)',
                  context: points.map((point) =>
                    `${formatCount(point.failed_traces)} failed / ${formatCount(point.traces)} total`,
                  ),
                },
                {
                  label: 'Failed',
                  values: points.map((point) => point.failed_traces),
                  variant: 'error',
                  context: points.map((point) => `avg ${formatDuration(point.avg_latency_ms)}`),
                },
              ]}
            />

            <BarChart
              label="Latency percentiles"
              title="p50 / p95 / p99"
              labels={percentiles.map((entry) => entry.label)}
              ariaLabel="Latency percentiles"
              formatValue={formatDuration}
              emptyLabel="No latency samples"
              series={[
                {
                  label: 'Duration',
                  values: percentiles.map((entry) => entry.value),
                  color: 'var(--chart-primary)',
                },
              ]}
            />

            <div className="insight-grid">
              <ModelDistributionTable models={models} filters={filters} />
              <SpanCompositionCard llmSpans={llmCalls} totalSpans={totalSpans} />
            </div>
          </div>

          <Section label="Top traces" title="Slowest traces">
            {slowTraces.length === 0 ? (
              <EmptyState title="No trace duration data" />
            ) : (
              <div className="table-scroll">
                <table className="data-table dense-table">
                  <thead>
                    <tr>
                      <th>Trace ID</th>
                      <th>Name</th>
                      <th>Duration</th>
                      <th>Status</th>
                      <th>Project</th>
                      <th>Model</th>
                    </tr>
                  </thead>
                  <tbody>
                    {slowTraces.map((trace, index) => (
                      <tr key={`${trace.trace_id ?? 'trace'}-${index}`}>
                        <td><CopyId value={trace.trace_id} trace /></td>
                        <td className="mono truncate-cell">{trace.name || '—'}</td>
                        <td className="mono">{formatDuration(trace.duration_ms)}</td>
                        <td><StatusTag status={trace.status} /></td>
                        <td className="muted-line">{trace.project_id || '—'}</td>
                        <td className="mono">{trace.model || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Section>

          <Section label="Top spans" title="Slowest spans">
            {slowSpans.length === 0 ? (
              <EmptyState title="No span duration data" />
            ) : (
              <div className="table-scroll">
                <table className="data-table dense-table">
                  <thead>
                    <tr>
                      <th>Span ID</th>
                      <th>Name</th>
                      <th>Type</th>
                      <th>Duration</th>
                      <th>Status</th>
                      <th>Trace ID</th>
                    </tr>
                  </thead>
                  <tbody>
                    {slowSpans.map((span, index) => (
                      <tr key={`${span.span_id ?? 'span'}-${index}`}>
                        <td><CopyId value={span.span_id} /></td>
                        <td className="mono truncate-cell">{span.name || '—'}</td>
                        <td className="muted-line">{span.span_type || '—'}</td>
                        <td className="mono">{formatDuration(span.duration_ms)}</td>
                        <td><StatusTag status={span.status} /></td>
                        <td><CopyId value={span.trace_id} trace /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Section>
        </>
      )}
    </FilterControls>
  );
}
