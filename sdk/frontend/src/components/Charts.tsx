import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { CSSProperties, KeyboardEvent, MouseEvent, ReactNode } from 'react';
import { EmptyState } from './States';
import { formatCount, toFiniteNumber } from '../utils/format';

export const CHART_COLORS = {
  primary: 'var(--chart-primary)',
  secondary: 'var(--chart-secondary)',
  tertiary: 'var(--chart-tertiary)',
  quaternary: 'var(--chart-quaternary)',
  error: 'var(--chart-error)',
} as const;

const PALETTE = [
  CHART_COLORS.primary,
  CHART_COLORS.secondary,
  CHART_COLORS.tertiary,
  CHART_COLORS.quaternary,
];

export interface ChartSeries {
  label: string;
  values: Array<number | null | undefined>;
  color?: string;
  variant?: 'default' | 'llm' | 'error';
  context?: Array<string | null | undefined>;
}

interface BaseChartProps {
  label: string;
  title?: ReactNode;
  labels: string[];
  series: ChartSeries[];
  height?: number;
  formatValue?: (value: number) => string;
  emptyLabel?: string;
  ariaLabel?: string;
  countAxis?: boolean;
}

interface HistogramProps {
  values: Array<number | null | undefined>;
  labels?: string[];
  height?: number;
  formatValue?: (value: number) => string;
  emptyLabel?: string;
  ariaLabel?: string;
  color?: string;
}

function isTimeLabel(value: string | undefined): boolean {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}(?:[T ]|$)/.test(value);
}

function labelFormatter(labels: string[]): (value: string | undefined) => string {
  const stamps = labels
    .filter(isTimeLabel)
    .map((label) => Date.parse(label))
    .filter((stamp) => Number.isFinite(stamp));
  const timeLabels = stamps.length > 0;
  const span = timeLabels ? Math.max(...stamps) - Math.min(...stamps) : 0;
  const useClock = span < 24 * 60 * 60 * 1000;
  return (value) => {
    if (!value) return '—';
    if (!timeLabels || !isTimeLabel(value)) {
      return value.length > 16 ? `${value.slice(0, 15)}…` : value;
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return useClock
      ? date.toLocaleTimeString(undefined, {
          hour: '2-digit',
          minute: '2-digit',
          hour12: false,
        })
      : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  };
}

function parseStamp(value: string | undefined): number | null {
  if (!isTimeLabel(value)) return null;
  const stamp = Date.parse(value ?? '');
  return Number.isFinite(stamp) ? stamp : null;
}

function formatWindowLabel(labels: string[], index: number): string {
  const label = labels[index] ?? '';
  const startStamp = parseStamp(label);
  if (startStamp === null) return label || '—';
  const start = new Date(startStamp);
  const nextStamp = parseStamp(labels[index + 1]);
  const prevStamp = parseStamp(labels[index - 1]);
  const endStamp = nextStamp ?? (prevStamp === null ? startStamp + 60 * 60 * 1000 : startStamp + (startStamp - prevStamp));
  const end = new Date(endStamp);
  const dateTime = { month: 'short' as const, day: 'numeric' as const, hour: '2-digit' as const, minute: '2-digit' as const, hour12: false as const };
  const startText = start.toLocaleString(undefined, dateTime);
  const sameDay = start.getFullYear() === end.getFullYear() && start.getMonth() === end.getMonth() && start.getDate() === end.getDate();
  const endText = sameDay
    ? end.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false })
    : end.toLocaleString(undefined, dateTime);
  return `${startText} – ${endText}`;
}

