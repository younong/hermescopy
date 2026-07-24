import { Navigate, Route, Routes } from "react-router-dom";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import App from "@/App";
import { ForcedPasswordChangePage } from "@/components/ForcedPasswordChangePage";
import { SystemActionsProvider } from "@/contexts/SystemActions";
import { useDashboardAuthIdentity } from "@/lib/useDashboardAuthIdentity";
import StandaloneGuiChatPage from "@/pages/StandaloneGuiChatPage";

export default function AuthenticatedApp() {
  const authIdentity = useDashboardAuthIdentity();

  if (authIdentity.authRequired && authIdentity.loading) {
    return (
      <div
        aria-busy="true"
        className="flex min-h-dvh items-center justify-center bg-white text-[#202124]"
      >
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  if (authIdentity.authRequired && authIdentity.error) {
    return (
      <div className="flex min-h-dvh items-center justify-center bg-background-base px-4 text-text-primary">
        <div className="max-w-md text-center">
          <h1 className="text-lg font-semibold">Authentication unavailable</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            The dashboard could not verify your session. Refresh the page or try again shortly.
          </p>
        </div>
      </div>
    );
  }

  if (authIdentity.authRequired && authIdentity.authMe?.must_change_password) {
    return <ForcedPasswordChangePage />;
  }

  if (authIdentity.authRequired && authIdentity.authMe?.role === "member") {
    return (
      <Routes>
        <Route path="/chat-gui" element={<StandaloneGuiChatPage />} />
        <Route path="*" element={<Navigate to="/chat-gui" replace />} />
      </Routes>
    );
  }

  return (
    <SystemActionsProvider>
      <App />
    </SystemActionsProvider>
  );
}
