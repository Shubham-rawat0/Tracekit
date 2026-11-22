import { useCallback, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { getConfig, getHealth } from '../api';
import { useProjects } from '../hooks/useProjects';
import type { Theme } from '../hooks/useTheme';
import type { ConfigResponse, HealthResponse } from '../types';
import { getToken } from '../utils/auth';
import Header from './Header';
import Sidebar from './Sidebar';

interface LayoutProps {
  hash: string;
  onRefresh: () => void;
  children: ReactNode;
  theme: Theme;
  onThemeChange: (theme: Theme) => void;
  showRefresh: boolean;
}

const SIDEBAR_COLLAPSED_KEY = 'tracekit-sidebar-collapsed';

function readSidebarCollapsed(): boolean {
  try {
    return window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === '1';
  } catch {
    return false;
  }
}

function tabLabelFromHash(hash: string): string {
  const path = hash.replace(/^#/, '').split('?')[0] || '/';
  if (path.startsWith('/overview')) return 'Overview';
  if (path.startsWith('/analytics')) return 'Analytics';
  if (path.startsWith('/errors')) return 'Errors';
  if (path.startsWith('/projects')) return 'Projects';
  if (path.startsWith('/docs')) return 'Docs';
  return 'Traces';
}

export default function Layout({
  hash,
  onRefresh,
  children,
  theme,
  onThemeChange,
  showRefresh,
}: LayoutProps) {
  const { error: projectsError, refresh: refreshProjects } = useProjects();
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [config, setConfig] = useState<ConfigResponse | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<number | null>(() => Date.now());
  const [retryCount, setRetryCount] = useState(0);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(readSidebarCollapsed);

  useEffect(() => {
    try {
      window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, sidebarCollapsed ? '1' : '0');
    } catch {
      return;
    }
  }, [sidebarCollapsed]);

  useEffect(() => {
    document.documentElement.classList.toggle('sidebar-collapsed', sidebarCollapsed);
    return () => {
      document.documentElement.classList.remove('sidebar-collapsed');
    };
  }, [sidebarCollapsed]);

  useEffect(() => {
    let active = true;
    Promise.all([getHealth(), getConfig()])
      .then(([nextHealth, nextConfig]) => {
        if (!active) return;
        setHealth(nextHealth);
        setConfig(nextConfig);
        setStatusError(null);
      })
      .catch((err: unknown) => {
        if (active) setStatusError(err instanceof Error ? err.message : 'Dashboard API unavailable');
      });
    return () => {
      active = false;
    };
  }, [retryCount]);

  const handleRefresh = useCallback(() => {
    setLastRefresh(Date.now());
    onRefresh();
  }, [onRefresh]);

  const retry = () => {
    setRetryCount((value) => value + 1);
    refreshProjects();
  };

  const healthStatus = health?.status ? String(health.status) : '';
  const dbPath = config?.db_path || '';
  const storageDetail = health?.storage_detail || dbPath;
  const statusOk = /^(ok|healthy|connected)$/i.test(healthStatus) &&
    !/unavailable|error|none|missing/i.test(storageDetail);
  const bannerError = statusError ?? projectsError;

  return (
    <div className="app-shell">
      <a className="shell-logo" href="#/traces" aria-label="TraceKit home">
        <img src="/tracekit-logo.svg" width={32} height={32} alt="" />
        <strong>TraceKit</strong>
      </a>
      <button
        type="button"
        className="sidebar-toggle"
        onClick={() => setSidebarCollapsed((value) => !value)}
        aria-expanded={!sidebarCollapsed}
        title={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
      >
        {sidebarCollapsed ? '»' : '«'}
      </button>
      <Sidebar
        hash={hash}
        dbPath={dbPath}
        statusOk={statusOk}
        lastRefresh={lastRefresh}
        collapsed={sidebarCollapsed}
      />
      <div className="app-column">
        <Header
          token={getToken()}
          tabLabel={tabLabelFromHash(hash)}
          theme={theme}
          onThemeChange={onThemeChange}
          onRefresh={handleRefresh}
          showRefresh={showRefresh}
        />
        {bannerError && (
          <div className="banner banner-error banner-global" role="alert">
            <span>{bannerError}</span>
            <button type="button" className="button button-small" onClick={retry}>
              Retry
            </button>
          </div>
        )}
        <main className="app-main">{children}</main>
      </div>
    </div>
  );
}
