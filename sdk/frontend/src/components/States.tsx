import type { ReactNode } from 'react';

interface SectionProps {
  label: string;
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}

export function Section({ label, title, actions, children }: SectionProps) {
  return (
    <section className="section">
      <div className="section-head">
        <div className="section-label">
          {label}
          {title !== undefined && title !== null && title !== '' && (
            <span className="section-title">{title}</span>
          )}
        </div>
        {actions && <div className="table-actions">{actions}</div>}
      </div>
      <div className="section-body">{children}</div>
    </section>
  );
}

interface EmptyStateProps {
  title?: string;
  message?: string;
  action?: ReactNode;
}

export function EmptyState({ title = 'Nothing here yet', message, action }: EmptyStateProps) {
  return (
    <div className="state">
      <strong>{title}</strong>
      {message && <span>{message}</span>}
      {action && <div className="state-action">{action}</div>}
    </div>
  );
}

interface ErrorStateProps {
  message: string;
  onRetry?: () => void;
}

export function ErrorState({ message, onRetry }: ErrorStateProps) {
  return (
    <div className="state state-error" role="alert">
      <span>{message}</span>
      {onRetry && (
        <button type="button" className="button button-small" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

interface LoadingRowsProps {
  rows?: number;
  columns?: number;
  label?: string;
}

export function LoadingRows({ rows = 5, columns = 4, label = 'Loading' }: LoadingRowsProps) {
  return (
    <div className="state-loading" role="status" aria-label={label}>
      <table className="data-table loading-table">
        <tbody>
          {Array.from({ length: rows }, (_, rowIndex) => (
            <tr key={rowIndex}>
              {Array.from({ length: columns }, (_, columnIndex) => (
                <td key={columnIndex}>
                  <span className="loading-placeholder" />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface LoadingStateProps {
  label?: string;
}

export function LoadingState({ label = 'Loading' }: LoadingStateProps) {
  return (
    <div className="state state-loading" role="status">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

interface PanelProps {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}

export function Panel({ title, subtitle, actions, children, className = '' }: PanelProps) {
  return (
    <section className={`section ${className}`.trim()}>
      <div className="section-head">
        <div className="section-label">
          {title}
          {subtitle && <span className="section-subtitle">{subtitle}</span>}
        </div>
        {actions && <div className="table-actions">{actions}</div>}
      </div>
      <div className="section-body">{children}</div>
    </section>
  );
}
