import { useCallback, useEffect, useMemo, useState } from 'react';
import { getAnalytics } from '../api';
import { BarChart, LineChart } from '../components/Charts';
import { toAnalyticsFilters } from '../components/Filters';
import FilterControls from '../components/FilterControls';
import { KpiStrip } from '../components/Metrics';
import SpanCompositionCard from '../components/SpanCompositionCard';
import { EmptyState, ErrorState, LoadingRows, Section } from '../components/States';
import { CostValue, TokenValue } from '../components/TokenValue';
import { useFilterOptions } from '../hooks/useFilterOptions';
import type { AnalyticsResponse, DashboardFilters } from '../types';
import { copyToClipboard } from '../utils/auth';
import { formatCount, formatDateTime, formatDuration, formatPercent, toFiniteNumber } from '../utils/format';
import { reportedTokens } from '../utils/tokenEstimation';

interface OverviewProps {
  filters: DashboardFilters;
  onFiltersChange: (patch: Partial<DashboardFilters>) => void;
  refreshSignal: number;
}

function countValue(value: unknown): number | null {
  const number = toFiniteNumber(value);
  return number === null ? null : Math.max(0, number);
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

export default function Overview({ filters, onFiltersChange, refreshSignal }: OverviewProps) {
  const [data, setData] = useState<AnalyticsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { projects, loading: projectsLoading, environments, models } = useFilterOptions();

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await getAnalytics(toAnalyticsFilters(filters), filters.bucket);
      setData(response);
    } catch (err) {
      setData(null);
      setError(err instanceof Error ? err.message : 'Failed to load overview');
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
  const recentErrors = useMemo(() => (data?.errors ?? []).slice(0, 10), [data]);
  const hasData = (toFiniteNumber(summary.total_traces) ?? 0) > 0 || points.length > 0;
  const summaryTokens = reportedTokens(summary);

  const kpis = [
    { label: 'Traces', value: formatCount(summary.total_traces) },
    {
      label: 'Errors',
      value: formatCount(summary.failed_traces),
      tone: (toFiniteNumber(summary.failed_traces) ?? 0) > 0 ? ('err' as const) : ('default' as const),
    },
    { label: 'Avg latency', value: formatDuration(summary.avg_latency_ms) },
    { label: 'p95 latency', value: formatDuration(summary.p95_latency_ms) },
    { label: 'LLM calls', value: formatCount(summary.total_llm_calls) },
    { label: 'Input tokens', value: <TokenValue value={summaryTokens.input} kind="input" /> },
    { label: 'Output tokens', value: <TokenValue value={summaryTokens.output} kind="output" /> },
    { label: 'Total tokens', value: <TokenValue value={summaryTokens.total} kind="total" /> },
    { label: 'Cost', value: <CostValue value={toFiniteNumber(summary.total_cost_usd)} /> },
    {
      label: 'Projects',
      value: projectsLoading ? '—' : formatCount(projects.length),
      hint: filters.project_id ? 'All projects' : undefined,
    },
  ];

  const totalSpans = countValue(summary.total_spans);
  const llmCalls = countValue(summary.total_llm_calls) ?? 0;

  return (
    <FilterControls
      tabKey="overview"
      title="Overview"
      subtitle="Traces, errors and latency at a glance"
      filters={filters}
      onFiltersChange={onFiltersChange}
      projects={projects}
      environments={environments}
      models={models}
    >
      {error && <ErrorState message={error} onRetry={() => void load()} />}
      {!error && loading && <LoadingRows rows={6} columns={6} label="Loading overview" />}
      {!error && !loading && !hasData && (
        <EmptyState title="No traces in this window" message="Adjust the global filters to see overview metrics." />
      )}
      {!error && !loading && hasData && (
        <>
          <KpiStrip items={kpis} />
          <div className="chart-grid tk-chart-grid overview-chart-layout">
            <BarChart
              label="Request volume"
              title={filters.bucket === 'day' ? 'Daily' : 'Hourly'}
              labels={labels}
              ariaLabel="Request volume over time"
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
            <LineChart
              label="Error rate"
              title="Failed traces / total traces"
              labels={labels}
              ariaLabel="Error rate over time"
              formatValue={(value) => formatPercent(value, 0)}
              series={[
                {
                  label: 'Error rate',
                  values: points.map((point) => point.error_rate),
                  variant: 'error',
                  context: points.map((point) =>
                    `${formatCount(point.failed_traces)} failed / ${formatCount(point.traces)} total`,
                  ),
                },
              ]}
            />
            <SpanCompositionCard llmSpans={llmCalls} totalSpans={totalSpans} />
          </div>
          <Section label="Recent errors" title={`${recentErrors.length} groups`}>
            {recentErrors.length === 0 ? (
              <EmptyState title="No errors recorded" message="No error groups were returned for this window." />
            ) : (
              <div className="table-scroll">
                <table className="data-table dense-table">
                  <thead>
                    <tr>
                      <th>Error type</th>
                      <th>Count</th>
                      <th>First seen</th>
                      <th>Last seen</th>
                      <th>Message</th>
                      <th>Sample trace</th>
                    </tr>
                  </thead>
                  <tbody>
                    {recentErrors.map((group, index) => (
                      <tr key={`${group.error_type ?? 'error'}-${index}`}>
                        <td className="mono">{group.error_type || '—'}</td>
                        <td className="mono">{formatCount(group.count)}</td>
                        <td className="muted-line">{formatDateTime(group.first_seen)}</td>
                        <td className="muted-line">{formatDateTime(group.last_seen)}</td>
                        <td className="muted-line truncate-cell" title={group.message ?? ''}>
                          {group.message || '—'}
                        </td>
                        <td><SampleTrace traceId={group.sample_trace_id} /></td>
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
