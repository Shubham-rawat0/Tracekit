import { useCallback, useEffect, useState } from 'react';
import { getTrace } from '../api';
import type { Trace } from '../types';

export interface UseTraceResult {
  trace: Trace | null;
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

export function useTrace(traceId: string): UseTraceResult {
  const [trace, setTrace] = useState<Trace | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setTrace(null);
    try {
      const data = await getTrace(traceId);
      setTrace(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load trace');
    } finally {
      setLoading(false);
    }
  }, [traceId]);

  useEffect(() => {
    void load();
  }, [load]);

  const refresh = useCallback(() => {
    void load();
  }, [load]);

  return { trace, loading, error, refresh };
}
