"""按任务类型下载并校验 H3 Production Studio 的最小模型集合。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

H3_REPO = "Comfy-Org/MiniMax-H3"
H3_REVISION = "4cc1d817b6184899b41293954329f576cb5ae86b"
SEEDVR2_REPO = "numz/SeedVR2_comfyUI"
SEEDVR2_REVISION = "09ced71023636e9bc8cdf9cdecfb2625d1e691e8"
RUNTIME = Path(os.environ.get("H3_RUNTIME", "/workspace/h3-runtime"))
STATUS_PATH = RUNTIME / "bootstrap-status.json"
CACHE = Path(os.environ.get("HF_HOME", "/workspace/hf-cache"))
MODEL_ROOT = Path(os.environ.get("COMFYUI_MODELS", "/opt/ComfyUI/models"))
PROFILE = {
    item.strip().lower()
    for item in os.environ.get("H3_MODEL_PROFILE", "ref2va").split(",")
    if item.strip()
}


@dataclass(frozen=True)
class ModelFile:
    profile: str
    repo: str
    revision: str
    remote_path: str
    category: str
    size: int
    sha256: str

    @property
    def destination(self) -> Path:
        return MODEL_ROOT / self.category / Path(self.remote_path).name


FILES = (
    ModelFile(
        "shared", H3_REPO, H3_REVISION,
        "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "text_encoders", 15_687_142_551,
        "35a88d51044231fe332301d7a62aa81e3f2cba62febeb446e2c1e3e0ef76f2c6",
    ),
    ModelFile(
        "shared", H3_REPO, H3_REVISION,
        "vae/minimax_h3_video_vae_fp16.safetensors",
        "vae", 5_207_808_496,
        "7c1f131492e7eddacaac9069a61b81bdd39de5cc96561e677c5eab1cdce5e522",
    ),
    ModelFile(
        "shared", H3_REPO, H3_REVISION,
        "vae/minimax_h3_audio_vae_fp32.safetensors",
        "vae", 605_254_808,
        "8e505d95dd1561d47abd43d4238fd40d9bb1ae9e147ed0a4cba778d76ae4db48",
    ),
    ModelFile(
        "ref2va", H3_REPO, H3_REVISION,
        "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        "diffusion_models", 20_970_379_616,
        "9255f52b6677845ad238f20dfaafa94727053694127ab7f255c048f0f9365779",
    ),
    ModelFile(
        "fl2va", H3_REPO, H3_REVISION,
        "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
        "diffusion_models", 20_970_379_616,
        "e889202c41dafb67b10d67b97f0d8541508036a6090af23425a5c2615d03c47a",
    ),
    ModelFile(
        "seedvr2", SEEDVR2_REPO, SEEDVR2_REVISION,
        "seedvr2_ema_3b_fp8_e4m3fn.safetensors",
        "SEEDVR2", 3_391_544_696,
        "3bf1e43ebedd570e7e7a0b1b60d6a02e105978f505c8128a241cde99a8240cff",
    ),
    ModelFile(
        "seedvr2", SEEDVR2_REPO, SEEDVR2_REVISION,
        "ema_vae_fp16.safetensors",
        "SEEDVR2", 501_324_814,
        "20678548f420d98d26f11442d3528f8b8c94e57ee046ef93dbb7633da8612ca1",
    ),
)

status_lock = threading.Lock()
status: dict[str, object] = {
    "status": "starting",
    "stage": "准备模型清单",
    "profile": sorted(PROFILE),
    "downloadedBytes": 0,
    "totalBytes": 0,
    "currentFile": None,
    "error": None,
    "updatedAt": time.time(),
}


def write_status(**patch: object) -> None:
    with status_lock:
        status.update(patch, updatedAt=time.time())
        RUNTIME.mkdir(parents=True, exist_ok=True)
        temporary = STATUS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(STATUS_PATH)


def selected_files() -> list[ModelFile]:
    requested = set(PROFILE) or {"ref2va"}
    if not requested.intersection({"ref2va", "fl2va"}):
        requested.add("ref2va")
    return [item for item in FILES if item.profile == "shared" or item.profile in requested]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(16 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def cache_bytes() -> int:
    total = 0
    if CACHE.exists():
        for path in CACHE.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
    return total


def monitor_downloads(stop: threading.Event, total_bytes: int) -> None:
    while not stop.wait(1):
        write_status(downloadedBytes=min(total_bytes, cache_bytes()))


def validate_manifest() -> None:
    destinations: set[Path] = set()
    for item in FILES:
        if item.destination in destinations:
            raise RuntimeError(f"模型目标路径重复：{item.destination}")
        destinations.add(item.destination)
        if item.size < 1_000_000 or len(item.sha256) != 64:
            raise RuntimeError(f"模型清单无效：{item.remote_path}")
        if item.remote_path.startswith("split_files/"):
            raise RuntimeError(f"模型路径仍包含已废弃目录：{item.remote_path}")


def download(item: ModelFile) -> None:
    from huggingface_hub import hf_hub_download

    destination = item.destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size == item.size:
        if sha256_file(destination) == item.sha256:
            print(f"[models] 已校验 {destination.name}", flush=True)
            return
        destination.unlink()

    cached = Path(hf_hub_download(
        repo_id=item.repo,
        filename=item.remote_path,
        revision=item.revision,
        cache_dir=CACHE,
        token=os.environ.get("HF_TOKEN") or None,
    ))
    if cached.stat().st_size != item.size:
        raise RuntimeError(
            f"{item.remote_path} 大小不符：{cached.stat().st_size} != {item.size}"
        )
    actual = sha256_file(cached)
    if actual != item.sha256:
        raise RuntimeError(f"{item.remote_path} SHA256 不符：{actual}")
    destination.unlink(missing_ok=True)
    destination.symlink_to(cached)
    print(f"[models] 已下载并校验 {destination.name}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    validate_manifest()
    chosen = selected_files()
    if args.self_test:
        print(json.dumps({
            "status": "ok",
            "profiles": sorted({item.profile for item in FILES}),
            "files": len(FILES),
        }, ensure_ascii=False))
        return

    total_bytes = sum(item.size for item in chosen)
    write_status(
        status="downloading",
        stage="下载 H3 模型",
        profile=sorted(PROFILE),
        totalBytes=total_bytes,
        downloadedBytes=min(total_bytes, cache_bytes()),
    )
    stop = threading.Event()
    monitor = threading.Thread(
        target=monitor_downloads, args=(stop, total_bytes), daemon=True, name="model-progress"
    )
    monitor.start()
    try:
        for index, item in enumerate(chosen, start=1):
            write_status(
                stage=f"下载并校验模型 {index}/{len(chosen)}",
                currentFile=Path(item.remote_path).name,
            )
            download(item)
        write_status(
            status="models_ready",
            stage="模型准备完成",
            currentFile=None,
            downloadedBytes=total_bytes,
        )
    except Exception as error:
        write_status(status="failed", stage="模型准备失败", error=str(error))
        raise
    finally:
        stop.set()
        monitor.join(timeout=2)


if __name__ == "__main__":
    main()
