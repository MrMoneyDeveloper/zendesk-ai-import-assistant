import axios from "axios";

const API = "http://127.0.0.1:8000/api";

export const generateData = async (prompt) => {
  const res = await axios.post(`${API}/generate`, { prompt });
  return res.data;
};