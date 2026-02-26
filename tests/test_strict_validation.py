import pytest

from backend.agent import Doc2MDAgent
from backend.preprocessor import fix_pandoc_table_codeblocks


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


def test_convert_chunk_with_retry_strips_heading_for_continuation(monkeypatch):
    agent = make_agent()

    def fake_convert_chunk(**kwargs):
        return (
            "### 2.2.5.6 查询组成员历史作业列表\n保留正文\n```bash\n# in-code comment\n```",
            {"truncated": False},
        )

    monkeypatch.setattr(agent, "_convert_chunk", fake_convert_chunk)
    converted, meta = agent._convert_chunk_with_retry(
        chunk="普通正文",
        structure={},
        chunk_index=1,
        total_chunks=1,
        section_id="2.2.5.6",
        section_heading="2.2.5.6 查询组成员历史作业列表",
        section_label="2.2.5.6 查询组成员历史作业列表",
        allowed_headings=[],
        continuation_mode=True,
        chunk_has_heading=False,
        previous_heading="",
        next_heading="",
    )

    assert "### 2.2.5.6 查询组成员历史作业列表" not in converted
    assert "保留正文" in converted
    assert "```bash\n# in-code comment\n```" in converted
    assert meta.get("attempts_used") == 1
    assert meta.get("removed_heading_lines") == 1


def test_convert_chunk_with_retry_normalizes_required_heading(monkeypatch):
    agent = make_agent()
    agent.max_chunk_retries = 1

    def fake_convert_chunk(**kwargs):
        return ("普通正文，没有编号标题", {"truncated": False})

    monkeypatch.setattr(agent, "_convert_chunk", fake_convert_chunk)
    converted, meta = agent._convert_chunk_with_retry(
        chunk="## 2.2.5.6 查询组成员历史作业列表\n原文正文",
        structure={},
        chunk_index=1,
        total_chunks=1,
        section_id="2.2.5.6",
        section_heading="2.2.5.6 查询组成员历史作业列表",
        section_label="2.2.5.6 查询组成员历史作业列表",
        allowed_headings=["2.2.5.6 查询组成员历史作业列表"],
        continuation_mode=False,
        chunk_has_heading=True,
        previous_heading="",
        next_heading="",
    )

    assert converted.startswith("##### 2.2.5.6 查询组成员历史作业列表")
    assert meta.get("fallback_used") is not True
    assert meta.get("normalized_heading") is True
    assert meta.get("attempts_used") == 1


def test_convert_chunk_with_retry_raises_when_fallback_disabled(monkeypatch):
    agent = make_agent()
    agent.max_chunk_retries = 0
    agent.allow_partial_on_chunk_failure = False

    def fake_convert_chunk(**kwargs):
        return ("普通正文，没有编号标题", {"truncated": False})

    monkeypatch.setattr(agent, "_convert_chunk", fake_convert_chunk)
    with pytest.raises(RuntimeError):
        agent._convert_chunk_with_retry(
            chunk="普通正文",
            structure={},
            chunk_index=1,
            total_chunks=1,
            section_id="2.2.5.6",
            section_heading="2.2.5.6 查询组成员历史作业列表",
            section_label="2.2.5.6 查询组成员历史作业列表",
            allowed_headings=["2.2.5.6 查询组成员历史作业列表"],
            continuation_mode=False,
            chunk_has_heading=False,
            previous_heading="",
            next_heading="",
        )


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


def test_json_masked_value_can_be_normalized():
    agent = make_agent()
    block = """
{
  "userId": 1118xxxx5311,
  "name": "demo"
}
""".strip()
    normalized, ok = agent._normalize_json_block(block)
    assert ok
    assert '"userId": "1118xxxx5311"' in normalized


