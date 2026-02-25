from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import string
import subprocess
import threading
import time
import uuid
import sys
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError


BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
OUTPUTS_DIR = BASE_DIR / "outputs"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

PYTHON_EXECUTABLE = os.environ.get("AUTOFIGURE_PYTHON") or sys.executable

# 从环境变量统一读取密钥和配置
API_KEY = os.environ.get("API_KEY", "")
SAM_API_KEY = os.environ.get("ROBOFLOW_API_KEY") or os.environ.get("FAL_KEY") or ""
DEFAULT_PROVIDER = os.environ.get("AUTOFIGURE_PROVIDER", "openrouter")
DEFAULT_SAM_BACKEND = "roboflow"
DEFAULT_SAM_PROMPT = "icon,symbol,shape,object,arrow,text,diagram,circle,box"

# 从方法文本中提取潜在的 SAM prompt 关键词
# 这些是科研图中常见的视觉元素类别词
_SAM_KEYWORD_CANDIDATES = {
    # 生物学
    "cell", "protein", "receptor", "molecule", "enzyme", "antibody",
    "membrane", "mitochondria", "nucleus", "ribosome", "vesicle",
    "chromosome", "dna", "rna", "gene", "bacteria", "virus",
    "macrophage", "neutrophil", "lymphocyte", "neuron", "synapse",
    "organ", "tissue", "blood", "tumor", "cancer",
    # 化学/结构
    "structure", "complex", "pathway", "channel", "pump",
    # 图形元素
    "arrow", "circle", "rectangle", "triangle", "star", "line",
    "label", "node", "edge", "block", "flow",
}

def _extract_sam_keywords(method_text: str) -> list[str]:
    """从方法文本中提取可能的图形元素关键词作为额外 SAM prompts"""
    words = set(re.findall(r'[a-zA-Z]{3,}', method_text.lower()))
    extra = [w for w in words if w in _SAM_KEYWORD_CANDIDATES]
    return extra


def _build_sam_prompt(method_text: str) -> str:
    """构建 SAM prompt：默认词 + 从方法文本提取的关键词"""
    base = set(DEFAULT_SAM_PROMPT.split(","))
    extra = _extract_sam_keywords(method_text)
    all_prompts = list(base | set(extra))
    result = ",".join(all_prompts)
    if extra:
        print(f"[sam] 从文本中提取了额外关键词: {extra}")
    print(f"[sam] 最终 SAM prompts: {result}")
    return result
DEFAULT_PLACEHOLDER_MODE = "label"
DEFAULT_MERGE_THRESHOLD = 0.01
JOB_TIMEOUT_SECONDS = 600  # 10 分钟超时
JOB_RETENTION_SECONDS = 3600  # 已完成任务保留 1 小时

# === 邀请码系统 ===
DB_PATH = os.path.join(os.environ.get("DATA_DIR", "."), "invites.db")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "ccccckd011122")
ADMIN_TOKENS: set[str] = set()


