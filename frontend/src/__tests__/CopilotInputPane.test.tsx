import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ModalProvider } from '../chrome/ModalContext';
import { ReasoningProvider } from '../chrome/ReasoningContext';
import { SelectionProvider } from '../chrome/SelectionContext';
import { CopilotInputPane } from '../panes/CopilotInputPane';

function renderPane() {
  return render(
    <SelectionProvider initial="AAPL">
      <ReasoningProvider>
        <ModalProvider>
          <CopilotInputPane />
        </ModalProvider>
      </ReasoningProvider>
    </SelectionProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('CopilotInputPane', () => {
  it('renders the textarea and a disabled run button by default', () => {
    globalThis.fetch = vi.fn();
    renderPane();
    expect(screen.getByLabelText(/thesis idea/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /run/i })).toBeDisabled();
  });

  it('enables run once enough text is entered', async () => {
    globalThis.fetch = vi.fn();
    const user = userEvent.setup();
    renderPane();
    await user.type(screen.getByLabelText(/thesis idea/i), 'AAPL looks strong for earnings');
    expect(screen.getByRole('button', { name: /run/i })).not.toBeDisabled();
  });

  it('offers preset prompts seeded with the selected symbol', async () => {
    globalThis.fetch = vi.fn();
    const user = userEvent.setup();
    renderPane();
    const preset = screen.getByRole('button', { name: /AAPL beats earnings this quarter/i });
    await user.click(preset);
    expect(screen.getByLabelText(/thesis idea/i)).toHaveValue('AAPL beats earnings this quarter');
    expect(screen.getByRole('button', { name: /run/i })).not.toBeDisabled();
  });
});
