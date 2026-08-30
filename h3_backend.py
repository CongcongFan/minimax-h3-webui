"""ComfyUI API client and MiniMax H3 workflow builders.

This module talks to a running ComfyUI instance purely over its HTTP API.
It deliberately does not import any ComfyUI code, so this project stays an
independent work rather than a derivative of ComfyUI (GPL-3.0).

Model weights are never downloaded automatically. See README.md.
"""

from __future__ import annotations

import json
import mimetypes
import random
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# --------------------------------------------------------------------------
# MiniMax H3 model constraints
# --------------------------------------------------------------------------

#: Generation canvas must be a multiple of this many pixels on each axis.
CANVAS_MULTIPLE = 32

#: Frames are generated in blocks; valid lengths sit on a 17k+5 grid at 24fps.
FRAME_BLOCK = 17
FRAME_OFFSET = 5

FPS = 24

#: The model is trained for a 768px short edge, capped at 768x1344.
MAX_SHORT_EDGE = 768
MAX_LONG_EDGE = 1344

#: Practical ceiling; the model is documented for roughly 15 seconds.
MAX_DURATION_SEC = 15.0

# Width x height presets, grouped by aspect ratio. Values are the documented
# multiple-of-32 canvases from the upstream ComfyUI workflow templates.
RESOLUTION_PRESETS: dict[str, tuple[int, int]] = {
    "16:9  608 x 352   (0.2MP, fastest)": (608, 352),
    "16:9  736 x 416   (0.3MP)": (736, 416),
    "16:9  864 x 480   (0.4MP, recommended)": (864, 480),
    "16:9  960 x 544   (0.5MP)": (960, 544),
    "16:9  1152 x 640  (0.7MP)": (1152, 640),
    "16:9  1344 x 768  (1.0MP, max quality)": (1344, 768),
    "9:16  352 x 608   (vertical, fastest)": (352, 608),
    "9:16  480 x 864   (vertical, recommended)": (480, 864),
    "9:16  768 x 1344  (vertical, max quality)": (768, 1344),
    "1:1   512 x 512   (square, fast)": (512, 512),
    "1:1   768 x 768   (square, recommended)": (768, 768),
    "4:3   768 x 576   (classic)": (768, 576),
    "21:9  1152 x 480  (cinemascope)": (1152, 480),
}

DEFAULT_RESOLUTION = "16:9  864 x 480   (0.4MP, recommended)"

#: Filename fragments used to pre-select the right model in each dropdown.
MODEL_HINTS = {
    "fl2va": "fl2va",      # text-to-video and first/last-frame image-to-video
    "ref2va": "ref2va",    # reference-to-video
    "text_encoder": "qwen3vl",
    "video_vae": "video_vae",
    "audio_vae": "audio_vae",
}


def snap_length(duration_sec: float) -> int:
    """Convert a duration in seconds to a frame count the model accepts.

    MiniMax H3 generates in blocks of 17 frames plus a 5-frame offset, so the
    requested duration is rounded *up* onto that grid.
    """
    n = max(FRAME_OFFSET, round(duration_sec * FPS))
    return n + (FRAME_OFFSET - (n % FRAME_BLOCK)) % FRAME_BLOCK


def length_to_seconds(length: int) -> float:
    return length / FPS


