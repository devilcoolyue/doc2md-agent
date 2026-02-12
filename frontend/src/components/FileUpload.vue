<template>
  <section class="workspace-card">
    <h2 class="panel-title">上传文档并开始转换</h2>

    <div
      class="dropzone"
      :class="{ active: dragActive }"
      @dragover.prevent="dragActive = true"
      @dragleave.prevent="dragActive = false"
      @drop.prevent="handleDrop"
    >
      <div>
        <div class="dropzone-title">拖拽 .docx/.doc 到这里</div>
        <p class="dropzone-desc">或点击下方按钮选择文件</p>
        <div class="toolbar" style="justify-content:center;margin-top:12px;">
          <label class="btn btn-ghost" for="doc-file">选择文件</label>
          <input id="doc-file" type="file" accept=".docx,.doc" hidden @change="handleInput" />
        </div>
      </div>
    </div>

    <div class="field-row">
      <label for="provider-select">AI 提供商</label>
      <select id="provider-select" v-model="selectedProvider">
        <option v-for="item in providers" :key="item.name" :value="item.name">
          {{ item.name }} / {{ item.model || "default" }}
        </option>
      </select>
    </div>

    <div class="chip-row" v-if="selectedFile">
      <span class="chip accent">文件: {{ selectedFile.name }}</span>
      <span class="chip">{{ (selectedFile.size / 1024).toFixed(1) }} KB</span>
    </div>

    <p class="state-note error" v-if="error">{{ error }}</p>

    <div class="toolbar" style="margin-top:16px;">
      <button class="btn btn-primary" :disabled="!canStart || submitting" @click="start">
        {{ submitting ? "提交中..." : "开始转换" }}
      </button>
    </div>
  </section>
</template>

<script setup>
import { computed, ref, watch } from "vue";

const props = defineProps({
  providers: {
    type: Array,
    default: () => []
  },
  currentProvider: {
    type: String,
    default: ""
  },
  submitting: {
    type: Boolean,
    default: false
  },
  error: {
    type: String,
    default: ""
  }
});

const emit = defineEmits(["start"]);

const dragActive = ref(false);
const selectedFile = ref(null);
const selectedProvider = ref(props.currentProvider);

watch(
  () => props.currentProvider,
  (value) => {
    if (value && !selectedProvider.value) {
      selectedProvider.value = value;
    }
  },
  { immediate: true }
);

const canStart = computed(() => Boolean(selectedFile.value) && Boolean(selectedProvider.value));

function handleInput(event) {
  const [file] = event.target.files || [];
  selectedFile.value = file || null;
}

function handleDrop(event) {
  dragActive.value = false;
  const [file] = event.dataTransfer?.files || [];
  selectedFile.value = file || null;
}

function start() {
  if (!canStart.value) {
    return;
  }
  emit("start", {
    file: selectedFile.value,
    provider: selectedProvider.value
  });
}
</script>
