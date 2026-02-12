# Doc2MD Agent Web 服务化改造方案

## Context

当前项目是一个 CLI 工具，通过 `python main.py convert <file.docx>` 手动运行，没有界面、没有服务化。需要改造为：
- FastAPI 后端 + Vue 3 前端（前后端分离）
- 基础功能：文件上传、转换、下载结果、转换进度显示
- Systemd 部署

## 对现有代码的修改（最小化）

### 1. `agent.py` — 添加进度回调（约 10 行）

在 `convert()` 方法中添加 `progress_callback` 可选参数，在关键阶段调用回调：

```python
def convert(self, input_path: str, output_dir: str, progress_callback=None) -> tuple[str, dict]:
```

回调插入点（均用 `if progress_callback:` 保护，不影响 CLI）：
- 第 71 行后（预处理完成）：`progress_callback("preprocess", 0, 0)`
- 第 82 行后（结构分析完成）：`progress_callback("analyze", 0, 0)`
- 第 104 行后（每个 chunk 转换完成）：`progress_callback("convert", i+1, len(chunks))`
- 第 138 行前（开始生成目录）：`progress_callback("toc", 0, 0)`
- 第 154 行后（转换完成）：`progress_callback("done", 0, 0)`

### 2. `main.py` — 提取 `load_config` 函数

将 `load_config()`（第 37-79 行）提取到新文件 `config_loader.py`，`main.py` 改为 `from config_loader import load_config`。

### 3. `requirements.txt` — 添加 3 个依赖

```
fastapi>=0.104.0
uvicorn[standard]>=0.24.0
python-multipart>=0.0.6
```

## 新增文件

### 4. `config_loader.py`（新建，~45 行）

从 `main.py` 提取的 `load_config()` 函数，供 `main.py` 和 `server.py` 共用。

### 5. `server.py`（新建，~200 行）— FastAPI 后端

**API 端点：**

| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/api/convert` | 上传 .docx 文件，启动转换任务，返回 `{task_id}` |
| GET | `/api/tasks/{task_id}` | 轮询任务状态和进度 |
| GET | `/api/tasks/{task_id}/download` | 下载转换结果 .tar.gz |
| GET | `/api/tasks/{task_id}/preview` | 获取 Markdown 内容用于预览 |
| GET | `/api/config/providers` | 获取可用 AI 提供商列表 |

**任务管理：**
- 内存字典 `dict[str, TaskInfo]` 存储任务状态
- 每个转换任务在独立 `threading.Thread` 中运行
- 前端每 2 秒轮询 `/api/tasks/{task_id}` 获取进度
- 进度回调将阶段信息写入 TaskInfo（stage、current_chunk、total_chunks、progress百分比）

**静态文件服务：**
- 生产环境直接由 FastAPI 提供 Vue 构建产物（`web/dist/`）

### 6. `web/` 目录（新建）— Vue 3 前端

```
web/
├── package.json
├── vite.config.js          # 开发时代理 /api → localhost:8080
├── index.html
└── src/
    ├── main.js             # 创建 Vue 应用，注册 Element Plus
    ├── App.vue             # 单页面，3 个状态切换
    ├── api.js              # Axios API 封装
    └── components/
        ├── FileUpload.vue        # 拖拽上传 + 提供商选择
        ├── ConversionProgress.vue # 进度条 + 阶段文字
        └── ResultView.vue        # 下载 + Markdown 预览
```

**UI 设计（单页面 3 状态）：**
- **上传态**：拖拽上传区域 + AI 提供商选择 + "开始转换" 按钮
- **转换中**：进度条（0-100%）+ 阶段文字（"AI 转换中 3/8..."）
- **完成态**：下载按钮 + Markdown 预览 + Token 用量统计 + "转换新文档" 按钮

**技术栈：** Vue 3 (Composition API) + Vite + Element Plus + Axios + marked（Markdown 渲染）

### 7. `deploy/` 目录（新建）— 部署配置

**`deploy/doc2md.service`** — Systemd 服务单元：
```ini
[Unit]
Description=Doc2MD Agent Web Service
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/doc2md
ExecStart=/opt/doc2md/venv/bin/uvicorn server:app --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**`deploy/start.sh`** — 启动/部署脚本（构建前端 + 创建 venv + 启动服务）

## 实施顺序

```
Phase 1: 后端（先可测试）
  1. 创建 config_loader.py（提取 load_config）
  2. 修改 main.py（改为 import）
  3. 修改 agent.py（添加 progress_callback）
  4. 创建 server.py（FastAPI 全部端点）
  5. 更新 requirements.txt

Phase 2: 前端
  6. 初始化 web/ 项目（package.json、vite.config.js、index.html、main.js）
  7. 实现 FileUpload.vue
  8. 实现 ConversionProgress.vue
  9. 实现 ResultView.vue
  10. 实现 App.vue + api.js（串联所有组件）

Phase 3: 部署
  11. 创建 deploy/doc2md.service + deploy/start.sh
```

## 验证方式

1. **CLI 不受影响**：`python main.py convert test.docx` 正常工作
2. **后端测试**：`python server.py` 启动后，用 curl 测试上传和轮询
3. **前端开发**：`cd web && npm run dev`，访问 localhost:5173 测试完整流程
4. **生产构建**：`cd web && npm run build`，然后 `python server.py` 访问 localhost:8080 验证静态文件服务