def snap_canvas(width: int, height: int) -> tuple[int, int]:
    """Round a canvas to the multiple-of-32 grid the model requires."""
    w = max(CANVAS_MULTIPLE, round(width / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
    h = max(CANVAS_MULTIPLE, round(height / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
    return w, h


# --------------------------------------------------------------------------
# ComfyUI HTTP client
# --------------------------------------------------------------------------


class ComfyError(RuntimeError):
    """Raised when ComfyUI rejects a request or a prompt fails to execute."""


def _combo_options(spec: Any) -> list[str]:
    """Extract the option list from a ComfyUI input spec.

    ComfyUI has used two shapes over time::

        [["a", "b"], {...}]                      # older
        ["COMBO", {"options": ["a", "b"], ...}]  # newer

    Both appear in the same schema depending on the node, so handle each.
    """
    if not isinstance(spec, list) or not spec:
        return []
    head = spec[0]
    if isinstance(head, list):
        return [str(x) for x in head]
    if len(spec) > 1 and isinstance(spec[1], dict):
        return [str(x) for x in spec[1].get("options", [])]
    return []


class ComfyClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8188", timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- low level ---------------------------------------------------------

    def _get(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise ComfyError(f"Could not reach ComfyUI at {url}: {exc}") from exc

    def _get_bytes(self, path: str) -> bytes:
        url = f"{self.base_url}{path}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.URLError as exc:
            raise ComfyError(f"Could not download from {url}: {exc}") from exc

    def _post_json(self, path: str, payload: dict) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise ComfyError(f"ComfyUI rejected the request: {body}") from exc
        except urllib.error.URLError as exc:
            raise ComfyError(f"Could not reach ComfyUI at {url}: {exc}") from exc

    # -- connectivity ------------------------------------------------------

    def is_alive(self) -> bool:
        try:
            self._get("/system_stats")
            return True
        except ComfyError:
            return False

    def system_stats(self) -> dict:
        return self._get("/system_stats")

    # -- model discovery ---------------------------------------------------

    def list_models(self) -> dict[str, list[str]]:
        """Return the model files ComfyUI can currently see, by category.

        Nothing is downloaded here; this only reports what is already present
        so the UI can offer accurate dropdowns.
        """
        out = {"diffusion_models": [], "text_encoders": [], "vae": []}
        try:
            unet = self._get("/object_info/UNETLoader")
            spec = unet["UNETLoader"]["input"]["required"]["unet_name"]
            out["diffusion_models"] = _combo_options(spec)
        except (ComfyError, KeyError):
            pass
        try:
            clip = self._get("/object_info/CLIPLoader")
            spec = clip["CLIPLoader"]["input"]["required"]["clip_name"]
            out["text_encoders"] = _combo_options(spec)
        except (ComfyError, KeyError):
            pass
        try:
            vae = self._get("/object_info/VAELoader")
            spec = vae["VAELoader"]["input"]["required"]["vae_name"]
            out["vae"] = _combo_options(spec)
        except (ComfyError, KeyError):
            pass
        return out

    def list_samplers(self) -> list[str]:
        try:
            info = self._get("/object_info/KSamplerSelect")
            return _combo_options(
                info["KSamplerSelect"]["input"]["required"]["sampler_name"]
            )
        except (ComfyError, KeyError):
            return ["res_multistep"]

    def list_schedulers(self) -> list[str]:
        try:
            info = self._get("/object_info/BasicScheduler")
            return _combo_options(
                info["BasicScheduler"]["input"]["required"]["scheduler"]
            )
        except (ComfyError, KeyError):
            return ["simple", "normal", "beta"]

    def has_node(self, class_type: str) -> bool:
        """True when ComfyUI exposes the requested node class."""
        try:
            info = self._get(f"/object_info/{class_type}")
            return bool(info)
        except ComfyError:
            return False

    def has_h3_nodes(self) -> bool:
        """True when this ComfyUI build ships the MiniMax H3 nodes."""
        return self.has_node("MiniMaxH3ImageToVideo")

    # -- uploads / downloads -----------------------------------------------

    def upload_file(self, path: str | Path, subfolder: str = "") -> str:
        """Upload a local image or video into ComfyUI's input folder.

        Returns the filename ComfyUI stored it under.
        """
        path = Path(path)
        if not path.is_file():
            raise ComfyError(f"File not found: {path}")

        # Unique name so a re-used filename never resolves to a stale upload.
        name = f"h3studio_{uuid.uuid4().hex[:12]}{path.suffix.lower()}"
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

        boundary = f"----h3studio{uuid.uuid4().hex}"
        parts: list[bytes] = []

        def _field(key: str, value: str) -> None:
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n".encode()
            )

        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n".encode()
        )
        parts.append(path.read_bytes())
        parts.append(b"\r\n")
        _field("type", "input")
        _field("overwrite", "true")
        if subfolder:
            _field("subfolder", subfolder)
        parts.append(f"--{boundary}--\r\n".encode())

        body = b"".join(parts)
        req = urllib.request.Request(
            f"{self.base_url}/upload/image",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 300)) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ComfyError(
                f"Upload failed: {exc.read().decode('utf-8', 'replace')}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ComfyError(f"Upload failed: {exc}") from exc

        stored = result.get("name", name)
        sub = result.get("subfolder", "")
        return f"{sub}/{stored}" if sub else stored

    def download_output(self, filename: str, subfolder: str, dest: Path) -> Path:
        """Fetch a generated file out of ComfyUI and save it locally."""
        query = urllib.parse.urlencode(
            {"filename": filename, "subfolder": subfolder, "type": "output"}
        )
        data = self._get_bytes(f"/view?{query}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    # -- prompt execution --------------------------------------------------

    def submit(self, graph: dict) -> str:
        result = self._post_json("/prompt", {"prompt": graph})
        if "prompt_id" not in result:
            raise ComfyError(f"Unexpected response from ComfyUI: {result}")
        node_errors = result.get("node_errors") or {}
        if node_errors:
            raise ComfyError(f"Workflow validation failed: {node_errors}")
        return result["prompt_id"]

    def interrupt(self) -> None:
        try:
            self._post_json("/interrupt", {})
        except ComfyError:
            pass

    def history(self, prompt_id: str) -> dict | None:
        data = self._get(f"/history/{prompt_id}")
        entry = data.get(prompt_id)
        return entry or None

    def wait(
        self,
        prompt_id: str,
        poll_sec: float = 3.0,
        on_progress: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict:
        """Block until a prompt finishes; return its output map.

        Raises ComfyError with the Python traceback message if it failed.
        """
        started = time.time()
        while True:
            if should_cancel is not None and should_cancel():
                self.interrupt()
                raise ComfyError("Cancelled.")

            entry = self.history(prompt_id)
            if entry:
                status = entry.get("status", {})
                state = status.get("status_str")
                if state == "success":
                    return entry.get("outputs", {})
                if state == "error":
                    raise ComfyError(_describe_failure(status))

            if on_progress is not None:
                on_progress(f"{int(time.time() - started)}s")
            time.sleep(poll_sec)


def _describe_failure(status: dict) -> str:
    """Turn ComfyUI's error status block into something a user can act on."""
    for kind, payload in status.get("messages", []):
        if kind != "execution_error":
            continue
        node = payload.get("node_type", "?")
        message = (payload.get("exception_message") or "").strip()
        lowered = message.lower()
        if "not enough memory" in lowered and "cpu" in lowered:
            return (
                f"{node}: ran out of system RAM.\n\n"
                "On Windows the pagefile usually lives on C:, so a nearly-full "
                "C: drive shows up as a RAM error. Check free space on C: and "
                "close other large programs, then run the job again - ComfyUI "
                "caches finished nodes, so a failure in the final decode step "
                "does not throw away the sampling work."
            )
        if "out of memory" in lowered or "cuda" in lowered:
            return (
                f"{node}: ran out of GPU VRAM.\n\n"
                "Try a smaller resolution preset or a shorter duration. "
                "Reference-to-video needs noticeably more VRAM than the other "
                "modes."
            )
        return f"{node}: {message}"
    return "Generation failed; see the ComfyUI console for details."


# --------------------------------------------------------------------------
# Workflow graphs
# --------------------------------------------------------------------------


@dataclass
class ModelSet:
    """Which weight files to load. Nothing here is ever auto-downloaded."""

    diffusion_model: str
    text_encoder: str
    video_vae: str
    audio_vae: str
    weight_dtype: str = "default"


@dataclass
class SamplingSettings:
    steps: int = 20
    sampler: str = "res_multistep"
    scheduler: str = "simple"
    seed: int = -1  # -1 picks a fresh random seed per run

    def resolved_seed(self) -> int:
        if self.seed is None or self.seed < 0:
            return random.randint(0, 2**32 - 1)
        return int(self.seed)


@dataclass
class ReferenceInputs:
    """Reference material for reference-to-video (r2v) generation."""

    images: list[str] = field(default_factory=list)   # uploaded filenames
    video: str | None = None                          # uploaded filename
    audio: str | None = None                          # uploaded filename
    use_video_audio: bool = True
    image_size_mode: str = "match"                    # "match" or "max"


def _common_tail(
    graph: dict, models: ModelSet, sampling: SamplingSettings, seed: int, prefix: str
) -> dict:
    """Attach the shared sampler/decode/save chain to a conditioning source.

    Node "6" must already exist and expose (CONDITIONING, LATENT).
    """
    graph["7"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}}
    graph["8"] = {
        "class_type": "KSamplerSelect",
        "inputs": {"sampler_name": sampling.sampler},
    }
    graph["9"] = {
        "class_type": "BasicScheduler",
        "inputs": {
            "model": ["3", 0],
            "scheduler": sampling.scheduler,
            "steps": int(sampling.steps),
            "denoise": 1.0,
        },
    }
    graph["10"] = {
        "class_type": "BasicGuider",
        "inputs": {"model": ["3", 0], "conditioning": ["6", 0]},
    }
    graph["11"] = {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": ["7", 0],
            "guider": ["10", 0],
            "sampler": ["8", 0],
            "sigmas": ["9", 0],
            "latent_image": ["6", 1],
        },
    }
    # The sampler emits a packed audio+video latent; each decoder takes its half.
    graph["12"] = {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["11", 0], "vae": ["1", 0]},
    }
    graph["13"] = {
        "class_type": "VAEDecodeAudio",
        "inputs": {"samples": ["11", 0], "vae": ["2", 0]},
    }
    graph["14"] = {
        "class_type": "CreateVideo",
        "inputs": {
            "images": ["12", 0],
            "audio": ["13", 0],
            "fps": FPS,
            "bit_depth": 10,
        },
    }
    graph["15"] = {
        "class_type": "SaveVideo",
        "inputs": {
            "video": ["14", 0],
            "filename_prefix": f"video/{prefix}",
            "format": "mp4",
            "codec": "h264",
            "codec.encoding": "re-encode",
            "codec.encoding.crf": 14,
        },
    }
    return graph


def _loaders(models: ModelSet) -> dict:
    return {
        "1": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": models.video_vae},
        },
        "2": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": models.audio_vae},
        },
        "3": {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": models.diffusion_model,
                "weight_dtype": models.weight_dtype,
            },
        },
        "4": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": models.text_encoder,
                "type": "minimax",
                "device": "default",
            },
        },
    }


