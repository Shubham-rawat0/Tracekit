import { useMemo, useState } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from 'react';
import type { SpanNode } from '../types';
import { formatDateTime, formatDuration, spanDuration, statusClass, toFiniteNumber, toMillis } from '../utils/format';
import { EmptyState, ErrorState, LoadingRows, Section } from '../components/States';
import { hasValue, isLlmSpan, isToolSpan, pickValue } from '../components/SpanInspector';
import type { ReplayLlmSpan, ReplaySpec, ReplayWithSpec } from '../components/SpanInspector';
import { TokenGroup } from '../components/TokenValue';
import { learnTokenHistory, resolveTokens } from '../utils/tokenEstimation';

interface WaterfallProps {
  traceId: string;
  traceStart?: string | null;
  replay: ReplayWithSpec | null;
  loading: boolean;
  error: string | null;
  refresh: () => void;
  replaySpec: ReplaySpec | null;
  onReplaySpan: (entry: ReplayLlmSpan) => void;
  onSelectSpan: (span: SpanNode | null) => void;
  selectedSpanId: string | null;
}

interface FlatRow {
  node: SpanNode;
  depth: number;
}

const TICK_FRACTIONS = [0, 0.25, 0.5, 0.75, 1];

function childrenOf(node: SpanNode): SpanNode[] {
  return Array.isArray(node.children) ? node.children : [];
}

function byStart(a: SpanNode, b: SpanNode): number {
  return (toMillis(a.start_time) ?? 0) - (toMillis(b.start_time) ?? 0);
}

function sortTree(nodes: SpanNode[]): SpanNode[] {
  return [...nodes].sort(byStart).map((node) => ({ ...node, children: sortTree(childrenOf(node)) }));
}

function buildTree(replay: ReplayWithSpec | null): SpanNode[] {
  const supplied = replay?.tree;
  if (Array.isArray(supplied) && supplied.length > 0) return sortTree(supplied);
  const spans = replay?.spans;
  if (!Array.isArray(spans) || spans.length === 0) return [];
  const nodes = spans.map((span) => ({ ...span, children: [] as SpanNode[] }));
  const roots: SpanNode[] = [];
  spans.forEach((span, index) => {
    const node = nodes[index];
    const parentId = span['parent_span_id'];
    const parentIndex = typeof parentId === 'string' ? spans.findIndex((item) => item.span_id === parentId) : -1;
    if (parentIndex >= 0 && parentIndex !== index) {
      const parent = nodes[parentIndex];
      parent.children = [...(parent.children ?? []), node];
    } else {
      roots.push(node);
    }
  });
  return sortTree(roots);
}

function countNodes(nodes: SpanNode[]): number {
  return nodes.reduce((total, node) => total + 1 + countNodes(childrenOf(node)), 0);
}

function earliestStart(nodes: SpanNode[]): number | null {
  let result: number | null = null;
  for (const node of nodes) {
    const start = toMillis(node.start_time);
    if (start !== null && (result === null || start < result)) result = start;
    const child = earliestStart(childrenOf(node));
    if (child !== null && (result === null || child < result)) result = child;
  }
  return result;
}

function latestEnd(nodes: SpanNode[], origin: number | null): number {
  let max = 0;
  for (const node of nodes) {
    const start = toMillis(node.start_time);
    const duration = spanDuration(node);
    const end = start !== null ? start + duration : duration;
    const relative = origin !== null && start !== null ? end - origin : duration;
    if (relative > max) max = relative;
    const child = latestEnd(childrenOf(node), origin);
    if (child > max) max = child;
  }
  return max;
}

function searchText(node: SpanNode): string {
  const attributes = node.attributes && typeof node.attributes === 'object' ? node.attributes : {};
  return [
    node.name,
    node.span_id,
    node.span_type,
    node.status,
    pickValue(node, ['model', 'model_name']),
    pickValue(attributes, ['model', 'model_name', 'tool_name']),
  ]
    .filter((value) => hasValue(value))
    .map((value) => String(value).toLowerCase())
    .join(' ');
}