def test_replace_output_json_blocks_with_source():
    agent = make_agent()
    source_chunk = """
```json
{
  "code": "0",
  "msg": "success"
}
```
""".strip()
    converted_chunk = """
```json
{
  "code": "200",
  "msg": "placeholder"
}
```
""".strip()
    replaced = agent._replace_output_json_blocks_with_source(source_chunk, converted_chunk)
    assert '"code": "0"' in replaced
    assert "placeholder" not in replaced


def test_normalize_json_block_can_smart_fill_missing_parts():
    agent = make_agent()
    block = """
{
  userId: 1118xxxx5311
  'name': 'demo'
""".strip()
    normalized, ok = agent._normalize_json_block(block)
    assert ok
    assert '"userId": "1118xxxx5311"' in normalized
    assert '"name": "demo"' in normalized


def test_replace_output_json_blocks_with_source_falls_back_to_plain_code_when_unrecoverable():
    agent = make_agent()
    source_chunk = """
```json
{
  "items": [1,, 2]
}
```
""".strip()
    converted_chunk = """
```json
{
  "items": []
}
```
""".strip()
    replaced = agent._replace_output_json_blocks_with_source(source_chunk, converted_chunk)
    assert "```json" not in replaced
    assert "以下json格式可能有问题，请检查" in replaced
    assert '  "items": [1,, 2]' in replaced


def test_validate_chunk_output_accepts_json_fallback_code_block():
    agent = make_agent()
    source_chunk = """
```json
{
  "items": [1,, 2]
}
```
""".strip()
    converted_chunk = agent._replace_output_json_blocks_with_source(
        source_chunk,
        """
```json
{
  "items": []
}
```
""".strip(),
    )
    ok, reason = agent._validate_chunk_output(
        source_chunk=source_chunk,
        converted_chunk=converted_chunk,
        allowed_headings=[],
        continuation_mode=False,
        llm_meta={"truncated": False},
    )
    assert ok
    assert reason == ""


def test_sanitize_output_json_blocks_with_report_downgrades_invalid_generated_json():
    agent = make_agent()
    converted_chunk = """
```json
{
  "items": [1,, 2]
}
```
""".strip()
    fixed, report = agent._sanitize_output_json_blocks_with_report(converted_chunk)
    assert report["output_json_blocks"] == 1
    assert report["output_json_fallback"] == 1
    assert "```json" not in fixed
    assert "以下json格式可能有问题，请检查" in fixed


def test_fix_pandoc_table_codeblocks_supports_colon_border():
    text = """
+:-------------------+
| {                 |
| \\"code\\": \\"0\\" |
| }                 |
+--------------------+
""".strip()
    fixed = fix_pandoc_table_codeblocks(text)
    assert "```json" in fixed
    assert '"code": "0"' in fixed


def test_validate_chunk_output_rejects_new_json_block_when_source_has_no_json():
    agent = make_agent()
    source_chunk = "标准正确格式为：{\"code\":0,\"msg\":\"\",\"data\":Json对象或列表}"
    converted_chunk = """
```json
{
  "code": "0"
}
```
""".strip()
    ok, reason = agent._validate_chunk_output(
        source_chunk=source_chunk,
        converted_chunk=converted_chunk,
        allowed_headings=[],
        continuation_mode=False,
        llm_meta={"truncated": False},
    )
    assert not ok
    assert "禁止新增 JSON 代码块" in reason


def test_ensure_allowed_heading_in_chunk_rewrites_plain_heading_and_level():
    agent = make_agent()
    converted = "## 安全认证\n正文"
    fixed, changed = agent._ensure_allowed_heading_in_chunk(
        converted_chunk=converted,
        allowed_headings=["2.2.1 安全认证"],
        continuation_mode=False,
        chunk_has_heading=True,
    )
    assert changed is True
    assert fixed.startswith("#### 2.2.1 安全认证")


def test_normalize_json_block_handles_mailto_and_invalid_escape():
    agent = make_agent()
    block = """
{
  "email": ["117907290@qq.com"，](mailto:"117907290@qq.com"，)
  "CoreSpec": "\\*"
}
""".strip()
    normalized, ok = agent._normalize_json_block(block)
    assert ok
    assert '"email": "117907290@qq.com"' in normalized
    assert '"CoreSpec": "*"' in normalized


