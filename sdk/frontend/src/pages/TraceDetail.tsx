import { useCallback, useEffect, useRef, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import '../trace-detail-v2.css';
import { useSpans } from '../hooks/useSpans';
import { useTrace } from '../hooks/useTrace';
import type { DashboardFilters, SpanNode, TraceTab } from '../types';
import { copyToClipboard } from '../utils/auth';
import { hrefWithFilters } from '../utils/urlFilters';
import { formatDateTime, formatDuration, statusClass } from '../utils/format';
import { EmptyState, ErrorState, LoadingRows, Section } from '../components/States';
import ReplayPanel from '../components/ReplayPanel';
import { CostValue, TokenGroup, TokenValue } from '../components/TokenValue';
import { resolveAggregateTokens, resolveTokens } from '../utils/tokenEstimation';
import SpanInspector, {
  findCost,
  hasValue,
  isExceptionEvent,
  isLlmSpan,
  pickValue,
  toText,
} from '../components/SpanInspector';
import type { ReplayLlmSpan, ReplaySpec, ReplayWithSpec } from '../components/SpanInspector';
import Waterfall from './Waterfall';

interface TraceDetailProps {
  traceId: string;
  tab: TraceTab;
  onNavigate: (tab: TraceTab) => void;
  filters: DashboardFilters;
  onFiltersChange: (patch: Partial<DashboardFilters>) => void;
  refreshSignal: number;
}

const TRACE_TABS: Array<{ id: TraceTab; label: string }> = [
  { id: 'waterfall', label: 'Waterfall' },
  { id: 'overview', label: 'Overview' },
  { id: 'input', label: 'Input' },
  { id: 'output', label: 'Output' },
  { id: 'events', label: 'Events' },
  { id: 'attributes', label: 'Attributes' },
  { id: 'error', label: 'Error' },
  { id: 'raw', label: 'Raw' },
];

const WIDTH_KEY = 'tracekit.spanPanel.width';
const OPEN_KEY = 'tracekit.spanPanel.open';
const MIN_PANEL = 280;
const MAX_PANEL = 720;

function readWidth(): number {
  try {
    const raw = window.localStorage.getItem(WIDTH_KEY);
    const value = raw === null ? Number.NaN : Number(raw);
    return Number.isFinite(value) && value >= MIN_PANEL && value <= MAX_PANEL ? value : 420;
  } catch {
    return 420;
  }
}

function readOpen(): boolean {
  try {
    return window.localStorage.getItem(OPEN_KEY) === '1';
  } catch {
    return false;
  }
}

function dedupeSpans(spans: SpanNode[]): SpanNode[] {
  const seen = new Set<string>();
  return spans.filter((span) => {
    const id = span.span_id ? String(span.span_id) : '';
    if (!id) return true;
    if (seen.has(id)) return false;
    seen.add(id);
    return true;
  });
}

function collectSpans(replay: ReplayWithSpec | null): SpanNode[] {
  if (!replay) return [];
  const flat = Array.isArray(replay.spans) ? replay.spans : [];
  if (flat.length > 0) return dedupeSpans(flat);
  const result: SpanNode[] = [];
  const walk = (nodes: SpanNode[] | null | undefined) => {
    if (!Array.isArray(nodes)) return;
    for (const node of nodes) {
      result.push(node);
      walk(node.children);
    }
  };
  walk(replay.tree);
  return dedupeSpans(result);
}

type StatusTone = 'ok' | 'error' | 'neutral';

function statusTone(status?: string | null): StatusTone {
  const className = statusClass(status);
  if (className === 'status-ok') return 'ok';
  if (className === 'status-error') return 'error';
  return 'neutral';
}

function statusLabel(status?: string | null): string {
  if (!hasValue(status) || String(status).trim() === '') return '—';
  const tone = statusTone(status);
  if (tone === 'ok') return 'OK';
  if (tone === 'error') return 'Error';
  return String(status);
}

function displayValue(value: string | null | undefined): string {
  return value && value.trim() !== '' ? value : '—';
}

function KvTable({ rows }: { rows: Array<[string, ReactNode]> }) {
  if (rows.length === 0) return <EmptyState title="No values recorded" />;
  return (
    <table className="kv-table">
      <thead>
        <tr>
          <th>Key</th>
          <th>Value</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([key, value], index) => (
          <tr key={`${key}-${index}`}>
            <td>{key}</td>
            <td>{value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function MonoBlock({ value, empty }: { value: unknown; empty: string }) {
  if (!hasValue(value)) return <div className="ins-empty">{empty}</div>;
  return <pre className="json-view">{toText(value)}</pre>;
}

function RawBlock({ value }: { value: unknown }) {
  const [copied, setCopied] = useState(false);
  const text = toText(value);
  const copy = async () => {
    const ok = await copyToClipboard(text);
    setCopied(ok);
    window.setTimeout(() => setCopied(false), 1500);
  };
  return (
    <div className="ins-stack">
      <div className="table-actions">
        <button type="button" className="copy-text" onClick={() => void copy()}>
          {copied ? 'copied' : 'copy json'}
        </button>
      </div>
      <pre className="json-view">{text}</pre>
    </div>
  );
}

export default function TraceDetail({
  traceId,
  tab,
  onNavigate,
  filters,
  refreshSignal,
}: TraceDetailProps) {
  const { trace, loading, error, refresh } = useTrace(traceId);
  const {
    replay,
    loading: replayLoading,
    error: replayError,
    refresh: refreshReplay,
  } = useSpans(traceId);
  const [copied, setCopied] = useState(false);
  const [selectedSpan, setSelectedSpan] = useState<SpanNode | null>(null);
  const [panelWidth, setPanelWidth] = useState<number>(readWidth);
  const [panelOpen, setPanelOpen] = useState<boolean>(readOpen);
  const [dragging, setDragging] = useState(false);
  const [replayTarget, setReplayTarget] = useState<ReplayLlmSpan | null>(null);
  const dragRef = useRef<{ startX: number; startWidth: number } | null>(null);
  const widthRef = useRef(panelWidth);
  const lastRefresh = useRef(refreshSignal);

  useEffect(() => {
    if (lastRefresh.current === refreshSignal) return;
    lastRefresh.current = refreshSignal;
    refresh();
    refreshReplay();
  }, [refreshSignal, refresh, refreshReplay]);

  useEffect(() => {
    try {
      window.localStorage.setItem(OPEN_KEY, panelOpen ? '1' : '0');
    } catch {
      return;
    }
  }, [panelOpen]);

  useEffect(() => {
    if (!dragging) return;
    const onMove = (event: globalThis.MouseEvent) => {
      const start = dragRef.current;
      if (!start) return;
      const limit = Math.min(MAX_PANEL, Math.max(MIN_PANEL, window.innerWidth * 0.6));
      const next = Math.max(MIN_PANEL, Math.min(limit, start.startWidth - (event.clientX - start.startX)));
      widthRef.current = next;
      setPanelWidth(next);
    };
    const onUp = () => {
      setDragging(false);
      dragRef.current = null;
      try {
        window.localStorage.setItem(WIDTH_KEY, String(widthRef.current));
      } catch {
        return;
      }
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, [dragging]);

  const handleSelectSpan = useCallback((span: SpanNode | null) => {
    setSelectedSpan(span);
    setPanelOpen(Boolean(span));
  }, []);

  const closePanel = useCallback(() => {
    setPanelOpen(false);
    setSelectedSpan(null);
  }, []);

  const handleCopy = async () => {
    const ok = await copyToClipboard(traceId);
    setCopied(ok);
    window.setTimeout(() => setCopied(false), 1500);
  };

  if (loading) {
    return (
      <div className="td2-page trace-detail">
        <LoadingRows rows={6} columns={3} label="Loading trace" />
      </div>
    );
  }

  if (error || !trace) {
    return (
      <div className="td2-page trace-detail">
        <ErrorState message={error || 'Trace not found.'} onRetry={refresh} />
      </div>
    );
  }

  const replayWithSpec = replay as ReplayWithSpec | null;
  const replaySpec: ReplaySpec | null = replayWithSpec?.replay_spec ?? null;
  const spans = collectSpans(replayWithSpec);
  const llmSpans = spans.filter(isLlmSpan);
  const events = Array.isArray(trace.events) ? trace.events : [];
  const exceptionEvents = events.filter(isExceptionEvent);
  const errorSpans = spans.filter(
    (span) => hasValue(span.error) || statusClass(span.status) === 'status-error',
  );
  const attributeRows = Object.entries(trace.attributes ?? {});
  const traceError = hasValue(trace.error) ? String(trace.error) : null;

  const spanTokens = resolveAggregateTokens(spans, trace.trace_id);
  const spanTokensAvailable =
    spanTokens.input !== null || spanTokens.output !== null || spanTokens.total !== null;
  const traceTokens = spanTokensAvailable ? spanTokens : resolveTokens(trace, trace.trace_id);
  const tokenSource = spanTokensAvailable ? 'spans' : 'trace';
  const spanCosts = spans.map((span) => findCost(span));
  const totalCost = spanCosts.some((value) => value !== null)
    ? spanCosts.reduce<number>((sum, value) => sum + (value ?? 0), 0)
    : findCost(trace);

  const models = Array.from(
    new Set(
      spans
        .map((span) => pickValue(span, ['model', 'model_name']))
        .concat([trace.model_name, trace.model])
        .filter((value): value is string => typeof value === 'string' && value.trim() !== ''),
    ),
  );

  const eligible = replaySpec?.eligible === true && Boolean(replaySpec.selected);
  const replayReason = replaySpec?.reason ?? null;
  const entries = Array.isArray(replaySpec?.llm_spans) ? replaySpec.llm_spans : [];
  const selectedEntry = selectedSpan?.span_id
    ? entries.find((entry) => entry.span_id === selectedSpan.span_id) ?? null
    : null;

  const stats: Array<{ label: string; value: ReactNode; tone: StatusTone; hint?: string }> = [
    { label: 'Status', value: statusLabel(trace.status), tone: statusTone(trace.status) },
    { label: 'Duration', value: displayValue(formatDuration(trace.duration_ms)), tone: 'neutral' },
    {
      label: 'Spans',
      value: replayLoading ? '—' : String(spans.length),
      hint: `${llmSpans.length} llm`,
      tone: 'neutral',
    },
    {
      label: 'Total tokens',
      value: replayLoading ? '—' : <TokenGroup tokens={traceTokens} stacked />,
      tone: 'neutral',
    },
    {
      label: 'Cost',
      value: replayLoading ? '—' : <CostValue value={totalCost} estimated={traceTokens.estimated} />,
      tone: 'neutral',
    },
  ];

  const metaItems: Array<[string, string]> = [
    ['Project', displayValue(trace.project_id || trace.project_name)],
    ['Environment', displayValue(trace.environment)],
    ['Started', displayValue(formatDateTime(trace.start_time))],
    ['Ended', displayValue(formatDateTime(trace.end_time))],
    ['Models', models.length > 0 ? models.join(', ') : '—'],
  ];

  const metaRows: Array<[string, ReactNode]> = [
    ['project', <span className="mono">{trace.project_id || trace.project_name || '—'}</span>],
    ['environment', <span className="mono">{trace.environment || '—'}</span>],
    ['started', <span className="mono">{formatDateTime(trace.start_time)}</span>],
    ['ended', <span className="mono">{formatDateTime(trace.end_time)}</span>],
    ['models', <span className="mono">{models.length > 0 ? models.join(', ') : '—'}</span>],
    ['trace_id', <span className="mono">{trace.trace_id}</span>],
  ];

  const handleDownload = () => {
    const blob = new Blob([toText(trace)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${trace.trace_id}.json`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  const renderTab = () => {
    if (tab === 'waterfall') {
      return (
        <Waterfall
          traceId={traceId}
          traceStart={trace.start_time}
          replay={replayWithSpec}
          loading={replayLoading}
          error={replayError}
          refresh={refreshReplay}
          replaySpec={replaySpec}
          onReplaySpan={setReplayTarget}
          onSelectSpan={handleSelectSpan}
          selectedSpanId={selectedSpan?.span_id ?? null}
        />
      );
    }

    if (tab === 'overview') {
      return (
        <>
          <Section label="Overview" title={trace.name || trace.trace_id}>
            <KvTable rows={metaRows} />
          </Section>
          <Section label="Span metrics">
            {replayLoading ? (
              <LoadingRows rows={3} columns={3} label="Loading span metrics" />
            ) : replayError ? (
              <ErrorState message={replayError} onRetry={refreshReplay} />
            ) : (
              <KvTable
                rows={[
                  ['span_count', String(spans.length)],
                  ['llm_calls', String(llmSpans.length)],
                  ['input_tokens', <TokenValue value={traceTokens.input} estimated={traceTokens.inputEstimated} kind="input" />],
                  ['output_tokens', <TokenValue value={traceTokens.output} estimated={traceTokens.outputEstimated} kind="output" />],
                  ['total_tokens', <TokenValue value={traceTokens.total} estimated={traceTokens.totalEstimated} kind="total" />],
                  ['token_source', tokenSource],
                  ['cost', <CostValue value={totalCost} estimated={traceTokens.estimated} />],
                ]}
              />
            )}
          </Section>
          <Section label="Replay availability">
            {replayLoading ? (
              <div className="ins-empty">Loading replay metadata…</div>
            ) : eligible ? (
              <div className="ins-empty">
                Available for {replaySpec?.selected?.name || replaySpec?.selected?.span_id || 'an LLM span'}.
              </div>
            ) : (
              <EmptyState title="Replay unavailable" message={replayReason ?? 'No eligible LLM span recorded.'} />
            )}
          </Section>
        </>
      );
    }

    if (tab === 'input') {
      return (
        <Section label="Input" title="prompt">
          <MonoBlock value={trace.prompt} empty="No prompt recorded for this trace." />
        </Section>
      );
    }

    if (tab === 'output') {
      return (
        <Section label="Output" title="response">
          <MonoBlock value={trace.response} empty="No response recorded for this trace." />
        </Section>
      );
    }

    if (tab === 'events') {
      return (
        <Section label="Events" title={String(events.length)}>
          {events.length === 0 ? (
            <EmptyState title="No events" message="No events were recorded for this trace." />
          ) : (
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Timestamp</th>
                    <th>Attributes</th>
                  </tr>
                </thead>
                <tbody>
                  {events.map((event, index) => (
                    <tr key={`${String(event.name ?? 'event')}-${index}`}>
                      <td className="mono">{String(event.name ?? 'event')}</td>
                      <td className="mono">{formatDateTime(event.timestamp ?? event.time)}</td>
                      <td className="mono">{hasValue(event.attributes) ? toText(event.attributes) : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Section>
      );
    }

    if (tab === 'attributes') {
      return (
        <Section label="Attributes">
          <KvTable
            rows={attributeRows.map(
              ([key, value]): [string, ReactNode] => [key, <span className="mono">{toText(value)}</span>],
            )}
          />
        </Section>
      );
    }

    if (tab === 'error') {
      return (
        <>
          <Section label="Trace error">
            {traceError ? (
              <div className="banner banner-error" role="alert">
                <span>{traceError}</span>
              </div>
            ) : (
              <EmptyState title="No trace error" message="No error recorded on this trace." />
            )}
          </Section>
          <Section label="Error spans" title={String(errorSpans.length)}>
            {errorSpans.length === 0 ? (
              <EmptyState title="No failed spans" />
            ) : (
              <ul className="ins-event-list">
                {errorSpans.map((span, index) => (
                  <li key={`${span.span_id ?? 'span'}-${index}`}>
                    <div className="ins-event-head">
                      <strong>{span.name || span.span_id || 'span'}</strong>
                      <span className={`status-tag ${statusClass(span.status)}`}>
                        {span.status ?? 'unknown'}
                      </span>
                    </div>
                    <div className="mono ins-event-time">{span.span_id ?? '—'}</div>
                    {hasValue(span.error) && (
                      <pre className="json-view">{String(span.error)}</pre>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </Section>
          <Section label="Exception events" title={String(exceptionEvents.length)}>
            {exceptionEvents.length === 0 ? (
              <EmptyState title="No exception events" />
            ) : (
              <ul className="ins-event-list">
                {exceptionEvents.map((event, index) => (
                  <li key={`${String(event.name ?? 'event')}-${index}`}>
                    <div className="ins-event-head">
                      <strong>{String(event.name ?? 'event')}</strong>
                      <span className="mono ins-event-time">
                        {formatDateTime(event.timestamp ?? event.time)}
                      </span>
                    </div>
                    {hasValue(event.attributes) && (
                      <pre className="json-view">{toText(event.attributes)}</pre>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </Section>
        </>
      );
    }

    return <Section label="Raw trace JSON"><RawBlock value={trace} /></Section>;
  };

  return (
    <div className="td2-page trace-detail">
      <header className="td2-header trace-header">
        <a className="td2-back" href={hrefWithFilters('#/traces', filters)}>
          ← All traces
        </a>
        <div className="td2-title-row trace-title-row">
          <div className="td2-title-block">
            <div className="td2-title-line">
              <h1 className="td2-title">{trace.name || trace.trace_id}</h1>
              <span className={`td2-status-pill td2-status-pill--${statusTone(trace.status)}`}>
                <span className="td2-status-dot" aria-hidden="true" />
                {statusLabel(trace.status)}
              </span>
            </div>
            <div className="td2-id-line">
              <span className="td2-id mono">{trace.trace_id}</span>
              <button type="button" className="td2-copy" onClick={() => void handleCopy()}>
                {copied ? 'Copied' : 'Copy ID'}
              </button>
            </div>
          </div>
          <div className="td2-header-actions table-actions">
            <button type="button" className="td2-btn td2-btn--outline" onClick={handleDownload}>
              Download JSON
            </button>
            {eligible && replaySpec?.selected && (
              <button
                type="button"
                className="td2-btn td2-btn--primary"
                onClick={() => setReplayTarget(replaySpec.selected ?? null)}
              >
                Replay
              </button>
            )}
          </div>
        </div>
      </header>

      <section className="td2-stats" aria-label="Trace statistics">
        {stats.map((stat) => (
          <div className={`td2-stat td2-stat--${stat.tone}`} key={stat.label}>
            <span className="td2-stat-label">{stat.label}</span>
            <span className="td2-stat-value mono">{stat.value}</span>
            {stat.hint && <span className="td2-stat-hint mono">{stat.hint}</span>}
          </div>
        ))}
      </section>

      <section className="td2-meta-card" aria-label="Trace metadata">
        {metaItems.map(([label, value]) => (
          <div className="td2-meta-item" key={label}>
            <span className="td2-meta-label">{label}</span>
            <span className="td2-meta-value mono" title={value}>{value}</span>
          </div>
        ))}
      </section>

      <div className="td2-tabs tabs" role="tablist" aria-label="Trace sections">
        {TRACE_TABS.map((item) => (
          <button
            type="button"
            key={item.id}
            role="tab"
            aria-selected={tab === item.id}
            className={tab === item.id ? 'td2-tab active' : 'td2-tab'}
            onClick={() => onNavigate(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div
        className={`td2-workspace trace-workspace${panelOpen ? ' has-panel' : ''}`}
        style={{ '--span-panel-width': `${panelWidth}px` } as CSSProperties}
      >
        <div className="td2-main trace-main">
          <div className="td2-tab-content">{renderTab()}</div>
        </div>
        {panelOpen && (
          <>
            <div
              className={`wf-resize${dragging ? ' dragging' : ''}`}
              role="separator"
              aria-orientation="vertical"
              aria-label="Resize span panel"
              onMouseDown={(event) => {
                event.preventDefault();
                dragRef.current = { startX: event.clientX, startWidth: panelWidth };
                setDragging(true);
              }}
            />
            <aside className="span-panel td2-panel" aria-label="Span inspector">
              <SpanInspector
                span={selectedSpan}
                replayEntry={selectedEntry}
                spec={replaySpec}
                traceId={traceId}
                onClose={closePanel}
                onReplay={setReplayTarget}
              />
            </aside>
          </>
        )}
      </div>

      {replayTarget && replaySpec && (
        <ReplayPanel
          traceId={traceId}
          entry={replayTarget}
          spec={replaySpec}
          onClose={() => setReplayTarget(null)}
        />
      )}
    </div>
  );
}
