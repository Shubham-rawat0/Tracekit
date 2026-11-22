import { formatCount, formatPercent, toFiniteNumber } from '../utils/format';

interface SpanCompositionCardProps {
  llmSpans?: number | null;
  totalSpans?: number | null;
}

export default function SpanCompositionCard({ llmSpans, totalSpans }: SpanCompositionCardProps) {
  const llm = Math.max(0, toFiniteNumber(llmSpans) ?? 0);
  const reportedTotal = toFiniteNumber(totalSpans);
  const total = Math.max(0, reportedTotal ?? llm);
  const other = Math.max(0, total - llm);
  const share = total > 0 ? llm / total : 0;
  const llmWidth = total > 0 ? (llm / total) * 100 : 0;
  const otherWidth = total > 0 ? (other / total) * 100 : 0;

  return (
    <section className="insight-card span-composition" aria-label="Span composition">
      <header className="insight-card-head">
        <span className="insight-label">Span composition</span>
        <span className="insight-meta mono">{formatCount(total)} spans</span>
      </header>
      <div className="composition-summary">
        <strong className="mono">{formatPercent(share, 0)}</strong>
        <span>LLM spans</span>
      </div>
      <div
        className="composition-track"
        role="img"
        aria-label={`LLM spans ${formatCount(llm)}, other spans ${formatCount(other)}`}
      >
        <span
          className={llmWidth === 0 ? 'composition-segment llm is-empty' : 'composition-segment llm'}
          style={{ width: `${llmWidth}%` }}
          data-tooltip={`LLM spans: ${formatCount(llm)} (${formatPercent(share, 0)})`}
          title={`LLM spans: ${formatCount(llm)} (${formatPercent(share, 0)})`}
        />
        <span
          className={otherWidth === 0 ? 'composition-segment other is-empty' : 'composition-segment other'}
          style={{ width: `${otherWidth}%` }}
          data-tooltip={`Other spans: ${formatCount(other)} (${formatPercent(total > 0 ? other / total : 0, 0)})`}
          title={`Other spans: ${formatCount(other)} (${formatPercent(total > 0 ? other / total : 0, 0)})`}
        />
      </div>
      <div className="composition-legend">
        <span className="composition-legend-item">
          <span className="composition-swatch llm" aria-hidden="true" />
          <span>LLM spans</span>
          <span className="mono">{formatCount(llm)}</span>
        </span>
        <span className="composition-legend-item">
          <span className="composition-swatch other" aria-hidden="true" />
          <span>Other spans</span>
          <span className="mono">{formatCount(other)}</span>
        </span>
      </div>
      {reportedTotal === null && <p className="composition-note">Span count unavailable for this window.</p>}
    </section>
  );
}
