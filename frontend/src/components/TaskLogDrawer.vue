<template>
  <button
    v-if="visible"
    type="button"
    class="log-toggle-btn"
    @click="$emit('toggle')"
  >
    {{ open ? "收起日志" : "查看日志" }}
    <span class="log-toggle-count">{{ events.length }}</span>
  </button>

  <aside
    v-if="visible"
    class="log-drawer"
    :class="{ open }"
    aria-label="任务日志"
  >
    <header class="log-drawer-header">
      <div>
        <h3 class="log-drawer-title">任务日志</h3>
        <p class="hint-text log-status">状态: {{ task.status || "--" }} · 共 {{ events.length }} 条</p>
      </div>
      <button class="log-close-btn" type="button" @click="$emit('close')">关闭</button>
    </header>

    <div ref="logListRef" class="log-drawer-list" v-if="displayedEvents.length">
      <div
        class="log-item"
        :class="eventRowClass(event)"
        v-for="(event, index) in displayedEvents"
        :key="`${event.timestamp || index}-${index}`"
      >
        <span class="log-time">{{ formatTime(event.timestamp) }}</span>
        <span class="log-type">{{ formatType(event.type) }}</span>
        <span class="log-scope" v-if="formatScope(event)">{{ formatScope(event) }}</span>
        <span class="log-message">{{ event.message }}</span>
      </div>
    </div>
    <p class="hint-text" v-else style="margin-top:10px;">等待任务日志...</p>
  </aside>
</template>

<script setup>
import { computed, nextTick, ref, watch } from "vue";

const props = defineProps({
  visible: {
    type: Boolean,
    default: false
  },
  open: {
    type: Boolean,
    default: false
  },
  task: {
    type: Object,
    default: () => ({})
  },
  events: {
    type: Array,
    default: () => []
  }
});

defineEmits(["toggle", "close"]);

const logListRef = ref(null);

const displayedEvents = computed(() => {
  return props.events.slice(-800);
});

function scrollToLatest() {
  if (!props.open) {
    return;
  }
  nextTick(() => {
    if (!logListRef.value) {
      return;
    }
    logListRef.value.scrollTop = logListRef.value.scrollHeight;
  });
}

watch(
  () => displayedEvents.value.length,
  () => {
    scrollToLatest();
  }
);

watch(
  () => {
    const last = displayedEvents.value[displayedEvents.value.length - 1];
    if (!last) {
      return "";
    }
    return `${last.timestamp || ""}-${last.message || ""}`;
  },
  () => {
    scrollToLatest();
  }
);

watch(
  () => props.open,
  (isOpen) => {
    if (isOpen) {
      scrollToLatest();
    }
  }
);

function formatTime(value) {
  if (!value) {
    return "--:--:--";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleTimeString("zh-CN", {
    hour12: false
  });
}

function formatType(type) {
  const value = String(type || "info");
  if (value.startsWith("llm_call_")) {
    return "LLM";
  }
  if (value.startsWith("chunk_")) {
    return "CHUNK";
  }
  if (value.startsWith("json_")) {
    return "JSON";
  }
  if (value.includes("error") || value.endsWith("failed")) {
    return "ERROR";
  }
  if (value.includes("warning") || value.endsWith("fallback")) {
    return "WARN";
  }
  if (value.startsWith("pipeline_")) {
    return "PIPE";
  }
  if (value.startsWith("preprocess_")) {
    return "PRE";
  }
  if (value.startsWith("analyze_")) {
    return "ANL";
  }
  if (value.startsWith("postprocess_")) {
    return "POST";
  }
  return "INFO";
}

function formatScope(event) {
  const section = event.section_label || event.section_heading || event.section_id || "";
  const chunkText =
    event.chunk_index && event.total_chunks
      ? `片段 ${event.chunk_index}/${event.total_chunks}`
      : "";
  if (chunkText && section) {
    return `${chunkText} · ${section}`;
  }
  if (chunkText) {
    return chunkText;
  }
  return section;
}

function eventRowClass(event) {
  const type = String(event.type || "");
  if (type === "error" || type.endsWith("failed") || type.includes("error")) {
    return "is-error";
  }
  if (type === "chunk_validation_failed" || type.endsWith("fallback")) {
    return "is-warn";
  }
  if (type === "chunk_validation_passed" || type === "pipeline_completed") {
    return "is-success";
  }
  return "is-info";
}
</script>
