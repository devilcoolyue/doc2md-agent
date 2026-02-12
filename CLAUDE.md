# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Doc2MD Agent converts Word (.docx) documents into well-formatted Markdown files using a multi-stage AI pipeline. It uses pandoc for initial extraction, then sends content through LLM calls for structure analysis, chunk-by-chunk conversion, and TOC generation.

## Commands

```bash
# Install dependencies (requires Python 3.8+ and pandoc)
pip install -r requirements.txt

# Generate config file
python main.py init

# Convert a document
python main.py convert <file.docx>
python main.py convert <file.docx> -o ./output/ -p deepseek

# List supported AI providers
python main.py providers
```

## Architecture

The conversion pipeline runs in 4 sequential steps orchestrated by `Doc2MDAgent.convert()` in `agent.py`:

1. **Preprocessing** (`preprocessor.py`): Calls pandoc to extract raw markdown + images from docx. `fix_pandoc_table_codeblocks()` converts pandoc's single-column tables (containing JSON/code) into fenced code blocks. `split_content()` chunks text at heading boundaries while respecting code block boundaries.

2. **Structure Analysis** (`agent.py` → `llm_provider.py`): Sends the first 3000 chars to the LLM to identify document type, heading level mappings, and structural metadata. Returns JSON.

3. **Chunk Conversion** (`agent.py` → `llm_provider.py`): Each chunk is sent to the LLM with the structure analysis context for formatting into clean Markdown.

4. **Post-processing** (`agent.py`): Merges chunks, fixes image paths via mapping table, strips pandoc artifacts (`{#anchor}` attributes, `{width=... height=...}`), generates TOC (via LLM with `_simple_toc` fallback), and cleans up output.

### LLM Provider Layer (`llm_provider.py`)

All providers except Anthropic use the OpenAI SDK with different `base_url`/`model` values (deepseek, zhipu, qwen, ollama, openai, custom). Anthropic uses its native SDK if installed, otherwise falls back to OpenAI-compatible mode. The unified entry point is `LLMProvider.chat(system_prompt, user_prompt)`.

### Prompt Templates (`prompts.py`)

Three prompt pairs (system + user) control AI behavior:
- `ANALYZE_STRUCTURE_*` — document structure analysis, returns JSON
- `CONVERT_*` — chunk-to-markdown conversion with detailed formatting rules
- `GENERATE_TOC_*` — heading list to anchor-linked TOC

## Configuration

`config.yaml` (loaded by `main.py:load_config()`). Search order: `./config.yaml` → `./config.yml` → `~/.doc2md/config.yaml`. Environment variable `DOC2MD_API_KEY` overrides the config file's `api_key` for any provider.

## Key Design Decisions

- Prompt templates are separated from code logic in `prompts.py` — modify formatting rules there without touching pipeline code.
- Long documents are split into chunks (`chunk_size` default 8000 chars) to stay within model context windows. Splitting respects code block boundaries.
- Image path fixing uses a mapping table built during preprocessing, with regex-based fallback for common pandoc path patterns (`media/media/imageX.png`).
- `.doc` files are handled by converting to `.docx` via LibreOffice before processing.
