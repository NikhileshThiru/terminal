/**
 * Canonical hex palette for canvas/SVG code (lightweight-charts can't read
 * CSS variables). MUST mirror the @theme tokens in tailwind.css.
 */
export const palette = {
  bg: '#0a0a0c',
  bgElevated: '#111114',
  bgElevated2: '#18181d',
  border: '#1f1f26',
  borderStrong: '#2c2c36',
  text: '#e6e6e9',
  textMuted: '#92929e',
  textDim: '#5c5c66',
  accent: '#ffb224',
  up: '#2bd576',
  down: '#ff4d6d',
  info: '#58a6ff',
  warning: '#ffd166',
} as const;

/** "rgba(...)" with alpha from a #rrggbb hex. */
export function withAlpha(hex: string, alpha: number): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
