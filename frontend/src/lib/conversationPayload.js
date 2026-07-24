export const CONVERSATION_MESSAGE_LIMIT = 12000;

export function compactConversationMessage(
  value,
  maxLength = CONVERSATION_MESSAGE_LIMIT,
) {
  const text = String(value ?? "");
  const limit = Math.max(Number(maxLength) || CONVERSATION_MESSAGE_LIMIT, 1);
  if (text.length <= limit) return text;

  const marker = "\n\n[Middle omitted from chat history; the full request was still sent for processing.]\n\n";
  if (marker.length >= limit) return text.slice(0, limit);

  const available = limit - marker.length;
  const headLength = Math.ceil(available * 0.72);
  const tailLength = available - headLength;
  return `${text.slice(0, headLength)}${marker}${text.slice(-tailLength)}`;
}
