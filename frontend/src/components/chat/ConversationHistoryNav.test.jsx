import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import ConversationHistoryNav from "./ConversationHistoryNav";


const conversations = [
  {
    conversation_id: "CHAT-001",
    title: "Claims Routing Review",
    title_source: "ai",
    updated_at: "2026-07-23T12:00:00Z",
    message_preview: "Create a claims routing trigger",
    message_count: 4,
    batches: [
      {
        batch_id: "BATCH-001",
        status: "preview_ready",
        created_at: "2026-07-23T12:00:00Z",
        prompt_preview: "Create claims routing",
      },
      {
        batch_id: "BATCH-002",
        status: "failed",
        created_at: "2026-07-23T12:05:00Z",
        prompt_preview: "Add broker escalation",
      },
    ],
  },
];


test("expands a named chat and opens a nested batch", () => {
  const onSelectConversation = vi.fn();
  const onSelectBatch = vi.fn();
  render(
    <ConversationHistoryNav
      conversations={conversations}
      onSelectConversation={onSelectConversation}
      onSelectBatch={onSelectBatch}
    />
  );

  expect(screen.getByText("Claims Routing Review")).toBeInTheDocument();
  expect(screen.getByLabelText("AI-assigned title")).toBeInTheDocument();
  expect(screen.queryByText("Add broker escalation")).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Expand Claims Routing Review" }));
  fireEvent.click(screen.getByText("Add broker escalation"));
  expect(onSelectBatch).toHaveBeenCalledWith("CHAT-001", "BATCH-002");

  fireEvent.click(screen.getByTitle("Continue Claims Routing Review"));
  expect(onSelectConversation).toHaveBeenCalledWith("CHAT-001");
});


test("filters chats by nested batch content", () => {
  render(
    <ConversationHistoryNav
      conversations={conversations}
      search="broker escalation"
      onSearchChange={() => {}}
    />
  );

  expect(screen.getByText("Claims Routing Review")).toBeInTheDocument();
});
