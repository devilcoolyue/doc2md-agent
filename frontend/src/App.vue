<template>
  <div class="page-shell">
    <header class="hero">
      <h1 class="hero-title">Doc2MD Studio</h1>
      <p class="hero-subtitle">
        前后端分离架构下的文档转换工作台。上传 Word 文档，轮询任务进度，完成后直接下载并在线预览 Markdown。
      </p>
      <div class="chip-row">
        <span class="chip accent">前端: Vue 3 + Vite</span>
        <span class="chip">后端: FastAPI</span>
        <span class="chip">模式: 任务轮询</span>
      </div>
    </header>

    <FileUpload
      v-if="view === 'upload'"
      :providers="providers"
      :current-provider="currentProvider"
      :submitting="submitting"
      :error="requestError"
      @start="startTask"
    />

    <ConversionProgress
      v-else-if="view === 'processing'"
      :task="task"
      :stopping="stopping"
      :preview-html="markdownHtml"
      :preview-partial="previewPartial"
      @stop="requestStopTask"
      @reset="resetAll"
    />

    <ResultView
      v-else
      :markdown-html="markdownHtml"
      :usage="usage"
      :task-status="task.status"
      :download-url="downloadUrl"
      @reset="resetAll"
    />

    <TaskLogDrawer
      :visible="Boolean(taskId)"
      :open="logDrawerOpen"
      :task="task"
      :events="task.events || []"
      @toggle="logDrawerOpen = !logDrawerOpen"
      @close="logDrawerOpen = false"
    />
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { marked } from "marked";
import {
  createTask,
  getDownloadUrl,
  getPreview,
  getProviders,
  getTask,
  stopTask as stopTaskApi
} from "./api";
import FileUpload from "./components/FileUpload.vue";
import ConversionProgress from "./components/ConversionProgress.vue";
import ResultView from "./components/ResultView.vue";
import TaskLogDrawer from "./components/TaskLogDrawer.vue";

const providers = ref([]);
const currentProvider = ref("");
const task = ref({});
const taskId = ref("");
const usage = ref({});
const markdownHtml = ref("");
const previewPartial = ref(true);
const submitting = ref(false);
const stopping = ref(false);
const requestError = ref("");
const logDrawerOpen = ref(false);
let poller = null;

const view = computed(() => {
  if (!taskId.value) {
    return "upload";
  }
  if (task.value.status === "completed" || task.value.status === "stopped") {
    return "done";
  }
  return "processing";
});

const downloadUrl = computed(() => {
  if (!taskId.value) {
    return "#";
  }
  return getDownloadUrl(taskId.value);
});

async function loadProviders() {
  try {
    const data = await getProviders();
    providers.value = data.providers || [];
    currentProvider.value =
      data.current_provider || providers.value[0]?.name || "";
  } catch (error) {
    requestError.value = `加载提供商失败: ${error.message}`;
  }
}

async function startTask({ file, provider }) {
  requestError.value = "";
  submitting.value = true;
  try {
    const data = await createTask(file, provider);
    taskId.value = data.task_id;
    task.value = {
      task_id: data.task_id,
      status: "queued",
      progress: 0,
      message: "任务已创建"
    };
    usage.value = {};
    markdownHtml.value = "";
    previewPartial.value = true;
    stopping.value = false;
    logDrawerOpen.value = true;
    startPolling();
  } catch (error) {
    requestError.value = `提交任务失败: ${error.message}`;
    resetAll(false);
  } finally {
    submitting.value = false;
  }
}

function stopPolling() {
  if (poller) {
    clearInterval(poller);
    poller = null;
  }
}

function startPolling() {
  stopPolling();
  pollTask();
  poller = setInterval(pollTask, 1200);
}

async function requestStopTask() {
  if (!taskId.value) {
    return;
  }
  if (task.value.status === "completed" || task.value.status === "failed" || task.value.status === "stopped") {
    return;
  }
  if (stopping.value || task.value.status === "stopping") {
    return;
  }

  const confirmed = window.confirm("确认停止当前任务吗？确认后会打包当前已生成的 Markdown 内容。");
  if (!confirmed) {
    return;
  }

  requestError.value = "";
  stopping.value = true;
  try {
    const data = await stopTaskApi(taskId.value);
    task.value = {
      ...task.value,
      status: data.status || "stopping",
      message: data.message || "已收到停止请求，正在结束任务"
    };
    await pollTask();
  } catch (error) {
    requestError.value = `停止任务失败: ${error.message}`;
  } finally {
    stopping.value = false;
  }
}

