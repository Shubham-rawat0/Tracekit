import { useEffect, useRef, useState } from 'react';
import { WiMoonAltWaningCrescent3 } from 'react-icons/wi';
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

  const isDark = theme === 'dark';

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
            aria-pressed={isDark}
            title={isDark ? 'Switch to light' : 'Switch to dark'}
            onClick={() => onThemeChange(isDark ? 'light' : 'dark')}
          >
            {isDark ? (
              <svg className="theme-toggle-icon theme-icon-sun" width="20" height="20" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <circle cx="12" cy="12" r="5" fill="currentColor" />
                <g stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
                  <path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M19.1 4.9l-1.8 1.8M6.7 17.3l-1.8 1.8" />
                </g>
              </svg>
            ) : (
              <WiMoonAltWaningCrescent3
                className="theme-toggle-icon theme-icon-moon"
                aria-hidden="true"
                focusable="false"
              />
            )}
          </button>
        </div>
      </div>
    </header>
  );
}
