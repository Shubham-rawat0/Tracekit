import { useCallback, useEffect, useRef, useState } from 'react';
import Layout from './components/Layout';
import { useTheme } from './hooks/useTheme';
import Analytics from './pages/Analytics';
import Docs from './pages/Docs';
import Errors from './pages/Errors';
import Home from './pages/Home';
import Overview from './pages/Overview';
import Projects from './pages/Projects';
import TraceDetail from './pages/TraceDetail';
import { defaultDashboardFilters } from './types';
import type { DashboardFilters, TraceTab } from './types';
import { filtersFromHash, hrefWithFilters, syncFiltersToHash } from './utils/urlFilters';

type Route =
  | { kind: 'home' }
  | { kind: 'overview' }
  | { kind: 'analytics' }
  | { kind: 'errors' }
  | { kind: 'projects' }
  | { kind: 'docs'; sectionId?: string }
  | { kind: 'trace'; id: string; tab: TraceTab };

const TRACE_TABS: TraceTab[] = [
  'waterfall',
  'overview',
  'input',
  'output',
  'events',
  'attributes',
  'error',
  'raw',
];

function parseRoute(hash: string): Route {
  const withoutHash = hash.startsWith('#') ? hash.slice(1) : hash;
  const path = (withoutHash.split('?')[0] || '/').replace(/\/+$/, '') || '/';
  if (path === '/' || path === '/traces') return { kind: 'home' };
  if (path === '/overview') return { kind: 'overview' };
  if (path === '/analytics') return { kind: 'analytics' };
  if (path === '/errors') return { kind: 'errors' };
  if (path === '/projects') return { kind: 'projects' };
  if (path === '/docs') return { kind: 'docs' };
  const docsMatch = path.match(/^\/docs\/([^/]+)$/);
  if (docsMatch) return { kind: 'docs', sectionId: decodeURIComponent(docsMatch[1]) };
  const match = path.match(/^\/traces\/([^/]+)(?:\/([^/]+))?$/);
  if (match) {
    const candidate = match[2] as TraceTab | undefined;
    return {
      kind: 'trace',
      id: decodeURIComponent(match[1]),
      tab: candidate && TRACE_TABS.includes(candidate) ? candidate : 'waterfall',
    };
  }
  return { kind: 'home' };
}

export default function App() {
  const [hash, setHash] = useState(() => window.location.hash || '#/');
  const [filters, setFilters] = useState<DashboardFilters>(
    () => filtersFromHash(window.location.hash) ?? { ...defaultDashboardFilters },
  );
  const [refreshSignal, setRefreshSignal] = useState(0);
  const filtersRef = useRef(filters);
  const { theme, setTheme } = useTheme();

  filtersRef.current = filters;

  useEffect(() => {
    const onHashChange = () => {
      const nextHash = window.location.hash || '#/';
      setHash(nextHash);
      const shared = filtersFromHash(nextHash);
      if (shared) setFilters(shared);
      else syncFiltersToHash(filtersRef.current, nextHash);
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  useEffect(() => {
    syncFiltersToHash(filters);
  }, [filters]);

  const updateFilters = useCallback((patch: Partial<DashboardFilters>) => {
    setFilters((current) => ({ ...current, ...patch }));
  }, []);

  const handleRefresh = useCallback(() => {
    setRefreshSignal((value) => value + 1);
  }, []);

  const handleSelectProject = useCallback(
    (projectId: string | null) => {
      const next = { ...filtersRef.current, project_id: projectId ?? '' };
      setFilters(next);
      window.location.hash = hrefWithFilters('#/traces', next);
    },
    [],
  );

  const route = parseRoute(hash);

  const handleTraceNavigate = useCallback(
    (tab: TraceTab) => {
      if (route.kind !== 'trace') return;
      window.location.hash = hrefWithFilters(
        `#/traces/${encodeURIComponent(route.id)}/${tab}`,
        filtersRef.current,
      );
    },
    [route],
  );

  const projectsProps = {
    filters,
    onFiltersChange: updateFilters,
    refreshSignal,
    onSelectProject: handleSelectProject,
  };

  return (
    <Layout
      hash={hash}
      onRefresh={handleRefresh}
      theme={theme}
      onThemeChange={setTheme}
      showRefresh={route.kind !== 'home' && route.kind !== 'docs'}
    >
      {route.kind === 'home' ? (
        <Home
          filters={filters}
          onFiltersChange={updateFilters}
          refreshSignal={refreshSignal}
          onRefresh={handleRefresh}
        />
      ) : route.kind === 'overview' ? (
        <Overview filters={filters} onFiltersChange={updateFilters} refreshSignal={refreshSignal} />
      ) : route.kind === 'analytics' ? (
        <Analytics filters={filters} onFiltersChange={updateFilters} refreshSignal={refreshSignal} />
      ) : route.kind === 'errors' ? (
        <Errors filters={filters} onFiltersChange={updateFilters} refreshSignal={refreshSignal} />
      ) : route.kind === 'projects' ? (
        <Projects {...projectsProps} />
      ) : route.kind === 'docs' ? (
        <Docs sectionId={route.sectionId} />
      ) : (
        <TraceDetail
          key={route.id}
          traceId={route.id}
          tab={route.tab}
          onNavigate={handleTraceNavigate}
          filters={filters}
          onFiltersChange={updateFilters}
          refreshSignal={refreshSignal}
        />
      )}
    </Layout>
  );
}
