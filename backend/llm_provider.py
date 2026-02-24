"""
AI Provider 抽象层
所有主流大模型 API 都兼容 OpenAI 的 chat/completions 接口，
因此统一使用 openai SDK 作为客户端，只需切换 base_url 和 model。
Anthropic 是唯一的例外，需要单独处理。
"""

import os
import logging
import time
from typing import Any, Callable, Optional
from openai import OpenAI

logger = logging.getLogger(__name__)

# 内置定价表：(每百万输入tokens价格, 每百万输出tokens价格, 货币符号)
PRICING = {
    ("openai", "gpt-4o"): (2.50, 10.00, "$"),
    ("openai", "gpt-4o-mini"): (0.15, 0.60, "$"),
    ("deepseek", "deepseek-chat"): (2.0, 3.0, "¥"),
    ("deepseek", "deepseek-reasoner"): (2.0, 3.0, "¥"),
    ("anthropic", "claude-sonnet-4-20250514"): (3.0, 15.0, "$"),
    ("zhipu", "glm-4-plus"): (50.0, 50.0, "¥"),
    ("qwen", "qwen-max"): (20.0, 60.0, "¥"),
}


class LLMProvider:
    """统一的大模型调用接口"""

    def __init__(self, config: dict, event_callback: Optional[Callable[[dict[str, Any]], None]] = None):
        self.provider_name = config.get("provider", "deepseek")
        provider_conf = config.get("providers", {}).get(self.provider_name, {})
        self.event_callback = event_callback

        self.api_key = provider_conf.get("api_key", "")
        self.base_url = provider_conf.get("base_url", "")
        self.model = provider_conf.get("model", "")
        self.max_tokens = provider_conf.get("max_tokens", 16000)
        self.temperature = provider_conf.get("temperature", 0)
        self.call_count = 0

        # Token 用量统计
        self.total_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "input_cost": 0.0,
            "output_cost": 0.0,
            "total_cost": 0.0,
            "calls": 0,
        }

        # 解析定价：优先使用配置文件中的 pricing，其次内置表，ollama 默认免费
        pricing_conf = provider_conf.get("pricing")
        if pricing_conf:
            self._input_price = pricing_conf.get("input", 0.0)
            self._output_price = pricing_conf.get("output", 0.0)
            self._currency = pricing_conf.get("currency", "$")
        elif (self.provider_name, self.model) in PRICING:
            p = PRICING[(self.provider_name, self.model)]
            self._input_price, self._output_price, self._currency = p
        elif self.provider_name == "ollama":
            self._input_price, self._output_price, self._currency = 0.0, 0.0, "$"
        else:
            self._input_price, self._output_price, self._currency = 0.0, 0.0, "$"

        # 环境变量覆盖（方便 CI/CD）
        env_key = os.environ.get("DOC2MD_API_KEY")
        if env_key:
            self.api_key = env_key

        if self.provider_name == "anthropic":
            self._init_anthropic()
        else:
            self._init_openai_compatible()

    def _init_openai_compatible(self):
        """初始化 OpenAI 兼容客户端（覆盖 deepseek/qwen/zhipu/ollama/openai/custom）"""
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )
        self._call = self._call_openai

    def _init_anthropic(self):
        """Anthropic 使用自己的 SDK 格式，但也可以用 OpenAI 兼容层"""
        # Anthropic 现在也支持 OpenAI 兼容接口
        # 如果用户安装了 anthropic SDK 则用原生，否则 fallback
        try:
            import anthropic
            self.client = anthropic.Anthropic(api_key=self.api_key)
            self._call = self._call_anthropic_native
        except ImportError:
            logger.warning("anthropic SDK 未安装，使用 OpenAI 兼容模式")
            self.client = OpenAI(
                api_key=self.api_key,
                base_url="https://api.anthropic.com/v1/",
            )
            self._call = self._call_openai

    def _call_openai(self, system_prompt: str, user_prompt: str) -> tuple[str, int, int, str]:
        """调用 OpenAI 兼容接口"""
        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        prompt_tokens = response.usage.prompt_tokens if response.usage else 0
        completion_tokens = response.usage.completion_tokens if response.usage else 0
        content = response.choices[0].message.content or ""
        finish_reason = response.choices[0].finish_reason or "unknown"
        return content, prompt_tokens, completion_tokens, finish_reason

    def _call_anthropic_native(self, system_prompt: str, user_prompt: str) -> tuple[str, int, int, str]:
        """调用 Anthropic 原生接口"""
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_prompt},
            ],
        )
        prompt_tokens = response.usage.input_tokens if response.usage else 0
        completion_tokens = response.usage.output_tokens if response.usage else 0
        content = response.content[0].text if response.content else ""
        finish_reason = response.stop_reason or "unknown"
        return content, prompt_tokens, completion_tokens, finish_reason

    def _emit_event(self, payload: dict[str, Any]) -> None:
        if self.event_callback:
            self.event_callback(payload)

    def _describe_operation(self, context: dict[str, Any]) -> str:
        operation = context.get("operation")
        if operation == "analyze_structure":
            return "结构分析"
        if operation == "convert_chunk":
            chunk_index = context.get("chunk_index")
            total_chunks = context.get("total_chunks")
            if chunk_index and total_chunks:
                return f"分片转换 {chunk_index}/{total_chunks}"
            return "分片转换"
        if operation == "generate_toc":
            return "目录生成"
        return "通用调用"

    def _accumulate_usage(self, prompt_tokens: int, completion_tokens: int) -> dict[str, float]:
        """累计 token 用量和费用"""
        input_cost = (prompt_tokens * self._input_price) / 1_000_000
        output_cost = (completion_tokens * self._output_price) / 1_000_000
        total_cost = input_cost + output_cost

        self.total_usage["prompt_tokens"] += prompt_tokens
        self.total_usage["completion_tokens"] += completion_tokens
        self.total_usage["input_cost"] += input_cost
        self.total_usage["output_cost"] += output_cost
        self.total_usage["total_cost"] += total_cost
        self.total_usage["calls"] += 1
        logger.info(
            "本次用量: 输入=%s 输出=%s tokens, 费用=%s%.6f",
            prompt_tokens,
            completion_tokens,
            self._currency,
            total_cost,
        )
        return {
            "input_cost": input_cost,
            "output_cost": output_cost,
            "total_cost": total_cost,
        }

    def get_usage_summary(self) -> dict:
        """返回累计用量摘要"""
        prompt = self.total_usage["prompt_tokens"]
        completion = self.total_usage["completion_tokens"]
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "input_cost": self.total_usage["input_cost"],
            "output_cost": self.total_usage["output_cost"],
            "total_cost": self.total_usage["total_cost"],
            "currency": self._currency,
            "input_price_per_million": self._input_price,
            "output_price_per_million": self._output_price,
            "pricing_unit": "per_1m_tokens",
            "cost_formula": "total_cost = prompt_tokens * input_price_per_million / 1_000_000 + completion_tokens * output_price_per_million / 1_000_000",
            "llm_calls": self.total_usage["calls"],
        }

    def chat_with_meta(
        self,
        system_prompt: str,
        user_prompt: str,
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        统一调用入口
        :param system_prompt: 系统提示词
        :param user_prompt: 用户输入
        :return: 带元信息的 AI 回复
        """
        context = context or {}
        self.call_count += 1
        call_id = self.call_count
        operation_desc = self._describe_operation(context)

        logger.info(f"调用 [{self.provider_name}] 模型: {self.model}")
        logger.debug(f"输入长度: system={len(system_prompt)}, user={len(user_prompt)}")
        self._emit_event(
            {
                "type": "llm_call_started",
                "call_id": call_id,
                "provider": self.provider_name,
                "model": self.model,
                "operation": context.get("operation", "generic"),
                "operation_desc": operation_desc,
                "chunk_index": context.get("chunk_index"),
                "total_chunks": context.get("total_chunks"),
                "system_chars": len(system_prompt),
                "user_chars": len(user_prompt),
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "message": (
                    f"LLM 调用 #{call_id} 开始：{self.provider_name}/{self.model}，"
                    f"操作={operation_desc}，输入字符(system={len(system_prompt)}, user={len(user_prompt)})"
                ),
            }
        )

        start_time = time.perf_counter()
        try:
            result, prompt_tokens, completion_tokens, finish_reason = self._call(system_prompt, user_prompt)
        except Exception as exc:
            elapsed = time.perf_counter() - start_time
            self._emit_event(
                {
                    "type": "llm_call_failed",
                    "call_id": call_id,
                    "provider": self.provider_name,
                    "model": self.model,
                    "operation": context.get("operation", "generic"),
                    "operation_desc": operation_desc,
                    "elapsed_seconds": round(elapsed, 3),
                    "error": str(exc),
                    "message": f"LLM 调用 #{call_id} 失败：{exc}",
                }
            )
            raise

        elapsed = time.perf_counter() - start_time
        call_cost = self._accumulate_usage(prompt_tokens, completion_tokens)
        truncated = finish_reason in {"length", "max_tokens"}

        logger.debug(f"输出长度: {len(result)}")
        self._emit_event(
            {
                "type": "llm_call_completed",
                "call_id": call_id,
                "provider": self.provider_name,
                "model": self.model,
                "operation": context.get("operation", "generic"),
                "operation_desc": operation_desc,
                "chunk_index": context.get("chunk_index"),
                "total_chunks": context.get("total_chunks"),
                "elapsed_seconds": round(elapsed, 3),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "finish_reason": finish_reason,
                "truncated": truncated,
                "input_cost": call_cost["input_cost"],
                "output_cost": call_cost["output_cost"],
                "total_cost": call_cost["total_cost"],
                "currency": self._currency,
                "message": (
                    f"LLM 调用 #{call_id} 完成：耗时 {elapsed:.2f}s，"
                    f"tokens(输入={prompt_tokens}, 输出={completion_tokens}, finish={finish_reason})，"
                    f"费用 {self._currency}{call_cost['total_cost']:.6f}"
                ),
            }
        )
        return {
            "content": result,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "finish_reason": finish_reason,
            "truncated": truncated,
            "call_id": call_id,
        }

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        context: Optional[dict[str, Any]] = None,
    ) -> str:
        """兼容旧调用：仅返回文本内容"""
        return self.chat_with_meta(system_prompt, user_prompt, context=context)["content"]
