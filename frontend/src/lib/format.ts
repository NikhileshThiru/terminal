/** Shared formatting helpers. One home for the little functions that were
 * previously copy-pasted per pane. */

/** "14:05" local wall-clock from an ISO timestamp. */
export function fmtHM(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const pad = (n: number) => n.toString().padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** "14:05:09" local wall-clock from an ISO timestamp (null-tolerant). */
export function fmtHMS(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const pad = (n: number) => n.toString().padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** Compact "now / 42s / 5m / 3h / 2d" relative label from an epoch-ms instant. */
export function relTime(ms: number | null): string {
  if (ms === null) return '—';
  const secs = Math.floor((Date.now() - ms) / 1000);
  if (!Number.isFinite(secs) || secs < 0) return '—';
  if (secs < 2) return 'now';
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  return `${Math.floor(hrs / 24)}d`;
}

/** relTime for ISO strings. */
export function relTimeIso(iso: string | null): string {
  if (!iso) return '—';
  const t = Date.parse(iso);
  return Number.isNaN(t) ? '—' : relTime(t);
}

/** "-$123" / "$1,234" — whole-dollar amounts with sign folded into the $. */
export function fmtUsd(n: number, digits = 0): string {
  const sign = n < 0 ? '-' : '';
  return `${sign}$${Math.abs(n).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

/** "+1.23%" / "-0.45%" with explicit sign. */
export function fmtPct(n: number, digits = 2): string {
  return `${n >= 0 ? '+' : ''}${n.toFixed(digits)}%`;
}

/** "$4.34T" / "$120B" market-cap style compaction. */
export function fmtCompactUsd(usd: number): string {
  if (usd >= 1e12) return `$${(usd / 1e12).toFixed(2)}T`;
  if (usd >= 1e9) return `$${(usd / 1e9).toFixed(2)}B`;
  if (usd >= 1e6) return `$${(usd / 1e6).toFixed(0)}M`;
  return `$${usd.toLocaleString()}`;
}

/** "today 14:05" / "yesterday 09:12" / "2026-06-02 16:20". */
export function fmtDateTimeSmart(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  const isYesterday = d.toDateString() === yesterday.toDateString();
  const time = fmtHM(iso);
  if (sameDay) return `today ${time}`;
  if (isYesterday) return `yesterday ${time}`;
  return `${d.toISOString().slice(0, 10)} ${time}`;
}

/** Truncate with a single trailing ellipsis character. */
export function truncate(s: string, max: number): string {
  if (s.length <= max) return s;
  return s.slice(0, max - 1).trimEnd() + '…';
}
