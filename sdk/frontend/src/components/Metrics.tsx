import type { ReactNode } from 'react';

export interface KpiItem {
  label: string;
  value: string | number | ReactNode;
  hint?: string;
  tone?: 'default' | 'ok' | 'err' | 'warn';
  title?: string;
}

function toneClass(tone: KpiItem['tone'] = 'default'): string {
  return tone === 'default' ? '' : ` ${tone}`;
}

export function KpiStrip({ items }: { items: KpiItem[] }) {
  return (
    <div className="kpi-strip">
      {items.map((item, index) => (
        <div className={`kpi-item${toneClass(item.tone)}`} key={`${item.label}-${index}`}>
          <span className="kpi-label">{item.label}</span>
          <strong
            className="kpi-value"
            title={item.title ?? (typeof item.value === 'string' || typeof item.value === 'number' ? String(item.value) : undefined)}
          >
            {item.value}
          </strong>
          {item.hint && <span className="kpi-hint">{item.hint}</span>}
        </div>
      ))}
    </div>
  );
}

interface MetricCardProps {
  label: string;
  value: string;
  hint?: string;
  tone?: 'default' | 'good' | 'warn' | 'bad' | 'ok' | 'err';
}

export function MetricCard({ label, value, hint, tone = 'default' }: MetricCardProps) {
  const mappedTone =
    tone === 'good' ? 'ok' : tone === 'bad' ? 'err' : tone === 'ok' || tone === 'err' || tone === 'warn'
      ? tone
      : 'default';
  return (
    <div className={`kpi-item${toneClass(mappedTone)}`}>
      <span className="kpi-label">{label}</span>
      <strong className="kpi-value" title={value}>
        {value}
      </strong>
      {hint && <span className="kpi-hint">{hint}</span>}
    </div>
  );
}

export function MetricGrid({ children }: { children: ReactNode }) {
  return <div className="kpi-strip">{children}</div>;
}
