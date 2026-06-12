import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

interface ModalState {
  title: string;
  content: ReactNode;
}

interface ModalApi {
  open: (state: ModalState) => void;
  close: () => void;
}

const ModalCtx = createContext<ModalApi | null>(null);

export function ModalProvider({ children }: { children: ReactNode }) {
  const [modal, setModal] = useState<ModalState | null>(null);

  const open = useCallback((state: ModalState) => setModal(state), []);
  const close = useCallback(() => setModal(null), []);

  useEffect(() => {
    if (!modal) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [modal, close]);

  return (
    <ModalCtx.Provider value={{ open, close }}>
      {children}
      {modal &&
        createPortal(
          <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm"
            onClick={close}
            role="dialog"
            aria-modal="true"
            aria-label={modal.title}
          >
            <div
              className="bg-bg-elevated border-border-strong relative max-h-[90vh] w-[min(1200px,95vw)] overflow-auto rounded border"
              onClick={(e) => e.stopPropagation()}
            >
              <header className="bg-bg-elevated-2 border-border sticky top-0 z-10 flex items-center justify-between border-b px-5 py-3">
                <h2 className="text-text text-sm font-semibold tracking-wider uppercase">
                  {modal.title}
                </h2>
                <button
                  type="button"
                  onClick={close}
                  className="text-text-muted hover:text-text cursor-pointer text-lg leading-none"
                  aria-label="Close"
                >
                  ×
                </button>
              </header>
              <div className="p-5">{modal.content}</div>
            </div>
          </div>,
          document.body,
        )}
    </ModalCtx.Provider>
  );
}

export function useModal(): ModalApi {
  const ctx = useContext(ModalCtx);
  if (!ctx) throw new Error('useModal must be used inside <ModalProvider>');
  return ctx;
}
