<template>
  <section class="workspace-card">
    <h2 class="panel-title">转换进行中</h2>
    <div class="chip-row">
      <span class="chip accent">任务: {{ task.task_id || "--" }}</span>
      <span class="chip">状态: {{ task.status }}</span>
      <span class="chip" v-if="task.provider">Provider: {{ task.provider }}</span>
      <span class="chip" v-if="task.model">Model: {{ task.model }}</span>
      <span class="chip" v-if="task.llm_calls_total">
        LLM 调用: {{ task.llm_calls_finished || 0 }}/{{ task.llm_calls_total }}
      </span>
    </div>

    <div class="progress-shell">
      <div class="progress-fill" :style="{ width: `${task.progress || 0}%` }"></div>
    </div>
    <p class="state-note">{{ stageText }}</p>

    <div class="chip-row" v-if="task.total_chunks">
      <span class="chip">
        分片 {{ task.current_chunk || 0 }}/{{ task.total_chunks }}
      </span>
    </div>

    <p class="hint-text" v-if="task.updated_at">
      最后更新: {{ formatTime(task.updated_at) }}
    </p>
    <p class="hint-text" style="margin-top:10px;">日志已改为右侧弹窗展示，可随时查看最新进度。</p>
    <p class="hint-text" style="margin-top:10px;" v-if="!previewHtml">已生成的内容会实时显示在下方预览区。</p>

    <p class="hint-text" style="margin-top:14px;" v-if="previewHtml">
      {{ previewPartial ? "已生成内容预览（实时）" : "Markdown 预览" }}
    </p>
    <div
      v-if="previewHtml"
      class="preview-pane preview-pane-inline"
      v-html="previewHtml"
    ></div>

    <p class="state-note error" v-if="task.status === 'failed'">
      {{ task.error || "转换失败，请重试。" }}
    </p>

    <div class="toolbar" style="margin-top:16px;" v-if="showToolbar">
      <button
        v-if="canStop || task.status === 'stopping'"
        class="btn btn-danger"
        :disabled="!canStop"
        @click="$emit('stop')"
      >
        {{ stopButtonText }}
      </button>
      <button class="btn btn-ghost" v-if="task.status === 'failed'" @click="$emit('reset')">
        返回重新上传
      </button>
    </div>
  </section>
</template>

<script setup>
import { computed } from "vue";

const props = defineProps({
  task: {
    type: Object,
    required: true
  },
  previewHtml: {
    type: String,
    default: ""
  },
  previewPartial: {
    type: Boolean,
    default: true
  },
  stopping: {
    type: Boolean,
    default: false
  }
});

defineEmits(["reset", "stop"]);

const stageText = computed(() => {
  if (props.task.message) {
    return props.task.message;
  }
  return "正在等待状态更新...";
});

const canStop = computed(() => {
  return (props.task.status === "queued" || props.task.status === "running") && !props.stopping;
});

const showToolbar = computed(() => {
  return canStop.value || props.task.status === "stopping" || props.task.status === "failed";
});

const stopButtonText = computed(() => {
  if (props.task.status === "stopping" || props.stopping) {
    return "停止中...";
  }
  return "停止任务";
});

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
