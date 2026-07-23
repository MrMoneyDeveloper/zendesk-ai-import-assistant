import axios from "axios";

const API_ORIGIN = String(import.meta.env.VITE_API_BASE_URL || "")
  .trim()
  .replace(/\/+$/, "");
const API_ROOT = `${API_ORIGIN}/api`;
const IMPORT_ASSISTANT_ROOT = `${API_ROOT}/import-assistant`;

export async function testApis() {
  const res = await axios.get(`${API_ROOT}/test-apis`);
  return res.data;
}

export async function generateBatch(payload) {
  const res = await axios.post(`${IMPORT_ASSISTANT_ROOT}/generate-async`, payload);
  return res.data;
}

export async function getJob(batchId) {
  const res = await axios.get(`${IMPORT_ASSISTANT_ROOT}/jobs/${batchId}`);
  return res.data;
}

export async function listJobs(limit = 20) {
  const res = await axios.get(`${IMPORT_ASSISTANT_ROOT}/jobs`, {
    params: { limit },
  });
  return res.data;
}

export async function listConversations(limit = 60) {
  const res = await axios.get(`${IMPORT_ASSISTANT_ROOT}/conversations`, {
    params: { limit },
  });
  return res.data;
}

export async function getConversation(conversationId) {
  const res = await axios.get(`${IMPORT_ASSISTANT_ROOT}/conversations/${conversationId}`);
  return res.data;
}

export async function createConversation(payload) {
  const res = await axios.post(`${IMPORT_ASSISTANT_ROOT}/conversations`, payload);
  return res.data;
}

export async function appendConversationMessage(conversationId, payload) {
  const res = await axios.post(
    `${IMPORT_ASSISTANT_ROOT}/conversations/${conversationId}/messages`,
    payload
  );
  return res.data;
}

export async function updateConversation(conversationId, payload) {
  const res = await axios.patch(
    `${IMPORT_ASSISTANT_ROOT}/conversations/${conversationId}`,
    payload
  );
  return res.data;
}

export async function controlBatchJob(batchId, payload) {
  const res = await axios.post(`${IMPORT_ASSISTANT_ROOT}/jobs/${batchId}/control`, payload);
  return res.data;
}

export async function getCheckpoints(batchId) {
  const res = await axios.get(`${IMPORT_ASSISTANT_ROOT}/jobs/${batchId}/checkpoints`);
  return res.data;
}

export async function decideCheckpoint(batchId, checkpointId, payload) {
  const res = await axios.post(
    `${IMPORT_ASSISTANT_ROOT}/jobs/${batchId}/checkpoints/${checkpointId}/decision`,
    payload
  );
  return res.data;
}

export async function getPreview(batchId) {
  const res = await axios.get(`${IMPORT_ASSISTANT_ROOT}/preview/${batchId}`);
  return res.data;
}

export async function approveBatch(payload) {
  const res = await axios.post(`${IMPORT_ASSISTANT_ROOT}/approve`, payload);
  return res.data;
}

export async function deployBatch(payload) {
  const res = await axios.post(`${IMPORT_ASSISTANT_ROOT}/deploy`, payload);
  return res.data;
}

export async function getIntegrationsStatus() {
  const res = await axios.get(`${IMPORT_ASSISTANT_ROOT}/integrations/status`);
  return res.data;
}

export async function validateZendeskCredentials(payload) {
  const res = await axios.post(`${IMPORT_ASSISTANT_ROOT}/zendesk/validate`, payload);
  return res.data;
}

export async function getZendeskContext(payload) {
  const res = await axios.post(`${IMPORT_ASSISTANT_ROOT}/zendesk/context`, payload);
  return res.data;
}

export async function askContextQuestion(payload) {
  const res = await axios.post(`${IMPORT_ASSISTANT_ROOT}/context-question`, payload);
  return res.data;
}

export async function checkZendeskHelpCenterReadiness(payload) {
  const res = await axios.post(
    `${IMPORT_ASSISTANT_ROOT}/zendesk/help-center/readiness`,
    payload
  );
  return res.data;
}

export async function extractAttachment(file) {
  const formData = new FormData();
  formData.append("file", file);
  const res = await axios.post(`${IMPORT_ASSISTANT_ROOT}/attachments/extract`, formData, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return res.data;
}