function filterTree(nodes: SpanNode[], needle: string): SpanNode[] {
  const result: SpanNode[] = [];
  for (const node of nodes) {
    const children = filterTree(childrenOf(node), needle);
    if (searchText(node).includes(needle) || children.length > 0) {
      result.push({ ...node, children });
    }
  }
  return result;
}

function flattenTree(
  nodes: SpanNode[],
  depth: number,
  collapsed: Set<string>,
  rows: FlatRow[],
): void {
  for (const node of nodes) {
    const id = node.span_id ? String(node.span_id) : '';
    rows.push({ node, depth });
    if (!collapsed.has(id)) flattenTree(childrenOf(node), depth + 1, collapsed, rows);
  }
}

function collectIds(nodes: SpanNode[], result: string[]): void {
  for (const node of nodes) {
    if (node.span_id) result.push(String(node.span_id));
    collectIds(childrenOf(node), result);
  }
}

function TimelineGrid() {
  return (
    <span className="wf-grid" aria-hidden="true">
      {TICK_FRACTIONS.map((fraction) => (
        <span key={fraction} className="wf-grid-line" style={{ left: `${fraction * 100}%` }} />
      ))}
    </span>
  );
}

export default function Waterfall({
  traceId,
  traceStart,
  replay,
  loading,
  error,
  refresh,
  replaySpec,
  onSelectSpan,
  selectedSpanId,
}: WaterfallProps) {
  const [zoom, setZoom] = useState(1);
  const [query, setQuery] = useState('');
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [focusIndex, setFocusIndex] = useState(0);

  const tree = useMemo(() => buildTree(replay), [replay]);
  const allIds = useMemo(() => {
    const ids: string[] = [];
    collectIds(tree, ids);
    return ids;
  }, [tree]);
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return needle ? filterTree(tree, needle) : tree;
  }, [tree, query]);
  learnTokenHistory(tree);
  const rows = useMemo(() => {
    const result: FlatRow[] = [];
    flattenTree(filtered, 0, collapsed, result);
    return result;
  }, [filtered, collapsed]);

  const origin = useMemo(() => toMillis(traceStart) ?? earliestStart(tree), [traceStart, tree]);
  const scale = useMemo(() => {
    const explicit = toFiniteNumber(replay?.trace?.duration_ms);
    if (explicit !== null && explicit > 0) return explicit;
    const computed = latestEnd(tree, origin);
    return computed > 0 ? computed : 1;
  }, [replay, origin, tree]);

  const entries = Array.isArray(replaySpec?.llm_spans) ? replaySpec.llm_spans : [];
  const ticks = TICK_FRACTIONS.map((fraction) => scale * fraction);
  const activeIndex = rows.length === 0 ? -1 : Math.min(Math.max(focusIndex, 0), rows.length - 1);

  const changeZoom = (next: number) => setZoom(Math.max(0.25, Math.min(8, next)));

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const key = event.key;
    if (key === 'Escape') {
      event.preventDefault();
      onSelectSpan(null);
      return;
    }
    if (key === '+' || key === '=') {
      event.preventDefault();
      changeZoom(zoom * 2);
      return;
    }
    if (key === '-' || key === '_') {
      event.preventDefault();
      changeZoom(zoom / 2);
      return;
    }
    if (rows.length === 0) return;
    if (key === 'ArrowDown' || key === 'j' || key === 'ArrowUp' || key === 'k') {
      event.preventDefault();
      const delta = key === 'ArrowDown' || key === 'j' ? 1 : -1;
      const next = Math.max(0, Math.min(rows.length - 1, activeIndex + delta));
      setFocusIndex(next);
      onSelectSpan(rows[next].node);
      return;
    }
    if (key === 'Enter') {
      event.preventDefault();
      onSelectSpan(rows[activeIndex].node);
    }
  };

  const toggleNode = (id: string) => {
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toolbar: ReactNode = (
    <div className="wf-toolbar">
      <div className="wf-toolbar-left">
        <label className="field wf-search">
          <span>Filter spans</span>
          <input
            type="text"
            value={query}
            placeholder="name, id, type, model"
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <span className="wf-stat">
          {rows.length} / {countNodes(tree)} spans
        </span>
      </div>
      <div className="wf-toolbar-right">
        <button
          type="button"
          className="td2-btn"
          onClick={() => setCollapsed(new Set(allIds))}
        >
          Collapse all
        </button>
        <button type="button" className="td2-btn" onClick={() => setCollapsed(new Set())}>
          Expand all
        </button>
        <button
          type="button"
          className="td2-btn td2-btn--icon"
          aria-label="Zoom out"
          onClick={() => changeZoom(zoom / 2)}
        >
          −
        </button>
        <span className="mono wf-stat">{Math.round(zoom * 100)}%</span>
        <button
          type="button"
          className="td2-btn td2-btn--icon"
          aria-label="Zoom in"
          onClick={() => changeZoom(zoom * 2)}
        >
          +
        </button>
        <button type="button" className="td2-btn td2-btn--ghost" onClick={() => setZoom(1)}>
          Fit
        </button>
      </div>
    </div>
  );

  return (
    <div className="td2-wf">
    <Section
      label="Waterfall"
      title={`${countNodes(tree)} spans`}
      actions={<span className="muted-line mono">trace {traceId}</span>}
    >
      {toolbar}
      {error && <ErrorState message={error} onRetry={refresh} />}
      {loading && <LoadingRows rows={6} columns={4} label="Loading spans" />}
      {!loading && !error && tree.length === 0 && (
        <EmptyState title="No spans recorded" message="Select a trace with recorded spans to inspect its timeline." />
      )}
      {!loading && !error && tree.length > 0 && rows.length === 0 && (
        <div className="wf-empty">No spans match "{query}".</div>
      )}
      {!loading && !error && rows.length > 0 && (
        <div
          className="wf-table"
          role="grid"
          tabIndex={0}
          aria-label="Span waterfall"
          onKeyDown={handleKeyDown}
        >
          <div
            className="wf-content"
            style={{ minWidth: `max(${Math.round(zoom * 100)}%, 860px, 100%)` }}
          >
            <div className="wf-row wf-header" role="row">
              <span className="wf-cell">Span / Type / Model</span>
              <span className="wf-cell">Timeline</span>
              <span className="wf-cell wf-meta">Status / Tokens / Duration</span>
            </div>
            <div className="wf-row wf-ruler-row" role="row">
              <span className="wf-cell wf-tree" />
              <div className="wf-cell wf-timeline wf-ruler">
                {ticks.map((tick, index) => (
                  <span
                    key={index}
                    className="wf-tick mono"
                    style={{ left: `${TICK_FRACTIONS[index] * 100}%` }}
                  >
                    <span className="wf-tick-mark" aria-hidden="true" />
                    {index === 0 || index === 2 || index === 4 ? formatDuration(tick) : ''}
                  </span>
                ))}
              </div>
              <span className="wf-cell wf-meta" />
            </div>
            {rows.map(({ node, depth }, index) => {
              const start = toMillis(node.start_time);
              const offset = start !== null && origin !== null ? Math.max(0, start - origin) : 0;
              const duration = spanDuration(node);
              const left = scale > 0 ? Math.min(99, (offset / scale) * 100) : 0;
              const width = scale > 0 ? Math.max(0.8, Math.min(100 - left, (duration / scale) * 100)) : 0.8;
              const id = node.span_id ? String(node.span_id) : '';
              const tokens = resolveTokens(node, id || undefined);
              const model = pickValue(node, ['model', 'model_name']) ?? pickValue(node.attributes, ['model', 'model_name']);
              const error = hasValue(node.error) || statusClass(node.status) === 'status-error';
              const llm = isLlmSpan(node) && !error;
              const tool = isToolSpan(node) && !error;
              const selected = Boolean(node.span_id && String(node.span_id) === selectedSpanId);
              const children = childrenOf(node);
              const expanded = !collapsed.has(id);
              const title = [
                node.name || 'span',
                `type: ${node.span_type || 'unknown'}`,
                `start: ${formatDateTime(node.start_time)}`,
                `offset: ${formatDuration(offset)}`,
                `duration: ${formatDuration(duration)}`,
                `status: ${node.status || 'unknown'}`,
                id ? `span_id: ${id}` : '',
              ]
                .filter(Boolean)
                .join(' | ');
              const barTitle = [
                node.name || 'span',
                `type: ${node.span_type || 'unknown'}`,
                `start offset: ${formatDuration(offset)}`,
                `duration: ${formatDuration(duration)}`,
                `model: ${hasValue(model) ? String(model) : '—'}`,
              ].join(' | ');
              const entry = id ? entries.find((item) => item.span_id === id) : null;

              return (
                <div
                  key={id || `row-${index}`}
                  role="row"
                  aria-label={`Select span ${node.name || id || 'span'}`}
                  aria-selected={selected}
                  className={`wf-row${selected ? ' selected wf-selected' : ''}`}
                  title={title}
                  onClick={() => {
                    setFocusIndex(index);
                    onSelectSpan(node);
                  }}
                >
                  <div className="wf-cell wf-tree">
                    <span className="wf-indent" aria-hidden="true">
                      {Array.from({ length: depth }, (_, guide) => (
                        <span key={guide} className="wf-indent-guide" />
                      ))}
                    </span>
                    <button
                      type="button"
                      className="wf-toggle"
                      aria-expanded={children.length > 0 ? expanded : undefined}
                      disabled={children.length === 0}
                      onClick={(event) => {
                        event.stopPropagation();
                        if (id) toggleNode(id);
                      }}
                    >
                      {children.length > 0 ? (expanded ? '▾' : '▸') : '·'}
                    </button>
                    <span className="wf-name-block">
                      <span className="wf-name">{node.name || 'span'}</span>
                      <span className="wf-subline mono">
                        <span className="wf-type">{node.span_type || 'unknown'}</span>
                        {hasValue(model) && <span className="wf-model">{String(model)}</span>}
                      </span>
                    </span>
                  </div>
                  <div className="wf-cell wf-timeline">
                    <TimelineGrid />
                    <div className="wf-bar-area">
                      <div className="wf-bar-wrap" style={{ left: `${left}%`, width: `${width}%` }} title={barTitle}>
                        <div
                          className={`wf-bar${llm ? ' llm' : ''}${tool ? ' tool' : ''}${error ? ' error' : ''}`}
                        />
                        <span className="wf-bar-label inside">{formatDuration(duration)}</span>
                      </div>
                    </div>
                  </div>
                  <div className="wf-cell wf-meta">
                    {entry && <span className="wf-tokens">replay</span>}
                    <span className="wf-tokens"><TokenGroup tokens={tokens} compact /></span>
                    <span className={`td2-status-pill td2-status-pill--${
                      statusClass(node.status) === 'status-ok'
                        ? 'ok'
                        : statusClass(node.status) === 'status-error'
                          ? 'error'
                          : 'neutral'
                    }`}>
                      <span className="td2-status-dot" aria-hidden="true" />
                      {node.status || 'unknown'}
                    </span>
                    <span className="wf-duration">{formatDuration(duration)}</span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
      {tree.length > 0 && (
        <div className="wf-legend" aria-label="Span type legend">
          <span className="wf-legend-item"><span className="wf-legend-swatch llm" />LLM</span>
          <span className="wf-legend-item"><span className="wf-legend-swatch tool" />Tool</span>
          <span className="wf-legend-item"><span className="wf-legend-swatch other" />Other</span>
        </div>
      )}
    </Section>
    </div>
  );
}