def test_merge_hierarchical_field_tables_into_single_table():
    agent = make_agent()
    markdown = """
| 名称 | 类型 | 说明 |
| :--- | :--- | :--- |
| data | Object | 用户信息数据对象 |

`data` 对象字段说明：

| 名称 | 类型 | 说明 |
| :--- | :--- | :--- |
| email | String | 邮箱地址 |
| userId | Number | 用户ID |
""".strip()
    merged = agent._merge_hierarchical_field_tables(markdown)
    assert "`data` 对象字段说明：" not in merged
    assert "| └─email | String | 邮箱地址 |" in merged
    assert "| └─userId | Number | 用户ID |" in merged


def test_merge_hierarchical_field_tables_supports_heading_style_parent_line():
    agent = make_agent()
    markdown = """
| 名称 | 类型 | 说明 |
| :--- | :--- | :--- |
| data | Object | 用户信息 |

#### data 对象字段说明:

| 名称 | 类型 | 说明 |
| :--- | :--- | :--- |
| data.userName | String | 用户名 |
| data.email | String | 邮箱 |
""".strip()
    merged = agent._merge_hierarchical_field_tables(markdown)
    assert "#### data 对象字段说明:" not in merged
    assert "| └─userName | String | 用户名 |" in merged
    assert "| └─email | String | 邮箱 |" in merged


def test_normalize_indented_hierarchy_in_single_table():
    agent = make_agent()
    markdown = """
| 参数名 | 类型 | 示例值 | 描述 |
| :--- | :--- | :--- | :--- |
| code | String | "0" | 接口返回状态码 |
| msg | String | "success" | 接口返回信息提示 |
| data | Object | {...} | 用户信息数据对象 |
|   country | String | "China" | 所在国家 |
|   language | String | "zh_CN" | 语言区域标识 |
""".strip()
    normalized = agent._normalize_indented_hierarchy_in_tables(markdown)
    assert "| └─country | String | \"China\" | 所在国家 |" in normalized
    assert "| └─language | String | \"zh_CN\" | 语言区域标识 |" in normalized


def test_normalize_prefixed_child_rows_in_single_table():
    agent = make_agent()
    markdown = """
| 名称 | 类型 | 说明 |
| :--- | :--- | :--- |
| data | Object | 用户信息数据对象 |
| data.country | String | 所在国家 |
| data.language | String | 语言区域标识 |
""".strip()
    normalized = agent._normalize_indented_hierarchy_in_tables(markdown)
    assert "| └─country | String | 所在国家 |" in normalized
    assert "| └─language | String | 语言区域标识 |" in normalized


def test_normalize_numbered_heading_levels_by_section_depth():
    agent = make_agent()
    markdown = """
## 2.1.2 资源权限
### 2.1.3 支持协议
## 2.1.5 接口格式
""".strip()
    normalized = agent._normalize_numbered_heading_levels(markdown)
    assert "#### 2.1.2 资源权限" in normalized
    assert "#### 2.1.3 支持协议" in normalized
    assert "#### 2.1.5 接口格式" in normalized


def test_promote_plain_numbered_heading_lines():
    agent = make_agent()
    markdown = """
2 接口设计

2.1 约定
这里是正文
""".strip()
    promoted = agent._promote_plain_numbered_heading_lines(markdown)
    assert "## 2 接口设计" in promoted
    assert "### 2.1 约定" in promoted


def test_promote_plain_numbered_heading_lines_ignores_ordered_list_items():
    agent = make_agent()
    markdown = """
接口调用流程：

1. 用户需要先注册平台账号；
2. 调用认证接口获取 token；
""".strip()
    promoted = agent._promote_plain_numbered_heading_lines(markdown)
    assert "## 1 用户需要先注册平台账号；" not in promoted
    assert "## 2 调用认证接口获取 token；" not in promoted


