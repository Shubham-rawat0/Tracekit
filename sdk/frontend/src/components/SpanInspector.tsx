import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import type { Replay, SpanNode, TraceEvent } from '../types';
import { copyToClipboard } from '../utils/auth';
import {
  formatDateTime,
  formatDuration,
  formatValue,
  latencyClass,
  spanDuration,
  statusClass,
  toFiniteNumber,
  toMillis,
} from '../utils/format';
import ReplayPanel from './ReplayPanel';
import { CostValue, TokenValue } from './TokenValue';
import { learnTokenHistory, readTokenCounts, resolveTokens } from '../utils/tokenEstimation';

export interface ReplayLlmSpan {
  span_id?: string | null;
  name?: string | null;
  model?: string | null;
  provider?: string | null;
  input?: unknown;
  output?: unknown;
  parameters?: Record<string, unknown> | null;
  parameters_available?: boolean | null;
  token_usage?: Record<string, unknown> | null;
  token_usage_available?: boolean | null;
  latency_ms?: number | null;
  status?: string | null;
  error?: unknown;
  [key: string]: unknown;
}

export interface ReplaySpec {
  trace_id?: string | null;
  eligible?: boolean | null;
  reason?: string | null;
  llm_spans?: ReplayLlmSpan[] | null;
  selected?: ReplayLlmSpan | null;
  warning?: string | null;
  [key: string]: unknown;
}

export interface ReplayWithSpec extends Replay {
  replay_spec?: ReplaySpec | null;
}

export interface TokenCounts {
  input: number | null;
  output: number | null;
  total: number | null;
}

type PanelTab = 'summary' | 'input' | 'tools' | 'events' | 'attributes' | 'replay' | 'raw';

const PANEL_TABS: Array<{ id: PanelTab; label: string }> = [
  { id: 'summary', label: 'Summary' },
  { id: 'input', label: 'Input' },
  { id: 'tools', label: 'Tools' },
  { id: 'events', label: 'Events' },
  { id: 'attributes', label: 'Attributes' },
  { id: 'replay', label: 'Replay' },
  { id: 'raw', label: 'Raw JSON' },
];

export function hasValue(value: unknown): boolean {
  return value !== null && value !== undefined;
}

export function toText(value: unknown): string {
  return formatValue(value);
}

export function formatCount(value: unknown): string {
  const number = toFiniteNumber(value);
  return number === null ? '—' : String(number);
}

export function formatCost(value: unknown): string {
  const number = toFiniteNumber(value);
  if (number === null) return '—';
  return `$${number.toFixed(Math.abs(number) < 1 ? 4 : 2)}`;
}

export function getAttributes(
  source: Record<string, unknown> | null | undefined,
): Record<string, unknown> {
  const value = source?.attributes;
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return {};
}

export function pickValue(
  source: Record<string, unknown> | null | undefined,
  keys: string[],
): unknown {
  if (!source) return null;
  for (const key of keys) {
    const value = source[key];
    if (hasValue(value)) return value;
  }
  return null;
}

export function getTokenCounts(source: Record<string, unknown> | null | undefined): TokenCounts {
  return readTokenCounts(source);
}

export function findCost(source: Record<string, unknown> | null | undefined): number | null {
  const attributes = getAttributes(source);
  const keys = ['cost', 'cost_usd', 'total_cost', 'totalCost', 'estimated_cost'];
  const value = pickValue(source ?? {}, keys) ?? pickValue(attributes, keys);
  return toFiniteNumber(value);
}

function kindOf(span: SpanNode): string {
  const attributes = getAttributes(span);
  const value =
    pickValue(span, ['span_type', 'type', 'kind']) ??
    pickValue(attributes, ['span_type', 'type', 'kind']);
  return String(value ?? '').toLowerCase();
}

const LLM_SPAN_TYPES = [
  'llm',
  'generation',
  'chat',
  'completion',
  'text_completion',
  'text_generation',
  'model_call',
  'openai',
  'anthropic',
  'bedrock',
  'gemini',
];

const NON_LLM_SPAN_TYPES = new Set(['tool', 'retriever']);

const LLM_NAME_MARKERS = [
  'chat',
  'completion',
  'llm',
  'openai',
  'anthropic',
  'gemini',
  'invoke_model',
  'generate',
];

