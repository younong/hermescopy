import type { ReactNode } from "react";
import { GuiChatShell } from "@/features/gui-chat/components/GuiChatShell";

interface GuiChatPageProps {
  headerActions?: ReactNode;
  showTerminalChatAction?: boolean;
}

export default function GuiChatPage(props: GuiChatPageProps) {
  return <GuiChatShell {...props} />;
}
