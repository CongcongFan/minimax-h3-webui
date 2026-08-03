"""Job queue for H3 Studio.

Generation is slow enough that being able to line up several clips while one
is rendering matters more than raw throughput. A single worker thread runs
jobs in order, which also means a queued clip can chain off the output of the
job before it - the frame it needs does not exist until then.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from h3_backend import (
    ComfyClient,
    ComfyError,
    ModelSet,
    ReferenceInputs,
    SamplingSettings,
    build_reference_graph,
    build_video_graph,
    extract_last_frame,
)

JobStatus = Literal["queued", "running", "done", "error", "cancelled"]
JobMode = Literal["t2v", "i2v", "r2v"]


@dataclass
class Job:
    mode: JobMode
    prompt: str
    width: int
    height: int
    length: int
    models: ModelSet
    sampling: SamplingSettings

    # Local paths, uploaded to ComfyUI when the job actually starts.
    first_frame: str | None = None
    last_frame: str | None = None
    ref_images: list[str] = field(default_factory=list)
    ref_video: str | None = None
    ref_audio: str | None = None
    use_video_audio: bool = True
    ref_image_size: str = "match"

    #: Take the final frame of the previous finished job as this job's start
    #: frame. Resolved at run time, so it works for jobs queued in advance.
    chain_from_previous: bool = False

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    label: str = ""
    status: JobStatus = "queued"
    error: str = ""
    output_path: str | None = None
    prompt_id: str | None = None
    queued_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    seed_used: int | None = None

    @property
    def duration_sec(self) -> float:
        return self.length / 24

    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at


class QueueManager:
    """Runs queued jobs one at a time against a ComfyUI instance."""

    def __init__(self, client: ComfyClient, output_dir: Path, work_dir: Path):
        self.client = client
        self.output_dir = Path(output_dir)
        self.work_dir = Path(work_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)

        self._jobs: list[Job] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._cancel_current = threading.Event()
        self._current_id: str | None = None
        self._status_line = "Idle"
        self._stop = False

        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    # -- public API --------------------------------------------------------

    def add(self, job: Job) -> Job:
        with self._lock:
            self._jobs.append(job)
        self._wake.set()
        return job

    def jobs(self) -> list[Job]:
        with self._lock:
            return list(self._jobs)

    def status_line(self) -> str:
        return self._status_line

    def completed(self) -> list[Job]:
        return [j for j in self.jobs() if j.status == "done" and j.output_path]

    def cancel(self, job_id: str) -> bool:
        """Drop a queued job, or interrupt it if it is the one running."""
        with self._lock:
            for job in self._jobs:
                if job.id != job_id:
                    continue
                if job.status == "queued":
                    job.status = "cancelled"
                    return True
                if job.status == "running":
                    self._cancel_current.set()
                    return True
                return False
        return False

    def clear_finished(self) -> int:
        with self._lock:
            keep = [j for j in self._jobs if j.status in ("queued", "running")]
            removed = len(self._jobs) - len(keep)
            self._jobs = keep
        return removed

    def shutdown(self) -> None:
        self._stop = True
        self._wake.set()

    # -- worker ------------------------------------------------------------

    def _next_job(self) -> Job | None:
        with self._lock:
            for job in self._jobs:
                if job.status == "queued":
                    return job
        return None

    def _previous_output(self, before: Job) -> str | None:
        """Most recent successful output queued ahead of ``before``."""
        with self._lock:
            try:
                cutoff = self._jobs.index(before)
            except ValueError:
                cutoff = len(self._jobs)
            for job in reversed(self._jobs[:cutoff]):
                if job.status == "done" and job.output_path:
                    return job.output_path
        return None

    def _run(self) -> None:
        while not self._stop:
            job = self._next_job()
            if job is None:
                self._status_line = "Idle"
                self._wake.wait(timeout=2.0)
                self._wake.clear()
                continue

            self._cancel_current.clear()
            self._current_id = job.id
            job.status = "running"
            job.started_at = time.time()
            try:
                self._execute(job)
                job.status = "done"
                self._status_line = f"Finished {job.label or job.id}"
            except ComfyError as exc:
                job.status = "cancelled" if "cancel" in str(exc).lower() else "error"
                job.error = str(exc)
                self._status_line = f"Failed: {job.label or job.id}"
            except Exception:  # noqa: BLE001 - surface anything unexpected in the UI
                job.status = "error"
                job.error = traceback.format_exc(limit=3)
                self._status_line = f"Failed: {job.label or job.id}"
            finally:
                job.finished_at = time.time()
                self._current_id = None

    def _execute(self, job: Job) -> None:
        client = self.client

        first_frame = job.first_frame
        if job.chain_from_previous:
            previous = self._previous_output(job)
            if not previous:
                raise ComfyError(
                    "Nothing to continue from - no earlier clip in this queue "
                    "has finished successfully."
                )
            self._status_line = f"{job.label or job.id}: taking the last frame"
            chained = self.work_dir / f"chain_{job.id}.png"
            first_frame = str(extract_last_frame(previous, chained))

        self._status_line = f"{job.label or job.id}: uploading inputs"
        uploaded_first = client.upload_file(first_frame) if first_frame else None
        uploaded_last = client.upload_file(job.last_frame) if job.last_frame else None

        seed = job.sampling.resolved_seed()
        job.seed_used = seed
        prefix = f"h3studio_{job.id}"

        if job.mode == "r2v":
            refs = ReferenceInputs(
                images=[client.upload_file(p) for p in job.ref_images[:9]],
                video=client.upload_file(job.ref_video) if job.ref_video else None,
                audio=client.upload_file(job.ref_audio) if job.ref_audio else None,
                use_video_audio=job.use_video_audio,
                image_size_mode=job.ref_image_size,
            )
            graph = build_reference_graph(
                prompt=job.prompt,
                width=job.width,
                height=job.height,
                length=job.length,
                models=job.models,
                sampling=job.sampling,
                seed=seed,
                refs=refs,
                prefix=prefix,
            )
        else:
            graph = build_video_graph(
                prompt=job.prompt,
                width=job.width,
                height=job.height,
                length=job.length,
                models=job.models,
                sampling=job.sampling,
                seed=seed,
                first_frame=uploaded_first,
                last_frame=uploaded_last,
                prefix=prefix,
            )

        self._status_line = f"{job.label or job.id}: generating"
        job.prompt_id = client.submit(graph)

        def progress(elapsed: str) -> None:
            self._status_line = f"{job.label or job.id}: generating ({elapsed})"

        outputs = client.wait(
            job.prompt_id,
            on_progress=progress,
            should_cancel=self._cancel_current.is_set,
        )

        self._status_line = f"{job.label or job.id}: saving"
        job.output_path = str(self._save_output(outputs, job))

    def _save_output(self, outputs: dict, job: Job) -> Path:
        for node_output in outputs.values():
            for entry in node_output.get("images", []) or []:
                filename = entry.get("filename")
                if not filename:
                    continue
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                suffix = Path(filename).suffix or ".mp4"
                dest = self.output_dir / f"{stamp}_{job.id}{suffix}"
                return self.client.download_output(
                    filename, entry.get("subfolder", ""), dest
                )
        raise ComfyError("The job finished but produced no video file.")