export function isLlmSpan(span: SpanNode): boolean {
  const kind = kindOf(span);
  if (NON_LLM_SPAN_TYPES.has(kind)) return false;
  if (LLM_SPAN_TYPES.some((type) => kind === type || kind.includes(type))) return true;
  const name = String(span.name ?? '').toLowerCase();
  if (LLM_NAME_MARKERS.some((marker) => name.includes(marker))) return true;
  const metadataValue = (span as Record<string, unknown>).metadata;
  const metadata =
    metadataValue && typeof metadataValue === 'object' && !Array.isArray(metadataValue)
      ? (metadataValue as Record<string, unknown>)
      : {};
  for (const container of [getAttributes(span), metadata]) {
    for (const [key, value] of Object.entries(container)) {
      const keyText = key.trim().toLowerCase();
      if (keyText.startsWith('gen_ai.')) return true;
      if (
        keyText === 'llm.provider' ||
        keyText === 'llm.model_name' ||
        keyText === 'llm.request.model' ||
        keyText === 'llm.response.model'
      ) {
        return true;
      }
      if (
        (keyText === 'model' || keyText === 'model_name') &&
        typeof value === 'string' &&
        value.trim() &&
        (kind === '' || kind === 'internal' || kind === 'unknown' || hasValue(span.input) || hasValue(span.output))
      ) {
        return true;
      }
    }
  }
  return false;
}

export function isToolSpan(span: SpanNode): boolean {
  const kind = kindOf(span);
  return kind === 'tool' || kind.includes('tool');
}

export function isRetrieverSpan(span: SpanNode): boolean {
  const kind = kindOf(span);
  return kind === 'retriever' || kind.includes('retriev');
}

export function isExceptionEvent(event: TraceEvent): boolean {
  return /exception|error/i.test(String(event.name ?? ''));
}

