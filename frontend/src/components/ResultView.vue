<template>
  <section class="workspace-card">
    <h2 class="panel-title">转换完成</h2>

    <div class="chip-row">
      <span class="chip accent">结果已生成</span>
      <span class="chip" v-if="usage.prompt_tokens !== undefined">
        输入 Tokens: {{ usage.prompt_tokens }}
      </span>
      <span class="chip" v-if="usage.completion_tokens !== undefined">
        输出 Tokens: {{ usage.completion_tokens }}
      </span>
      <span class="chip" v-if="usage.llm_calls">LLM 调用: {{ usage.llm_calls }} 次</span>
      <span class="chip" v-if="usage.total_cost !== undefined">
        总费用: {{ usage.currency || "$" }}{{ Number(usage.total_cost).toFixed(6) }}
      </span>
    </div>
    <p class="hint-text" style="margin-top:12px;" v-if="costFormula">
      费用计算：{{ costFormula }}
    </p>

    <div class="toolbar" style="margin-top:16px;">
      <a class="btn btn-primary" :href="downloadUrl">下载压缩包</a>
      <button class="btn btn-ghost" @click="$emit('reset')">转换新文档</button>
    </div>

    <p class="hint-text" style="margin-top:14px;">Markdown 预览</p>
    <div ref="previewPane" class="preview-pane" v-html="markdownHtml" @click="onPreviewClick"></div>
  </section>
</template>

<script setup>
import { computed, ref } from "vue";

const props = defineProps({
  markdownHtml: {
    type: String,
    default: ""
  },
  usage: {
    type: Object,
    default: () => ({})
  },
  downloadUrl: {
    type: String,
    required: true
  }
});

defineEmits(["reset"]);

const previewPane = ref(null);

const costFormula = computed(() => {
  const promptTokens = Number(props.usage.prompt_tokens || 0);
  const completionTokens = Number(props.usage.completion_tokens || 0);
  const inputPrice = Number(props.usage.input_price_per_million || 0);
  const outputPrice = Number(props.usage.output_price_per_million || 0);
  const currency = props.usage.currency || "$";

  if (!inputPrice && !outputPrice) {
    return "";
  }
  return `(${promptTokens} ÷ 1,000,000 × ${currency}${inputPrice}) + (${completionTokens} ÷ 1,000,000 × ${currency}${outputPrice})`;
});

function normalizeAnchor(value) {
  return decodeURIComponent(value || "")
    .toLowerCase()
    .replace(/^#/, "")
    .replace(/[^\w\u4e00-\u9fff\s-]/g, "")
    .trim()
    .replace(/\s+/g, "-");
}

function resolveAnchorTarget(anchor) {
  if (!previewPane.value) {
    return null;
  }

  const normalizedAnchor = normalizeAnchor(anchor);
  const headings = previewPane.value.querySelectorAll("h1, h2, h3, h4, h5, h6");

  for (const heading of headings) {
    const id = heading.getAttribute("id") || "";
    const headingText = heading.textContent || "";
    if (id === anchor) {
      return heading;
    }
    if (normalizeAnchor(id) === normalizedAnchor) {
      return heading;
    }
    if (normalizeAnchor(headingText) === normalizedAnchor) {
      return heading;
    }
  }

  return null;
}

function onPreviewClick(event) {
  const targetElement = event.target;
  if (!(targetElement instanceof Element)) {
    return;
  }
  const link = targetElement.closest("a[href^='#']");
  if (!link) {
    return;
  }

  const href = link.getAttribute("href") || "";
  const anchor = href.slice(1);
  if (!anchor) {
    return;
  }

  const target = resolveAnchorTarget(anchor);
  if (!target) {
    return;
  }

  event.preventDefault();
  target.scrollIntoView({ behavior: "smooth", block: "start" });
}
</script>
