"""
Doc2MD Web 后端服务（FastAPI）。
"""

from __future__ import annotations

import shutil
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.agent import Doc2MDAgent, TaskStoppedError
from backend.config_loader import load_config

OUTPUT_ROOT = Path("output/tasks")
FRONTEND_DIST = Path("frontend/dist")
ALLOWED_SUFFIXES = {".docx", ".doc"}
MAX_TASK_EVENTS = 2000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TaskInfo:
    task_id: str
    status: str
    stage: str = "queued"
    progress: int = 0
    message: str = "等待处理"
    current_chunk: int = 0
    total_chunks: int = 0
    provider: str | None = None
    model: str | None = None
    llm_calls_total: int = 0
    llm_calls_finished: int = 0
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    usage: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    output_file: str | None = None
    partial_preview_file: str | None = None
    archive_file: str | None = None
    source_name: str | None = None
    error: str | None = None
    stop_requested: bool = False

    def to_api_dict(self) -> dict[str, Any]:
        return asdict(self)


app = FastAPI(title="Doc2MD Agent API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TASKS: dict[str, TaskInfo] = {}
TASK_LOCK = threading.Lock()


def _require_task(task_id: str) -> TaskInfo:
    with TASK_LOCK:
        task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


def _update_task(task_id: str, **kwargs: Any) -> None:
    with TASK_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return
        for key, value in kwargs.items():
            setattr(task, key, value)
        task.updated_at = _utc_now()


def _partial_preview_path(task_id: str, task: TaskInfo) -> Path:
    if task.partial_preview_file:
        return Path(task.partial_preview_file)
    return OUTPUT_ROOT / task_id / "result" / ".partial_preview.md"


def _resolve_preview_path(task_id: str, task: TaskInfo) -> Path | None:
    candidates: list[Path] = []
    if task.output_file:
        candidates.append(Path(task.output_file))
    candidates.append(_partial_preview_path(task_id, task))
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _resolve_output_root(task_id: str, task: TaskInfo) -> Path | None:
    if task.output_file:
        output_file = Path(task.output_file)
        if output_file.exists():
            return output_file.parent.resolve()

    partial_preview = _partial_preview_path(task_id, task)
    if partial_preview.exists():
        return partial_preview.parent.resolve()

    default_root = (OUTPUT_ROOT / task_id / "result").resolve()
    if default_root.exists():
        return default_root
    return None


def _append_task_event(task_id: str, event_type: str, message: str, **details: Any) -> None:
    with TASK_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return

        event: dict[str, Any] = {
            "timestamp": _utc_now(),
            "type": event_type,
            "message": message,
        }
        for key, value in details.items():
            if value is not None:
                event[key] = value

        task.events.append(event)
        if len(task.events) > MAX_TASK_EVENTS:
            task.events = task.events[-MAX_TASK_EVENTS:]
        task.updated_at = _utc_now()


def _is_stop_requested(task_id: str) -> bool:
    with TASK_LOCK:
        task = TASKS.get(task_id)
    return bool(task and task.stop_requested)


def _build_task_archive(output_dir: Path) -> str:
    return shutil.make_archive(
        base_name=str(output_dir),
        format="gztar",
        root_dir=str(output_dir.parent),
        base_dir=output_dir.name,
    )


def _ensure_partial_output_file(
    task_id: str,
    input_path: Path,
    output_dir: Path,
    agent: Doc2MDAgent | None = None,
) -> str | None:
    with TASK_LOCK:
        task = TASKS.get(task_id)

    if task and task.output_file and Path(task.output_file).exists():
        return task.output_file

    completed_output = output_dir / f"{input_path.stem}.md"
    if completed_output.exists():
        return str(completed_output)

    preview_path = _partial_preview_path(task_id, task) if task else output_dir / ".partial_preview.md"
    if not preview_path.exists():
        return None

    content = preview_path.read_text(encoding="utf-8")
    if not content.strip():
        return None

    if agent is not None:
        try:
            content = agent.postprocess_partial_markdown(content)
            preview_path.write_text(content, encoding="utf-8")
            _append_task_event(task_id, "partial_postprocess", "已对停止任务的阶段性内容执行后处理")
        except Exception as exc:
            _append_task_event(task_id, "partial_postprocess_failed", f"partial 后处理失败，保留原始内容：{exc}")

    partial_output = output_dir / f"{input_path.stem}.partial.md"
    partial_output.write_text(content, encoding="utf-8")
    return str(partial_output)


def _progress_from_stage(stage: str, current: int, total: int, message: str | None = None) -> tuple[int, str]:
    if stage == "preprocess":
        ratio = (current / total) if total else 0
        return 8 + int(ratio * 22), message or "文档预处理中"
    if stage == "analyze":
        ratio = (current / total) if total else 0
        return 30 + int(ratio * 10), message or "结构分析中"
    if stage == "convert":
        ratio = (current / total) if total else 0
        progress = 40 + int(ratio * 48)
        return progress, message or f"AI 转换中 {current}/{total}"
    if stage == "toc":
        ratio = (current / total) if total else 0
        return 90 + int(ratio * 8), message or "生成目录中"
    if stage == "done":
        return 100, message or "转换完成"
    return 5, message or "任务启动"


def _on_progress(task_id: str, stage: str, current: int, total: int, message: str | None = None) -> None:
    progress, stage_message = _progress_from_stage(stage, current, total, message)
    _update_task(
        task_id,
        stage=stage,
        progress=min(progress, 99) if stage != "done" else 100,
        message=stage_message,
        current_chunk=current,
        total_chunks=total,
    )
    _append_task_event(
        task_id,
        "progress",
        stage_message,
        stage=stage,
        current=current,
        total=total,
        progress=min(progress, 99) if stage != "done" else 100,
    )


def _on_agent_event(task_id: str, payload: dict[str, Any]) -> None:
    event_type = str(payload.get("type", "info"))
    message = str(payload.get("message", "转换步骤更新"))
    call_id = payload.get("call_id")
    planned_calls = payload.get("planned_calls")

    with TASK_LOCK:
        task = TASKS.get(task_id)
    if not task:
        return

    update_fields: dict[str, Any] = {}

    if isinstance(planned_calls, int):
        update_fields["llm_calls_total"] = max(task.llm_calls_total, planned_calls)
    if isinstance(call_id, int):
        if event_type in {"llm_call_started", "llm_call_completed", "llm_call_failed"}:
            current_total = update_fields.get("llm_calls_total", task.llm_calls_total)
            update_fields["llm_calls_total"] = max(current_total, call_id)
        if event_type == "llm_call_completed":
            update_fields["llm_calls_finished"] = max(task.llm_calls_finished, call_id)

    if update_fields:
        _update_task(task_id, **update_fields)

    details = {k: v for k, v in payload.items() if k != "message"}
    _append_task_event(task_id, event_type, message, **details)


def _run_task(task_id: str, input_path: Path, output_dir: Path, provider: str | None) -> None:
    agent: Doc2MDAgent | None = None
    try:
        config = load_config(provider_override=provider)
        selected_provider = config.get("provider")
        selected_model = config.get("providers", {}).get(selected_provider, {}).get("model", "")
        if _is_stop_requested(task_id):
            raise TaskStoppedError("任务在启动前被停止")
        partial_preview_path = output_dir / ".partial_preview.md"
        _update_task(
            task_id,
            status="running",
            stage="init",
            progress=5,
            message="任务启动",
            provider=selected_provider,
            model=selected_model,
            partial_preview_file=str(partial_preview_path),
        )
        _append_task_event(
            task_id,
            "system",
            f"任务启动：provider={selected_provider}, model={selected_model}",
        )

        agent = Doc2MDAgent(
            config,
            event_callback=lambda payload: _on_agent_event(task_id, payload),
            stop_checker=lambda: _is_stop_requested(task_id),
        )
        output_file, usage = agent.convert(
            str(input_path),
            str(output_dir),
            progress_callback=lambda stage, current, total, message=None: _on_progress(
                task_id, stage, current, total, message
            ),
        )

        archive_path = _build_task_archive(output_dir)
        with TASK_LOCK:
            task_snapshot = TASKS.get(task_id)
            current_total = task_snapshot.llm_calls_total if task_snapshot else 0
            current_finished = task_snapshot.llm_calls_finished if task_snapshot else 0
        _update_task(
            task_id,
            status="completed",
            stage="done",
            progress=100,
            message="转换完成",
            usage=usage,
            output_file=output_file,
            archive_file=archive_path,
            stop_requested=False,
            llm_calls_total=max(
                int(usage.get("llm_calls", 0)),
                current_total,
            ),
            llm_calls_finished=max(
                int(usage.get("llm_calls", 0)),
                current_finished,
            ),
        )
        _append_task_event(
            task_id,
            "system",
            f"转换完成：输出文件 {Path(output_file).name}",
            archive_file=archive_path,
        )
    except TaskStoppedError:
        usage = agent.llm.get_usage_summary() if agent else {}
        partial_output = _ensure_partial_output_file(task_id, input_path, output_dir, agent=agent)
        archive_path = _build_task_archive(output_dir)
        with TASK_LOCK:
            task_snapshot = TASKS.get(task_id)
            current_total = task_snapshot.llm_calls_total if task_snapshot else 0
            current_finished = task_snapshot.llm_calls_finished if task_snapshot else 0
        _update_task(
            task_id,
            status="stopped",
            stage="stopped",
            progress=100,
            message="任务已停止，已打包已生成内容",
            usage=usage,
            output_file=partial_output,
            archive_file=archive_path,
            error=None,
            stop_requested=False,
            llm_calls_total=max(
                int(usage.get("llm_calls", 0)),
                current_total,
            ),
            llm_calls_finished=max(
                int(usage.get("llm_calls", 0)),
                current_finished,
            ),
        )
        _append_task_event(
            task_id,
            "stopped",
            "任务已停止，已打包当前已生成内容",
            archive_file=archive_path,
            output_file=partial_output,
        )
    except Exception as exc:
        _update_task(
            task_id,
            status="failed",
            stage="error",
            progress=100,
            message="转换失败",
            error=str(exc),
            stop_requested=False,
        )
        _append_task_event(task_id, "error", f"转换失败：{exc}")


@app.post("/api/convert")
async def create_conversion_task(
    file: UploadFile = File(...),
    provider: str | None = Form(default=None),
) -> dict[str, str]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="仅支持 .docx 或 .doc 文件")

    task_id = uuid.uuid4().hex
    task_root = OUTPUT_ROOT / task_id
    input_dir = task_root / "input"
    output_dir = task_root / "result"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    input_path = input_dir / (file.filename or f"upload{suffix}")
    data = await file.read()
    input_path.write_bytes(data)

    task = TaskInfo(
        task_id=task_id,
        status="queued",
        source_name=input_path.name,
        partial_preview_file=str(output_dir / ".partial_preview.md"),
    )
    with TASK_LOCK:
        TASKS[task_id] = task
    _append_task_event(task_id, "queued", "任务已创建，等待处理")

    thread = threading.Thread(
        target=_run_task,
        args=(task_id, input_path, output_dir, provider),
        daemon=True,
    )
    thread.start()

    return {"task_id": task_id}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    task = _require_task(task_id)
    return task.to_api_dict()