def build_video_graph(
    prompt: str,
    width: int,
    height: int,
    length: int,
    models: ModelSet,
    sampling: SamplingSettings,
    seed: int,
    first_frame: str | None = None,
    last_frame: str | None = None,
    prefix: str = "h3studio",
) -> dict:
    """Build a text-to-video or image-to-video graph.

    With no frames supplied this is plain t2v. Supplying ``first_frame`` makes
    it image-to-video; supplying both keyframes interpolates between them.
    The same ``fl2va`` checkpoint covers all three cases.
    """
    graph = _loaders(models)

    node6: dict[str, Any] = {
        "clip": ["4", 0],
        "vae": ["1", 0],
        "prompt": prompt,
        "width": int(width),
        "height": int(height),
        "length": int(length),
    }

    next_id = 20
    if first_frame:
        graph[str(next_id)] = {
            "class_type": "LoadImage",
            "inputs": {"image": first_frame},
        }
        node6["first_frame"] = [str(next_id), 0]
        next_id += 1
    if last_frame:
        graph[str(next_id)] = {
            "class_type": "LoadImage",
            "inputs": {"image": last_frame},
        }
        node6["last_frame"] = [str(next_id), 0]
        next_id += 1

    graph["6"] = {"class_type": "MiniMaxH3ImageToVideo", "inputs": node6}
    return _common_tail(graph, models, sampling, seed, prefix)