def test_promote_plain_numbered_heading_lines_ignores_zero_value_rows():
    agent = make_agent()
    markdown = "0 接口调用成功"
    promoted = agent._promote_plain_numbered_heading_lines(markdown)
    assert "## 0 接口调用成功" not in promoted


def test_content_guard_rejects_heavy_deletion():
    agent = make_agent()
    agent.content_guard_min_tokens = 1
    source = """
2.2.1 用户信息接口
请求参数包含 userId、accountId、email、groupId
返回结果包含 code、msg、data、userName、clusterId
""".strip()
    ok, reason = agent._check_content_preservation(source, "仅保留一行。")
    assert not ok
    assert "主体内容疑似被删减" in reason


def test_insert_toc_before_first_numbered_heading_even_with_later_h1():
    agent = make_agent()
    markdown = """
## 1 引言
正文

# 安全认证
正文
""".strip()
    toc = "- [1 引言](#1-引言)"
    inserted = agent._insert_toc(markdown, toc)
    assert inserted.find("## 目录") < inserted.find("## 1 引言")


def test_insert_toc_replaces_existing_toc_block():
    agent = make_agent()
    markdown = """
---

## 目录

- [1 引言](#1-引言)

---

## 1 引言
正文
""".strip()
    toc = """
- [1 引言](#1-引言)
  - [1.1 目的](#11-目的)
""".strip()
    inserted = agent._insert_toc(markdown, toc)
    assert inserted.count("## 目录") == 1
    assert inserted.count("(#11-目的)") == 1


def test_flatten_residual_grid_table_rows_merges_into_previous_table():
    agent = make_agent()
    markdown = """
| 名称 | 类型 | 说明 |
| :--- | :--- | :--- |
| records | Object[] | 数据列表 |

|                         +-----------------+-----------------+--------------------------+
|                         | userName        | String          | 作业提交用户 |
|                         +-----------------+-----------------+--------------------------+
|                         | jobId           | String          | 作业 ID |
+-------------------------+-----------------+-----------------+--------------------------+
""".strip()
    flattened = agent._flatten_residual_grid_table_rows(markdown)
    assert "| └─userName | String | 作业提交用户 |" in flattened
    assert "| └─jobId | String | 作业 ID |" in flattened
    assert "+-----------------" not in flattened


def test_convert_residual_grid_tables_to_markdown_table():
    agent = make_agent()
    markdown = """
+:-------+:-------+:--------+:--------+:---------------+:-------+:-------------------------------+
| 名称                                                 | 类型   | 说明                           |
+------------------------------------------------------+--------+--------------------------------+
| code                                                 | String | 错误码                         |
+------------------------------------------------------+--------+--------------------------------+
| data                                                 | Object | 集群队列信息对象               |
+--------+---------------------------------------------+--------+--------------------------------+
|        | clusterId                                   | Object | 集群 ID 对应的队列信息         |
| └─name | String | 集群名称 |
+--------+---------------------------------------------+--------+--------------------------------+
""".strip()
    converted = agent._convert_residual_grid_tables(markdown)
    assert "+------------------------------------------------------+" not in converted
    assert "| 名称 | 类型 | 说明 |" in converted
    assert "| code | String | 错误码 |" in converted
    assert "| └─clusterId | Object | 集群 ID 对应的队列信息 |" in converted


def test_convert_residual_grid_tables_preserves_four_columns_and_merges_wrapped_desc():
    agent = make_agent()
    markdown = """
+----------------+---------+--------+----------------------------------------+
| 名称           | 类型    | 必填   | 说明                                   |
+----------------+---------+--------+----------------------------------------+
| password       | String  | 是     | 密码，规则：6~30位字符。               |
|                |         |        | 该值若不填，则邮箱或手机号至少填一项。 |
+----------------+---------+--------+----------------------------------------+
| email          | String  | 否     | 邮件通知地址                           |
+----------------+---------+--------+----------------------------------------+
    """.strip()
    converted = agent._convert_residual_grid_tables(markdown)
    assert "| 名称 | 类型 | 必填 | 说明 |" in converted
    assert "| password | String | 是 | 密码，规则：6~30位字符。该值若不填，则邮箱或手机号至少填一项。 |" in converted
    assert "|  |  |  | 该值若不填，则邮箱或手机号至少填一项。 |" not in converted


