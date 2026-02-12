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
├── main.py                   # CLI 兼容入口（转发到 backend.cli）
├── server.py                 # Web 兼容入口（转发到 backend.server）
├── config.example.yaml
├── requirements.txt
└── output/
```

## 工作流

1. `pandoc` 提取 doc/docx 内容和图片
2. AI 分析文档结构
3. AI 分片转换为 Markdown
4. 后处理（目录、图片路径、清理）并输出压缩包

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
# 默认监听 http://localhost:8080
```

### 前端开发

```bash
cd frontend
npm install
npm run dev
# 访问 http://localhost:5173
```

Vite 已配置代理：`/api -> http://localhost:8080`。

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

## 部署（systemd）

`deploy/doc2md.service` 示例：

```ini
ExecStart=/opt/doc2md/venv/bin/uvicorn backend.server:app --host 0.0.0.0 --port 8080
```

快速启动脚本：

```bash
./deploy/start.sh
```

## 自定义 Prompt

编辑 `backend/prompts.py`：

- `ANALYZE_STRUCTURE_SYSTEM`
- `CONVERT_SYSTEM`
- `GENERATE_TOC_SYSTEM`

## 注意事项

- 不要提交真实 API Key
- 不要提交敏感源文档到 `output/`
