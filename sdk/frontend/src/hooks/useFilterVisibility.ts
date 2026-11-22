import { useEffect, useState } from 'react';

const VISIBILITY_KEY = 'tracekit-filters-visible';

function readVisible(): boolean {
  try {
    const stored = window.localStorage.getItem(VISIBILITY_KEY);
    return stored === null ? true : stored === '1';
  } catch {
    return true;
  }
}

export function useFilterVisibility() {
  const [visible, setVisible] = useState(readVisible);

  useEffect(() => {
    try {
      window.localStorage.setItem(VISIBILITY_KEY, visible ? '1' : '0');
    } catch {
      return;
    }
  }, [visible]);

  return { visible, setVisible };
}
