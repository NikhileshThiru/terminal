import { createContext, useContext, useMemo, useState, type ReactNode } from 'react';

export type PageKey = 'dashboard' | 'autonomous' | 'portfolio' | 'eval' | 'copilot';

interface NavigationApi {
  page: PageKey;
  setPage: (p: PageKey) => void;
}

const Ctx = createContext<NavigationApi | null>(null);

export function NavigationProvider({
  children,
  initial = 'dashboard',
}: {
  children: ReactNode;
  initial?: PageKey;
}) {
  const [page, setPage] = useState<PageKey>(initial);
  const api = useMemo<NavigationApi>(() => ({ page, setPage }), [page]);
  return <Ctx.Provider value={api}>{children}</Ctx.Provider>;
}

export function useNavigation(): NavigationApi {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('useNavigation must be used inside <NavigationProvider>');
  return ctx;
}