function stripHtml(text) {
  if (!text) {
    return "";
  }
  const div = document.createElement("div");
  div.innerHTML = text;
  return div.textContent || div.innerText || "";
}

function slugifyHeading(title) {
  return title
    .toLowerCase()
    .replace(/[^\w\u4e00-\u9fff\s-]/g, "")
    .trim()
    .replace(/\s+/g, "-");
}

function withApiBase(path) {
  if (!path) {
    return "";
  }
  if (/^https?:\/\//i.test(path)) {
    return path;
  }
  const base = import.meta.env.VITE_API_BASE || "";
  if (!base) {
    return path;
  }
  const trimmedBase = base.replace(/\/$/, "");
  const trimmedPath = path.replace(/^\//, "");
  return `${trimmedBase}/${trimmedPath}`;
}

function resolvePreviewImageUrl(href, assetBaseUrl) {
  if (!href) {
    return "";
  }
  const value = href.trim();
  if (/^(https?:\/\/|data:|blob:|#)/i.test(value)) {
    return value;
  }
  const normalizedPath = value.replace(/^\.?\//, "");
  const normalizedBase = withApiBase(assetBaseUrl).replace(/\/$/, "");
  return `${normalizedBase}/${normalizedPath}`;
}

function renderMarkdown(content, assetBaseUrl) {
  const headingCounter = new Map();
  const renderer = new marked.Renderer();

  renderer.heading = (text, level) => {
    const rawHeading = stripHtml(text);
    const baseId = slugifyHeading(rawHeading) || `heading-${level}`;
    const index = (headingCounter.get(baseId) || 0) + 1;
    headingCounter.set(baseId, index);
    const id = index === 1 ? baseId : `${baseId}-${index}`;
    return `<h${level} id="${id}">${text}</h${level}>`;
  };

  renderer.image = (href, title, text) => {
    const src = resolvePreviewImageUrl(href, assetBaseUrl);
    const titleAttr = title ? ` title="${title}"` : "";
    return `<img src="${src}" alt="${text || ""}"${titleAttr} loading="lazy" />`;
  };

  return marked.parse(content || "", { renderer });
}

async function refreshPreview(taskData) {
  if (!taskId.value) {
    return;
  }
  try {
    const preview = await getPreview(taskId.value);
    usage.value = preview.usage || taskData.usage || usage.value || {};
    markdownHtml.value = renderMarkdown(
      preview.content || "",
      preview.asset_base_url || `/api/tasks/${taskId.value}/assets`
    );
    previewPartial.value = Boolean(preview.partial);
  } catch (error) {
    const statusCode = Number(error?.response?.status || 0);
    if (statusCode === 404 || statusCode === 409) {
      return;
    }
    requestError.value = `预览刷新失败: ${error.message}`;
  }
}

async function pollTask() {
  if (!taskId.value) {
    return;
  }

  try {
    const taskData = await getTask(taskId.value);
    task.value = taskData;

    if (
      taskData.status === "running" ||
      taskData.status === "stopping" ||
      taskData.status === "failed" ||
      taskData.status === "completed" ||
      taskData.status === "stopped"
    ) {
      await refreshPreview(taskData);
    }

    if (taskData.status === "completed" || taskData.status === "stopped") {
      stopPolling();
    }

    if (taskData.status === "failed") {
      stopPolling();
    }
  } catch (error) {
    requestError.value = `轮询失败: ${error.message}`;
    stopPolling();
  }
}

function resetAll(clearError = true) {
  stopPolling();
  logDrawerOpen.value = false;
  stopping.value = false;
  taskId.value = "";
  task.value = {};
  usage.value = {};
  markdownHtml.value = "";
  previewPartial.value = true;
  if (clearError) {
    requestError.value = "";
  }
}

onMounted(loadProviders);
onBeforeUnmount(stopPolling);
</script>
