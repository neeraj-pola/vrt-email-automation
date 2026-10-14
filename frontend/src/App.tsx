import { Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./context/AuthContext";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import LeadDetail from "./pages/LeadDetail";
import Analytics from "./pages/Analytics";
import ModifyDraft from "./pages/ModifyDraft";

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { session, loading, authEnabled } = useAuth();
  if (!authEnabled) return <>{children}</>; // local/mock mode: auth not configured yet
  if (loading) return <div className="empty-state">Loading…</div>;
  if (!session) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

function RedirectIfAuthed({ children }: { children: React.ReactNode }) {
  const { session, loading, authEnabled } = useAuth();
  if (!authEnabled || loading) return <>{children}</>;
  if (session) return <Navigate to="/" replace />;
  return <>{children}</>;
}

function AppRoutes() {
  return (
    <Routes>
      <Route
        path="/login"
        element={
          <RedirectIfAuthed>
            <Login />
          </RedirectIfAuthed>
        }
      />
      <Route
        path="/"
        element={
          <RequireAuth>
            <Dashboard />
          </RequireAuth>
        }
      />
      <Route
        path="/leads/:id"
        element={
          <RequireAuth>
            <LeadDetail />
          </RequireAuth>
        }
      />
      <Route
        path="/analytics"
        element={
          <RequireAuth>
            <Analytics />
          </RequireAuth>
        }
      />
      <Route
        path="/modify-draft"
        element={
          <RequireAuth>
            <ModifyDraft />
          </RequireAuth>
        }
      />
    </Routes>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <AppRoutes />
    </AuthProvider>
  );
}
