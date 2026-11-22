import { useCallback, useEffect, useState } from 'react';
import { archiveProject, createProject, getProjects, unarchiveProject } from '../api';
import type { CreateProjectPayload, Project } from '../types';

export interface UseProjectsResult {
  projects: Project[];
  loading: boolean;
  error: string | null;
  busy: boolean;
  refresh: () => void;
  create: (payload: CreateProjectPayload) => Promise<Project>;
  archive: (projectId: string) => Promise<void>;
  unarchive: (projectId: string) => Promise<void>;
  clearError: () => void;
}

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : 'Request failed';
}

export function useProjects(): UseProjectsResult {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const rows = await getProjects();
      setProjects(Array.isArray(rows) ? rows : []);
    } catch (err) {
      setProjects([]);
      setError(messageOf(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const create = useCallback(async (payload: CreateProjectPayload): Promise<Project> => {
    setBusy(true);
    setError(null);
    try {
      const created = await createProject(payload);
      setProjects((prev) => [...prev, created]);
      return created;
    } catch (err) {
      setError(messageOf(err));
      throw err;
    } finally {
      setBusy(false);
    }
  }, []);

  const archive = useCallback(async (projectId: string): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      await archiveProject(projectId);
      setProjects((prev) =>
        prev.map((project) =>
          project.project_id === projectId ? { ...project, archived: true } : project,
        ),
      );
    } catch (err) {
      setError(messageOf(err));
      throw err;
    } finally {
      setBusy(false);
    }
  }, []);

  const unarchive = useCallback(async (projectId: string): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      await unarchiveProject(projectId);
      setProjects((prev) =>
        prev.map((project) =>
          project.project_id === projectId ? { ...project, archived: false } : project,
        ),
      );
    } catch (err) {
      setError(messageOf(err));
      throw err;
    } finally {
      setBusy(false);
    }
  }, []);

  const clearError = useCallback(() => setError(null), []);
  const refresh = useCallback(() => void load(), [load]);

  return {
    projects,
    loading,
    error,
    busy,
    refresh,
    create,
    archive,
    unarchive,
    clearError,
  };
}
