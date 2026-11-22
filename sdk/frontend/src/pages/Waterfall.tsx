import { useEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent } from 'react';
import '../inspector.css';
import type { SpanNode } from '../types';
import { hasValue, isLlmSpan, isToolSpan, pickValue } from '../components/SpanInspector';
import type { ReplayLlmSpan, ReplaySpec, ReplayWithSpec } from '../components/SpanInspector';
import { statusClass, toFiniteNumber, toMillis } from '../utils/format';
import { EmptyState, ErrorState, LoadingRows, Section } from '../components/States';
import { learnTokenHistory, resolveTokens } from '../utils/tokenEstimation';
import {
  buildSpanTree,
  collectSpanIds,
  countTreeNodes,
  filterSpanTree,
  flattenVisibleSpans,
  indexSpanTree,
  matchingSpanIds,
  normalizeSpanTree,
} from '../utils/spanTree';

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

type SpanCategory = 'agent' | 'chain' | 'llm' | 'tool' | 'other';

const TICK_FRACTIONS = [0, 0.25, 0.5, 0.75, 1];
const ROW_HEIGHT = 46;
const HEADER_HEIGHT = 34;
const AXIS_HEIGHT = 28;
const CONTENT_OFFSET = HEADER_HEIGHT + AXIS_HEIGHT;
const VIRTUALIZE_AFTER = 300;
const OVERSCAN = 8;

function durationLabel(milliseconds: number): string {
  if (!Number.isFinite(milliseconds)) return '—';
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  return `${(milliseconds / 1000).toFixed(2)} s`;
}

function axisLabel(milliseconds: number): string {
  return `${(milliseconds / 1000).toFixed(2)}s`;
}

function tokenLabel(tokens: { input: number | null; output: number | null }): string {
  if (tokens.input === null && tokens.output === null) return '';
  return `${tokens.input ?? '—'}→${tokens.output ?? '—'} tok`;
}

function categoryOf(node: SpanNode, hasChildren: boolean): SpanCategory {
  if (isToolSpan(node)) return 'tool';
  if (isLlmSpan(node)) return 'llm';
  const kind = String(node.span_type ?? node.type ?? '').toLowerCase();
  const name = String(node.name ?? '').toLowerCase();
  if (kind.includes('agent') || name.includes('agent')) return 'agent';
  if (kind.includes('chain') || kind.includes('graph') || hasChildren) return 'chain';
  return 'other';
}

function decorateSpan(
  node: SpanNode,
  parentName: string | null,
  traceStart: number | null,
  traceDuration: number,
): SpanNode {
  return {
    ...node,
    __tracekitParentName: parentName,
    __tracekitTraceStart: traceStart,
    __tracekitTraceDuration: traceDuration,
  } as SpanNode;
}

