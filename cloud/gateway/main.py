"""H3 云端安全网关。

网关是唯一公开端口；ComfyUI 只监听容器内部的 127.0.0.1。素材按哈希
去重，任务串行运行，成片通过带令牌的接口回收到 Mac。
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

from h3_backend import (
    ComfyClient,
    ComfyError,
    ModelSet,
    ReferenceInputs,
    SamplingSettings,
    build_reference_graph,
    build_video_graph,
)

RUNTIME = Path(os.environ.get("H3_RUNTIME", "/workspace/h3-runtime"))
ASSETS = RUNTIME / "assets"
OUTPUTS = RUNTIME / "outputs"
TOKEN = os.environ.get("H3_GATEWAY_TOKEN", "")
COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
MAX_ASSET_BYTES = int(os.environ.get("H3_MAX_ASSET_BYTES", str(4 * 1024**3)))
JOB_TIMEOUT_SECONDS = int(os.environ.get("H3_JOB_TIMEOUT_MIN", "45")) * 60
SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
ALLOWED_ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".wav", ".mp3", ".m4a"}
BOOTSTRAP_STATUS = RUNTIME / "bootstrap-status.json"
LOG_FILES = {
    "bootstrap": RUNTIME / "bootstrap.log",
    "comfy": RUNTIME / "comfy.log",
    "gateway": RUNTIME / "gateway.log",
}

for folder in (ASSETS, OUTPUTS):
    folder.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="H3 Production Gateway", docs_url=None, redoc_url=None)
client = ComfyClient(COMFY_URL, timeout=120)


def authorize(authorization: str | None = Header(default=None)) -> None:
    if not TOKEN:
        raise HTTPException(503, "网关令牌尚未配置")
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "令牌无效")


def models_for(reference_mode: bool) -> ModelSet:
    return ModelSet(
        diffusion_model=(
            "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
            if reference_mode
            else "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
        ),
        text_encoder="qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        video_vae="minimax_h3_video_vae_fp16.safetensors",
        audio_vae="minimax_h3_audio_vae_fp32.safetensors",
    )


@dataclass
class RemoteJob:
    id: str
    payload: dict[str, Any]
    status: str = "queued"
    stage: str = "等待"
    progress: float = 0.0
    prompt_id: str | None = None
    artifact: str | None = None
    raw_artifact: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancel_requested: bool = False

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("payload", None)
        data.pop("cancel_requested", None)
        for source, target in {
            "prompt_id": "promptId",
            "raw_artifact": "rawArtifact",
            "created_at": "createdAt",
            "started_at": "startedAt",
            "finished_at": "finishedAt",
        }.items():
            data[target] = data.pop(source)
        return data


jobs: dict[str, RemoteJob] = {}
job_queue: queue.Queue[str] = queue.Queue()
jobs_lock = threading.Lock()


def read_bootstrap_status() -> dict[str, Any]:
    try:
        value = json.loads(BOOTSTRAP_STATUS.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def tail_log(path: Path, limit: int = 8_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def runtime_health() -> dict[str, Any]:
    bootstrap = read_bootstrap_status()
    comfy_alive = client.is_alive()
    h3_nodes = comfy_alive and client.has_h3_nodes()
    profile = bootstrap.get("profile", [])
    needs_seedvr2 = isinstance(profile, list) and "seedvr2" in profile
    seedvr2_nodes = comfy_alive and client.has_node("SeedVR2")
    failed = bootstrap.get("status") == "failed"
    ready = comfy_alive and h3_nodes and (seedvr2_nodes or not needs_seedvr2)
    status_name = "failed" if failed else "ready" if ready else str(bootstrap.get("status", "starting"))
    stage = "云端执行器已就绪" if ready else str(bootstrap.get("stage", "启动中"))
    return {
        "status": status_name,
        "stage": stage,
        "profile": profile,
        "downloadedBytes": bootstrap.get("downloadedBytes", 0),
        "totalBytes": bootstrap.get("totalBytes", 0),
        "currentFile": bootstrap.get("currentFile"),
        "error": bootstrap.get("error"),
        "updatedAt": bootstrap.get("updatedAt"),
        "comfy": comfy_alive,
        "h3Nodes": h3_nodes,
        "seedvr2Nodes": seedvr2_nodes,
        "queue": job_queue.qsize(),
    }


def find_artifact(outputs: dict[str, Any], job_id: str) -> Path:
    for node_output in outputs.values():
        for bucket in ("images", "videos", "gifs"):
            for entry in node_output.get(bucket, []) or []:
                filename = entry.get("filename")
                if not filename:
                    continue
                destination = OUTPUTS / f"{job_id}{Path(filename).suffix or '.mp4'}"
                return client.download_output(filename, entry.get("subfolder", ""), destination)
    raise ComfyError("ComfyUI 报告成功，但没有返回视频文件")


def verify_video(path: Path) -> dict[str, Any]:
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "stream=codec_name,width,height,pix_fmt,r_frame_rate:format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if probe.returncode != 0:
        raise ComfyError(f"ffprobe 校验失败：{probe.stderr[-400:]}")
    report = json.loads(probe.stdout)
    duration = float(report.get("format", {}).get("duration", 0))
    if duration < 0.5:
        raise ComfyError("生成文件没有有效的视频时长")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    report["sha256"] = digest.hexdigest()
    return report


def wait_for_outputs(prompt_id: str, job: RemoteJob, deadline: float, on_progress: Any) -> dict[str, Any]:
    try:
        return client.wait(
            prompt_id,
            on_progress=on_progress,
            should_cancel=lambda: job.cancel_requested or time.time() >= deadline,
        )
    except ComfyError as error:
        if time.time() >= deadline and not job.cancel_requested:
            raise ComfyError(f"任务超过 {JOB_TIMEOUT_SECONDS // 60} 分钟，已中断并保留诊断信息") from error
        raise


def enhance_video(source: Path, job: RemoteJob, seed: int, deadline: float) -> Path:
    """用 SeedVR2 将竖屏短边增强到 1080，并保留原始 H3 文件。"""
    job.stage = "SeedVR2 高清增强"
    job.progress = 0.93
    uploaded = client.upload_file(source)
    graph = {
        "1": {"class_type": "LoadVideo", "inputs": {"file": uploaded}},
        "2": {"class_type": "GetVideoComponents", "inputs": {"video": ["1", 0]}},
        "3": {
            "class_type": "SeedVR2BlockSwap",
            "inputs": {"blocks_to_swap": 0, "offload_io_components": False},
        },
        "4": {
            "class_type": "SeedVR2ExtraArgs",
            "inputs": {
                "tiled_vae": False,
                "vae_tile_size": 512,
                "vae_tile_overlap": 64,
                "preserve_vram": False,
                "cache_model": True,
                "enable_debug": False,
                "device": "cuda:0",
            },
        },
        "5": {
            "class_type": "SeedVR2",
            "inputs": {
                "images": ["2", 0],
                "model": "seedvr2_ema_3b_fp8_e4m3fn.safetensors",
                "seed": seed,
                "new_resolution": 1080,
                "batch_size": 9,
                "color_correction": "wavelet",
                "input_noise_scale": 0.0,
                "latent_noise_scale": 0.0,
                "block_swap_config": ["3", 0],
                "extra_args": ["4", 0],
            },
        },
        "6": {
            "class_type": "CreateVideo",
            "inputs": {"images": ["5", 0], "audio": ["2", 1], "fps": ["2", 2]},
        },
        "7": {
            "class_type": "SaveVideo",
            "inputs": {
                "video": ["6", 0],
                "filename_prefix": f"video/h3production_{job.id}_1080p",
                "format": "mp4",
                "codec": "h264",
                "codec.encoding": "re-encode",
                "codec.encoding.crf": 14,
            },
        },
    }
    prompt_id = client.submit(graph)
    outputs = wait_for_outputs(
        prompt_id,
        job,
        deadline,
        on_progress=lambda _: setattr(job, "progress", min(0.99, job.progress + 0.001)),
    )
    return find_artifact(outputs, f"{job.id}_1080p")


def execute(job: RemoteJob) -> None:
    payload = job.payload
    preset = payload["preset"]
    deadline = time.time() + JOB_TIMEOUT_SECONDS
    asset_paths = [Path(path) for path in payload.get("assetPaths", [])][:9]
    for path in asset_paths:
        if ASSETS not in path.parents or not path.is_file():
            raise ComfyError("任务引用了尚未上传的素材")

    job.stage = "上传到 ComfyUI"
    job.progress = 0.04
    uploaded = [client.upload_file(path) for path in asset_paths]
    sampling = SamplingSettings(
        steps=int(preset.get("steps", 25)),
        sampler="res_multistep",
        scheduler="beta",
        seed=int(payload["seed"]),
    )
    reference_mode = bool(uploaded)
    prefix = f"h3production_{job.id}"
    if reference_mode:
        graph = build_reference_graph(
            prompt=payload["prompt"],
            width=int(preset["width"]),
            height=int(preset["height"]),
            length=int(preset.get("frames", 362)),
            models=models_for(True),
            sampling=sampling,
            seed=int(payload["seed"]),
            refs=ReferenceInputs(images=uploaded, image_size_mode="match"),
            prefix=prefix,
        )
    else:
        graph = build_video_graph(
            prompt=payload["prompt"],
            width=int(preset["width"]),
            height=int(preset["height"]),
            length=int(preset.get("frames", 362)),
            models=models_for(False),
            sampling=sampling,
            seed=int(payload["seed"]),
            prefix=prefix,
        )

    job.stage = "MiniMax H3 生成"
    job.progress = 0.08
    job.prompt_id = client.submit(graph)

    started = time.time()
    estimated = max(1, int(preset.get("estimatedSeconds", 1009)))

    def progress(_: str) -> None:
        elapsed = time.time() - started
        job.progress = min(0.92, 0.08 + 0.84 * elapsed / estimated)

    outputs = wait_for_outputs(
        job.prompt_id,
        job,
        deadline,
        on_progress=progress,
    )
    job.stage = "校验并准备下载"
    job.progress = 0.95
    artifact = find_artifact(outputs, job.id)
    job.raw_artifact = str(artifact)
    verification = verify_video(artifact)
    artifact.with_suffix(".verification.json").write_text(
        json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    final_artifact = artifact
    if bool(preset.get("upscale")):
        final_artifact = enhance_video(artifact, job, int(payload["seed"]), deadline)
        verification = verify_video(final_artifact)
        final_artifact.with_suffix(".verification.json").write_text(
            json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    job.artifact = str(final_artifact)
    job.progress = 1.0


def worker() -> None:
    while True:
        job_id = job_queue.get()
        with jobs_lock:
            job = jobs.get(job_id)
            if not job or job.status != "queued":
                job_queue.task_done()
                continue
            job.status = "running"
            job.stage = "准备"
            job.started_at = time.time()
        try:
            execute(job)
            job.status = "succeeded"
            job.stage = "已完成"
        except ComfyError as error:
            job.status = "cancelled" if job.cancel_requested else "failed"
            job.error = str(error)
        except Exception:
            job.status = "failed"
            job.error = traceback.format_exc(limit=5)
        finally:
            job.finished_at = time.time()
            job_queue.task_done()


threading.Thread(target=worker, daemon=True, name="h3-job-worker").start()


@app.get("/health")
def health(_: None = Depends(authorize)) -> dict[str, Any]:
    return runtime_health()


@app.get("/v1/diagnostics")
def diagnostics(_: None = Depends(authorize)) -> dict[str, Any]:
    """返回不含令牌、素材内容或提示词的有限诊断信息。"""
    return {
        "health": runtime_health(),
        "logs": {name: tail_log(path) for name, path in LOG_FILES.items()},
        "runtime": {
            "python": os.sys.version,
            "jobTimeoutSeconds": JOB_TIMEOUT_SECONDS,
        },
    }


@app.post("/v1/assets")
async def upload_asset(file: UploadFile = File(...), _: None = Depends(authorize)) -> dict[str, Any]:
    digest = hashlib.sha256()
    temporary = ASSETS / f".{uuid.uuid4().hex}.upload"
    suffix = Path(file.filename or "asset.bin").suffix.lower()
    if suffix not in ALLOWED_ASSET_SUFFIXES:
        raise HTTPException(415, "不支持的素材格式")
    size = 0
    with temporary.open("wb") as handle:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_ASSET_BYTES:
                temporary.unlink(missing_ok=True)
                raise HTTPException(413, "单个素材超过上传上限")
            digest.update(chunk)
            handle.write(chunk)
    sha = digest.hexdigest()
    destination = ASSETS / f"{sha}{suffix}"
    if destination.exists():
        temporary.unlink(missing_ok=True)
    else:
        temporary.replace(destination)
    return {"id": sha, "path": str(destination), "bytes": destination.stat().st_size}


@app.post("/v1/jobs")
def submit_job(payload: dict[str, Any], _: None = Depends(authorize)) -> dict[str, Any]:
    current_health = runtime_health()
    if current_health["status"] != "ready":
        detail = current_health.get("error") or current_health.get("stage") or "执行器尚未就绪"
        raise HTTPException(503, str(detail))
    required = ("prompt", "preset", "seed")
    if any(field not in payload for field in required):
        raise HTTPException(422, "任务快照缺少必要字段")
    prompt = payload.get("prompt")
    preset = payload.get("preset")
    asset_paths = payload.get("assetPaths", [])
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12_000:
        raise HTTPException(422, "提示词为空或过长")
    if not isinstance(preset, dict):
        raise HTTPException(422, "画质预设格式无效")
    if (preset.get("width"), preset.get("height")) not in {(544, 960), (640, 1152), (768, 1344)}:
        raise HTTPException(422, "画面尺寸不在 H3 安全预设中")
    if not isinstance(asset_paths, list) or len(asset_paths) > 11 or not all(isinstance(path, str) for path in asset_paths):
        raise HTTPException(422, "素材清单格式无效")
    job_id = payload.get("id") or uuid.uuid4().hex
    if not isinstance(job_id, str) or not SAFE_JOB_ID.fullmatch(job_id):
        raise HTTPException(422, "任务编号格式无效")
    with jobs_lock:
        if job_id in jobs:
            return jobs[job_id].public()
        job = RemoteJob(id=job_id, payload=payload)
        jobs[job_id] = job
    job_queue.put(job_id)
    return job.public()


@app.get("/v1/jobs/{job_id}")
def job_status(job_id: str, _: None = Depends(authorize)) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job.public()


@app.post("/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: str, _: None = Depends(authorize)) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        if job.status == "queued":
            job.status = "cancelled"
            job.stage = "已取消"
        elif job.status == "running":
            job.cancel_requested = True
            client.interrupt()
    return job.public()


@app.get("/v1/jobs/{job_id}/artifact")
def artifact(job_id: str, variant: str = "final", _: None = Depends(authorize)) -> FileResponse:
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job.status != "succeeded" or not job.artifact:
        raise HTTPException(409, "成片尚未准备好")
    selected = job.raw_artifact if variant == "raw" else job.artifact
    path = Path(selected or job.artifact)
    if not path.is_file():
        raise HTTPException(410, "远端成片已被清理")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/v1/session")
def session(_: None = Depends(authorize)) -> dict[str, Any]:
    with jobs_lock:
        counts: dict[str, int] = {}
        for job in jobs.values():
            counts[job.status] = counts.get(job.status, 0) + 1
    return {"jobs": counts, "runtime": str(RUNTIME)}