export function CopyButton({
  value,
  label = 'Copy',
  className = '',
}: {
  value: string;
  label?: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  const handle = async () => {
    const ok = await copyToClipboard(value);
    setCopied(ok);
    window.setTimeout(() => setCopied(false), 1500);
  };
  return (
    <button
      type="button"
      className={`button button-small button-ghost ${className}`.trim()}
      onClick={() => void handle()}
    >
      {copied ? 'Copied' : label}
    </button>
  );
}

export function ValueBlock({
  value,
  copyLabel,
  empty,
}: {
  value: unknown;
  copyLabel?: string;
  empty?: string;
}) {
  if (!hasValue(value)) return <div className="ins-empty">{empty ?? 'Not recorded.'}</div>;
  const text = toText(value);
  return (
    <div className="ins-stack">
      <pre className="json-view">{text}</pre>
      {copyLabel && <CopyButton value={text} label={copyLabel} />}
    </div>
  );
}

export function KeyValueTable({ rows }: { rows: Array<[string, unknown]> }) {
  if (rows.length === 0) return <div className="ins-empty">No values recorded.</div>;
  return (
    <table className="kv-table">
      <thead>
        <tr>
          <th>Key</th>
          <th>Value</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([key, value]) => (
          <tr key={key}>
            <td>{key}</td>
            <td>{toText(value)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function EventsList({ events }: { events: TraceEvent[] }) {
  if (events.length === 0) return <div className="ins-empty">No events recorded.</div>;
  return (
    <ul className="ins-event-list">
      {events.map((event, index) => {
        const attributes = getAttributes(event);
        const exception =
          pickValue(event, ['exception', 'error', 'message']) ??
          pickValue(attributes, ['exception.message', 'exception_message', 'error.message']);
        const stack =
          pickValue(event, ['stack', 'stacktrace', 'stack_trace']) ??
          pickValue(attributes, ['exception.stacktrace', 'exception.stack', 'error.stack', 'stack_trace']);
        return (
          <li key={`${String(event.name ?? 'event')}-${index}`}>
            <div className="ins-event-head">
              <strong>{String(event.name ?? 'event')}</strong>
              <span className="mono ins-event-time">
                {formatDateTime(event.timestamp ?? event.time)}
              </span>
            </div>
            {hasValue(exception) && <pre className="json-view">{toText(exception)}</pre>}
            {hasValue(stack) && <pre className="json-view">{toText(stack)}</pre>}
            {Object.keys(attributes).length > 0 && (
              <pre className="json-view">{toText(attributes)}</pre>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <section className="ins-subsection">
      <h4>{label}</h4>
      {children}
    </section>
  );
}

function JsonView({ value, empty }: { value: unknown; empty?: string }) {
  if (!hasValue(value)) return <div className="ins-empty">{empty ?? '—'}</div>;
  return <pre className="json-view">{toText(value)}</pre>;
}

interface SpanInspectorProps {
  span: SpanNode | null;
  replayEntry?: ReplayLlmSpan | null;
  spec?: ReplaySpec | null;
  traceId?: string;
  onClose?: () => void;
  onReplay?: (entry: ReplayLlmSpan) => void;
}

function categoryOf(span: SpanNode): 'agent' | 'chain' | 'llm' | 'tool' | 'other' {
  if (isToolSpan(span)) return 'tool';
  if (isLlmSpan(span)) return 'llm';
  const kind = String(span.span_type ?? span.type ?? '').toLowerCase();
  const name = String(span.name ?? '').toLowerCase();
  if (kind.includes('agent') || name.includes('agent')) return 'agent';
  if (kind.includes('chain') || kind.includes('graph')) return 'chain';
  return 'other';
}

function StatusPill({ status }: { status?: string | null }) {
  const tone = statusClass(status) === 'status-ok' ? 'ok' : statusClass(status) === 'status-error' ? 'error' : 'neutral';
  return (
    <span className={`td2-status-pill td2-status-pill--${tone}`}>
      <span className="td2-status-dot" aria-hidden="true" />
      {status || 'unknown'}
    </span>
  );
}

function SummaryItem({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="si2-item">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function ToolBlock({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="si2-tool-block">
      <span>{label}</span>
      {hasValue(value) ? <pre className="json-view">{toText(value)}</pre> : <div className="ins-empty">Not recorded.</div>}
    </div>
  );
}

export default function SpanInspector({
  span,
  replayEntry,
  spec,
  traceId,
  onClose,
  onReplay,
}: SpanInspectorProps) {
  const [tab, setTab] = useState<PanelTab>('summary');
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setTab('summary');
  }, [span?.span_id]);

  if (!span) {
    return <div className="wf2-empty">Select a span in the waterfall to inspect its details.</div>;
  }

  const record = span as Record<string, unknown>;
  const spanId = span.span_id ? String(span.span_id) : '';
  learnTokenHistory([span]);
  const tokens = resolveTokens(record, spanId || undefined);
  const attributes = getAttributes(record);
  const events = Array.isArray(span.events) ? (span.events as TraceEvent[]) : [];
  const duration = spanDuration(span);
  const model =
    pickValue(record, ['model', 'model_name']) ??
    pickValue(attributes, ['model', 'model_name']) ??
    replayEntry?.model ??
    null;
  const llm = isLlmSpan(span);
  const tool = isToolSpan(span);
  const retriever = isRetrieverSpan(span);
  const category = categoryOf(span);
  const errorType =
    pickValue(record, ['error_type']) ?? pickValue(attributes, ['error_type', 'exception.type']);
  const errorDetail =
    pickValue(record, ['error', 'error.message', 'error_message', 'exception', 'exception.message']) ??
    errorType ??
    pickValue(attributes, ['error', 'error.message', 'exception', 'exception.message']);
  const parentName = pickValue(record, ['__tracekitParentName']);
  const parentId = pickValue(record, ['parent_span_id']);
  const parentLabel = hasValue(parentName)
    ? `child of ${String(parentName)}`
    : hasValue(parentId)
      ? `child of ${String(parentId)}`
      : 'root';
  const traceStart = toFiniteNumber(pickValue(record, ['__tracekitTraceStart']));
  const traceDuration = toFiniteNumber(pickValue(record, ['__tracekitTraceDuration']));
  const spanStart = toMillis(span.start_time);
  const offset = spanStart !== null && traceStart !== null ? Math.max(0, spanStart - traceStart) : null;
  const percent = traceDuration !== null && traceDuration > 0 ? (duration / traceDuration) * 100 : null;
  const toolName = pickValue(attributes, ['tool_name', 'tool', 'name']) ?? span.name ?? null;
  const toolArgs =
    pickValue(record, ['input', 'arguments', 'args']) ??
    pickValue(attributes, ['arguments', 'args', 'tool_args']);
  const toolOutput =
    pickValue(record, ['output', 'result']) ?? pickValue(attributes, ['output', 'result']);
  const spanInput =
    pickValue(record, ['input', 'prompt', 'messages', 'arguments', 'args', 'query']) ??
    pickValue(attributes, ['arguments', 'args', 'query']) ??
    replayEntry?.input;
  const output =
    pickValue(record, ['output', 'response', 'result']) ??
    toolOutput ??
    pickValue(attributes, ['output', 'result']) ??
    replayEntry?.output;
  const query = pickValue(record, ['input', 'query']) ?? pickValue(attributes, ['query']);
  const documents =
    pickValue(attributes, ['documents', 'results', 'retrieved_documents']) ??
    pickValue(record, ['documents', 'results']) ??
    pickValue(record, ['output', 'response', 'result']);
  const scores = pickValue(attributes, ['scores', 'similarity_scores']) ?? pickValue(record, ['scores']);
  const stackFields = Object.entries(attributes).filter(([key]) => /stack|exception/i.test(key));
  const canInlineReplay = Boolean(spec && traceId && replayEntry);

  const copy = async () => {
    const ok = await copyToClipboard(spanId);
    setCopied(ok);
    window.setTimeout(() => setCopied(false), 1500);
  };

  const meta: Array<[string, ReactNode]> = [
    ['span_id', <span className="mono">{spanId || '—'}</span>],
    ['parent_span_id', <span className="mono">{String(pickValue(record, ['parent_span_id']) ?? '—')}</span>],
    ['type', <span className="mono">{span.span_type || 'unknown'}</span>],
    ['status', <StatusPill status={span.status} />],
    ['duration', <span className={`mono ${latencyClass(duration)}`}>{formatDuration(duration)}</span>],
    ['model', <span className="mono">{hasValue(model) ? String(model) : '—'}</span>],
    ['input_tokens', <TokenValue value={tokens.input} estimated={tokens.inputEstimated} kind="input" />],
    ['output_tokens', <TokenValue value={tokens.output} estimated={tokens.outputEstimated} kind="output" />],
    ['total_tokens', <TokenValue value={tokens.total} estimated={tokens.totalEstimated} kind="total" />],
    ['cost', <CostValue value={findCost(record)} estimated={tokens.estimated} />],
    ['start', <span className="mono">{formatDateTime(span.start_time)}</span>],
    ['end', <span className="mono">{formatDateTime(span.end_time)}</span>],
  ];

  const renderBody = () => {
    if (tab === 'summary') {
      return (
        <div className="si2-stack">
          <section className="si2-section">
            <h4>Span</h4>
            <dl className="si2-grid">
              <SummaryItem label="Type"><span className="mono">{span.span_type || 'unknown'}</span></SummaryItem>
              <SummaryItem label="Parent">{parentLabel}</SummaryItem>
              <SummaryItem label="Parent span ID"><span className="mono">{hasValue(parentId) ? String(parentId) : '—'}</span></SummaryItem>
              <SummaryItem label="Duration"><span className={`mono ${latencyClass(duration)}`}>{formatDuration(duration)}</span></SummaryItem>
              <SummaryItem label="Start offset"><span className="mono">{offset === null ? '—' : formatDuration(offset)}</span></SummaryItem>
              <SummaryItem label="% of trace"><span className="mono">{percent === null ? '—' : `${percent.toFixed(1)}%`}</span></SummaryItem>
              <SummaryItem label="Model"><span className="mono">{hasValue(model) ? String(model) : '—'}</span></SummaryItem>
              <SummaryItem label="Tokens in"><TokenValue value={tokens.input} estimated={tokens.inputEstimated} kind="input" /></SummaryItem>
              <SummaryItem label="Tokens out"><TokenValue value={tokens.output} estimated={tokens.outputEstimated} kind="output" /></SummaryItem>
              <SummaryItem label="Status"><StatusPill status={span.status} /></SummaryItem>
              <SummaryItem label="Span ID"><span className="mono">{spanId || '—'}</span></SummaryItem>
            </dl>
          </section>
          <section className="si2-section">
            <h4>Input</h4>
            {hasValue(spanInput) ? (
              <pre className="json-view si2-output">{toText(spanInput)}</pre>
            ) : (
              <div className="ins-empty">No input recorded for this span.</div>
            )}
          </section>
          <section className="si2-section">
            <h4>Output</h4>
            {hasValue(output) ? (
              <pre className="json-view si2-output">{toText(output)}</pre>
            ) : (
              <div className="ins-empty">No output recorded for this span.</div>
            )}
          </section>
          <section className="si2-section">
            <h4>Error</h4>
            {hasValue(errorDetail) ? (
              <pre className="json-view si2-output si2-error">{toText(errorDetail)}</pre>
            ) : (
              <div className="ins-empty">No error recorded for this span.</div>
            )}
          </section>
        </div>
      );
    }

    if (tab === 'input') {
      return (
        <div className="si2-stack">
          {llm && <Field label="Messages or prompt"><JsonView value={pickValue(record, ['input', 'prompt', 'messages']) ?? replayEntry?.input} empty="No input recorded." /></Field>}
          {llm && <Field label="Generation parameters"><JsonView value={pickValue(record, ['parameters', 'generation_params']) ?? pickValue(attributes, ['parameters', 'generation_params']) ?? replayEntry?.parameters} empty={replayEntry?.parameters_available === false ? 'Parameters were not recorded for the original call.' : 'No parameters recorded.'} /></Field>}
          {tool && <Field label="Tool arguments"><JsonView value={toolArgs} empty="No arguments recorded." /></Field>}
          {retriever && <Field label="Query"><JsonView value={query} empty="No query recorded." /></Field>}
          {!llm && !tool && !retriever && <Field label="Input"><JsonView value={pickValue(record, ['input', 'prompt'])} empty="No input recorded." /></Field>}
        </div>
      );
    }

    if (tab === 'tools') {
      if (!tool && !retriever) {
        return <div className="ins-empty">This span is not a tool call.</div>;
      }
      return (
        <div className="si2-stack">
          <div className="si2-tool-head">
            <span className={`wf2-dot ${category}`} aria-hidden="true" />
            <strong>{hasValue(toolName) ? String(toolName) : 'Tool call'}</strong>
            <span className="mono">{hasValue(model) ? String(model) : span.span_type || 'tool'}</span>
          </div>
          <div className="si2-tool-grid">
            <ToolBlock label={retriever ? 'Query' : 'Arguments'} value={retriever ? query : toolArgs} />
            <ToolBlock label={retriever ? 'Documents' : 'Result'} value={retriever ? documents : toolOutput} />
          </div>
          {retriever && hasValue(scores) && (
            <details className="si2-details">
              <summary>Retrieval scores</summary>
              <pre className="json-view">{toText(scores)}</pre>
            </details>
          )}
          <details className="si2-details">
            <summary>Tool attributes</summary>
            <pre className="json-view">{Object.keys(attributes).length > 0 ? toText(attributes) : 'No attributes recorded.'}</pre>
          </details>
        </div>
      );
    }

    if (tab === 'events') {
      return (
        <div className="si2-stack">
          <Field label="Events"><EventsList events={events} /></Field>
          {stackFields.map(([key, value]) => (
            <Field key={key} label={key}><pre className="json-view">{toText(value)}</pre></Field>
          ))}
        </div>
      );
    }

    if (tab === 'attributes') {
      return <KeyValueTable rows={Object.entries(attributes)} />;
    }

    if (tab === 'replay') {
      if (canInlineReplay && spec && traceId && replayEntry) {
        return <ReplayPanel inline traceId={traceId} entry={replayEntry} spec={spec} onClose={() => undefined} />;
      }
      if (onReplay && replayEntry) {
        return (
          <div className="si2-stack">
            <div className="ins-empty">This LLM span can be replayed.</div>
            <div className="replay-actions">
              <button type="button" className="button button-small" onClick={() => onReplay(replayEntry)}>
                Open replay
              </button>
            </div>
          </div>
        );
      }
      return <div className="ins-empty">No replay available for this span.</div>;
    }

    return (
      <div className="si2-stack">
        <KeyValueTable rows={meta} />
        <div className="table-actions">
          <button type="button" className="copy-text" onClick={() => void copy()}>
            {copied ? 'copied' : 'copy json'}
          </button>
        </div>
        <pre className="json-view">{toText(span)}</pre>
      </div>
    );
  };

  return (
    <>
      <div className="si2-head">
        <div className="si2-title-row">
          <span className={`wf2-dot ${category}`} aria-hidden="true" />
          <strong title={span.name || spanId || 'span'}>{span.name || spanId || 'span'}</strong>
        </div>
        <div className="si2-head-meta">
          <span className="mono">{span.span_type || 'unknown'}</span>
          <StatusPill status={span.status} />
          <span className="mono">{formatDuration(duration)}</span>
        </div>
        <div className="si2-actions">
          <button type="button" className="td2-copy" onClick={() => void copy()}>
            {copied ? 'Copied' : 'Copy ID'}
          </button>
          {onClose && (
            <button type="button" className="td2-btn td2-btn--ghost" onClick={onClose} aria-label="Close span panel">
              Close
            </button>
          )}
        </div>
      </div>
      <div className="tabs span-panel-tabs td2-tabs si2-tabs" role="tablist" aria-label="Span details">
        {PANEL_TABS.map((item) => (
          <button
            type="button"
            key={item.id}
            role="tab"
            aria-selected={tab === item.id}
            className={tab === item.id ? 'td2-tab active' : 'td2-tab'}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>
      <div className="span-panel-body si2-body">{renderBody()}</div>
    </>
  );
}