@app.post("/api/tasks/{task_id}/stop")
def stop_task(task_id: str) -> dict[str, Any]:
    task = _require_task(task_id)
    if task.status == "stopped":
        return {
            "task_id": task_id,
            "status": "stopped",
            "message": "任务已停止",
        }
    if task.status in {"completed", "failed"}:
        raise HTTPException(status_code=409, detail=f"任务状态为 {task.status}，无法停止")
    if task.stop_requested:
        return {
            "task_id": task_id,
            "status": "stopping",
            "message": "已收到停止请求，正在结束任务",
        }

    _update_task(
        task_id,
        status="stopping",
        stage="stopping",
        message="正在停止任务并打包已生成内容",
        stop_requested=True,
    )
    _append_task_event(task_id, "stop_requested", "用户请求停止任务，正在收尾并打包")
    return {
        "task_id": task_id,
        "status": "stopping",
        "message": "已收到停止请求，正在结束任务并打包已生成内容",
    }


@app.get("/api/tasks/{task_id}/download")
def download_task(task_id: str) -> FileResponse:
    task = _require_task(task_id)
    if task.status not in {"completed", "stopped"} or not task.archive_file:
        raise HTTPException(status_code=409, detail="任务尚未完成或停止，无法下载")

    archive_path = Path(task.archive_file)
    if not archive_path.exists():
        raise HTTPException(status_code=404, detail="结果文件不存在")

    download_name = f"{Path(task.source_name or 'result').stem}.tar.gz"
    return FileResponse(archive_path, media_type="application/gzip", filename=download_name)


