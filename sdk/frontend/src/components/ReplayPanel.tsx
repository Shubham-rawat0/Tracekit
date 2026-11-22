import { useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { ApiError, executeReplay, getReplayProviders } from '../api';
import { CopyButton, findCost, hasValue, toText } from '../components/SpanInspector';
import type { ReplayLlmSpan, ReplaySpec } from '../components/SpanInspector';
import { CostValue, TokenValue } from '../components/TokenValue';
import type { ReplayPayload, ReplayResult } from '../types';
import { formatDuration, statusClass, toFiniteNumber } from '../utils/format';
import { learnTokenHistory, resolveTokens } from '../utils/tokenEstimation';

type Stage = 'edit' | 'confirm' | 'result';

interface DiffLine {
  text: string;
  kind: 'same' | 'added' | 'removed';
}

function diffLines(before: string, after: string): { left: DiffLine[]; right: DiffLine[] } {
  const leftText = before.split('\n');
  const rightText = after.split('\n');
  const rows = leftText.length;
  const columns = rightText.length;
  if (rows * columns > 250000) {
    const rightSet = new Set(rightText);
    const leftSet = new Set(leftText);
    return {
      left: leftText.map((text) => ({ text, kind: rightSet.has(text) ? 'same' : 'removed' })),
      right: rightText.map((text) => ({ text, kind: leftSet.has(text) ? 'same' : 'added' })),
    };
  }
  const table: number[][] = Array.from({ length: rows + 1 }, () => new Array<number>(columns + 1).fill(0));
  for (let i = rows - 1; i >= 0; i -= 1) {
    for (let j = columns - 1; j >= 0; j -= 1) {
      table[i][j] =
        leftText[i] === rightText[j]
          ? table[i + 1][j + 1] + 1
          : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const left: DiffLine[] = [];
  const right: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < rows && j < columns) {
    if (leftText[i] === rightText[j]) {
      left.push({ text: leftText[i], kind: 'same' });
      right.push({ text: rightText[j], kind: 'same' });
      i += 1;
      j += 1;
    } else if (table[i + 1][j] >= table[i][j + 1]) {
      left.push({ text: leftText[i], kind: 'removed' });
      i += 1;
    } else {
      right.push({ text: rightText[j], kind: 'added' });
      j += 1;
    }
  }
  while (i < rows) {
    left.push({ text: leftText[i], kind: 'removed' });
    i += 1;
  }
  while (j < columns) {
    right.push({ text: rightText[j], kind: 'added' });
    j += 1;
  }
  return { left, right };
}

function DiffView({ original, replay }: { original: unknown; replay: unknown }) {
  const originalText = toText(original);
  const replayText = toText(replay);
  const diff = useMemo(() => diffLines(originalText, replayText), [originalText, replayText]);
  const column = (lines: DiffLine[]) => (
    <pre className="diff-lines">
      {lines.map((line, index) => (
        <span className={`diff-line ${line.kind}`} key={index}>
          {line.text || ' '}
        </span>
      ))}
    </pre>
  );
  return (
    <div className="diff-view">
      <div className="diff-pane">
        <h5>Original</h5>
        {column(diff.left)}
      </div>
      <div className="diff-pane">
        <h5>Replay</h5>
        {column(diff.right)}
      </div>
    </div>
  );
}

function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return 'Replay failed.';
}

function parseParameters(text: string): { value: Record<string, unknown>; error: string | null } {
  const trimmed = text.trim();
  if (trimmed === '') return { value: {}, error: null };
  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { value: {}, error: 'Parameters must be a JSON object.' };
    }
    return { value: parsed as Record<string, unknown>, error: null };
  } catch {
    return { value: {}, error: 'Parameters must be valid JSON.' };
  }
}

function temperatureOf(parameters: Record<string, unknown>): string {
  const value = parameters['temperature'];
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  if (typeof value === 'string' && value.trim()) return value;
  return '—';
}

interface ReplayPanelProps {
  traceId: string;
  entry: ReplayLlmSpan;
  spec: ReplaySpec;
  onClose: () => void;
  inline?: boolean;
}

