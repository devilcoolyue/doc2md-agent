# Doc2MD Agent

> 将 Word 文档转换为结构化 Markdown，并提供 CLI + Web（前后端分离）两种使用方式。

## 目录结构（前后端分离）

```text
doc-agent/
├── backend/                  # 后端 Python 代码
│   ├── __init__.py
│   ├── cli.py                # CLI 实现
│   ├── server.py             # FastAPI 服务
│   ├── config_loader.py      # 共享配置加载
│   ├── agent.py              # 转换管线编排
│   ├── preprocessor.py       # pandoc 提取/分片
│   ├── llm_provider.py       # LLM 适配层
│   └── prompts.py            # Prompt 模板
├── frontend/                 # Vue 3 前端
│   ├── package.json
│   ├── vite.config.js
│   └── src/
│       ├── App.vue
│       ├── api.js
│       ├── style.css
│       └── components/
├── deploy/
│   ├── doc2md.service        # systemd 单元
│   └── start.sh              # 一键部署/启动脚本
├── scripts/
│   └── install.sh            # 新服务器一行安装脚本（curl | bash）
├── main.py                   # CLI 兼容入口（转发到 backend.cli）
├── server.py                 # Web 兼容入口（转发到 backend.server）
├── config.example.yaml
├── requirements.txt
└── output/
```

## 工作流

1. `pandoc` 提取 doc/docx 内容和图片
2. 规则优先分析文档结构（目录/编号层级），必要时再回退 AI
3. 按章节优先分片，AI 严格保真转换（支持分片校验与重试）
4. 后处理（目录、图片路径、清理、严格校验）并输出压缩包

## 环境准备

- Python 3.10+
- `pandoc`（必需）
- `libreoffice`（仅 `.doc` 需要）
- Node.js 18+（Web 前端开发/构建需要）

## CLI 用法

```bash
pip install -r requirements.txt
python main.py init
python main.py convert your.docx -o ./output/your-doc -p deepseek
python main.py providers
```

## Web 用法（前后端分离）

### 后端

```bash
pip install -r requirements.txt
python server.py
# 默认监听 http://localhost:9999
```

### 前端开发

```bash
cd frontend
npm install
npm run dev
# 访问 http://localhost:10086
```

Vite 已配置代理：`/api -> http://localhost:9999`。

### 前端生产构建

```bash
cd frontend
npm run build
cd ..
python server.py
```

构建产物在 `frontend/dist/`，后端会自动托管静态文件。

## API 端点

- `POST /api/convert`：上传文件并创建转换任务
- `GET /api/tasks/{task_id}`：轮询任务状态/进度
- `GET /api/tasks/{task_id}/download`：下载 `.tar.gz` 结果
- `GET /api/tasks/{task_id}/preview`：获取 Markdown 预览内容
- `GET /api/config/providers`：获取可用提供商

## 配置

1. 复制模板：`config.example.yaml -> config.yaml`
2. 设置 API Key（推荐使用环境变量 `DOC2MD_API_KEY`）

关键转换配置（`conversion`）：

- `chunk_strategy`: `section`（推荐，按章节优先）或 `size`
- `strict_mode`: 开启后会校验“标题不增不减”“错误码不扩写”
- `max_chunk_retries`: 单个分片校验失败时自动重试次数
- `allow_partial_on_chunk_failure`: 分片重试耗尽后回退原文分片并继续，避免整单失败
- `allow_partial_on_validation_failure`: 最终严格校验失败时降级放行并输出结果（会记录告警）
- `deterministic_toc`: 开启后使用非 AI 目录生成，结构更稳定

## 部署（systemd）

### 新服务器一行安装并启动

```bash
curl -fsSL https://raw.githubusercontent.com/devilcoolyue/doc2md-agent/main/scripts/install.sh | bash
```

说明：

1. 脚本会拉取 `devilcoolyue/doc2md-agent` 最新代码
2. 然后调用项目内 `deploy/start.sh` 做环境检查、依赖安装、DeepSeek API Key 填写和前后端启动
3. 默认安装目录是 `$HOME/doc2md-agent`

可选：自定义安装目录

```bash
curl -fsSL https://raw.githubusercontent.com/devilcoolyue/doc2md-agent/main/scripts/install.sh | INSTALL_DIR=/opt/doc2md-agent bash
```

`deploy/doc2md.service` 示例：

```ini
ExecStart=/opt/doc2md/venv/bin/uvicorn backend.server:app --host 0.0.0.0 --port 9999
```

快速启动脚本：

```bash
./deploy/start.sh
```

`deploy/start.sh` 会执行以下动作：

1. 先执行 `git pull`（若工作区无未提交改动）
2. 自动检测 Linux 发行版并安装缺失的 `pandoc`
3. 检查 Python/Node 环境并安装依赖
4. 提示输入默认大模型配置（`deepseek`，固定 `base_url=https://api.deepseek.com/v1`，只需粘贴 API Key）
5. 一键启动后端 `9999` 与前端 `10086`

## 自定义 Prompt

编辑 `backend/prompts.py`：

- `ANALYZE_STRUCTURE_SYSTEM`
- `CONVERT_SYSTEM`
- `GENERATE_TOC_SYSTEM`

## 注意事项

- 不要提交真实 API Key
- 不要提交敏感源文档到 `output/`
