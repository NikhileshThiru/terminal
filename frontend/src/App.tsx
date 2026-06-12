import { Header } from './chrome/Header';
import { ModalProvider } from './chrome/ModalContext';
import { NavigationProvider, useNavigation } from './chrome/NavigationContext';
import { ReasoningProvider } from './chrome/ReasoningContext';
import { SelectionProvider } from './chrome/SelectionContext';
import { SidebarLayout } from './chrome/SidebarLayout';
import { AutonomousPage } from './pages/AutonomousPage';
import { CopilotPage } from './pages/CopilotPage';
import { Dashboard } from './pages/Dashboard';
import { EvalPage } from './pages/EvalPage';
import { PortfolioPage } from './pages/PortfolioPage';

export default function App() {
  return (
    <SelectionProvider initial="AAPL">
      <ReasoningProvider>
        <NavigationProvider initial="dashboard">
          <ModalProvider>
            <main className="bg-bg text-text font-mono flex h-screen flex-col overflow-hidden">
              <Header />
              <SidebarLayout>
                <PageRouter />
              </SidebarLayout>
            </main>
          </ModalProvider>
        </NavigationProvider>
      </ReasoningProvider>
    </SelectionProvider>
  );
}

function PageRouter() {
  const { page } = useNavigation();
  switch (page) {
    case 'dashboard':
      return <Dashboard />;
    case 'portfolio':
      return <PortfolioPage />;
    case 'autonomous':
      return <AutonomousPage />;
    case 'eval':
      return <EvalPage />;
    case 'copilot':
      return <CopilotPage />;
  }
}
