import "./style.css";
import { React, registry } from "./runtime";
import { GuiChatKanbanPane } from "./components/GuiChatKanbanPane";

function Kanban() {
  return <div data-gui-chat className="kanban-plugin-root"><GuiChatKanbanPane /></div>;
}

registry.register("kanban", Kanban);
registry.registerWorkspace("kanban", "kanban", Kanban);

export { Kanban };
