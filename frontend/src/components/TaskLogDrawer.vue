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
        <p class="hint-text">状态: {{ task.status || "--" }} · 共 {{ events.length }} 条</p>
      </div>
      <button class="btn btn-ghost" type="button" @click="$emit('close')">关闭</button>
    </header>

    <div ref="logListRef" class="log-drawer-list" v-if="displayedEvents.length">
      <div
        class="log-item"
        v-for="(event, index) in displayedEvents"
        :key="`${event.timestamp || index}-${index}`"
      >
        <span class="log-time">{{ formatTime(event.timestamp) }}</span>
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
  return props.events.slice(-200);
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
</script>
