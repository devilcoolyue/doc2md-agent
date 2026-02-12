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

    <p class="state-note error" v-if="task.status === 'failed'">
      {{ task.error || "转换失败，请重试。" }}
    </p>

    <div class="toolbar" style="margin-top:16px;" v-if="task.status === 'failed'">
      <button class="btn btn-ghost" @click="$emit('reset')">返回重新上传</button>
    </div>
  </section>
</template>

<script setup>
import { computed } from "vue";

const props = defineProps({
  task: {
    type: Object,
    required: true
  }
});

defineEmits(["reset"]);

const stageText = computed(() => {
  if (props.task.message) {
    return props.task.message;
  }
  return "正在等待状态更新...";
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