def test_convert_residual_grid_tables_infers_nested_depth_from_leading_empty_columns():
    agent = make_agent()
    markdown = """
+:-----------+:-------------+:---------------------------+:-------------------+:-------------------+
| 名称                                                   | 类型               | 说明               |
+--------------------------------------------------------+--------------------+--------------------+
| data                                                   | Object             | 返回数据对象       |
+------------+-------------------------------------------+--------------------+--------------------+
|            | records                                   | Array\\<Object\\>  | 账户限额记录列表   |
+------------+--------------+----------------------------+--------------------+--------------------+
|                           | accountId                  | String             | 账户 ID            |
|                           | groupId                    | String             | 账户所属组 ID      |
+---------------------------+----------------------------+--------------------+--------------------+
""".strip()
    converted = agent._convert_residual_grid_tables(markdown)
    normalized = agent._normalize_hierarchy_with_object_row_fallback(converted)
    assert "| data | Object | 返回数据对象 |" in normalized
    assert "| └─records | Array\\<Object\\> | 账户限额记录列表 |" in normalized
    assert "|   └─accountId | String | 账户 ID |" in normalized
    assert "|   └─groupId | String | 账户所属组 ID |" in normalized


def test_convert_pandoc_simple_tables_to_markdown_table():
    agent = make_agent()
    markdown = """
---------------------- ------------------------------------------------
组件                   组件访问地址

门户组件               <https://CONTROL_IP>

管理组件               <https://MANAGE_IP>
---------------------- ------------------------------------------------
""".strip()
    converted = agent._convert_pandoc_simple_tables(markdown)
    assert "----------------------" not in converted
    assert "| 组件 | 组件访问地址 |" in converted
    assert "| 门户组件 | <https://CONTROL_IP> |" in converted
    assert "| 管理组件 | <https://MANAGE_IP> |" in converted


def test_convert_plain_text_tabular_blocks_to_markdown_table():
    agent = make_agent()
    markdown = """
请求参数：
名称 类型 必填 说明

token String 是 用户令牌
""".strip()
    converted = agent._convert_plain_text_tabular_blocks(markdown)
    assert "| 名称 | 类型 | 必填 | 说明 |" in converted
    assert "| token | String | 是 | 用户令牌 |" in converted


def test_merge_wrapped_description_rows_in_markdown_tables():
    agent = make_agent()
    markdown = """
| 名称 | 类型 | 说明 |
| :--- | :--- | :--- |
| maxSubmitJobs | String | 最大提交作业数 |
|  |  | 不限制：UNLIMITED |
| maxProc | String | 最大处理器数 |
|  |  | 不限制：UNLIMITED |
""".strip()
    merged = agent._merge_wrapped_description_rows_in_tables(markdown)
    assert "| maxSubmitJobs | String | 最大提交作业数 不限制：UNLIMITED |" in merged
    assert "| maxProc | String | 最大处理器数 不限制：UNLIMITED |" in merged
    assert "|  |  | 不限制：UNLIMITED |" not in merged


def test_expand_required_only_tables_with_description():
    agent = make_agent()
    markdown = """
| 名称 | 类型 | 必填 |
| :--- | :--- | :--- |
| status | String | 是 add代表邀请，remove代表移除 |
| quota | Long | 否 （邀请必填,移除不用填写） |
| groupId | String | 是 |
""".strip()
    expanded = agent._expand_required_only_tables_with_description(markdown)
    assert "| 名称 | 类型 | 必填 | 说明 |" in expanded
    assert "| status | String | 是 | add代表邀请，remove代表移除 |" in expanded
    assert "| quota | Long | 否 | （邀请必填,移除不用填写） |" in expanded
    assert "| groupId | String | 是 |  |" in expanded