function TimelineGrid() {
  return (
    <span className="wf2-grid" aria-hidden="true">
      {TICK_FRACTIONS.map((fraction) => (
        <span key={fraction} className="wf2-grid-line" style={{ left: `${fraction * 100}%` }} />
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
  onSelectSpan,
  selectedSpanId,
}: WaterfallProps) {
  const [zoom, setZoom] = useState(1);
  const [query, setQuery] = useState('');
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [focusIndex, setFocusIndex] = useState(0);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportHeight, setViewportHeight] = useState(600);
  const tableRef = useRef<HTMLDivElement | null>(null);

  const tree = useMemo(() => {
    const flat = Array.isArray(replay?.spans) ? replay.spans : [];
    if (flat.length > 0) return buildSpanTree(flat as SpanNode[]).roots;
    return normalizeSpanTree(Array.isArray(replay?.tree) ? replay.tree : []);
  }, [replay]);
  const total = useMemo(() => countTreeNodes(tree), [tree]);
  const index = useMemo(() => indexSpanTree(tree), [tree]);
  const allIds = useMemo(() => collectSpanIds(tree), [tree]);
  const filtered = useMemo(() => filterSpanTree(tree, query), [tree, query]);
  const matches = useMemo(() => matchingSpanIds(tree, query), [tree, query]);

  useEffect(() => {
    learnTokenHistory(tree);
  }, [tree]);

  const origin = useMemo(() => {
    const explicit = toMillis(traceStart);
    if (explicit !== null) return explicit;
    let earliest: number | null = null;
    const walk = (nodes: typeof tree) => {
      for (const node of nodes) {
        const start = toMillis(node.start_time);
        if (start !== null && (earliest === null || start < earliest)) earliest = start;
        walk(node.children);
      }
    };
    walk(tree);
    return earliest;
  }, [traceStart, tree]);

  const scale = useMemo(() => {
    const explicit = toFiniteNumber(replay?.trace?.duration_ms);
    if (explicit !== null && explicit > 0) return explicit;
    let latest = 0;
    const walk = (nodes: typeof tree) => {
      for (const node of nodes) {
        const start = toMillis(node.start_time);
        const duration = toFiniteNumber(node.duration_ms) ?? 0;
        const end = start !== null ? start + Math.max(0, duration) : Math.max(0, duration);
        const relative = origin !== null && start !== null ? end - origin : end;
        if (relative > latest) latest = relative;
        walk(node.children);
      }
    };
    walk(tree);
    return latest > 0 ? latest : 1;
  }, [replay, origin, tree]);

  const rows = useMemo(
    () => flattenVisibleSpans(filtered, { collapsed, matches: query.trim() ? matches : null }),
    [filtered, collapsed, matches, query],
  );

  const virtualized = rows.length > VIRTUALIZE_AFTER;
  const startIndex = virtualized
    ? Math.max(0, Math.floor((scrollTop - CONTENT_OFFSET) / ROW_HEIGHT) - OVERSCAN)
    : 0;
  const endIndex = virtualized
    ? Math.min(
        rows.length,
        Math.ceil((scrollTop - CONTENT_OFFSET + viewportHeight) / ROW_HEIGHT) + OVERSCAN,
      )
    : rows.length;
  const visibleRows = rows.slice(startIndex, endIndex);
  const activeIndex = rows.length === 0 ? -1 : Math.min(Math.max(focusIndex, 0), rows.length - 1);

  useEffect(() => {
    const element = tableRef.current;
    if (!element) return;
    const update = () => {
      setScrollTop(element.scrollTop);
      setViewportHeight(element.clientHeight);
    };
    update();
    element.addEventListener('scroll', update, { passive: true });
    if (typeof ResizeObserver === 'undefined') return () => element.removeEventListener('scroll', update);
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => {
      element.removeEventListener('scroll', update);
      observer.disconnect();
    };
  }, [rows.length]);

  useEffect(() => {
    if (!virtualized || activeIndex < 0 || !tableRef.current) return;
    const element = tableRef.current;
    const top = CONTENT_OFFSET + activeIndex * ROW_HEIGHT;
    const bottom = top + ROW_HEIGHT;
    if (top < element.scrollTop + CONTENT_OFFSET) {
      element.scrollTop = Math.max(0, top - CONTENT_OFFSET);
    } else if (bottom > element.scrollTop + element.clientHeight) {
      element.scrollTop = bottom - element.clientHeight;
    }
  }, [activeIndex, virtualized]);

  const changeZoom = (next: number) => setZoom(Math.max(0.5, Math.min(4, next)));

  const selectRow = (rowIndex: number) => {
    const row = rows[rowIndex];
    if (!row) return;
    const id = row.node.span_id ? String(row.node.span_id) : '';
    setFocusIndex(rowIndex);
    if (row.node.synthetic) return;
    const parentId = index.parentByChild.get(id) ?? null;
    const parentName = parentId ? index.byId.get(parentId)?.name ?? null : null;
    onSelectSpan(decorateSpan(row.node, parentName, origin, scale));
  };

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      onSelectSpan(null);
      return;
    }
    if (event.key === '+' || event.key === '=') {
      event.preventDefault();
      changeZoom(zoom * 1.5);
      return;
    }
    if (event.key === '-' || event.key === '_') {
      event.preventDefault();
      changeZoom(zoom / 1.5);
      return;
    }
    if (rows.length === 0) return;
    if (event.key === 'ArrowDown' || event.key === 'j' || event.key === 'ArrowUp' || event.key === 'k') {
      event.preventDefault();
      const delta = event.key === 'ArrowDown' || event.key === 'j' ? 1 : -1;
      selectRow(Math.max(0, Math.min(rows.length - 1, activeIndex + delta)));
      return;
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      selectRow(activeIndex);
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

  return (
    <div className="td2-wf wf2">
      <Section
        label="Waterfall"
        title={`${total} spans`}
        actions={<span className="muted-line mono">trace {traceId}</span>}
      >
        <div className="wf2-toolbar">
          <label className="field wf2-search">
            <span>Filter spans</span>
            <input
              type="text"
              value={query}
              placeholder="name, type, model, id"
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <div className="wf2-legend" aria-label="Span type legend">
            <span className="wf2-legend-item"><span className="wf2-dot agent" />Agent</span>
            <span className="wf2-legend-item"><span className="wf2-dot chain" />Chain</span>
            <span className="wf2-legend-item"><span className="wf2-dot llm" />LLM</span>
            <span className="wf2-legend-item"><span className="wf2-dot tool" />Tool</span>
          </div>
          <span className="wf2-stat mono">
            {rows.length} / {total} spans
          </span>
          <div className="wf2-actions">
            <button type="button" className="td2-btn" onClick={() => setCollapsed(new Set(allIds))}>
              Collapse all
            </button>
            <button type="button" className="td2-btn" onClick={() => setCollapsed(new Set())}>
              Expand all
            </button>
            <button
              type="button"
              className="td2-btn td2-btn--icon"
              aria-label="Zoom out"
              onClick={() => changeZoom(zoom / 1.5)}
            >
              <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
                <circle cx="5" cy="5" r="3.25" fill="none" stroke="currentColor" strokeWidth="1.2" />
                <path d="M2.75 5h4.5M7.5 7.5 10 10" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
              </svg>
            </button>
            <span className="mono wf2-stat">{Math.round(zoom * 100)}%</span>
            <button
              type="button"
              className="td2-btn td2-btn--icon"
              aria-label="Zoom in"
              onClick={() => changeZoom(zoom * 1.5)}
            >
              <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
                <circle cx="5" cy="5" r="3.25" fill="none" stroke="currentColor" strokeWidth="1.2" />
                <path d="M2.75 5h4.5M5 2.75v4.5M7.5 7.5 10 10" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
              </svg>
            </button>
            <button type="button" className="td2-btn td2-btn--ghost" onClick={() => setZoom(1)}>
              Fit
            </button>
          </div>
        </div>

        {error && <ErrorState message={error} onRetry={refresh} />}
        {loading && <LoadingRows rows={6} columns={4} label="Loading spans" />}
        {!loading && !error && total === 0 && (
          <EmptyState
            title="No spans recorded"
            message="Select a trace with recorded spans to inspect its timeline."
          />
        )}
        {!loading && !error && total > 0 && rows.length === 0 && (
          <div className="wf2-empty">No spans match “{query}”.</div>
        )}
        {!loading && !error && rows.length > 0 && (
          <div
            className="wf2-table"
            ref={tableRef}
            role="treegrid"
            tabIndex={0}
            aria-label="Span waterfall"
            onKeyDown={handleKeyDown}
          >
            <div
              className="wf2-content"
              style={{ minWidth: `max(820px, ${Math.round(zoom * 100)}%, 100%)` }}
            >
              <div className="wf2-head" role="row">
                <div className="wf2-head-name" role="columnheader">Span</div>
                <div className="wf2-head-axis" role="columnheader">
                  {TICK_FRACTIONS.map((fraction, tickIndex) => (
                    <span
                      key={fraction}
                      className="wf2-tick"
                      style={{ left: `${fraction * 100}%` }}
                    >
                      {axisLabel(scale * fraction)}
                      {tickIndex === TICK_FRACTIONS.length - 1 ? ' total' : ''}
                    </span>
                  ))}
                </div>
              </div>

              {startIndex > 0 && <div style={{ height: startIndex * ROW_HEIGHT }} aria-hidden="true" />}

              {visibleRows.map((row, relativeIndex) => {
                const rowIndex = startIndex + relativeIndex;
                const node = row.node;
                const id = node.span_id ? String(node.span_id) : '';
                const synthetic = Boolean(node.synthetic);
                const name = node.name || id || 'span';
                const childCount = id ? index.childCountById.get(id) ?? 0 : node.children.length;
                const hasChildren = childCount > 0;
                const expanded = !collapsed.has(id);
                const category = categoryOf(node, hasChildren);
                const model = pickValue(node, ['model', 'model_name']) ?? pickValue(node.attributes, ['model', 'model_name']);
                const tokens = resolveTokens(node as unknown as Record<string, unknown>, id || undefined);
                const tokenText = tokenLabel(tokens);
                const start = toMillis(node.start_time);
                const offset = start !== null && origin !== null ? Math.max(0, start - origin) : 0;
                const duration = toFiniteNumber(node.duration_ms) ?? 0;
                const left = scale > 0 ? Math.min(99.6, (offset / scale) * 100) : 0;
                const width = scale > 0 ? Math.max(0.4, Math.min(100 - left, (duration / scale) * 100)) : 0.4;
                const failed = hasValue(node.error) || statusClass(node.status) === 'status-error';
                const selected = Boolean(id && id === selectedSpanId);
                const container = hasChildren && category !== 'llm' && category !== 'tool';
                const inside = width > 11 && !synthetic;
                const subline = synthetic
                  ? 'spans with an unknown parent'
                  : [category, hasValue(model) ? String(model) : null, tokenText || null]
                      .filter(Boolean)
                      .join(' · ');

                return (
                  <div
                    key={id || `row-${rowIndex}`}
                    role="treeitem"
                    aria-level={row.depth + 1}
                    aria-selected={selected}
                    aria-expanded={hasChildren ? expanded : undefined}
                    className={`wf2-row${selected ? ' selected' : ''}${failed ? ' errored' : ''}${synthetic ? ' synthetic' : ''}`}
                    style={{ height: ROW_HEIGHT }}
                    onClick={() => selectRow(rowIndex)}
                  >
                    <div className="wf2-name-cell">
                      <span className="wf2-guides" aria-hidden="true">
                        {row.guides.map((continues, guideIndex) => (
                          <span
                            key={guideIndex}
                            className={`wf2-guide${continues ? ' continues' : ''}`}
                            style={{ left: guideIndex * 20 + 9 }}
                          />
                        ))}
                        {row.depth > 0 && (
                          <span
                            className="wf2-elbow"
                            style={{ left: (row.depth - 1) * 20 + 9 }}
                          />
                        )}
                      </span>
                      {hasChildren ? (
                        <button
                          type="button"
                          className="wf2-toggle"
                          aria-label={expanded ? `Collapse ${name}` : `Expand ${name}`}
                          aria-expanded={expanded}
                          onClick={(event) => {
                            event.stopPropagation();
                            toggleNode(id);
                          }}
                        >
                          <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
                            <path
                              d={expanded ? 'M2 3.5 5 6.5 8 3.5' : 'M3.5 2 6.5 5 3.5 8'}
                              fill="none"
                              stroke="currentColor"
                              strokeWidth="1.4"
                              strokeLinecap="round"
                              strokeLinejoin="round"
                            />
                          </svg>
                        </button>
                      ) : (
                        <span className="wf2-toggle-spacer" aria-hidden="true" />
                      )}
                      <span className={`wf2-dot ${category}`} aria-hidden="true" />
                      <span className="wf2-name-block">
                        <span className="wf2-name" title={name}>{name}</span>
                        <span className="wf2-subline mono" title={subline}>{subline}</span>
                      </span>
                    </div>
                    <div className="wf2-timeline">
                      <TimelineGrid />
                      {!synthetic && (
                        <div
                          className="wf2-bar-wrap"
                          style={{ left: `${left}%`, width: `${width}%` }}
                        >
                          <div
                            className={`wf2-bar ${category}${container ? ' container' : ' leaf'}${failed ? ' errored' : ''}`}
                          />
                          <span className={`wf2-bar-label ${inside ? 'inside' : 'outside right'}`}>
                            {durationLabel(duration)}
                            {!inside && tokenText ? ` · ${tokenText}` : ''}
                          </span>
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}

              {endIndex < rows.length && (
                <div style={{ height: (rows.length - endIndex) * ROW_HEIGHT }} aria-hidden="true" />
              )}
            </div>
          </div>
        )}
      </Section>
    </div>
  );
}
