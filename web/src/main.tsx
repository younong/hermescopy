import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import "./index.css";
import AuthenticatedApp from "./AuthenticatedApp";
import { I18nProvider } from "./i18n";
import { exposePluginSDK } from "./plugins";
import { ThemeProvider } from "./themes";
import { HERMES_BASE_PATH } from "./lib/api";
import { DashboardAuthIdentityProvider } from "./lib/useDashboardAuthIdentity";
import PublicJoinApp, { isPublicJoinPath } from "./PublicJoinApp";

// Expose the plugin SDK before rendering so plugins loaded via <script>
// can access React, components, etc. immediately.
exposePluginSDK();

createRoot(document.getElementById("root")!).render(
  <BrowserRouter basename={HERMES_BASE_PATH || undefined}>
    <I18nProvider>
      <ThemeProvider>
        {isPublicJoinPath(window.location.pathname, HERMES_BASE_PATH) ? (
          <PublicJoinApp />
        ) : (
          <DashboardAuthIdentityProvider>
            <AuthenticatedApp />
          </DashboardAuthIdentityProvider>
        )}
      </ThemeProvider>
    </I18nProvider>
  </BrowserRouter>,
);