def test_normalize_api_label_lines_bolds_common_labels():
    agent = make_agent()
    markdown = """
接口地址：/ac/openapi/v3/center
请求方式：GET
接口描述：获取授权集群地址
""".strip()
    normalized = agent._normalize_api_label_lines(markdown)
    assert "**接口地址：** /ac/openapi/v3/center" in normalized
    assert "**请求方式：** GET" in normalized
    assert "**接口描述：** 获取授权集群地址" in normalized


def test_normalize_api_label_lines_fixes_missing_space_after_bold_label():
    agent = make_agent()
    markdown = """
**接口地址：**/ac/openapi/v3/center
**请求方式：**GET
""".strip()
    normalized = agent._normalize_api_label_lines(markdown)
    assert "**接口地址：** /ac/openapi/v3/center" in normalized
    assert "**请求方式：** GET" in normalized


def test_wrap_curl_commands_in_code_blocks():
    agent = make_agent()
    markdown = """
请求示例：
curl --location --request GET 'https://CONTROL_IP/ac/openapi/v3/center'
""".strip()
    wrapped = agent._wrap_curl_commands_in_code_blocks(markdown)
    assert "```bash" in wrapped
    assert "curl --location --request GET 'https://CONTROL_IP/ac/openapi/v3/center'" in wrapped
    assert wrapped.count("```") >= 2


def test_wrap_multiline_curl_commands_with_blank_lines():
    agent = make_agent()
    markdown = """
请求示例：
curl \\--request GET \\

\\--url 'http://127.0.0.1:9999/api/user/v3/info' \\

\\--header 'token: xxx'
""".strip()
    wrapped = agent._wrap_curl_commands_in_code_blocks(markdown)
    assert "```bash" in wrapped
    fenced_body = wrapped.split("```bash", 1)[1].split("```", 1)[0]
    assert "\\--url 'http://127.0.0.1:9999/api/user/v3/info' \\" in fenced_body
    assert "\\--header 'token: xxx'" in fenced_body


def test_normalize_hierarchy_from_nearby_json_examples():
    agent = make_agent()
    markdown = """
| 名称 | 类型 | 说明 |
| :--- | :--- | :--- |
| code | String | 接口状态码 |
| msg | String | 接口返回信息 |
| data | Object | 返回对象 |
| └─groupDetail | Object | 团队详情 |
| └─accountId | String | 账户 ID |
| └─id | String | 团队 ID |
| └─userGroupInfo | Object | 团队成员信息 |
| └─leader | String | 团队负责人 |
| └─userNum | Integer | 团队人数 |

```json
{
  "code": "0",
  "msg": "success",
  "data": {
    "groupDetail": {
      "accountId": "123",
      "id": "456"
    },
    "userGroupInfo": {
      "leader": "demo",
      "userNum": 5
    }
  }
}
```
""".strip()
    normalized = agent._normalize_hierarchy_from_nearby_json_examples(markdown)
    assert "| └─groupDetail | Object | 团队详情 |" in normalized
    assert "|   └─accountId | String | 账户 ID |" in normalized
    assert "|   └─id | String | 团队 ID |" in normalized
    assert "| └─userGroupInfo | Object | 团队成员信息 |" in normalized
    assert "|   └─leader | String | 团队负责人 |" in normalized
    assert "|   └─userNum | Integer | 团队人数 |" in normalized


