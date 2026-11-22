import { useEffect, useRef, useState } from 'react';
import type { Theme } from '../hooks/useTheme';
import { copyToClipboard, obscureToken } from '../utils/auth';

interface HeaderProps {
  token: string;
  tabLabel: string;
  theme: Theme;
  onThemeChange: (theme: Theme) => void;
  onRefresh: () => void;
  showRefresh: boolean;
}

const TOKEN_KEY = 'tracekit-token-visible';
const THEME_CLIP_LEFT = 'tracekit-theme-left';
const THEME_CLIP_RIGHT = 'tracekit-theme-right';

function readFlag(key: string): boolean {
  try {
    return window.localStorage.getItem(key) === '1';
  } catch {
    return false;
  }
}

function writeFlag(key: string, value: boolean): void {
  try {
    window.localStorage.setItem(key, value ? '1' : '0');
  } catch {
    return;
  }
}

export default function Header({
  token,
  tabLabel,
  theme,
  onThemeChange,
  onRefresh,
  showRefresh,
}: HeaderProps) {
  const [tokenVisible, setTokenVisible] = useState(() => readFlag(TOKEN_KEY));
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    writeFlag(TOKEN_KEY, tokenVisible);
  }, [tokenVisible]);

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    };
  }, []);

  const handleCopy = async () => {
    const ok = await copyToClipboard(token);
    setCopied(ok);
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => setCopied(false), 1800);
  };

  return (
    <header className="app-header">
      <div className="app-header-inner">
        <nav className="breadcrumb" aria-label="Breadcrumb">
          <span>Workspace</span>
          <span className="breadcrumb-separator" aria-hidden="true">/</span>
          <strong>{tabLabel}</strong>
        </nav>
        <div className="app-header-actions">
          <div className="token-pill mono" title="Dashboard access token">
            <span className="token-pill-label">Token</span>
            <button
              type="button"
              className="token-pill-value"
              onClick={() => setTokenVisible((visible) => !visible)}
              aria-pressed={tokenVisible}
              title={tokenVisible ? 'Hide token' : 'Show token'}
            >
              {tokenVisible ? token || 'not set' : obscureToken(token)}
            </button>
            <button type="button" className="token-pill-copy" onClick={() => void handleCopy()}>
              {copied ? 'copied' : 'copy'}
            </button>
          </div>
          {showRefresh && (
            <button type="button" className="button button-small" onClick={onRefresh}>
              Refresh
            </button>
          )}
          <button
            type="button"
            className="theme-toggle-button"
            aria-label="Toggle theme"
            aria-pressed={theme === 'dark'}
            title={theme === 'dark' ? 'Switch to light' : 'Switch to dark'}
            onClick={() => onThemeChange(theme === 'dark' ? 'light' : 'dark')}
          >
            <svg className="theme-toggle-icon theme-toggle-split" width="20" height="20" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <defs>
                <clipPath id={THEME_CLIP_LEFT}>
                  <rect x="0" y="0" width="12" height="24" />
                </clipPath>
                <clipPath id={THEME_CLIP_RIGHT}>
                  <rect x="12" y="0" width="12" height="24" />
                </clipPath>
              </defs>
              <g clipPath={`url(#${THEME_CLIP_LEFT})`}>
                <circle cx="12" cy="12" r="6.5" fill="currentColor" />
                <g stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
                  <path d="M12 2v2.4M12 19.6V22M2 12h2.4M2.9 4.9 4.6 6.6M2.9 19.1 4.6 17.4" />
                </g>
              </g>
              <g clipPath={`url(#${THEME_CLIP_RIGHT})`}>
                <path d="M18.8 15.8A7.2 7.2 0 0 1 11 5.2a7.2 7.2 0 1 0 7.8 10.6Z" fill="currentColor" />
              </g>
              <line x1="12" y1="4" x2="12" y2="20" stroke="var(--surface)" strokeWidth="1.2" />
            </svg>
          </button>
        </div>
      </div>
    </header>
  );
}
