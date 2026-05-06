import { MessageSquarePlus, Search, MoreHorizontal, FolderOpen } from "lucide-react";
import { Button } from "../ui/button";

const sidebarItems = [
  "Search chats",
  "Codex",
  "More",
  "Projects",
  "Zendesk Assistant",
  "Calc Project",
];

export default function Sidebar() {
  return (
    <aside className="hidden w-72 flex-col border-r border-slate-800 bg-[#161a21] p-3 text-slate-300 lg:flex">
      <div className="mb-3 px-2 text-lg font-semibold text-white">ChatGPT</div>
      <Button variant="ghost" className="mb-3 w-full justify-start gap-2 text-sm">
        <MessageSquarePlus size={16} />
        New chat
      </Button>
      <div className="space-y-1">
        {sidebarItems.map((item) => (
          <button
            key={item}
            type="button"
            className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm hover:bg-slate-800"
          >
            {item === "Search chats" && <Search size={15} />}
            {item === "Projects" && <FolderOpen size={15} />}
            {item === "More" && <MoreHorizontal size={15} />}
            {item !== "Search chats" && item !== "Projects" && item !== "More" && <span className="h-2 w-2 rounded-full bg-slate-500" />}
            <span>{item}</span>
          </button>
        ))}
      </div>
      <div className="mt-auto rounded-md border border-slate-700 bg-slate-900/50 p-3 text-xs text-slate-400">
        AI Zendesk Import Assistant
      </div>
    </aside>
  );
}
