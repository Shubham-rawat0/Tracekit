let token = '';

const search = new URLSearchParams(window.location.search);
if (search.has('token')) {
  token = search.get('token') ?? '';
  search.delete('token');
  const query = search.toString();
  const nextUrl = `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`;
  window.history.replaceState(window.history.state, '', nextUrl);
}

export function getToken(): string {
  return token;
}

export function setToken(value: string): void {
  token = value;
}

export function getAuthHeaders(): Record<string, string> {
  return {
    Authorization: `Bearer ${token}`,
    Accept: 'application/json',
  };
}

export function obscureToken(value: string = token, visible = 4): string {
  if (!value) return 'not set';
  if (value.length <= visible * 2) return '•'.repeat(Math.max(4, value.length));
  return `${value.slice(0, visible)}${'•'.repeat(6)}${value.slice(-visible)}`;
}

export async function copyToClipboard(value: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return true;
    }
    const area = document.createElement('textarea');
    area.value = value;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.top = '-1000px';
    document.body.appendChild(area);
    area.select();
    const copied = document.execCommand('copy');
    document.body.removeChild(area);
    return copied;
  } catch {
    return false;
  }
}
