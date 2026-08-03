"""Bulk queue import: turn an LLM-authored JSON scene list into Jobs.

The point of this module is to let an LLM (or a human) write out an entire
multi-scene sequence as one JSON document and hand it straight to the queue,
instead of filling in the form once per clip. The schema stays deliberately
small: a top-level ``defaults`` block, and a ``scenes`` array where each entry
needs only ``mode``, ``prompt``, and ``duration_sec`` - everything else has a
sensible fallback.

See README.md for the full schema and a worked example.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from h3_backend import (
    DEFAULT_RESOLUTION,
    MODEL_HINTS,
    RESOLUTION_PRESETS,
    ComfyError,
    ModelSet,
    SamplingSettings,
    pick_default,
    snap_canvas,
    snap_length,
)
from h3_queue import Job

VALID_MODES = {"t2v", "i2v", "r2v"}
_RES_RE = re.compile(r"^\s*(\d+)\s*[xX×]\s*(\d+)\s*$")


class SceneBatchError(ComfyError):
    """Raised with every problem found, so a bad JSON can be fixed in one pass."""


def _resolve_resolution(value, fallback_wh: tuple[int, int]) -> tuple[int, int]:
    if not value:
        return fallback_wh
    if value in RESOLUTION_PRESETS:
        return RESOLUTION_PRESETS[value]
    match = _RES_RE.match(str(value))
    if not match:
        raise ValueError(f'resolution must look like "864x480", got {value!r}')
    return snap_canvas(int(match.group(1)), int(match.group(2)))


def _resolve_path(raw: str, base_dir: Path, field: str, errors: list[str]) -> str | None:
    path = Path(raw)
    if not path.is_absolute():
        path = base_dir / path
    if not path.is_file():
        errors.append(f"{field}: file not found: {path}")
        return None
    return str(path)


def parse_scene_batch(
    text: str,
    base_dir: Path,
    diffusion_opts: list[str],
    encoder_opts: list[str],
    vae_opts: list[str],
    samplers: list[str],
    schedulers: list[str],
) -> tuple[list[Job], list[str]]:
    """Parse a scene-batch JSON document into a list of ready-to-queue Jobs.

    Returns ``(jobs, notes)`` on success, where ``notes`` are informational
    (e.g. "scene 2 will auto-chain"), not problems. Raises ``SceneBatchError``
    - with every problem found, not just the first - if the document is
    invalid. Nothing is queued unless the whole batch is valid.
    """
    errors: list[str] = []
    notes: list[str] = []

    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SceneBatchError(f"Invalid JSON: {exc}") from exc

    if not isinstance(doc, dict) or not isinstance(doc.get("scenes"), list) or not doc["scenes"]:
        raise SceneBatchError('Top level must be an object with a non-empty "scenes" array.')

    defaults = doc.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise SceneBatchError('"defaults" must be an object.')

    try:
        default_wh = _resolve_resolution(
            defaults.get("resolution"), RESOLUTION_PRESETS[DEFAULT_RESOLUTION]
        )
    except ValueError as exc:
        raise SceneBatchError(f"defaults: {exc}") from exc

    jobs: list[Job] = []

    for index, raw_scene in enumerate(doc["scenes"], start=1):
        if not isinstance(raw_scene, dict):
            errors.append(f"scene {index}: must be an object")
            continue

        label = str(raw_scene.get("label") or f"scene{index}")
        tag = f"scene {index} ({label})"

        mode = raw_scene.get("mode")
        if mode not in VALID_MODES:
            errors.append(f'{tag}: "mode" must be one of {sorted(VALID_MODES)}, got {mode!r}')
            continue

        prompt = raw_scene.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            errors.append(f'{tag}: "prompt" is required and must be non-empty text')
            continue

        duration = raw_scene.get("duration_sec")
        if not isinstance(duration, (int, float)) or duration <= 0:
            errors.append(f'{tag}: "duration_sec" must be a positive number, got {duration!r}')
            continue
        length = snap_length(float(duration))

        try:
            width, height = _resolve_resolution(raw_scene.get("resolution"), default_wh)
        except ValueError as exc:
            errors.append(f"{tag}: {exc}")
            continue

        steps = int(raw_scene.get("steps", defaults.get("steps", 20)))
        seed = int(raw_scene.get("seed", defaults.get("seed", -1)))
        sampler = raw_scene.get("sampler") or defaults.get("sampler") or (
            "res_multistep" if "res_multistep" in samplers else (samplers[0] if samplers else "euler")
        )
        scheduler = raw_scene.get("scheduler") or defaults.get("scheduler") or (
            "simple" if "simple" in schedulers else (schedulers[0] if schedulers else "normal")
        )
        if sampler not in samplers:
            errors.append(f"{tag}: unknown sampler {sampler!r} (available: {samplers})")
            continue
        if scheduler not in schedulers:
            errors.append(f"{tag}: unknown scheduler {scheduler!r} (available: {schedulers})")
            continue

        model_hint = MODEL_HINTS["ref2va"] if mode == "r2v" else MODEL_HINTS["fl2va"]
        diffusion_model = (
            raw_scene.get("diffusion_model") or defaults.get("diffusion_model")
            or pick_default(diffusion_opts, model_hint)
        )
        text_encoder = (
            raw_scene.get("text_encoder") or defaults.get("text_encoder")
            or pick_default(encoder_opts, MODEL_HINTS["text_encoder"])
        )
        video_vae = (
            raw_scene.get("video_vae") or defaults.get("video_vae")
            or pick_default(vae_opts, MODEL_HINTS["video_vae"])
        )
        audio_vae = (
            raw_scene.get("audio_vae") or defaults.get("audio_vae")
            or pick_default(vae_opts, MODEL_HINTS["audio_vae"])
        )
        weight_dtype = raw_scene.get("weight_dtype") or defaults.get("weight_dtype") or "default"

        if not all([diffusion_model, text_encoder, video_vae, audio_vae]):
            errors.append(f"{tag}: no matching model files detected in ComfyUI for mode {mode!r}")
            continue

        models = ModelSet(
            diffusion_model=diffusion_model,
            text_encoder=text_encoder,
            video_vae=video_vae,
            audio_vae=audio_vae,
            weight_dtype=weight_dtype,
        )
        sampling = SamplingSettings(steps=steps, sampler=sampler, scheduler=scheduler, seed=seed)

        first_frame = last_frame = ref_video = ref_audio = None
        ref_images: list[str] = []
        chain = False

        if mode == "i2v":
            if raw_scene.get("start_image"):
                first_frame = _resolve_path(
                    raw_scene["start_image"], base_dir, f"{tag}: start_image", errors
                )
            if raw_scene.get("end_image"):
                last_frame = _resolve_path(
                    raw_scene["end_image"], base_dir, f"{tag}: end_image", errors
                )
            explicit_chain = raw_scene.get("chain_from_previous")
            if explicit_chain is not None:
                chain = bool(explicit_chain)
            elif not first_frame:
                chain = True
                notes.append(f"{tag}: no start_image given, will continue from the previous clip")
            if not first_frame and not chain:
                errors.append(f'{tag}: i2v needs "start_image", or omit it to auto-chain')
                continue

        elif mode == "r2v":
            for i, raw_path in enumerate(raw_scene.get("ref_images") or []):
                resolved = _resolve_path(raw_path, base_dir, f"{tag}: ref_images[{i}]", errors)
                if resolved:
                    ref_images.append(resolved)
            if raw_scene.get("ref_video"):
                ref_video = _resolve_path(raw_scene["ref_video"], base_dir, f"{tag}: ref_video", errors)
            if raw_scene.get("ref_audio"):
                ref_audio = _resolve_path(raw_scene["ref_audio"], base_dir, f"{tag}: ref_audio", errors)
            if not (ref_images or ref_video or ref_audio):
                errors.append(f"{tag}: r2v needs at least one of ref_images, ref_video, ref_audio")
                continue

        jobs.append(
            Job(
                mode=mode,
                prompt=prompt,
                width=width,
                height=height,
                length=length,
                models=models,
                sampling=sampling,
                first_frame=first_frame,
                last_frame=last_frame,
                ref_images=ref_images,
                ref_video=ref_video,
                ref_audio=ref_audio,
                use_video_audio=bool(raw_scene.get("use_video_audio", True)),
                ref_image_size=raw_scene.get("ref_image_size", "match"),
                chain_from_previous=chain,
                label=label,
            )
        )

    if errors:
        raise SceneBatchError(
            f"Found {len(errors)} problem(s) in the scene batch:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    return jobs, notes
