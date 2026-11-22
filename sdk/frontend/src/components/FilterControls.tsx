import type { ReactNode } from 'react';
import { useFilterVisibility } from '../hooks/useFilterVisibility';
import type { DashboardFilters, Project } from '../types';
import { getActiveChips } from '../utils/filterChips';
import DashboardFilterBar from './TracesFilterBar';
import Icon from './Icons';

interface FilterControlsProps {
  tabKey: string;
  title: string;
  subtitle?: ReactNode;
  filters: DashboardFilters;
  onFiltersChange: (patch: Partial<DashboardFilters>) => void;
  projects: Project[];
  environments: string[];
  models: string[];
  actions?: ReactNode;
  children: ReactNode;
}

export default function FilterControls({
  tabKey,
  title,
  subtitle,
  filters,
  onFiltersChange,
  projects,
  environments,
  models,
  actions,
  children,
}: FilterControlsProps) {
  void tabKey;
  const { visible, setVisible } = useFilterVisibility();
  const chips = getActiveChips(filters, projects);
  const shownChips = chips.slice(0, 4);
  const hiddenCount = chips.length - shownChips.length;

  return (
    <div className="tk-page">
      <header className="tk-page-header">
        <div className="tk-page-heading-text">
          <h1>{title}</h1>
          {subtitle !== undefined && subtitle !== null && subtitle !== '' && (
            <p className="tk-subtitle">{subtitle}</p>
          )}
        </div>
        <div className="tk-page-actions">
          {actions}
          <button
            type="button"
            className="tk-filter-toggle-button tk-filter-toggle-icon"
            aria-label={visible ? 'Hide filters' : 'Show filters'}
            aria-expanded={visible}
            title={visible ? 'Hide filters' : 'Show filters'}
            onClick={() => setVisible((current) => !current)}
          >
            <Icon name="sliders" size={15} />
          </button>
        </div>
      </header>

      {!visible && chips.length > 0 && (
        <div className="tk-filter-summary-row" aria-label="Active filters">
          <span className="tk-active-label">Active:</span>
          {shownChips.map((chip) => (
            <span className="tk-summary-chip" key={chip.key}>
              {chip.label}
            </span>
          ))}
          {hiddenCount > 0 && <span className="tk-summary-chip">+{hiddenCount} more</span>}
        </div>
      )}

      <div className={visible ? 'tk-filter-collapse is-open' : 'tk-filter-collapse'} aria-hidden={!visible}>
        <div className="tk-filter-collapse-inner">
          <DashboardFilterBar
            filters={filters}
            onChange={onFiltersChange}
            projects={projects}
            environments={environments}
            models={models}
          />
        </div>
      </div>

      {children}
    </div>
  );
}
