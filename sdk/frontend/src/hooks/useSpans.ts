import { useCallback, useEffect, useMemo, useState } from 'react';
import { getReplay } from '../api';
import type { Replay, SpanNode } from '../types';

export interface UseSpansResult {
  replay: Replay | null;
  tree: SpanNode[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

export function useSpans(traceId: string): UseSpansResult {
  const [replay, setReplay] = useState<Replay | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setReplay(null);
    try {
      const data = await getReplay(traceId);
      setReplay(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load replay');
    } finally {
      setLoading(false);
    }
  }, [traceId]);

  useEffect(() => {
    void load();
  }, [load]);

  const tree = useMemo<SpanNode[]>(() => {
    const value = replay?.tree;
    return Array.isArray(value) ? value : [];
  }, [replay]);

  const refresh = useCallback(() => {
    void load();
  }, [load]);

  return { replay, tree, loading, error, refresh };
}
