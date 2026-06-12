import type { ReactNode } from 'react';

import { WatchlistPane } from '../panes/WatchlistPane';
import { useNavigation, type PageKey } from './NavigationContext';

interface NavItem {
  key: PageKey;
  label: string;
  icon: string;
  description?: string;
}

const NAV: NavItem[] = [
  { key: 'dashboard', label: 'Dashboard', icon: '◉', description: 'Live autonomous state' },
  { key: 'autonomous', label: 'Autonomous', icon: '⚙', description: 'Worker + catalysts' },
  { key: 'portfolio', label: 'Portfolio', icon: '◫', description: 'Paper accounts + equity' },
  { key: 'eval', label: 'Evaluation', icon: '◈', description: 'Per-bucket scoring' },
  {
    key: 'copilot',
    label: 'Ask the Agent',
    icon: '⌖',
    description: 'Run the trading agent on your own idea',
  },
];

/**
 * Sidebar layout: left rail with nav + persistent watchlist, main pane is
 * the current page. The watchlist stays mounted across pages so the
 * selected-symbol context (chart, chain) doesn't reset on every nav.
 */
export function SidebarLayout({ children }: { children: ReactNode }) {
  const { page, setPage } = useNavigation();

  return (
    <div className="bg-bg text-text flex min-h-0 flex-1 overflow-hidden">
      <aside
        className="border-border bg-bg-elevated flex shrink-0 flex-col border-r"
        style={{ width: 240 }}
      >
        <nav className="border-border shrink-0 border-b py-2">
          <ul className="space-y-0.5 px-2">
            {NAV.map((item) => {
              const active = item.key === page;
              return (
                <li key={item.key}>
                  <button
                    type="button"
                    onClick={() => setPage(item.key)}
                    className={`group flex w-full cursor-pointer items-center gap-2.5 rounded border-l-2 px-2 py-1.5 text-left transition-colors ${
                      active
                        ? 'bg-accent/10 text-accent border-accent'
                        : 'text-text-muted hover:bg-bg-elevated-2 hover:text-text border-transparent'
                    }`}
                    title={item.description}
                  >
                    <span className="w-4 text-center font-mono text-sm">{item.icon}</span>
                    <span className="text-[12px] tracking-wide">{item.label}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        </nav>
        <div className="min-h-0 flex-1 overflow-hidden">
          <WatchlistPane />
        </div>
      </aside>
      <section className="flex min-h-0 flex-1 flex-col overflow-hidden">{children}</section>
    </div>
  );
}