def build_reference_graph(
    prompt: str,
    width: int,
    height: int,
    length: int,
    models: ModelSet,
    sampling: SamplingSettings,
    seed: int,
    refs: ReferenceInputs,
    prefix: str = "h3studio",
) -> dict:
    """Build a reference-to-video graph (needs the ``ref2va`` checkpoint).

    References are presented to the model in a fixed order - images, then
    videos, then standalone audio - and the prompt refers to them positionally
    as ``<Picture 1>``, ``<Video 1>``, ``<Audio 1>`` and so on.
    """
    graph = _loaders(models)

    node6: dict[str, Any] = {
        "clip": ["4", 0],
        "vae": ["1", 0],
        "audio_vae": ["2", 0],
        "prompt": prompt,
        "width": int(width),
        "height": int(height),
        "length": int(length),
        "ref_image_size": refs.image_size_mode,
    }

    next_id = 20
    for index, image_name in enumerate(refs.images[:9]):
        graph[str(next_id)] = {
            "class_type": "LoadImage",
            "inputs": {"image": image_name},
        }
        node6[f"ref_images.ref_image_{index}"] = [str(next_id), 0]
        next_id += 1

    if refs.video:
        load_id = str(next_id)
        next_id += 1
        split_id = str(next_id)
        next_id += 1
        graph[load_id] = {
            "class_type": "LoadVideo",
            "inputs": {"file": refs.video},
        }
        graph[split_id] = {
            "class_type": "GetVideoComponents",
            "inputs": {"video": [load_id, 0]},
        }
        node6["ref_videos.ref_video_0"] = [split_id, 0]
        if refs.use_video_audio:
            # Slot 1 is the reference video's own soundtrack.
            node6["ref_video_audios.ref_video_audio_0"] = [split_id, 1]

    if refs.audio:
        load_id = str(next_id)
        next_id += 1
        graph[load_id] = {
            "class_type": "LoadAudio",
            "inputs": {"audio": refs.audio},
        }
        node6["ref_audios.ref_audio_0"] = [load_id, 0]

    graph["6"] = {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": node6}
    return _common_tail(graph, models, sampling, seed, prefix)


# --------------------------------------------------------------------------
# Media helpers
# --------------------------------------------------------------------------


def ffmpeg_available() -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            check=True,
            timeout=15,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def extract_last_frame(video_path: str | Path, dest: str | Path) -> Path:
    """Grab the final frame of a video, for chaining it into the next clip."""
    video_path, dest = Path(video_path), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "ffmpeg", "-y",
            "-sseof", "-0.2",
            "-i", str(video_path),
            "-update", "1",
            "-frames:v", "1",
            str(dest),
        ],
        capture_output=True,
        timeout=180,
    )
    if not dest.is_file():
        detail = proc.stderr.decode("utf-8", "replace")[-500:]
        raise ComfyError(f"Could not extract the last frame:\n{detail}")
    return dest


def concat_videos(paths: list[str | Path], dest: str | Path) -> Path:
    """Join clips end to end without re-encoding.

    All inputs come from the same pipeline, so codec and resolution already
    match and a stream copy is safe.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    listing = dest.with_suffix(".concat.txt")
    listing.write_text(
        "".join(f"file '{Path(p).as_posix()}'\n" for p in paths), encoding="utf-8"
    )
    proc = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-c", "copy", str(dest)],
        capture_output=True,
        timeout=600,
    )
    listing.unlink(missing_ok=True)
    if not dest.is_file():
        detail = proc.stderr.decode("utf-8", "replace")[-500:]
        raise ComfyError(f"Could not join the clips:\n{detail}")
    return dest


def pick_default(options: list[str], *hints: str) -> str | None:
    """Choose the option whose filename best matches a hint.

    Ties prefer entries sitting directly in the model folder over ones nested
    in a subfolder, since the same weights are often visible both ways.
    """
    for hint in hints:
        matches = [o for o in options if hint.lower() in o.lower()]
        if matches:
            return min(matches, key=lambda o: ("/" in o or "\\" in o, len(o)))
    return options[0] if options else None
