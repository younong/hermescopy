import { useMemo } from "react";
import { SelectionSwitcher } from "@nous-research/ui/ui/components/selection-switcher";
import { AuthWidget } from "@/components/AuthWidget";
import { PageHeaderProvider } from "@/contexts/PageHeaderProvider";
import { ProfileProvider } from "@/contexts/ProfileProvider";
import GuiChatPage from "@/pages/GuiChatPage";
import { useTheme } from "@/themes";

export default function StandaloneGuiChatPage() {
  const { theme } = useTheme();
  const headerActions = useMemo(() => <AuthWidget variant="header" />, []);

  return (
    <ProfileProvider>
      <div
        data-layout-variant={theme.layoutVariant ?? "standard"}
        className="flex h-dvh max-h-dvh min-h-0 flex-col overflow-hidden bg-background-base text-text-primary antialiased"
      >
        <SelectionSwitcher />
        <PageHeaderProvider pluginTabs={[]}>
          <div className="flex min-h-0 min-w-0 flex-1 flex-col px-3 pb-0 pt-1 sm:px-6 sm:pt-2 lg:pt-4">
            <GuiChatPage
              headerActions={headerActions}
              showTerminalChatAction={false}
            />
          </div>
        </PageHeaderProvider>
      </div>
    </ProfileProvider>
  );
}