def test_normalize_hierarchy_from_nearby_json_examples_without_existing_markers():
    agent = make_agent()
    markdown = """
| 名称 | 类型 | 说明 |
| :--- | :--- | :--- |
| code | String | 返回码 |
| msg | String | 返回信息 |
| data | Object | 返回对象 |
| total | Integer | 总记录数 |
| pages | Integer | 总页数 |
| records | Array\\<Object\\> | 记录列表 |
| accountId | String | 账户ID |
| groupId | String | 组ID |

```json
{
  "code": "0",
  "msg": "success",
  "data": {
    "total": 1,
    "pages": 1,
    "records": [
      {
        "accountId": "1",
        "groupId": "2"
      }
    ]
  }
}
```
""".strip()
    normalized = agent._normalize_hierarchy_from_nearby_json_examples(markdown)
    assert "| └─total | Integer | 总记录数 |" in normalized
    assert "| └─pages | Integer | 总页数 |" in normalized
    assert "| └─records | Array\\<Object\\> | 记录列表 |" in normalized
    assert "|   └─accountId | String | 账户ID |" in normalized
    assert "|   └─groupId | String | 组ID |" in normalized


def test_normalize_hierarchy_with_object_row_fallback_without_json():
    agent = make_agent()
    markdown = """
| 名称 | 类型 | 说明 |
| :--- | :--- | :--- |
| code | String | 状态码 |
| data | Object | 返回对象 |
| └─groupDetail |  |  |
| └─accountId | String | 账户 ID |
| └─id | String | 团队 ID |
| └─userGroupInfo |  |  |
| └─leader | String | 团队负责人 |
| └─userNum | Integer | 当前团队人数 |
""".strip()
    normalized = agent._normalize_hierarchy_with_object_row_fallback(markdown)
    assert "| └─groupDetail |  |  |" in normalized
    assert "|   └─accountId | String | 账户 ID |" in normalized
    assert "|   └─id | String | 团队 ID |" in normalized
    assert "| └─userGroupInfo |  |  |" in normalized
    assert "|   └─leader | String | 团队负责人 |" in normalized
    assert "|   └─userNum | Integer | 当前团队人数 |" in normalized


def test_normalize_json_fenced_blocks_formats_plain_fence_json():
    agent = make_agent()
    markdown = """
```
{"code":"0","msg":"success"}
```
""".strip()
    normalized = agent._normalize_json_fenced_blocks(markdown)
    assert "```json" in normalized
    assert '  "code": "0"' in normalized
    assert '  "msg": "success"' in normalized


def test_postprocess_partial_markdown_fixes_toc_table_and_code_styles():
    agent = make_agent()
    markdown = """
2.2.1.1 获取授权集群地址
接口地址：/ac/openapi/v3/center
请求方式：GET
接口描述：获取授权集群地址

请求参数：
名称 类型 必填 说明
token String 是 用户令牌

请求示例：
curl --location --request GET 'https://CONTROL_IP/ac/openapi/v3/center'
""".strip()
    fixed = agent.postprocess_partial_markdown(markdown)
    assert "## 目录" in fixed
    assert "(#2211-获取授权集群地址)" in fixed
    assert "| 名称 | 类型 | 必填 | 说明 |" in fixed
    assert "**请求方式：** GET" in fixed
    assert "```bash" in fixed


def test_build_partial_preview_markdown_applies_postprocess():
    agent = make_agent()
    chunks = [
        "2.2.1.1 获取授权集群地址\n请求方式：GET",
        "请求示例：\ncurl --location --request GET 'https://CONTROL_IP/ac/openapi/v3/center'",
    ]
    preview = agent._build_partial_preview_markdown(chunks)
    assert "## 目录" in preview
    assert "**请求方式：** GET" in preview
    assert "```bash" in preview


def test_build_partial_preview_markdown_falls_back_to_raw_when_postprocess_fails(monkeypatch):
    agent = make_agent()

    def raise_error(_: str) -> str:
        raise RuntimeError("mock postprocess failure")

    monkeypatch.setattr(agent, "postprocess_partial_markdown", raise_error)
    preview = agent._build_partial_preview_markdown(["第一段", "第二段"])
    assert "第一段\n\n第二段" in preview
