import type { SpanNode } from '../types';

export function toFiniteNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

export function toMillis(value: unknown): number | null {
  if (typeof value !== 'string' && typeof value !== 'number') return null;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  const numeric = toFiniteNumber(value);
  if (numeric !== null && /^\d+(\.\d+)?$/.test(value.trim())) return numeric;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

export function spanDuration(node: SpanNode): number {
  const direct = toFiniteNumber(node.duration_ms);
  if (direct !== null && direct >= 0) return direct;
  const start = toMillis(node.start_time);
  const end = toMillis(node.end_time);
  if (start !== null && end !== null && end >= start) return end - start;
  return 0;
}

export function latencyClass(durationMs?: number | null): string {
  const value = toFiniteNumber(durationMs);
  if (value === null) return 'latency-unknown';
  if (value < 100) return 'latency-good';
  if (value <= 500) return 'latency-warn';
  return 'latency-bad';
}

export function formatDuration(value?: number | null): string {
  const ms = toFiniteNumber(value);
  if (ms === null) return '—';
  if (ms < 1) return `${ms.toFixed(2)} ms`;
  if (ms < 1000) return `${Math.round(ms * 10) / 10} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

export function formatDateTime(value?: string | number | null): string {
  if (value === null || value === undefined || value === '') return '—';
  const millis = toMillis(value);
  if (millis === null) return String(value);
  const date = new Date(millis);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString();
}

export function formatAxisTime(value?: string | number | null): string {
  const millis = toMillis(value);
  if (millis === null) return '—';
  const date = new Date(millis);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString(undefined, {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function formatWindow(start?: string | null, end?: string | null): string {
  if (!start && !end) return '—';
  return `${formatDateTime(start)} → ${formatDateTime(end)}`;
}

export function formatCount(value?: number | null): string {
  const number = toFiniteNumber(value);
  if (number === null) return '—';
  return Math.round(number).toLocaleString();
}

export function formatDecimal(value?: number | null, digits = 1): string {
  const number = toFiniteNumber(value);
  if (number === null) return '—';
  return number.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function formatPercent(value?: number | null, digits = 1): string {
  const number = toFiniteNumber(value);
  if (number === null) return '—';
  return `${(number * 100).toFixed(digits)}%`;
}

export function formatCost(value?: number | null): string {
  const number = toFiniteNumber(value);
  if (number === null) return '—';
  const digits = Math.abs(number) < 0.01 && number !== 0 ? 6 : 4;
  return `$${number.toFixed(digits)}`;
}

export function relativeTime(value?: number | string | null, now = Date.now()): string {
  const millis = toMillis(value);
  if (millis === null) return '—';
  const seconds = Math.max(0, Math.round((now - millis) / 1000));
  if (seconds < 5) return 'just now';
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function statusClass(status?: string | null): string {
  const normalized = (status ?? 'unknown').toLowerCase();
  if (normalized === 'ok' || normalized === 'success' || normalized === 'completed') return 'status-ok';
  if (normalized === 'error' || normalized === 'failed' || normalized === 'failure') return 'status-error';
  if (normalized === 'pending') return 'status-pending';
  if (normalized === 'running') return 'status-running';
  if (normalized === 'cancelled' || normalized === 'canceled') return 'status-cancelled';
  return 'status-unknown';
}
