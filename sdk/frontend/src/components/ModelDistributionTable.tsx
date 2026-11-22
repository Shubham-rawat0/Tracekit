import { useMemo, useState } from 'react';
import type { AnalyticsModel, DashboardFilters } from '../types';
import { hrefWithFilters } from '../utils/urlFilters';
import { formatCount, formatDuration, formatPercent, toFiniteNumber } from '../utils/format';

type SortKey = 'model' | 'share' | 'calls' | 'failed' | 'avg' | 'p95';

interface ModelDistributionTableProps {
  models: AnalyticsModel[];
  filters: DashboardFilters;
}

const COLUMNS: { key: SortKey; label: string; numeric?: boolean; className?: string }[] = [
  { key: 'model', label: 'Model' },
  { key: 'share', label: 'Share of calls' },
  { key: 'calls', label: 'Calls', numeric: true },
  { key: 'failed', label: 'Failed', numeric: true },
  { key: 'avg', label: 'Avg', numeric: true, className: 'md-col-avg' },
  { key: 'p95', label: 'P95', numeric: true, className: 'md-col-p95' },
];

const SORT_LABELS: Record<SortKey, string> = {
  model: 'model',
  share: 'share',
  calls: 'calls',
  failed: 'failed',
  avg: 'average latency',
  p95: 'p95 latency',
};

export default function ModelDistributionTable({ models, filters }: ModelDistributionTableProps) {
  const [sortKey, setSortKey] = useState<SortKey>('calls');
  const [sortDesc, setSortDesc] = useState(true);

  const rows = useMemo(() => {
    const totalCalls = models.reduce((sum, model) => sum + Math.max(0, toFiniteNumber(model.traces) ?? 0), 0);
    const prepared = models.map((model) => {
      const name = model.model || 'unknown';
      const calls = Math.max(0, toFiniteNumber(model.traces) ?? 0);
      return {
        name,
        calls,
        failed: toFiniteNumber(model.failed_traces),
        avg: toFiniteNumber(model.avg_latency_ms),
        p95: toFiniteNumber(model.p95_latency_ms),
        share: totalCalls > 0 ? calls / totalCalls : 0,
        href: hrefWithFilters('#/traces', { ...filters, model_name: name }),
      };
    });

    const valueFor = (row: (typeof prepared)[number]): string | number | null => {
      switch (sortKey) {
        case 'model':
          return row.name;
        case 'share':
          return row.share;
        case 'calls':
          return row.calls;
        case 'failed':
          return row.failed;
        case 'avg':
          return row.avg;
        case 'p95':
          return row.p95;
      }
    };

    return prepared.sort((a, b) => {
      const left = valueFor(a);
      const right = valueFor(b);
      if (left === null && right === null) return 0;
      if (left === null) return 1;
      if (right === null) return -1;
      const result = typeof left === 'string' && typeof right === 'string'
        ? left.localeCompare(right)
        : Number(left) - Number(right);
      return sortDesc ? -result : result;
    });
  }, [models, filters, sortKey, sortDesc]);

  const handleSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDesc((current) => !current);
      return;
    }
    setSortKey(key);
    setSortDesc(key !== 'model');
  };

  return (
    <section className="insight-card model-distribution" aria-label="Model distribution">
      <header className="insight-card-head">
        <span className="insight-label">Model distribution</span>
        <span className="insight-meta mono">{formatCount(models.length)} models</span>
        <span className="insight-sort-note">Sorted by {SORT_LABELS[sortKey]}</span>
      </header>
      {models.length === 0 ? (
        <p className="insight-empty">No model data for this window.</p>
      ) : (
        <div className="md-table-wrap">
          <table className="md-table">
            <thead>
              <tr>
                {COLUMNS.map((column) => (
                  <th
                    key={column.key}
                    className={[column.numeric ? 'md-numeric' : '', column.className ?? ''].join(' ').trim() || undefined}
                    aria-sort={sortKey === column.key ? (sortDesc ? 'descending' : 'ascending') : 'none'}
                  >
                    <button type="button" onClick={() => handleSort(column.key)}>
                      {column.label}
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={row.name}
                  className="md-row"
                  onClick={() => {
                    window.location.hash = row.href;
                  }}
                >
                  <td className="md-model">
                    <a href={row.href} onClick={(event) => event.stopPropagation()}>{row.name}</a>
                  </td>
                  <td>
                    <div className="md-share">
                      <span className="md-share-track">
                        <span
                          className={row.name === 'unknown' ? 'md-share-fill unknown' : 'md-share-fill'}
                          style={{ width: `${row.share * 100}%` }}
                        />
                      </span>
                      <span className="mono">{formatPercent(row.share, 0)}</span>
                    </div>
                  </td>
                  <td className="md-numeric mono">{formatCount(row.calls)}</td>
                  <td className={`md-numeric mono ${row.failed === null ? 'md-missing' : row.failed > 0 ? 'md-failed' : 'md-ok'}`}>
                    {row.failed === null ? '—' : formatCount(row.failed)}
                  </td>
                  <td className={`md-numeric mono md-col-avg ${row.avg === null ? 'md-missing' : ''}`}>
                    {row.avg === null ? '—' : formatDuration(row.avg)}
                  </td>
                  <td className={`md-numeric mono md-col-p95 ${row.p95 === null ? 'md-missing' : ''}`}>
                    {row.p95 === null ? '—' : formatDuration(row.p95)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
