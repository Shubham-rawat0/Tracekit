import type { ReactNode } from 'react';
import { formatCost, toFiniteNumber } from '../utils/format';
import {
  COST_ESTIMATE_TOOLTIP,
  ESTIMATE_TOOLTIP,
  OUTPUT_ESTIMATE_TOOLTIP,
  type ResolvedTokens,
} from '../utils/tokenEstimation';

interface TokenValueProps {
  value: number | null;
  estimated?: boolean;
  kind?: 'input' | 'output' | 'total';
  label?: string;
  strong?: boolean;
}

function displayCount(value: number | null): string {
  const number = toFiniteNumber(value);
  if (number === null) return '—';
  return Math.round(number).toLocaleString();
}

function estimateTooltip(kind: TokenValueProps['kind'], explicit?: string): string {
  if (explicit) return explicit;
  return kind === 'output' || kind === 'total' ? OUTPUT_ESTIMATE_TOOLTIP : ESTIMATE_TOOLTIP;
}

export function TokenValue({
  value,
  estimated = false,
  kind = 'total',
  label,
  strong = false,
}: TokenValueProps): ReactNode {
  const number = toFiniteNumber(value);
  return (
    <span
      className={`tk-token-value${estimated ? ' estimated' : ''}${strong ? ' strong' : ''}`}
      title={estimated && number !== null ? estimateTooltip(kind) : undefined}
    >
      {label && <span className="tk-token-label">{label}</span>}
        <span className="tk-token-number">{displayCount(number)}</span>
    </span>
  );
}

interface TokenGroupProps {
  tokens: ResolvedTokens;
  compact?: boolean;
  stacked?: boolean;
}

export function TokenGroup({ tokens, compact = false, stacked = false }: TokenGroupProps): ReactNode {
  return (
    <span className={`tk-token-group${compact ? ' compact' : ''}${stacked ? ' stacked' : ''}`}>
      <TokenValue
        value={tokens.input}
        estimated={tokens.inputEstimated}
        kind="input"
        label={compact ? undefined : 'in'}
      />
      {!stacked && <span className="tk-token-sep" aria-hidden="true">·</span>}
      <TokenValue
        value={tokens.output}
        estimated={tokens.outputEstimated}
        kind="output"
        label={compact ? undefined : 'out'}
      />
      {!stacked && <span className="tk-token-sep" aria-hidden="true">·</span>}
      <TokenValue
        value={tokens.total}
        estimated={tokens.totalEstimated}
        kind="total"
        label={compact ? undefined : 'total'}
        strong
      />
      {compact && <span className="tk-token-unit">tokens</span>}
    </span>
  );
}

interface CostValueProps {
  value: number | null;
  estimated?: boolean;
}

export function CostValue({ value, estimated = false }: CostValueProps): ReactNode {
  const number = toFiniteNumber(value);
  return (
    <span
      className={`tk-token-value tk-cost-value${estimated ? ' estimated' : ''}`}
      title={estimated && number !== null ? COST_ESTIMATE_TOOLTIP : undefined}
    >
      <span className="tk-token-number">
        {number === null ? '—' : formatCost(number)}
      </span>
    </span>
  );
}
