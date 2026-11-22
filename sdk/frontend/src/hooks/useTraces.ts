import { useCallback, useEffect, useRef, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import { getTraces } from '../api';
import type { Trace, TraceFilters } from '../types';

export interface UseTracesResult {
  traces: Trace[];
  loading: boolean;
  error: string | null;
  filters: TraceFilters;
  setFilters: Dispatch<SetStateAction<TraceFilters>>;
  refresh: () => void;
}

export function useTraces(initialFilters: TraceFilters = {}, pollMs = 0): UseTracesResult {
  const [traces, setTraces] = useState<Trace[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filters, setFilters] = useState<TraceFilters>(initialFilters);
  const filtersRef = useRef(filters);
  const loadRef = useRef<() => Promise<void>>(async () => {});

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const rows = await getTraces(filtersRef.current);
      setTraces(Array.isArray(rows) ? rows : []);
    } catch (err) {
      setTraces([]);
      setError(err instanceof Error ? err.message : 'Failed to load traces');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    filtersRef.current = filters;
  }, [filters]);

  useEffect(() => {
    loadRef.current = load;
  }, [load]);

  useEffect(() => {
    void load();
  }, [filters, load]);

  useEffect(() => {
    if (pollMs <= 0) return;
    const timer = window.setInterval(() => {
      void loadRef.current();
    }, pollMs);
    return () => window.clearInterval(timer);
  }, [pollMs]);

  const refresh = useCallback(() => {
    void load();
  }, [load]);

  return { traces, loading, error, filters, setFilters, refresh };
}
