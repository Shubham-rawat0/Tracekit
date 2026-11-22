import { useEffect, useState } from 'react';
import { relativeTime } from '../utils/format';
import Icon from './Icons';
import type { IconName } from './Icons';

interface SidebarProps {
  hash: string;
  dbPath?: string;
  statusOk?: boolean;
  statusLabel?: string;
  lastRefresh?: number | null;
  collapsed: boolean;
}

const NAV_ITEMS: { label: string; icon: IconName; href: string; path: string }[] = [
  { label: 'Overview', icon: 'overview', href: '#/overview', path: '/overview' },
  { label: 'Traces', icon: 'traces', href: '#/traces', path: '/traces' },
  { label: 'Analytics', icon: 'analytics', href: '#/analytics', path: '/analytics' },
  { label: 'Errors', icon: 'errors', href: '#/errors', path: '/errors' },
  { label: 'Projects', icon: 'projects', href: '#/projects', path: '/projects' },
  { label: 'Docs', icon: 'docs', href: '#/docs', path: '/docs' },
];

function isActive(path: string, itemPath: string): boolean {
  if (itemPath === '/traces') return path === '/' || path.startsWith('/traces');
  if (itemPath === '/docs') return path === '/docs' || path.startsWith('/docs/');
  return path === itemPath;
}

export default function Sidebar({
  hash,
  dbPath = '',
  statusOk = false,
  statusLabel,
  lastRefresh = null,
  collapsed,
}: SidebarProps) {
  const [now, setNow] = useState(() => Date.now());
  const path = hash.replace(/^#/, '').split('?')[0] || '/';

  useEffect(() => {
    const interval = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(interval);
  }, []);

  return (
    <aside className={collapsed ? 'app-sidebar collapsed' : 'app-sidebar'}>
      <nav className="sidebar-nav" aria-label="Main navigation">
        {!collapsed && <div className="sidebar-label">Workspace</div>}
        {NAV_ITEMS.map((item) => {
          const active = isActive(path, item.path);
          return (
            <a
              key={item.path}
              className={active ? 'nav-link active' : 'nav-link'}
              href={item.href}
              title={collapsed ? item.label : undefined}
              aria-label={collapsed ? item.label : undefined}
              aria-current={active ? 'page' : undefined}
            >
              <Icon name={item.icon} size={16} className="nav-glyph" />
              {!collapsed && <span className="nav-text">{item.label}</span>}
            </a>
          );
        })}
      </nav>

      <div className="sidebar-footer">
        {dbPath && (
          <div className="sidebar-db mono" title={dbPath}>
            {collapsed ? 'DB' : dbPath}
          </div>
        )}
        <div className="sidebar-status mono">
          {statusLabel ?? (statusOk ? 'ok' : 'offline')}
          {!collapsed && ` · refreshed ${relativeTime(lastRefresh, now)}`}
        </div>
      </div>
    </aside>
  );
}
