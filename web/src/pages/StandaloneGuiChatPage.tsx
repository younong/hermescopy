import GuiChatPage from "@/pages/GuiChatPage";
import { ProfileProvider } from "@/contexts/ProfileProvider";

export default function StandaloneGuiChatPage() {
  return (
    <ProfileProvider>
      <div className="relative flex h-dvh max-h-dvh min-h-0 overflow-hidden bg-white text-[#202124] antialiased">
        <GuiChatPage />
      </div>
    </ProfileProvider>
  );
}