function niceStep(raw: number): number {
  if (!Number.isFinite(raw) || raw <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const residual = raw / magnitude;
  const factor = residual <= 1 ? 1 : residual <= 2 ? 2 : residual <= 5 ? 5 : 10;
  return factor * magnitude;
}

function buildTicks(max: number, countAxis: boolean): number[] {
  const top = Math.max(0, max);
  if (countAxis) {
    if (top <= 4) {
      const ceiling = Math.max(1, Math.ceil(top));
      return Array.from({ length: ceiling + 1 }, (_, index) => ceiling - index);
    }
    const step = niceStep(top / 4);
    return [4, 3, 2, 1, 0].map((index) => index * step);
  }
  const scale = top > 0 ? top : 1;
  return [4, 3, 2, 1, 0].map((index) => (index / 4) * scale);
}

function contiguousSegments(values: Array<number | null | undefined>): number[][] {
  const segments: number[][] = [];
  let current: number[] = [];
  values.forEach((value, index) => {
    if (toFiniteNumber(value) === null) {
      if (current.length > 0) segments.push(current);
      current = [];
      return;
    }
    current.push(index);
  });
  if (current.length > 0) segments.push(current);
  return segments;
}

function seriesNumbers(series: ChartSeries[]): number[] {
  return series.flatMap((entry) =>
    entry.values
      .map((value) => toFiniteNumber(value))
      .filter((value): value is number => value !== null)
      .map((value) => Math.max(0, value)),
  );
}

function labelIndexes(count: number): number[] {
  if (count <= 1) return count === 1 ? [0] : [];
  if (count === 2) return [0, 1];
  const middle = Math.round((count - 1) / 2);
  return Array.from(new Set([0, middle, count - 1]));
}

function barClass(variant: ChartSeries['variant']): string {
  if (variant === 'error') return ' chart-bar-error';
  if (variant === 'llm') return ' chart-bar-llm';
  return '';
}

function barPath(x: number, y: number, width: number, height: number, radius: number): string {
  const r = Math.max(0, Math.min(radius, width / 2, height));
  const bottom = y + height;
  if (r === 0) return `M${x},${bottom} V${y} H${x + width} V${bottom} Z`;
  return `M${x},${bottom} V${y + r} Q${x},${y} ${x + r},${y} H${x + width - r} Q${x + width},${y} ${x + width},${y + r} V${bottom} Z`;
}

function useElementSize<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const [size, setSize] = useState({ width: 720, height: 320 });

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const update = () => {
      const rect = element.getBoundingClientRect();
      setSize({
        width: Math.max(1, Math.round(rect.width)),
        height: Math.max(1, Math.round(rect.height)),
      });
    };
    update();
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', update);
      return () => window.removeEventListener('resize', update);
    }
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  return { ref, size };
}

interface LegendItem {
  label: string;
  color: string;
  value?: string;
}

export function ChartLegend({ items }: { items: LegendItem[] }) {
  if (items.length === 0) return null;
  return (
    <span className="chart-legend">
      {items.map((item, index) => (
        <span className="legend-item" key={`${item.label}-${index}`}>
          <span className="legend-swatch" style={{ background: item.color }} />
          {item.label}
          {item.value && <span className="mono">{item.value}</span>}
        </span>
      ))}
    </span>
  );
}

interface ChartCardProps {
  label: string;
  title?: ReactNode;
  ariaLabel?: string;
  legend: ReactNode;
  children: ReactNode;
}

export function ChartCard({ label, title, ariaLabel, legend, children }: ChartCardProps) {
  return (
    <section className="chart-card" aria-label={ariaLabel ?? label}>
      <header className="chart-card-head">
        <div className="chart-card-heading">
          <span className="chart-card-label">{label}</span>
          {title !== undefined && title !== null && title !== '' && (
            <span className="chart-card-title">{title}</span>
          )}
        </div>
        <div className="chart-card-legend">{legend}</div>
      </header>
      <div className="chart-card-body">{children}</div>
    </section>
  );
}

interface TooltipRow {
  label: string;
  value: string;
  color: string;
  context?: string | null;
}

function TooltipContent({ title, rows }: { title: string; rows: TooltipRow[] }) {
  return (
    <>
      <div className="chart-tooltip-title">{title}</div>
      {rows.map((row, index) => (
        <div className="chart-tooltip-row" key={`${row.label}-${index}`}>
          <div className="chart-tooltip-line">
            <span className="chart-tooltip-swatch" style={{ background: row.color }} />
            <span className="chart-tooltip-label">{row.label}</span>
            <span className="chart-tooltip-value">{row.value}</span>
          </div>
          {row.context && <div className="chart-tooltip-context">{row.context}</div>}
        </div>
      ))}
    </>
  );
}

interface ChartTooltipProps {
  x: number;
  y: number;
  width: number;
  height: number;
  children: ReactNode;
}

function ChartTooltip({ x, y, width, height, children }: ChartTooltipProps) {
  const ref = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const bounds = element.getBoundingClientRect();
    const pad = 8;
    let left = x + 14;
    let top = y + 14;
    if (left + bounds.width > width - pad) left = x - bounds.width - 14;
    if (left < pad) left = pad;
    if (top + bounds.height > height - pad) top = y - bounds.height - 14;
    if (top < pad) top = pad;
    element.style.left = `${left}px`;
    element.style.top = `${top}px`;
    element.style.visibility = 'visible';
  }, [x, y, width, height, children]);

  return (
    <div ref={ref} className="chart-tooltip" style={{ visibility: 'hidden' as CSSProperties['visibility'] }}>
      {children}
    </div>
  );
}

interface FrameProps extends BaseChartProps {
  variant: 'bar' | 'line';
}

