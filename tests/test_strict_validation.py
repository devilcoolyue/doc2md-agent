import pytest

from backend.agent import Doc2MDAgent


def make_agent() -> Doc2MDAgent:
    config = {
        "provider": "deepseek",
        "providers": {
            "deepseek": {
                "api_key": "test-key",
                "base_url": "http://localhost:11434/v1",
                "model": "deepseek-chat",
                "max_tokens": 4096,
                "temperature": 0,
            }
        },
        "conversion": {
            "chunk_size": 8000,
            "chunk_strategy": "section",
            "generate_toc": True,
            "deterministic_toc": True,
            "strict_mode": True,
            "max_chunk_retries": 2,
            "image_dir": "images",
        },
    }
    return Doc2MDAgent(config)


def test_extract_expected_headings_from_toc():
    agent = make_agent()
    raw_md = """
目录

[1 引言 [1](#引言)](#引言)
[1.1 目的 [1](#目的)](#目的)
[2 接口设计 [2](#接口设计)](#接口设计)

# 引言 {#引言 .HPC题1}
""".strip()
    headings = agent._extract_expected_headings_from_toc(raw_md)
    assert headings == ["1 引言", "1.1 目的", "2 接口设计"]


def test_validate_chunk_output_rejects_heading_in_continuation():
    agent = make_agent()
    ok, reason = agent._validate_chunk_output(
        source_chunk="普通正文",
        converted_chunk="## 2 接口\n内容",
        allowed_headings=["2 接口"],
        continuation_mode=True,
        llm_meta={"truncated": False},
    )
    assert not ok
    assert "续片" in reason


def test_validate_chunk_output_rejects_hallucinated_error_codes():
    agent = make_agent()
    source_chunk = """
# 错误码
10001        内部错误
10003        参数不全
""".strip()
    converted_chunk = """
##### 2.2.2.12 错误码
| 错误码 | 说明 |
|---|---|
| 10001 | 内部错误 |
| 10003 | 参数不全 |
| 100000 | 系统异常 |
""".strip()
    ok, reason = agent._validate_chunk_output(
        source_chunk=source_chunk,
        converted_chunk=converted_chunk,
        allowed_headings=["2.2.2.12 错误码"],
        continuation_mode=False,
        llm_meta={"truncated": False},
    )
    assert not ok
    assert "不存在的错误码" in reason


def test_validate_final_output_detects_missing_heading():
    agent = make_agent()
    raw_md = """
[1 引言 [1](#引言)](#引言)
[2 接口设计 [2](#接口设计)](#接口设计)

# 引言 {#引言 .HPC题1}
内容
# 接口设计 {#接口设计 .HPC题1}
内容
""".strip()
    final_md = """
# 文档标题

## 1 引言
内容
""".strip()
    with pytest.raises(RuntimeError):
        agent._validate_final_output(
            raw_md=raw_md,
            final_md=final_md,
            expected_headings=["1 引言", "2 接口设计"],
        )
