# Repository Guidelines

## Project Structure & Module Organization
This repository is a Python CLI tool with a flat module layout at the root:
- `main.py`: Click-based CLI entry point (`init`, `convert`, `providers`).
- `agent.py`: End-to-end conversion pipeline orchestration.
- `preprocessor.py`: pandoc extraction, image handling, and content chunking.
- `llm_provider.py`: provider abstraction for OpenAI-compatible and Anthropic APIs.
- `prompts.py`: prompt templates for structure analysis, conversion, and TOC generation.
- `config.example.yaml`: starter config template.
- `output/`: generated Markdown, images, and archives.

## Build, Test, and Development Commands
- `python -m venv .venv && source .venv/bin/activate`: create and activate local environment.
- `pip install -r requirements.txt`: install runtime dependencies.
- `python main.py init`: generate `config.yaml`.
- `python main.py providers`: list supported providers/models.
- `python main.py convert <file.docx> -o ./output/<name> -p deepseek`: run conversion.

Required external tools:
- `pandoc` is required for document extraction.
- `libreoffice` is required only when input files are `.doc` (legacy format).

## Coding Style & Naming Conventions
- Follow PEP 8 and 4-space indentation.
- Use `snake_case` for functions/variables/files and `PascalCase` for classes.
- Keep modules focused by responsibility (pipeline, provider, preprocessing, prompts).
- Keep prompt constants in `UPPER_SNAKE_CASE` and place them in `prompts.py`.

## Testing Guidelines
No automated test suite is currently checked in. For new features:
- Add tests under `tests/` using `pytest`.
- Name tests `test_<module>.py`.
- Prioritize coverage for parsing/chunking logic and failure paths.

Before opening a PR, run at least:
- `python -m compileall .`
- one smoke test: `python main.py convert <sample.docx>`.

## Commit & Pull Request Guidelines
Git history is not available in this workspace snapshot, so project-specific commit conventions could not be inferred. Use Conventional Commits:
- `feat: add provider timeout option`
- `fix: preserve fenced code blocks during split`

PRs should include:
- change summary and motivation
- test commands and outcomes
- config changes (if any)
- sample output path or screenshot for formatting-impacting changes

## Security & Configuration Tips
- Do not commit real API keys in `config.yaml`.
- Prefer environment variables such as `DOC2MD_API_KEY` for local secrets.
- Avoid committing sensitive source documents to `output/`.
