import axios from "axios";

const http = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || "",
  timeout: 120000
});

export async function getProviders() {
  const { data } = await http.get("/api/config/providers");
  return data;
}

export async function createTask(file, provider) {
  const form = new FormData();
  form.append("file", file);
  if (provider) {
    form.append("provider", provider);
  }

  const { data } = await http.post("/api/convert", form);
  return data;
}

export async function getTask(taskId) {
  const { data } = await http.get(`/api/tasks/${taskId}`);
  return data;
}

export async function stopTask(taskId) {
  const { data } = await http.post(`/api/tasks/${taskId}/stop`);
  return data;
}

export async function getPreview(taskId) {
  const { data } = await http.get(`/api/tasks/${taskId}/preview`);
  return data;
}

export function getDownloadUrl(taskId) {
  return `${import.meta.env.VITE_API_BASE || ""}/api/tasks/${taskId}/download`;
}
