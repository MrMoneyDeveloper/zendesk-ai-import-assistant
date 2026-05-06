import axios from "axios";

const API_ROOT = "/api";
const IMPORT_ASSISTANT_ROOT = `${API_ROOT}/import-assistant`;

export async function testApis() {
  const res = await axios.get(`${API_ROOT}/test-apis`);
  return res.data;
}

export async function generateBatch(payload) {
  const res = await axios.post(`${IMPORT_ASSISTANT_ROOT}/generate`, payload);
  return res.data;
}

export async function getJob(batchId) {
  const res = await axios.get(`${IMPORT_ASSISTANT_ROOT}/jobs/${batchId}`);
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