@app.get("/api/tasks/{task_id}/preview")
def preview_markdown(task_id: str) -> dict[str, Any]:
    task = _require_task(task_id)
    preview_path = _resolve_preview_path(task_id, task)
    if not preview_path:
        raise HTTPException(status_code=409, detail="任务尚未生成可预览内容")

    completed_output = Path(task.output_file).resolve() if task.output_file else None
    is_completed_preview = (
        task.status == "completed"
        and completed_output is not None
        and completed_output == preview_path.resolve()
    )

    return {
        "content": preview_path.read_text(encoding="utf-8"),
        "usage": task.usage,
        "asset_base_url": f"/api/tasks/{task_id}/assets",
        "partial": not is_completed_preview,
        "status": task.status,
    }


@app.get("/api/tasks/{task_id}/assets/{asset_path:path}")
def preview_asset(task_id: str, asset_path: str) -> FileResponse:
    task = _require_task(task_id)
    output_root = _resolve_output_root(task_id, task)
    if not output_root:
        raise HTTPException(status_code=409, detail="任务尚未生成可访问的资源目录")
    target = (output_root / asset_path).resolve()

    if output_root not in target.parents and target != output_root:
        raise HTTPException(status_code=403, detail="非法路径")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="资源不存在")

    return FileResponse(target)


@app.get("/api/config/providers")
def list_providers() -> dict[str, Any]:
    config = load_config()
    providers = []
    for name, provider_conf in config.get("providers", {}).items():
        providers.append(
            {
                "name": name,
                "model": provider_conf.get("model", ""),
                "base_url": provider_conf.get("base_url", ""),
            }
        )

    return {
        "current_provider": config.get("provider"),
        "providers": providers,
    }


if (FRONTEND_DIST / "index.html").exists():
    assets_dir = FRONTEND_DIST / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_frontend(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")

        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.exists() and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.server:app", host="0.0.0.0", port=9999, reload=False)