export default function ReplayPanel({ traceId, entry, spec, onClose, inline = false }: ReplayPanelProps) {
  const [stage, setStage] = useState<Stage>('edit');
  const [prompt, setPrompt] = useState(() => (hasValue(entry.input) ? toText(entry.input) : ''));
  const [parametersText, setParametersText] = useState(() => {
    if (entry.parameters_available === false || !hasValue(entry.parameters)) return '{}';
    return toText(entry.parameters);
  });
  const [parameters, setParameters] = useState<Record<string, unknown>>({});
  const [model, setModel] = useState(() => (typeof entry.model === 'string' ? entry.model : ''));
  const [provider, setProvider] = useState(() => (typeof entry.provider === 'string' ? entry.provider : ''));
  const [providers, setProviders] = useState<string[]>([]);
  const [providersLoading, setProvidersLoading] = useState(true);
  const [providersError, setProvidersError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ReplayResult | null>(null);

  useEffect(() => {
    let active = true;
    setProvidersLoading(true);
    getReplayProviders()
      .then((data) => {
        if (!active) return;
        setProviders(Array.isArray(data?.providers) ? data.providers : []);
      })
      .catch((err: unknown) => {
        if (!active) return;
        setProvidersError(describeError(err));
      })
      .finally(() => {
        if (active) setProvidersLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  const warning =
    spec.warning ??
    'Replay re-executes a recorded request. It does not deterministically reproduce the original execution context.';
  const recordedProvider = typeof entry.provider === 'string' ? entry.provider : '';
  const providerOptions = Array.from(new Set([...providers, ...(recordedProvider ? [recordedProvider] : [])]));

  const prepare = () => {
    const parsed = parseParameters(parametersText);
    if (parsed.error) {
      setError(parsed.error);
      return;
    }
    setParameters(parsed.value);
    setError(null);
    setStage('confirm');
  };

  const execute = async (payloadParameters: Record<string, unknown>) => {
    setRunning(true);
    setError(null);
    try {
      const payload: ReplayPayload = {
        trace_id: traceId,
        span_id: typeof entry.span_id === 'string' ? entry.span_id : undefined,
        prompt,
        parameters: payloadParameters,
        model: model.trim() || undefined,
        provider: provider.trim() || undefined,
      };
      const execution = await executeReplay(payload);
      setResult(execution);
      setStage('result');
    } catch (err: unknown) {
      setError(describeError(err));
    } finally {
      setRunning(false);
    }
  };

  const run = async () => {
    await execute(parameters);
  };

  const runInline = async () => {
    const parsed = parseParameters(parametersText);
    if (parsed.error) {
      setError(parsed.error);
      return;
    }
    setParameters(parsed.value);
    await execute(parsed.value);
  };

  const original = result?.comparison?.original ?? null;
  const replay = result?.comparison?.replay ?? null;
  learnTokenHistory([original, replay]);
  const originalTokens = resolveTokens(original, original?.span_id ?? traceId);
  const replayTokens = resolveTokens(replay, replay?.span_id ?? traceId);
  const compact = (value: unknown) => (
    <span className="mono" title={toText(value)}>
      {hasValue(value) ? toText(value) : '—'}
    </span>
  );
  const comparisonRows: Array<[string, ReactNode, ReactNode]> = [
    ['Input', compact(original?.input), compact(replay?.input)],
    ['Output', compact(original?.output), compact(replay?.output)],
    ['Model', compact(original?.model), compact(replay?.model)],
    ['Provider', compact(original?.provider), compact(replay?.provider)],
    ['Parameters', compact(original?.parameters), compact(replay?.parameters)],
    ['Latency', formatDuration(toFiniteNumber(original?.latency_ms)), formatDuration(toFiniteNumber(replay?.latency_ms))],
    [
      'Input tokens',
      <TokenValue value={originalTokens.input} estimated={originalTokens.inputEstimated} kind="input" />,
      <TokenValue value={replayTokens.input} estimated={replayTokens.inputEstimated} kind="input" />,
    ],
    [
      'Output tokens',
      <TokenValue value={originalTokens.output} estimated={originalTokens.outputEstimated} kind="output" />,
      <TokenValue value={replayTokens.output} estimated={replayTokens.outputEstimated} kind="output" />,
    ],
    [
      'Total tokens',
      <TokenValue value={originalTokens.total} estimated={originalTokens.totalEstimated} kind="total" />,
      <TokenValue value={replayTokens.total} estimated={replayTokens.totalEstimated} kind="total" />,
    ],
    [
      'Cost',
      <CostValue value={findCost(original)} estimated={originalTokens.estimated} />,
      <CostValue value={findCost(replay)} estimated={replayTokens.estimated} />,
    ],
    [
      'Status',
      <span className={`status-tag ${statusClass(original?.status)}`}>{original?.status ?? 'unknown'}</span>,
      <span className={`status-tag ${statusClass(replay?.status)}`}>{replay?.status ?? 'unknown'}</span>,
    ],
    ['Error', compact(original?.error), compact(replay?.error)],
  ];

  const inlineParameters = parseParameters(parametersText).value;
  const recordedParameters =
    entry.parameters && typeof entry.parameters === 'object' && !Array.isArray(entry.parameters)
      ? entry.parameters
      : {};
  const inlineTemperature = temperatureOf(
    Object.keys(inlineParameters).length > 0 ? inlineParameters : recordedParameters,
  );
  const inlineModel =
    model.trim() || (typeof entry.model === 'string' && entry.model.trim() ? entry.model : '—');

  const inlineContent = (
    <div className="replay-panel td2-replay-inline">
      <div className="td2-replay-head">
        <div>
          <h3>Replay LLM request</h3>
          <span className="mono td2-replay-id">{entry.span_id ?? traceId}</span>
        </div>
        <button
          type="button"
          className="td2-btn td2-btn--primary"
          onClick={() => void runInline()}
          disabled={running}
        >
          {running ? 'Running…' : 'Run replay'}
        </button>
      </div>

      <div className="td2-warning" role="note">
        <strong>Replay is not deterministic.</strong> {warning}
      </div>

      {error && (
        <div className="banner banner-error td2-error" role="alert">
          <span>{error}</span>
        </div>
      )}

      {stage === 'result' && result ? (
        <div className="td2-replay-result">
          <span className={`td2-status-pill td2-status-pill--${
            statusClass(result.status) === 'status-ok'
              ? 'ok'
              : statusClass(result.status) === 'status-error'
                ? 'error'
                : 'neutral'
          }`}>
            <span className="td2-status-dot" aria-hidden="true" />
            {result.status ?? 'unknown'}
          </span>
          {hasValue(result.trace_id) && (
            <a className="td2-link mono" href={`#/traces/${encodeURIComponent(String(result.trace_id))}`}>
              Open replay trace
            </a>
          )}
          {hasValue(result.error) && <div className="td2-error">{String(result.error)}</div>}
          <pre className="td2-code-block">{toText(result)}</pre>
        </div>
      ) : (
        <>
          <section className="td2-replay-section">
            <h4>Prompt</h4>
            <pre className="td2-code-block">{prompt || '—'}</pre>
          </section>
          <div className="td2-replay-fields">
            <div className="td2-replay-field">
              <span>Model</span>
              <span className="mono">{inlineModel}</span>
            </div>
            <div className="td2-replay-field">
              <span>Temperature</span>
              <span className="mono">{inlineTemperature}</span>
            </div>
          </div>
          <details className="td2-replay-advanced">
            <summary>Edit request</summary>
            <label className="field">
              <span>Prompt</span>
              <textarea className="replay-textarea" rows={6} value={prompt} onChange={(event) => setPrompt(event.target.value)} />
            </label>
            <label className="field">
              <span>Parameters (JSON object)</span>
              <textarea className="replay-textarea" rows={4} value={parametersText} onChange={(event) => setParametersText(event.target.value)} />
            </label>
            <div className="replay-grid">
              <label className="replay-field">
                <span>Model</span>
                <input type="text" className="replay-input" value={model} placeholder="use recorded model" onChange={(event) => setModel(event.target.value)} />
              </label>
              <label className="replay-field">
                <span>Provider</span>
                <select className="replay-select" value={provider} onChange={(event) => setProvider(event.target.value)}>
                  <option value="">server default</option>
                  {providerOptions.map((item) => (
                    <option key={item} value={item}>{item}</option>
                  ))}
                </select>
              </label>
            </div>
          </details>
        </>
      )}
    </div>
  );

  if (inline) return inlineContent;

  const content = (
    <>
      <div className="section-head">
        <div className="section-label">
          Replay LLM request
          <span className="section-subtitle mono">{entry.span_id ?? traceId}</span>
        </div>
        {!inline && (
          <div className="table-actions">
            <button type="button" className="button button-small" onClick={onClose}>
              Close
            </button>
          </div>
        )}
      </div>

      <div className="ins-warning" role="note">
        <strong>Replay is not deterministic.</strong> {warning} Tool calls with external side effects are never replayed.
      </div>

      {error && (
        <div className="banner banner-error" role="alert">
          <span>
            {error}
            {/handler|provider/i.test(error) && ' Check that a replay handler is registered for the selected provider.'}
          </span>
        </div>
      )}

      {stage === 'edit' && (
        <>
          <label className="field">
            <span>Prompt</span>
            <textarea className="replay-textarea" rows={8} value={prompt} onChange={(event) => setPrompt(event.target.value)} />
          </label>
          {entry.parameters_available === false && (
            <div className="replay-status">Parameters were not recorded for the original call.</div>
          )}
          <label className="field">
            <span>Parameters (JSON object)</span>
            <textarea className="replay-textarea" rows={5} value={parametersText} onChange={(event) => setParametersText(event.target.value)} />
          </label>
          <div className="replay-grid">
            <label className="replay-field">
              <span>Model</span>
              <input type="text" className="replay-input" value={model} placeholder="use recorded model" onChange={(event) => setModel(event.target.value)} />
            </label>
            <label className="replay-field">
              <span>Provider</span>
              <select className="replay-select" value={provider} onChange={(event) => setProvider(event.target.value)}>
                <option value="">server default</option>
                {providerOptions.map((item) => (
                  <option key={item} value={item}>
                    {item}
                    {item === recordedProvider && !providers.includes(item) ? ' (recorded, handler not listed)' : ''}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="replay-status">
            {providersLoading ? 'Loading registered providers…' : providersError ? `Providers unavailable: ${providersError}` : `${providers.length} provider handler(s) registered`}
          </div>
          <div className="replay-actions">
            {!inline && (
              <button type="button" className="button button-small" onClick={onClose}>
                Cancel
              </button>
            )}
            <button type="button" className="button button-small" onClick={prepare}>
              Review replay
            </button>
          </div>
        </>
      )}

      {stage === 'confirm' && (
        <>
          <div className="ins-stack">
            <div className="ins-empty">Confirm the request below. Nothing is executed until you choose Run replay.</div>
            <table className="kv-table">
              <tbody>
                <tr><td>trace_id</td><td className="mono">{traceId}</td></tr>
                <tr><td>span_id</td><td className="mono">{entry.span_id ?? '—'}</td></tr>
                <tr><td>model</td><td className="mono">{model.trim() || 'server default'}</td></tr>
                <tr><td>provider</td><td className="mono">{provider.trim() || 'server default'}</td></tr>
              </tbody>
            </table>
            <section className="ins-subsection">
              <h4>Prompt</h4>
              <pre className="json-view">{prompt || ''}</pre>
            </section>
            <section className="ins-subsection">
              <h4>Parameters</h4>
              <pre className="json-view">{toText(parameters)}</pre>
            </section>
          </div>
          <div className="replay-actions">
            <button type="button" className="button button-small" onClick={() => setStage('edit')} disabled={running}>
              Back to edit
            </button>
            <button type="button" className="button button-small" onClick={() => void run()} disabled={running}>
              {running ? 'Running replay…' : 'Run replay'}
            </button>
          </div>
        </>
      )}

      {stage === 'result' && result && (
        <div className="replay-result">
          <div className="table-actions">
            <span className={`status-tag ${statusClass(result.status)}`}>{result.status ?? 'unknown'}</span>
            {hasValue(result.trace_id) && (
              <a className="button button-small" href={`#/traces/${encodeURIComponent(String(result.trace_id))}`}>
                Open replay trace
              </a>
            )}
          </div>
          <div className="replay-status">
            replay_of_trace_id:{' '}
            <a className="mono link" href={`#/traces/${encodeURIComponent(traceId)}`}>
              {result.replay_of_trace_id ?? traceId}
            </a>
            {' · '}replay_of_span_id: <span className="mono">{result.replay_of_span_id ?? '—'}</span>
          </div>
          {hasValue(result.error) && (
            <div className="banner banner-error" role="alert">
              <span>{String(result.error)}</span>
            </div>
          )}
          {hasValue(result.warning) && <div className="ins-warning">{String(result.warning)}</div>}
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Field</th>
                  <th>Original</th>
                  <th>Replay</th>
                </tr>
              </thead>
              <tbody>
                {comparisonRows.map(([label, before, after]) => (
                  <tr key={label}>
                    <td className="mono">{label}</td>
                    <td>{before}</td>
                    <td>{after}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <section className="ins-subsection">
            <h4>Input diff</h4>
            <DiffView original={original?.input} replay={replay?.input} />
          </section>
          <section className="ins-subsection">
            <h4>Output diff</h4>
            <DiffView original={original?.output} replay={replay?.output} />
          </section>
          <div className="replay-actions">
            <CopyButton value={toText(result)} label="Copy result JSON" />
            {!inline && (
              <button type="button" className="button button-small" onClick={onClose}>
                Done
              </button>
            )}
          </div>
        </div>
      )}
    </>
  );

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div
        className="modal replay-panel td2-replay-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Replay LLM span"
        onMouseDown={(event) => event.stopPropagation()}
      >
        {content}
      </div>
    </div>
  );
}
