import { describe, expect, test } from "vitest";

import {
  compactConversationMessage,
  CONVERSATION_MESSAGE_LIMIT,
} from "./conversationPayload";

describe("compactConversationMessage", () => {
  test("keeps messages that fit the API contract unchanged", () => {
    expect(compactConversationMessage("Create a claims trigger.")).toBe(
      "Create a claims trigger.",
    );
  });

  test("preserves the beginning and end of an oversized request", () => {
    const input = `START-${"x".repeat(15000)}-END`;
    const result = compactConversationMessage(input);

    expect(result).toHaveLength(CONVERSATION_MESSAGE_LIMIT);
    expect(result.startsWith("START-")).toBe(true);
    expect(result.endsWith("-END")).toBe(true);
    expect(result).toContain("Middle omitted from chat history");
  });
});
