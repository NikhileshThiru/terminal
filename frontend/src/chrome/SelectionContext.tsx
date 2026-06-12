import { createContext, useContext, useMemo, useState, type ReactNode } from 'react';

interface SelectionApi {
  selectedSymbol: string;
  setSelectedSymbol: (s: string) => void;
}

const SelectionCtx = createContext<SelectionApi | null>(null);

export function SelectionProvider({
  children,
  initial = 'AAPL',
}: {
  children: ReactNode;
  initial?: string;
}) {
  const [selectedSymbol, setSelectedSymbolRaw] = useState(initial);
  const api = useMemo<SelectionApi>(
    () => ({
      selectedSymbol,
      setSelectedSymbol: (s: string) => setSelectedSymbolRaw(s.toUpperCase()),
    }),
    [selectedSymbol],
  );
  return <SelectionCtx.Provider value={api}>{children}</SelectionCtx.Provider>;
}

export function useSelection(): SelectionApi {
  const ctx = useContext(SelectionCtx);
  if (!ctx) throw new Error('useSelection must be used inside <SelectionProvider>');
  return ctx;
}
