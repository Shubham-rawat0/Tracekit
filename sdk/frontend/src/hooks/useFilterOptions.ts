import { useEffect, useState } from 'react';
import { getTraces } from '../api';
import { useProjects } from './useProjects';

export function useFilterOptions() {
  const { projects, loading } = useProjects();
  const [environments, setEnvironments] = useState<string[]>([]);
  const [models, setModels] = useState<string[]>([]);

  useEffect(() => {
    let active = true;
    getTraces({ limit: 500 })
      .then((rows) => {
        if (!active) return;
        const nextEnvironments = new Set<string>();
        const nextModels = new Set<string>();
        rows.forEach((trace) => {
          if (typeof trace.environment === 'string' && trace.environment) {
            nextEnvironments.add(trace.environment);
          }
          const model = trace.model_name || trace.model;
          if (typeof model === 'string' && model) nextModels.add(model);
        });
        setEnvironments(Array.from(nextEnvironments).sort());
        setModels(Array.from(nextModels).sort());
      })
      .catch(() => {
        if (active) {
          setEnvironments([]);
          setModels([]);
        }
      });
    return () => {
      active = false;
    };
  }, []);

  return { projects, loading, environments, models };
}
