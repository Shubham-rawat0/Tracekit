import { useEffect, useRef, useState } from 'react';
import FilterControls from '../components/FilterControls';
import { KpiStrip } from '../components/Metrics';
import { EmptyState, ErrorState, LoadingRows, Section } from '../components/States';
import { useFilterOptions } from '../hooks/useFilterOptions';
import { useProjects } from '../hooks/useProjects';
import type { CreateProjectPayload, DashboardFilters, Project } from '../types';
import { copyToClipboard } from '../utils/auth';
import { formatCount, formatDateTime, toFiniteNumber } from '../utils/format';

interface ProjectsProps {
  filters: DashboardFilters;
  onFiltersChange: (patch: Partial<DashboardFilters>) => void;
  refreshSignal: number;
  onSelectProject: (projectId: string | null) => void;
}

function lastSeenText(project: Project): string {
  const record = project as Record<string, unknown>;
  const value = record.last_seen ?? record.lastSeen ?? record.last_activity ?? record.updated_at;
  if (typeof value === 'string' || typeof value === 'number') return formatDateTime(value);
  return '—';
}

function environmentsText(project: Project): string {
  const value = (project as Record<string, unknown>).environments;
  if (Array.isArray(value)) {
    const items = value.filter((item): item is string => typeof item === 'string' && item.length > 0);
    return items.length > 0 ? items.join(', ') : '—';
  }
  if (typeof value === 'string' && value.trim()) return value;
  return '—';
}

function CopyProjectId({ projectId }: { projectId: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    if (!(await copyToClipboard(projectId))) return;
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };
  return (
    <div className="table-actions">
      <span className="mono">{projectId}</span>
      <button type="button" className="copy-text" onClick={() => void copy()}>
        {copied ? 'Copied' : 'Copy'}
      </button>
    </div>
  );
}

export default function Projects({ filters, onFiltersChange, refreshSignal, onSelectProject }: ProjectsProps) {
  const { projects, loading, error, busy, refresh, create, archive, unarchive } = useProjects();
  const { environments, models } = useFilterOptions();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [form, setForm] = useState({ name: '', project_id: '', description: '' });
  const [submitError, setSubmitError] = useState<string | null>(null);
  const lastRefreshRef = useRef(0);

  useEffect(() => {
    if (refreshSignal === 0 || refreshSignal === lastRefreshRef.current) return;
    lastRefreshRef.current = refreshSignal;
    refresh();
  }, [refreshSignal, refresh]);

  const closeDialog = () => {
    setDialogOpen(false);
    setSubmitError(null);
  };

  const submit = async () => {
    if (!form.name.trim()) {
      setSubmitError('Name is required.');
      return;
    }
    const payload: CreateProjectPayload = {
      name: form.name.trim(),
      project_id: form.project_id.trim() || undefined,
      description: form.description.trim() || undefined,
    };
    try {
      await create(payload);
      setForm({ name: '', project_id: '', description: '' });
      closeDialog();
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : 'Could not create project.');
    }
  };

  const toggleArchive = async (project: Project) => {
    try {
      if (project.archived) await unarchive(project.project_id);
      else await archive(project.project_id);
    } catch {
      setSubmitError('Could not update the project.');
    }
  };

  const kpis = [
    { label: 'Projects', value: loading ? '—' : formatCount(projects.length) },
    {
      label: 'Archived',
      value: loading ? '—' : formatCount(projects.filter((project) => project.archived).length),
    },
    {
      label: 'Traces',
      value: loading
        ? '—'
        : formatCount(projects.reduce((total, project) => total + (toFiniteNumber(project.trace_count) ?? 0), 0)),
    },
  ];

  return (
    <FilterControls
      tabKey="projects"
      title="Projects"
      subtitle="Trace projects and their environments"
      filters={filters}
      onFiltersChange={onFiltersChange}
      projects={projects}
      environments={environments}
      models={models}
    >
      {error && <ErrorState message={error} onRetry={() => void refresh()} />}
      {submitError && !dialogOpen && <ErrorState message={submitError} />}
      <KpiStrip items={kpis} />
      <Section
        label="Projects"
        title={`${formatCount(projects.length)} projects`}
        actions={
          <button type="button" className="pagination-button" onClick={() => setDialogOpen(true)}>
            New project
          </button>
        }
      >
        {loading && <LoadingRows rows={5} columns={6} label="Loading projects" />}
        {!loading && error && <EmptyState title="Projects unavailable" message="n/a" />}
        {!loading && !error && projects.length === 0 && (
          <EmptyState title="No projects yet" message="Create one to organize your traces." />
        )}
        {!loading && !error && projects.length > 0 && (
          <div className="table-scroll">
            <table className="data-table dense-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Project ID</th>
                  <th>Traces</th>
                  <th>Last seen</th>
                  <th>Environments</th>
                  <th>Status</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {projects.map((project) => (
                  <tr key={project.project_id}>
                    <td className="truncate-cell" title={project.name ?? ''}>
                      {project.name || '—'}
                    </td>
                    <td><CopyProjectId projectId={project.project_id} /></td>
                    <td className="mono">{formatCount(project.trace_count)}</td>
                    <td className="muted-line">{lastSeenText(project)}</td>
                    <td className="muted-line">{environmentsText(project)}</td>
                    <td>
                      <span className={`status-tag ${project.archived ? 'warn' : 'ok'}`}>
                        {project.archived ? 'archived' : 'active'}
                      </span>
                    </td>
                    <td>
                      <div className="table-actions">
                        <a
                          className="link"
                          href="#/traces"
                          onClick={() => onSelectProject(project.project_id)}
                        >
                          View traces
                        </a>
                        <button
                          type="button"
                          className="copy-text"
                          disabled={busy}
                          onClick={() => void toggleArchive(project)}
                        >
                          {project.archived ? 'Unarchive' : 'Archive'}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      {dialogOpen && (
        <div className="modal-backdrop">
          <div className="modal" role="dialog" aria-modal="true" aria-labelledby="create-project-title">
            <h3 id="create-project-title">Create project</h3>
            <label className="field">
              <span>Name</span>
              <input
                type="text"
                autoFocus
                value={form.name}
                onChange={(event) => setForm((prev) => ({ ...prev, name: event.target.value }))}
              />
            </label>
            <label className="field">
              <span>Project ID (optional)</span>
              <input
                type="text"
                value={form.project_id}
                onChange={(event) => setForm((prev) => ({ ...prev, project_id: event.target.value }))}
              />
            </label>
            <label className="field">
              <span>Description (optional)</span>
              <input
                type="text"
                value={form.description}
                onChange={(event) => setForm((prev) => ({ ...prev, description: event.target.value }))}
              />
            </label>
            {submitError && <ErrorState message={submitError} />}
            <div className="modal-actions">
              <button type="button" className="pagination-button" onClick={closeDialog}>
                Cancel
              </button>
              <button
                type="button"
                className="pagination-button"
                disabled={busy}
                onClick={() => void submit()}
              >
                {busy ? 'Creating…' : 'Create'}
              </button>
            </div>
          </div>
        </div>
      )}
    </FilterControls>
  );
}