function ChartFrame({
  label,
  title,
  labels,
  series,
  variant,
  formatValue = formatCount,
  emptyLabel = 'No data in this range',
  ariaLabel,
  countAxis = false,
}: FrameProps) {
  const { ref, size } = useElementSize<HTMLDivElement>();
  const { width, height } = size;
  const pad = { left: 56, right: 18, top: 28, bottom: 34 };
  const innerWidth = Math.max(1, width - pad.left - pad.right);
  const innerHeight = Math.max(1, height - pad.top - pad.bottom);
  const numericValues = seriesNumbers(series);
  const pointCount = Math.max(labels.length, ...series.map((entry) => entry.values.length), 1);
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  const [pointer, setPointer] = useState({ x: 0, y: 0 });

  const xFor = (index: number) =>
    pointCount <= 1 ? pad.left + innerWidth / 2 : pad.left + (index / (pointCount - 1)) * innerWidth;
  const max = Math.max(0, ...numericValues);
  const ticks = buildTicks(max, countAxis);
  const tickMax = (ticks[0] ?? 0) > 0 ? ticks[0] : 1;
  const yFor = (value: number) => pad.top + innerHeight - (Math.max(0, value) / tickMax) * innerHeight;
  const indexes = labelIndexes(pointCount);
  const formatLabel = labelFormatter(labels);
  const formatTick = (value: number) =>
    countAxis ? Math.round(value).toLocaleString() : formatValue(value);
  const colors = series.map(
    (entry, index) =>
      entry.color
      ?? (entry.variant === 'error'
        ? CHART_COLORS.error
        : entry.variant === 'llm'
          ? CHART_COLORS.secondary
          : PALETTE[index % PALETTE.length]),
  );
  const legend = (
    <ChartLegend items={series.map((entry, index) => ({ label: entry.label, color: colors[index] }))} />
  );
  const card = (body: ReactNode) => (
    <ChartCard label={label} title={title} ariaLabel={ariaLabel ?? label} legend={legend}>
      {body}
    </ChartCard>
  );

  if (numericValues.length === 0 || series.length === 0) {
    return card(<div className="chart-empty"><EmptyState title={emptyLabel} /></div>);
  }

  const setPointerForIndex = (index: number) => {
    setPointer({ x: xFor(index), y: pad.top + 8 });
  };

  const handleMove = (event: MouseEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    const ratio = (x - pad.left) / innerWidth;
    const index = Math.max(0, Math.min(pointCount - 1, Math.round(ratio * (pointCount - 1))));
    setPointer({ x, y });
    setActiveIndex(index);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      setActiveIndex(null);
      return;
    }
    const current = activeIndex ?? (event.key === 'ArrowLeft' || event.key === 'Home' ? pointCount : -1);
    let next: number | null = null;
    if (event.key === 'ArrowRight') next = Math.min(pointCount - 1, current + 1);
    if (event.key === 'ArrowLeft') next = Math.max(0, current - 1);
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = pointCount - 1;
    if (next === null) return;
    event.preventDefault();
    setActiveIndex(next);
    setPointerForIndex(next);
  };

  const activeX = activeIndex === null ? 0 : xFor(activeIndex);
  const plot = (
    <div
      ref={ref}
      className="chart-plot"
      tabIndex={0}
      role="img"
      aria-label={ariaLabel ?? label}
      onMouseMove={handleMove}
      onMouseLeave={() => setActiveIndex(null)}
      onFocus={() => {
        if (activeIndex === null) {
          const index = Math.min(pointCount - 1, Math.floor(pointCount / 2));
          setActiveIndex(index);
          setPointerForIndex(index);
        }
      }}
      onBlur={() => setActiveIndex(null)}
      onKeyDown={handleKeyDown}
    >
      <svg className="chart-svg" viewBox={`0 0 ${width} ${height}`} width={width} height={height} aria-hidden="true">
        <title>{ariaLabel ?? label}</title>
        {ticks.map((tick, index) => {
          const y = yFor(tick);
          return (
            <g key={`tick-${tick}-${index}`}>
              <line className="chart-gridline" x1={pad.left} x2={width - pad.right} y1={y} y2={y} />
              <text className="chart-axis-label" x={pad.left - 8} y={y + 4} textAnchor="end">
                {formatTick(tick)}
              </text>
            </g>
          );
        })}
        <line className="chart-baseline" x1={pad.left} x2={width - pad.right} y1={yFor(0)} y2={yFor(0)} />
        {indexes.map((index) => (
          <text
            key={`label-${index}`}
            className="chart-axis-label"
            x={xFor(index)}
            y={height - 10}
            textAnchor={index === 0 ? 'start' : index === pointCount - 1 ? 'end' : 'middle'}
          >
            {formatLabel(labels[index])}
          </text>
        ))}
        {activeIndex !== null && (
          <line className="chart-crosshair" x1={activeX} x2={activeX} y1={pad.top} y2={height - pad.bottom} />
        )}
        {series.map((entry, seriesIndex) => {
          const color = colors[seriesIndex];
          if (variant === 'bar') {
            const groupWidth = innerWidth / pointCount;
            const barWidth = Math.max(2, Math.min(24, (groupWidth * 0.72) / Math.max(1, series.length)));
            const groupOffset = (series.length * barWidth) / 2;
            return (
              <g key={entry.label}>
                {Array.from({ length: pointCount }, (_, index) => {
                  const number = toFiniteNumber(entry.values[index]);
                  if (number === null) return null;
                  const value = Math.max(0, number);
                  const x = xFor(index) - groupOffset + seriesIndex * barWidth;
                  const y = yFor(value);
                  const active = activeIndex === index;
                  return (
                    <g key={`${entry.label}-${index}`}>
                      <path
                        className={`chart-bar${barClass(entry.variant)}${active ? ' is-active' : ''}`}
                        fill={entry.variant ? undefined : color}
                        d={barPath(x, Math.min(y, yFor(0)), Math.max(1, barWidth - 1), Math.max(0, Math.abs(yFor(0) - y)), 3)}
                      >
                        <title>{`${entry.label} · ${formatLabel(labels[index])}: ${formatTick(value)}`}</title>
                      </path>
                      {series.length === 1 && pointCount <= 12 && (
                        <text
                          className="chart-axis-label"
                          x={x + barWidth / 2}
                          y={Math.max(pad.top - 8, y - 8)}
                          textAnchor="middle"
                        >
                          {formatTick(value)}
                        </text>
                      )}
                    </g>
                  );
                })}
              </g>
            );
          }
          const lastIndex = entry.values.reduce<number>(
            (found, value, index) => (toFiniteNumber(value) !== null ? index : found),
            -1,
          );
          const lastValue = lastIndex >= 0 ? toFiniteNumber(entry.values[lastIndex]) : null;
          const lastValueText = formatTick(Math.max(0, lastValue ?? 0));
          return (
            <g key={entry.label}>
              {contiguousSegments(entry.values).map((segment, segmentIndex) => (
                <polyline
                  key={`${entry.label}-${segmentIndex}`}
                  className="chart-line"
                  fill="none"
                  stroke={color}
                  points={segment
                    .map((index) => `${xFor(index)},${yFor(toFiniteNumber(entry.values[index]) ?? 0)}`)
                    .join(' ')}
                />
              ))}
              {Array.from({ length: pointCount }, (_, index) => {
                const number = toFiniteNumber(entry.values[index]);
                if (number === null) return null;
                const value = Math.max(0, number);
                const active = activeIndex === index;
                return (
                  <circle
                    key={`${entry.label}-point-${index}`}
                    className={`chart-point${active ? ' is-active' : ''}`}
                    cx={xFor(index)}
                    cy={yFor(value)}
                    r={active ? 4.5 : 2.8}
                    fill={color}
                  >
                    <title>{`${entry.label} · ${formatLabel(labels[index])}: ${formatTick(value)}`}</title>
                  </circle>
                );
              })}
              {lastIndex >= 0 && series.length <= 3 && (
                <text
                  className="chart-axis-label"
                  x={Math.min(xFor(lastIndex) + 6, width - pad.right - 48)}
                  y={Math.max(pad.top - 8, yFor(Math.max(0, lastValue ?? 0)) - 8)}
                  textAnchor="start"
                >
                  {lastValueText}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      {activeIndex !== null && (
        <ChartTooltip x={pointer.x} y={pointer.y} width={width} height={height}>
          <TooltipContent
            title={formatWindowLabel(labels, activeIndex)}
            rows={series.map((entry, index) => {
              const value = toFiniteNumber(entry.values[activeIndex]);
              return {
                label: entry.label,
                value: value === null ? '—' : formatTick(value),
                color: colors[index],
                context: entry.context?.[activeIndex],
              };
            })}
          />
        </ChartTooltip>
      )}
    </div>
  );

  return card(plot);
}

export function BarChart(props: BaseChartProps) {
  return <ChartFrame {...props} variant="bar" />;
}

export function LineChart(props: BaseChartProps) {
  return <ChartFrame {...props} variant="line" />;
}

export function TimeChart(props: BaseChartProps & { variant?: 'bar' | 'line' }) {
  return props.variant === 'line' ? <LineChart {...props} /> : <BarChart {...props} />;
}

export function Histogram({
  values,
  labels,
  height,
  formatValue = formatCount,
  emptyLabel = 'No data in this range',
  ariaLabel = 'Histogram',
  color = CHART_COLORS.primary,
}: HistogramProps) {
  const series: ChartSeries[] = [{ label: 'Count', values, color }];
  return (
    <BarChart
      label={ariaLabel}
      labels={labels ?? values.map((_, index) => String(index + 1))}
      height={height}
      series={series}
      formatValue={formatValue}
      emptyLabel={emptyLabel}
      ariaLabel={ariaLabel}
      countAxis
    />
  );
}


