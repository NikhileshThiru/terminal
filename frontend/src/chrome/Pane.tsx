import type { ReactNode } from 'react';

import { useModal } from './ModalContext';

interface PaneProps {
  title: string;
  /** Optional inline label shown to the right of the title (e.g. symbol). */
  subtitle?: ReactNode;
  /** Optional render of an expanded "deep-dive" view inside a modal. */
  expand?: { title: string; content: ReactNode };
  /** Optional className applied to the body region. */
  bodyClassName?: string;
  children: ReactNode;
}

/**
 * Bloomberg-style pane shell. Compact header with the pane name, an optional
 * subtitle (e.g. the selected symbol), and an optional "expand" button that
 * opens the full deep-dive view in a modal.
 */
export function Pane({ title, subtitle, expand, bodyClassName, children }: PaneProps) {
  const modal = useModal();
  const handleExpand = expand
    ? () => modal.open({ title: expand.title, content: expand.content })
    : undefined;

  return (
    <section className="bg-bg-elevated border-border flex h-full min-h-0 flex-col overflow-hidden rounded border">
      <header className="bg-bg-elevated-2 border-border flex shrink-0 items-center justify-between border-b px-3 py-1.5">
        <div className="flex items-center gap-2">
          <h2 className="text-text-muted text-[10px] font-semibold tracking-[0.12em] uppercase">
            {title}
          </h2>
          {subtitle && <div className="text-text text-xs">{subtitle}</div>}
        </div>
        {handleExpand && (
          <button
            type="button"
            onClick={handleExpand}
            className="text-text-dim hover:text-text cursor-pointer text-xs leading-none"
            aria-label={`Expand ${title}`}
            title="Expand"
          >
            ⤢
          </button>
        )}
      </header>
      <div className={`min-h-0 flex-1 overflow-auto ${bodyClassName ?? 'p-2'}`}>{children}</div>
    </section>
  );
}
