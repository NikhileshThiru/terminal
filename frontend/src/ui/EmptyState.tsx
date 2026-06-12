/** Consistent empty-state copy block. Honest empties are a design feature
 * of this project (no fake zeros) — give them one voice. */
export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="animate-fade-in flex h-full min-h-16 flex-col items-center justify-center gap-1 p-4 text-center">
      <p className="text-text-muted text-xs">{title}</p>
      {hint && <p className="text-text-dim max-w-md text-[10px] leading-relaxed">{hint}</p>}
    </div>
  );
}