def init_db() -> None:
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS invite_codes (
            code TEXT PRIMARY KEY,
            code_type TEXT NOT NULL DEFAULT 'T',
            daily_limit INTEGER NOT NULL DEFAULT 5,
            used_today INTEGER NOT NULL DEFAULT 0,
            total_used INTEGER NOT NULL DEFAULT 0,
            last_used_date TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            expires_at TEXT
        )
    """)
    db.commit()
    db.close()


def _get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def _generate_code(code_type: str = "T") -> str:
    prefix = "P-" if code_type == "P" else "T-"
    chars = string.ascii_uppercase + string.digits
    suffix = "".join(secrets.choice(chars) for _ in range(8))
    return prefix + suffix


def _check_and_reset_daily(db: sqlite3.Connection, code_row: sqlite3.Row) -> dict:
    """检查并重置每日使用次数，返回更新后的字典"""
    today = date.today().isoformat()
    data = dict(code_row)
    if data["last_used_date"] != today:
        db.execute("UPDATE invite_codes SET used_today = 0, last_used_date = ? WHERE code = ?",
                   (today, data["code"]))
        db.commit()
        data["used_today"] = 0
        data["last_used_date"] = today
    return data


init_db()


@dataclass
class Job:
    job_id: str
    output_dir: Path
    process: subprocess.Popen
    queue: queue.Queue
    log_path: Path
    log_lock: threading.Lock = field(default_factory=threading.Lock)
    seen: set[str] = field(default_factory=set)
    done: bool = False
    last_stderr: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    # 所有订阅此任务 SSE 的 asyncio.Queue 列表
    _subscribers: list = field(default_factory=list)
    _sub_lock: threading.Lock = field(default_factory=threading.Lock)

    def push(self, event: str, data: dict) -> None:
        msg = {"event": event, "data": data}
        self.queue.put(msg)
        # 同时推送给所有异步订阅者
        with self._sub_lock:
            for aq, loop in self._subscribers:
                loop.call_soon_threadsafe(aq.put_nowait, msg)

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
        aq = asyncio.Queue()
        with self._sub_lock:
            self._subscribers.append((aq, loop))
        return aq

    def unsubscribe(self, aq: asyncio.Queue) -> None:
        with self._sub_lock:
            self._subscribers = [(q, l) for q, l in self._subscribers if q is not aq]

    def write_log(self, stream: str, line: str) -> None:
        with self.log_lock:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(f"[{stream}] {line}\n")


class RunRequest(BaseModel):
    method_text: str = Field(..., min_length=1)
    optimize_iterations: Optional[int] = None
    reference_image_path: Optional[str] = None
    invite_code: Optional[str] = None


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

JOBS: dict[str, Job] = {}


@app.exception_handler(RequestValidationError)
@app.exception_handler(ValidationError)
async def validation_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=422,
        content={"error": "请求参数有误，请检查输入内容。"},
    )


@app.exception_handler(500)
async def internal_error_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"error": "服务器内部错误，请稍后重试。"},
    )



class VerifyCodeRequest(BaseModel):
    code: str


@app.post("/api/verify-code")
def verify_code(req: VerifyCodeRequest) -> JSONResponse:
    db = _get_db()
    row = db.execute("SELECT * FROM invite_codes WHERE code = ?", (req.code,)).fetchone()
    if not row:
        db.close()
        return JSONResponse(status_code=404, content={"error": "邀请码不存在"})
    data = _check_and_reset_daily(db, row)
    db.close()
    if not data["is_active"]:
        return JSONResponse(status_code=403, content={"error": "邀请码已被禁用"})
    if data["expires_at"] and data["expires_at"] < date.today().isoformat():
        return JSONResponse(status_code=403, content={"error": "邀请码已过期"})
    remaining = data["daily_limit"] - data["used_today"]
    return JSONResponse({
        "valid": True,
        "code_type": data["code_type"],
        "daily_limit": data["daily_limit"],
        "used_today": data["used_today"],
        "remaining": remaining,
    })


@app.post("/api/run")
def run_job(req: RunRequest) -> JSONResponse:
    # 邀请码验证 + 扣次数
    if not req.invite_code:
        return JSONResponse(status_code=403, content={"error": "请输入邀请码后再生成"})
    db = _get_db()
    row = db.execute("SELECT * FROM invite_codes WHERE code = ?", (req.invite_code,)).fetchone()
    if not row:
        db.close()
        return JSONResponse(status_code=403, content={"error": "邀请码无效"})
    code_data = _check_and_reset_daily(db, row)
    if not code_data["is_active"]:
        db.close()
        return JSONResponse(status_code=403, content={"error": "邀请码已被禁用"})
    if code_data["expires_at"] and code_data["expires_at"] < date.today().isoformat():
        db.close()
        return JSONResponse(status_code=403, content={"error": "邀请码已过期"})
    if code_data["used_today"] >= code_data["daily_limit"]:
        db.close()
        return JSONResponse(status_code=403, content={"error": f"今日使用次数已达上限（{code_data['daily_limit']}次/天）"})
    # 扣次数
    db.execute("UPDATE invite_codes SET used_today = used_today + 1, total_used = total_used + 1, last_used_date = ? WHERE code = ?",
               (date.today().isoformat(), req.invite_code))
    db.commit()
    db.close()

    try:
        print(f"[api/run] method_text={req.method_text[:80]!r} optimize_iterations={req.optimize_iterations} ref={req.reference_image_path}")
        job_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        output_dir = OUTPUTS_DIR / job_id
        output_dir.mkdir(parents=True, exist_ok=True)

        if not API_KEY:
            return JSONResponse(
                status_code=500,
                content={"error": "服务器未配置 API 密钥，请联系管理员设置 API_KEY 环境变量。"},
            )

        # 检测中文并翻译
        method_text = req.method_text
        if re.search(r'[\u4e00-\u9fff]', method_text):
            try:
                method_text = _translate_chinese(method_text)
            except Exception as e:
                print(f"[translate] 翻译失败，使用原文: {e}")

        cmd = [
            PYTHON_EXECUTABLE,
            str(BASE_DIR / "autofigure2.py"),
            "--method_text",
            method_text,
            "--output_dir",
            str(output_dir),
            "--provider",
            DEFAULT_PROVIDER,
            "--api_key",
            API_KEY,
            "--sam_backend",
            DEFAULT_SAM_BACKEND,
            "--sam_prompt",
            _build_sam_prompt(method_text),
            "--placeholder_mode",
            DEFAULT_PLACEHOLDER_MODE,
            "--merge_threshold",
            str(DEFAULT_MERGE_THRESHOLD),
        ]

        if SAM_API_KEY:
            cmd += ["--sam_api_key", SAM_API_KEY]
        if req.optimize_iterations is not None:
            cmd += ["--optimize_iterations", str(req.optimize_iterations)]

        reference_path = req.reference_image_path
        if reference_path:
            reference_path = (
                str((BASE_DIR / reference_path).resolve())
                if not Path(reference_path).is_absolute()
                else reference_path
            )
            cmd += ["--reference_image_path", reference_path]

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        log_path = output_dir / "run.log"
        # 隐藏 API 密钥，避免泄露到日志
        safe_cmd = []
        skip_next = False
        for part in cmd:
            if skip_next:
                safe_cmd.append("***")
                skip_next = False
            elif part in ("--api_key", "--sam_api_key"):
                safe_cmd.append(part)
                skip_next = True
            else:
                safe_cmd.append(part)
        log_path.write_text(
            f"[meta] python={PYTHON_EXECUTABLE}\n[meta] cmd={' '.join(safe_cmd)}\n",
            encoding="utf-8",
        )

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
            cwd=str(BASE_DIR),
        )

        job = Job(
            job_id=job_id,
            output_dir=output_dir,
            process=process,
            queue=queue.Queue(),
            log_path=log_path,
        )
        JOBS[job_id] = job

        monitor_thread = threading.Thread(target=_monitor_job, args=(job,), daemon=True)
        monitor_thread.start()

        return JSONResponse({"job_id": job_id})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"error": f"启动任务失败：{e}"},
        )


@app.post("/api/upload")
async def upload_reference(file: UploadFile = File(...)) -> JSONResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="未提供文件")
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="仅支持图片文件")

    ext = Path(file.filename).suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
        ext = ".png"

    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件过大（上限 20MB）")

    name = f"{uuid.uuid4().hex}{ext}"
    out_path = UPLOADS_DIR / name
    out_path.write_bytes(data)

    rel_path = out_path.relative_to(BASE_DIR).as_posix()
    return JSONResponse(
        {"path": rel_path, "url": f"/api/uploads/{name}", "name": file.filename}
    )


@app.post("/api/cancel/{job_id}")
def cancel_job(job_id: str) -> JSONResponse:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.done:
        return JSONResponse({"status": "already_finished"})
    try:
        job.process.terminate()
    except Exception:
        pass
    return JSONResponse({"status": "cancelled"})


@app.get("/api/events/{job_id}")
async def stream_events(job_id: str) -> StreamingResponse:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    loop = asyncio.get_running_loop()
    aq = job.subscribe(loop)

    async def event_stream():
        try:
            # 如果任务已经完成，重放产物和完成事件
            if job.done:
                print(f"[SSE] job {job_id} already done, replaying events")
                # 重放已发现的 artifact 事件
                for rel_path in job.seen:
                    kind = _classify_artifact(rel_path)
                    name = Path(rel_path).name
                    yield _format_sse("artifact", {
                        "kind": kind,
                        "name": name,
                        "path": rel_path,
                        "url": f"/api/artifacts/{job.job_id}/{rel_path}",
                    })
                # 重放 finished 状态
                finished_data: dict = {"state": "finished", "code": job.process.returncode}
                if job.process.returncode and job.process.returncode != 0 and job.last_stderr:
                    finished_data["error"] = job.last_stderr
                yield _format_sse("status", finished_data)
                return

            while True:
                try:
                    item = await asyncio.wait_for(aq.get(), timeout=10.0)
                except asyncio.TimeoutError:
                    if job.done:
                        print(f"[SSE] job {job_id} done, closing stream")
                        break
                    # 心跳：每 10 秒发送，防止代理断连
                    yield ": keepalive\n\n"
                    continue
                if item.get("event") == "close":
                    print(f"[SSE] job {job_id} received close event")
                    break
                yield _format_sse(item["event"], item["data"])
        finally:
            job.unsubscribe(aq)
            print(f"[SSE] job {job_id} stream ended, subscriber removed")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/logs/{job_id}")
def get_logs(job_id: str) -> PlainTextResponse:
    """返回任务的日志文件内容（纯文本）"""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        content = job.log_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        content = ""
    return PlainTextResponse(content)


@app.get("/api/artifacts-list/{job_id}")
def list_artifacts(job_id: str) -> JSONResponse:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    items = []
    for path in sorted(job.output_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(job.output_dir).as_posix()
        kind = _classify_artifact(rel)
        items.append({
            "kind": kind,
            "name": path.name,
            "path": rel,
            "url": f"/api/artifacts/{job_id}/{rel}",
        })
    return JSONResponse(items)


@app.get("/api/artifacts/{job_id}/{path:path}")
def get_artifact(job_id: str, path: str) -> FileResponse:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    candidate = (job.output_dir / path).resolve()
    if not str(candidate).startswith(str(job.output_dir.resolve())):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(candidate)


@app.get("/api/uploads/{filename}")
def get_upload(filename: str) -> FileResponse:
    candidate = (UPLOADS_DIR / filename).resolve()
    if not str(candidate).startswith(str(UPLOADS_DIR.resolve())):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(candidate)


def _translate_chinese(text: str) -> str:
    """使用 OpenRouter API 将中文文本翻译为英文"""
    import requests as _requests

    base_url = "https://openrouter.ai/api/v1/chat/completions"
    referer = os.environ.get('OPENROUTER_REFERER', 'https://autofigure.app')
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {API_KEY}',
        'HTTP-Referer': referer,
        'X-Title': 'AutoFigure',
    }
    payload = {
        'model': 'google/gemini-2.0-flash-001',
        'messages': [
            {'role': 'system', 'content': 'You are a scientific translator. Translate the following Chinese text into English. Keep scientific terminology accurate. Output ONLY the translated text, nothing else.'},
            {'role': 'user', 'content': text},
        ],
        'max_tokens': 4000,
        'temperature': 0.3,
        'stream': False,
    }
    resp = _requests.post(base_url, headers=headers, json=payload, timeout=60)
    if resp.status_code != 200:
        raise Exception(f"Translation API error: {resp.status_code}")
    result = resp.json()
    choices = result.get('choices', [])
    if choices:
        translated = choices[0].get('message', {}).get('content', '').strip()
        if translated:
            print(f"[translate] 翻译完成: {text[:50]}... -> {translated[:50]}...")
            return translated
    raise Exception("Translation returned empty result")


def _format_sse(event: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=True)
    return f"event: {event}\ndata: {payload}\n\n"


def _monitor_job(job: Job) -> None:
    print(f"[monitor] job {job.job_id} started, pushing 'started' event")
    job.push("status", {"state": "started"})

    stdout_thread = threading.Thread(
        target=_pipe_output, args=(job, job.process.stdout, "stdout"), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_pipe_output, args=(job, job.process.stderr, "stderr"), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()

    idle_cycles = 0
    timed_out = False
    scan_count = 0
    while True:
        _scan_artifacts(job)
        scan_count += 1
        if scan_count % 20 == 0:
            print(f"[monitor] job {job.job_id} scan #{scan_count}, process alive={job.process.poll() is None}, seen={len(job.seen)} files")

        # 超时检查
        elapsed = time.time() - job.started_at
        if elapsed > JOB_TIMEOUT_SECONDS and job.process.poll() is None:
            print(f"[monitor] job {job.job_id} timed out after {elapsed:.0f}s")
            job.process.terminate()
            try:
                job.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                job.process.kill()
            timed_out = True
            break

        if job.process.poll() is not None:
            idle_cycles += 1
        else:
            idle_cycles = 0

        if idle_cycles >= 4:
            break
        time.sleep(0.5)

    _scan_artifacts(job)

    if timed_out:
        finished_data: dict = {"state": "finished", "code": -1, "error": f"任务超时（超过 {JOB_TIMEOUT_SECONDS // 60} 分钟），已自动终止。"}
    else:
        finished_data = {"state": "finished", "code": job.process.returncode}
        if job.process.returncode and job.process.returncode != 0 and job.last_stderr:
            finished_data["error"] = job.last_stderr

    print(f"[monitor] job {job.job_id} finished: code={finished_data.get('code')}, pushing finished event")
    job.push("status", finished_data)
    job.push(
        "artifact",
        {
            "kind": "log",
            "name": job.log_path.name,
            "path": job.log_path.relative_to(job.output_dir).as_posix(),
            "url": f"/api/artifacts/{job.job_id}/{job.log_path.name}",
        },
    )
    job.finished_at = time.time()
    job.done = True
    job.push("close", {})
    print(f"[monitor] job {job.job_id} close event pushed, done=True")

    # 清理过期任务，释放内存
    _cleanup_old_jobs()


def _cleanup_old_jobs() -> None:
    """清理已完成且超过保留时间的任务，释放内存"""
    now = time.time()
    expired = [
        jid for jid, j in JOBS.items()
        if j.done and j.finished_at > 0 and (now - j.finished_at) > JOB_RETENTION_SECONDS
    ]
    for jid in expired:
        JOBS.pop(jid, None)


def _pipe_output(job: Job, pipe, stream_name: str) -> None:
    if pipe is None:
        return
    for line in iter(pipe.readline, ""):
        text = line.rstrip()
        if text:
            job.write_log(stream_name, text)
            job.push("log", {"stream": stream_name, "line": text})
            if stream_name == "stderr":
                job.last_stderr = text
    pipe.close()


def _scan_artifacts(job: Job) -> None:
    output_dir = job.output_dir
    candidates = [
        output_dir / "figure.png",
        output_dir / "samed.png",
        output_dir / "template.svg",
        output_dir / "optimized_template.svg",
        output_dir / "final.svg",
    ]

    icons_dir = output_dir / "icons"
    if icons_dir.is_dir():
        candidates.extend(icons_dir.glob("icon_*.png"))

    for path in candidates:
        if not path.is_file():
            continue
        rel_path = path.relative_to(output_dir).as_posix()
        if rel_path in job.seen:
            continue
        job.seen.add(rel_path)

        kind = _classify_artifact(rel_path)
        print(f"[scan] job {job.job_id} found new artifact: {rel_path} (kind={kind})")
        job.push(
            "artifact",
            {
                "kind": kind,
                "name": path.name,
                "path": rel_path,
                "url": f"/api/artifacts/{job.job_id}/{rel_path}",
            },
        )


def _classify_artifact(rel_path: str) -> str:
    if rel_path == "figure.png":
        return "figure"
    if rel_path == "samed.png":
        return "samed"
    if rel_path.endswith("_nobg.png"):
        return "icon_nobg"
    if rel_path.startswith("icons/") and rel_path.endswith(".png"):
        return "icon_raw"
    if rel_path == "template.svg":
        return "template_svg"
    if rel_path == "optimized_template.svg":
        return "optimized_svg"
    if rel_path == "final.svg":
        return "final_svg"
    return "artifact"


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return True
    return False


def _pids_on_port(port: int) -> set[int]:
    pids: set[int] = set()

    if shutil.which("lsof"):
        result = subprocess.run(
            ["lsof", "-t", f"-i:{port}"],
            capture_output=True,
            text=True,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.add(int(line))
        return pids

    if shutil.which("ss"):
        result = subprocess.run(
            ["ss", "-lptn", f"sport = :{port}"],
            capture_output=True,
            text=True,
        )
        for line in result.stdout.splitlines():
            if "pid=" in line:
                for part in line.split("pid=")[1:]:
                    pid_str = "".join(ch for ch in part if ch.isdigit())
                    if pid_str:
                        pids.add(int(pid_str))
        return pids

    if shutil.which("netstat"):
        result = subprocess.run(
            ["netstat", "-tlnp"],
            capture_output=True,
            text=True,
        )
        for line in result.stdout.splitlines():
            if f":{port} " not in line or "LISTEN" not in line:
                continue
            fields = line.split()
            if fields and "/" in fields[-1]:
                pid_part = fields[-1].split("/")[0]
                if pid_part.isdigit():
                    pids.add(int(pid_part))

    return pids


def _read_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            data = handle.read()
        parts = [p for p in data.split(b"\x00") if p]
        return " ".join(part.decode(errors="ignore") for part in parts)
    except OSError:
        return ""


def _is_uvicorn_process(pid: int) -> bool:
    cmdline = _read_cmdline(pid)
    if not cmdline:
        return False
    if "uvicorn" not in cmdline:
        return False
    return "server:app" in cmdline or "server.py" in cmdline


def _terminate_pids(pids: set[int], timeout: float = 2.0) -> None:
    current_pid = os.getpid()
    for pid in sorted(pids):
        if pid <= 1 or pid == current_pid:
            continue
        if not _is_uvicorn_process(pid):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue

    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = False
        for pid in pids:
            if pid <= 1 or pid == current_pid:
                continue
            if not _is_uvicorn_process(pid):
                continue
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                continue
        if not alive:
            return
        time.sleep(0.1)

    for pid in sorted(pids):
        if pid <= 1 or pid == current_pid:
            continue
        if not _is_uvicorn_process(pid):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue


def _ensure_port_free(port: int) -> None:
    if not _port_in_use(port):
        return
    pids = _pids_on_port(port)
    if not pids:
        return
    _terminate_pids(pids)


# === Admin APIs ===

class AdminLoginRequest(BaseModel):
    password: str


class AdminGenerateRequest(BaseModel):
    code_type: str = "T"
    daily_limit: int = 5
    note: str = ""
    count: int = 1
    expires_at: Optional[str] = None


class AdminRevokeRequest(BaseModel):
    code: str


def _require_admin(request: Request) -> None:
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "") if auth.startswith("Bearer ") else ""
    if token not in ADMIN_TOKENS:
        raise HTTPException(status_code=401, detail="未授权")


@app.post("/api/admin/login")
def admin_login(req: AdminLoginRequest) -> JSONResponse:
    if req.password != ADMIN_PASSWORD:
        return JSONResponse(status_code=403, content={"error": "密码错误"})
    token = secrets.token_urlsafe(32)
    ADMIN_TOKENS.add(token)
    return JSONResponse({"token": token})


@app.get("/api/admin/codes")
def admin_list_codes(request: Request) -> JSONResponse:
    _require_admin(request)
    db = _get_db()
    rows = db.execute("SELECT * FROM invite_codes ORDER BY created_at DESC").fetchall()
    today = date.today().isoformat()
    codes = []
    for r in rows:
        d = dict(r)
        if d["last_used_date"] != today:
            d["used_today"] = 0
        codes.append(d)
    db.close()
    return JSONResponse(codes)


@app.post("/api/admin/generate")
def admin_generate(request: Request, req: AdminGenerateRequest) -> JSONResponse:
    _require_admin(request)
    count = max(1, min(20, req.count))
    db = _get_db()
    now = datetime.now().isoformat()
    generated = []
    for _ in range(count):
        code = _generate_code(req.code_type)
        db.execute(
            "INSERT INTO invite_codes (code, code_type, daily_limit, note, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (code, req.code_type, req.daily_limit, req.note, now, req.expires_at),
        )
        generated.append(code)
    db.commit()
    db.close()
    return JSONResponse({"codes": generated})


@app.post("/api/admin/revoke")
def admin_revoke(request: Request, req: AdminRevokeRequest) -> JSONResponse:
    _require_admin(request)
    db = _get_db()
    db.execute("UPDATE invite_codes SET is_active = 0 WHERE code = ?", (req.code,))
    db.commit()
    db.close()
    return JSONResponse({"ok": True})


@app.get("/api/admin/stats")
def admin_stats(request: Request) -> JSONResponse:
    _require_admin(request)
    db = _get_db()
    total = db.execute("SELECT COUNT(*) FROM invite_codes").fetchone()[0]
    active = db.execute("SELECT COUNT(*) FROM invite_codes WHERE is_active = 1").fetchone()[0]
    today = date.today().isoformat()
    used_today = db.execute("SELECT SUM(CASE WHEN last_used_date = ? THEN used_today ELSE 0 END) FROM invite_codes", (today,)).fetchone()[0] or 0
    total_used = db.execute("SELECT SUM(total_used) FROM invite_codes").fetchone()[0] or 0
    db.close()
    return JSONResponse({
        "total_codes": total,
        "active_codes": active,
        "used_today": used_today,
        "total_used": total_used,
    })


@app.get("/admin")
def admin_page() -> FileResponse:
    return FileResponse(str(WEB_DIR / "admin.html"))


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    port = 8000
    _ensure_port_free(port)
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        access_log=False,
    )
