"""先开放受保护的状态网关，再准备模型并启动内部 ComfyUI。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


RUNTIME = Path(os.environ.get("H3_RUNTIME", "/workspace/h3-runtime"))
STATUS_PATH = RUNTIME / "bootstrap-status.json"
BOOTSTRAP_LOG = RUNTIME / "bootstrap.log"
COMFY_LOG = RUNTIME / "comfy.log"
GATEWAY_LOG = RUNTIME / "gateway.log"
processes: list[subprocess.Popen[bytes]] = []


def update_status(**patch: object) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    current: dict[str, object] = {}
    try:
        current = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    current.update(patch, updatedAt=time.time())
    temporary = STATUS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATUS_PATH)


def wait_for_comfy(timeout: int = 600) -> None:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8188/system_stats", timeout=4):
                return
        except Exception:
            time.sleep(2)
    raise RuntimeError("ComfyUI 在限定时间内没有启动")


def tail(path: Path, limit: int = 2_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except FileNotFoundError:
        return ""


def stop(*_: object) -> None:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
    deadline = time.time() + 8
    for process in reversed(processes):
        if process.poll() is None:
            try:
                process.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                process.kill()
    raise SystemExit(0)


def keep_gateway_alive(gateway: subprocess.Popen[bytes]) -> None:
    while gateway.poll() is None:
        time.sleep(2)
    raise RuntimeError(f"安全网关意外退出，状态码 {gateway.returncode}")


def main() -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    update_status(
        status="starting",
        stage="启动安全网关",
        profile=[item for item in os.environ.get("H3_MODEL_PROFILE", "ref2va").split(",") if item],
        downloadedBytes=0,
        totalBytes=0,
        error=None,
    )
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    gateway_handle = GATEWAY_LOG.open("ab", buffering=0)
    gateway = subprocess.Popen(
        [
            "uvicorn", "main:app", "--app-dir", "/opt/h3",
            "--host", "0.0.0.0", "--port", "8000",
        ],
        stdout=gateway_handle,
        stderr=subprocess.STDOUT,
    )
    processes.append(gateway)
    time.sleep(1)
    if gateway.poll() is not None:
        raise RuntimeError(f"安全网关启动失败：{tail(GATEWAY_LOG)}")

    with BOOTSTRAP_LOG.open("ab", buffering=0) as bootstrap_handle:
        bootstrap = subprocess.run(
            [sys.executable, "/opt/h3/bootstrap_models.py"],
            stdout=bootstrap_handle,
            stderr=subprocess.STDOUT,
        )
    if bootstrap.returncode != 0:
        update_status(
            status="failed",
            stage="模型准备失败",
            error=tail(BOOTSTRAP_LOG, 4_000) or f"下载进程退出码 {bootstrap.returncode}",
        )
        keep_gateway_alive(gateway)
        return

    update_status(status="comfy_starting", stage="启动 ComfyUI 与 H3 节点", error=None)
    comfy_handle = COMFY_LOG.open("ab", buffering=0)
    comfy = subprocess.Popen(
        [
            sys.executable, "/opt/ComfyUI/main.py",
            "--listen", "127.0.0.1",
            "--port", "8188",
            "--disable-auto-launch",
        ],
        cwd="/opt/ComfyUI",
        stdout=comfy_handle,
        stderr=subprocess.STDOUT,
    )
    processes.append(comfy)
    try:
        wait_for_comfy()
        update_status(status="nodes_checking", stage="校验 H3 与增强节点")
    except Exception as error:
        update_status(
            status="failed",
            stage="ComfyUI 启动失败",
            error=f"{error}\n{tail(COMFY_LOG, 4_000)}",
        )
        keep_gateway_alive(gateway)
        return

    while True:
        if gateway.poll() is not None:
            raise RuntimeError(f"安全网关意外退出，状态码 {gateway.returncode}")
        if comfy.poll() is not None:
            update_status(
                status="failed",
                stage="ComfyUI 意外退出",
                error=tail(COMFY_LOG, 4_000),
            )
            keep_gateway_alive(gateway)
            return
        time.sleep(2)


if __name__ == "__main__":
    main()
