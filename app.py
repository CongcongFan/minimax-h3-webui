"""H3 Studio - a simple web UI for MiniMax H3 video generation.

Run ComfyUI (with MiniMax H3 weights already in place), then start this app
and open the address it prints. See README.md for setup.

This project does not download model weights. You supply them yourself, under
the MiniMax H3 Community License - which is not available in every territory.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import gradio as gr

from h3_backend import (
    DEFAULT_RESOLUTION,
    MAX_DURATION_SEC,
    MODEL_HINTS,
    RESOLUTION_PRESETS,
    ComfyClient,
    ModelSet,
    SamplingSettings,
    concat_videos,
    ffmpeg_available,
    length_to_seconds,
    pick_default,
    snap_length,
)
from h3_queue import Job, QueueManager

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"

STATUS_ICON = {
    "queued": "…",
    "running": "▶",
    "done": "✓",
    "error": "✗",
    "cancelled": "—",
}

LANGUAGE_NAMES = {"en": "English", "ja": "日本語"}

T = {
    "en": {
        "subtitle": "Video generation UI for MiniMax H3",
        "language": "Language",
        "prompt": "Prompt",
        "prompt_ph": (
            "Describe the shot, the motion, and the audio.\n\n"
            "H3 responds well to a second-by-second breakdown, e.g.\n"
            "[0.0-3.0s] The camera pushes in as rain begins to fall...\n"
            "[3.0-6.0s] She turns toward the light...\n"
            "Audio: soft piano, distant thunder."
        ),
        "duration": "Duration (seconds)",
        "resolution": "Resolution",
        "mode_t2v": "Text to Video",
        "mode_i2v": "Image to Video",
        "mode_r2v": "Reference to Video",
        "t2v_help": "Generate from the prompt alone.",
        "i2v_help": (
            "Animate a still image. Add an end frame to interpolate between "
            "two keyframes."
        ),
        "r2v_help": (
            "Carry a character, style, or motion across from reference "
            "material. Refer to them in the prompt as `<Picture 1>`, "
            "`<Video 1>`, `<Audio 1>`. Needs the ref2va checkpoint and "
            "noticeably more VRAM."
        ),
        "start_frame": "Start frame",
        "end_frame": "End frame (optional)",
        "chain": "Continue from the previous clip",
        "chain_info": (
            "Use the last frame of the clip before this one as the start "
            "frame. Queue several of these to build a long scene."
        ),
        "ref_images": "Reference images (up to 9)",
        "ref_video": "Reference video",
        "ref_audio": "Reference audio",
        "use_video_audio": "Also use the reference video's soundtrack",
        "ref_size": "Reference detail",
        "ref_size_info": "'max' keeps more identity detail but is several times slower.",
        "advanced": "Advanced settings",
        "steps": "Steps",
        "steps_info": "More steps means better coherence and longer renders. 20 is a good default.",
        "sampler": "Sampler",
        "scheduler": "Scheduler",
        "scheduler_info": "'beta' or 'normal' often beat 'simple' for reference-heavy prompts.",
        "seed": "Seed",
        "seed_info": "-1 picks a new random seed each run.",
        "models": "**Model files** — detected in ComfyUI. Nothing is downloaded automatically.",
        "diffusion": "Diffusion model",
        "encoder": "Text encoder",
        "vvae": "Video VAE",
        "avae": "Audio VAE",
        "dtype": "Weight precision",
        "label": "Label (optional)",
        "label_ph": "rooftop, shot 1",
        "add": "Add to queue",
        "add_info": "Runs immediately when nothing else is queued.",
        "queue": "Queue",
        "result": "Result",
        "refresh": "Refresh",
        "clear": "Clear finished",
        "cancel": "Cancel",
        "cancel_id": "Job ID to cancel",
        "join": "Join all finished clips",
        "join_info": "Concatenates every successful clip, in order, into one file.",
        "joined": "Joined video",
        "frames_note": "{frames} frames ({seconds:.2f}s at 24fps)",
        "idle": "Idle",
        "err_prompt": "Please write a prompt first.",
        "err_models": (
            "Model files are missing. Put the MiniMax H3 weights where ComfyUI "
            "can see them, restart ComfyUI, then reload this page."
        ),
        "err_start": "Image to Video needs a start frame, or tick 'Continue from the previous clip'.",
        "err_refs": "Reference to Video needs at least one reference input.",
        "err_join_few": "Need at least two finished clips to join.",
        "err_ffmpeg": "ffmpeg is required to join clips, but was not found.",
        "missing_models": (
            "> **No {items} found in ComfyUI.** Put the MiniMax H3 weights in "
            "place and restart — see the README. This app never downloads them "
            "for you."
        ),
        "failed": "failed",
    },
    "ja": {
        "subtitle": "MiniMax H3 動画生成UI",
        "language": "言語 / Language",
        "prompt": "プロンプト",
        "prompt_ph": (
            "映像・動き・音声を記述します。\n\n"
            "H3は秒単位の指定によく反応します。例:\n"
            "[0.0-3.0s] 雨が降り始め、カメラがゆっくり寄る...\n"
            "[3.0-6.0s] 彼女が光の方を振り向く...\n"
            "Audio: 静かなピアノ、遠くの雷鳴。"
        ),
        "duration": "長さ(秒)",
        "resolution": "解像度",
        "mode_t2v": "テキストから生成",
        "mode_i2v": "画像から生成",
        "mode_r2v": "参照から生成",
        "t2v_help": "プロンプトのみから生成します。",
        "i2v_help": "静止画を動かします。終了フレームも指定すると、2枚の間を補間します。",
        "r2v_help": (
            "参照素材からキャラクター・画風・動きを引き継ぎます。"
            "プロンプト内では `<Picture 1>`、`<Video 1>`、`<Audio 1>` のように"
            "参照します。ref2vaモデルが必要で、VRAM消費もかなり大きくなります。"
        ),
        "start_frame": "開始フレーム",
        "end_frame": "終了フレーム(任意)",
        "chain": "前のクリップから続ける",
        "chain_info": (
            "1つ前のクリップの最終フレームを開始フレームにします。"
            "これを並べてキューすれば長いシーンを作れます。"
        ),
        "ref_images": "参照画像(最大9枚)",
        "ref_video": "参照動画",
        "ref_audio": "参照音声",
        "use_video_audio": "参照動画の音声も使う",
        "ref_size": "参照の解像度",
        "ref_size_info": "'max' は同一性の再現が上がりますが数倍遅くなります。",
        "advanced": "詳細設定",
        "steps": "ステップ数",
        "steps_info": "多いほど破綻が減りますが時間がかかります。20が標準です。",
        "sampler": "サンプラー",
        "scheduler": "スケジューラー",
        "scheduler_info": "参照を多用する場合は 'beta' や 'normal' が 'simple' より良い傾向があります。",
        "seed": "シード",
        "seed_info": "-1 で毎回ランダムになります。",
        "models": "**モデルファイル** — ComfyUIから検出したものです。自動ダウンロードは行いません。",
        "diffusion": "拡散モデル",
        "encoder": "テキストエンコーダー",
        "vvae": "映像VAE",
        "avae": "音声VAE",
        "dtype": "重みの精度",
        "label": "ラベル(任意)",
        "label_ph": "屋上、カット1",
        "add": "キューに追加",
        "add_info": "他に実行中のものがなければすぐ生成が始まります。",
        "queue": "キュー",
        "result": "結果",
        "refresh": "更新",
        "clear": "完了分を消す",
        "cancel": "キャンセル",
        "cancel_id": "キャンセルするジョブID",
        "join": "完成クリップを全部つなげる",
        "join_info": "成功したクリップを順番に1本の動画へ連結します。",
        "joined": "連結した動画",
        "frames_note": "{frames} フレーム (24fpsで {seconds:.2f} 秒)",
        "idle": "待機中",
        "err_prompt": "先にプロンプトを入力してください。",
        "err_models": (
            "モデルファイルが見つかりません。MiniMax H3の重みをComfyUIから見える"
            "場所に配置し、ComfyUIを再起動してからこのページを再読み込みしてください。"
        ),
        "err_start": "画像から生成するには開始フレームが必要です(「前のクリップから続ける」でも可)。",
        "err_refs": "参照から生成するには参照素材が最低1つ必要です。",
        "err_join_few": "連結するには完成クリップが2本以上必要です。",
        "err_ffmpeg": "クリップの連結にはffmpegが必要ですが、見つかりませんでした。",
        "missing_models": (
            "> **{items} がComfyUIに見つかりません。** MiniMax H3の重みを配置して"
            "再起動してください(READMEを参照)。このアプリは重みを自動ダウンロード"
            "しません。"
        ),
        "failed": "失敗",
    },
}

MISSING_NAMES = {
    "en": {"diffusion": "diffusion model", "encoder": "text encoder", "vae": "VAE"},
    "ja": {"diffusion": "拡散モデル", "encoder": "テキストエンコーダー", "vae": "VAE"},
}


def load_config() -> dict:
    defaults = {
        "comfy_url": "http://127.0.0.1:8188",
        "output_dir": str(APP_DIR / "outputs"),
        "server_port": 7860,
        "share": False,
        "language": "en",
    }
    if CONFIG_PATH.is_file():
        try:
            defaults.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[warn] Ignoring unreadable config.json: {exc}")
    return defaults


def build_ui(manager: QueueManager, client: ComfyClient, lang: str) -> gr.Blocks:
    lang = lang if lang in T else "en"
    t = T[lang]

    models = client.list_models()
    samplers = client.list_samplers()
    schedulers = client.list_schedulers()

    diffusion_opts = models["diffusion_models"]
    encoder_opts = models["text_encoders"]
    vae_opts = models["vae"]

    #: Components whose text changes with the language, paired with a function
    #: producing the keyword arguments to re-apply for a given language.
    translated: list[tuple[gr.components.Component, callable]] = []

    def tr(component, **keys):
        """Register a component so the language switcher can relabel it.

        ``keys`` maps a component property to a key in the translation table,
        e.g. ``tr(box, label="prompt", placeholder="prompt_ph")``.
        """
        translated.append(
            (component, lambda tt: {prop: tt[key] for prop, key in keys.items()})
        )
        return component

    def describe_frames(seconds: float, tt: dict) -> str:
        frames = snap_length(seconds)
        return tt["frames_note"].format(
            frames=frames, seconds=length_to_seconds(frames)
        )

    def missing_notice(tt: dict, code: str) -> str:
        names = MISSING_NAMES[code]
        missing = [
            names[key]
            for key, opts in (
                ("diffusion", diffusion_opts),
                ("encoder", encoder_opts),
                ("vae", vae_opts),
            )
            if not opts
        ]
        if not missing:
            return ""
        return tt["missing_models"].format(items="、".join(missing)
                                           if code == "ja" else ", ".join(missing))

    default_duration = 5.0

    with gr.Blocks(title="H3 Studio", theme=gr.themes.Soft()) as demo:
        lang_state = gr.State(lang)

        with gr.Row():
            header = gr.Markdown(
                f"## H3 Studio\n{t['subtitle']}  ·  Powered by MiniMax H3"
            )
            language = gr.Dropdown(
                choices=[(name, code) for code, name in LANGUAGE_NAMES.items()],
                value=lang,
                label=t["language"],
                scale=0,
                min_width=160,
            )
        tr(language, label="language")

        missing_md = gr.Markdown(
            missing_notice(t, lang), visible=bool(missing_notice(t, lang))
        )

        with gr.Row():
            # ---------------- controls ----------------
            with gr.Column(scale=3):
                mode_state = gr.State("t2v")

                with gr.Tabs():
                    with gr.Tab(t["mode_t2v"]) as tab_t2v:
                        tr(tab_t2v, label="mode_t2v")
                        tr(gr.Markdown(t["t2v_help"]), value="t2v_help")
                    with gr.Tab(t["mode_i2v"]) as tab_i2v:
                        tr(tab_i2v, label="mode_i2v")
                        tr(gr.Markdown(t["i2v_help"]), value="i2v_help")
                        chain = tr(
                            gr.Checkbox(
                                label=t["chain"], value=False, info=t["chain_info"]
                            ),
                            label="chain", info="chain_info",
                        )
                        with gr.Row():
                            start_frame = tr(
                                gr.Image(
                                    label=t["start_frame"], type="filepath", height=200
                                ),
                                label="start_frame",
                            )
                            end_frame = tr(
                                gr.Image(
                                    label=t["end_frame"], type="filepath", height=200
                                ),
                                label="end_frame",
                            )
                    with gr.Tab(t["mode_r2v"]) as tab_r2v:
                        tr(tab_r2v, label="mode_r2v")
                        tr(gr.Markdown(t["r2v_help"]), value="r2v_help")
                        ref_images = tr(
                            gr.File(
                                label=t["ref_images"],
                                file_count="multiple",
                                file_types=["image"],
                            ),
                            label="ref_images",
                        )
                        with gr.Row():
                            ref_video = tr(
                                gr.Video(label=t["ref_video"]), label="ref_video"
                            )
                            ref_audio = tr(
                                gr.Audio(label=t["ref_audio"], type="filepath"),
                                label="ref_audio",
                            )
                        with gr.Row():
                            use_video_audio = tr(
                                gr.Checkbox(label=t["use_video_audio"], value=True),
                                label="use_video_audio",
                            )
                            ref_size = tr(
                                gr.Radio(
                                    choices=["match", "max"],
                                    value="match",
                                    label=t["ref_size"],
                                    info=t["ref_size_info"],
                                ),
                                label="ref_size", info="ref_size_info",
                            )

                prompt = tr(
                    gr.Textbox(
                        label=t["prompt"],
                        placeholder=t["prompt_ph"],
                        lines=10,
                        max_lines=30,
                    ),
                    label="prompt", placeholder="prompt_ph",
                )

                with gr.Row():
                    duration = tr(
                        gr.Slider(
                            minimum=1.0,
                            maximum=MAX_DURATION_SEC,
                            value=default_duration,
                            step=0.5,
                            label=t["duration"],
                        ),
                        label="duration",
                    )
                    resolution = tr(
                        gr.Dropdown(
                            choices=list(RESOLUTION_PRESETS),
                            value=DEFAULT_RESOLUTION,
                            label=t["resolution"],
                        ),
                        label="resolution",
                    )
                frames_note = gr.Markdown(describe_frames(default_duration, t))

                with gr.Accordion(t["advanced"], open=False) as advanced_box:
                    tr(advanced_box, label="advanced")
                    with gr.Row():
                        steps = tr(
                            gr.Slider(
                                minimum=1, maximum=60, value=20, step=1,
                                label=t["steps"], info=t["steps_info"],
                            ),
                            label="steps", info="steps_info",
                        )
                        seed = tr(
                            gr.Number(
                                value=-1, precision=0,
                                label=t["seed"], info=t["seed_info"],
                            ),
                            label="seed", info="seed_info",
                        )
                    with gr.Row():
                        sampler = tr(
                            gr.Dropdown(
                                choices=samplers,
                                value="res_multistep" if "res_multistep" in samplers
                                else (samplers[0] if samplers else None),
                                label=t["sampler"],
                            ),
                            label="sampler",
                        )
                        scheduler = tr(
                            gr.Dropdown(
                                choices=schedulers,
                                value="simple" if "simple" in schedulers
                                else (schedulers[0] if schedulers else None),
                                label=t["scheduler"],
                                info=t["scheduler_info"],
                            ),
                            label="scheduler", info="scheduler_info",
                        )
                    tr(gr.Markdown(t["models"]), value="models")
                    with gr.Row():
                        diffusion_model = tr(
                            gr.Dropdown(
                                choices=diffusion_opts,
                                value=pick_default(
                                    diffusion_opts, MODEL_HINTS["fl2va"]
                                ),
                                label=t["diffusion"],
                            ),
                            label="diffusion",
                        )
                        text_encoder = tr(
                            gr.Dropdown(
                                choices=encoder_opts,
                                value=pick_default(
                                    encoder_opts, MODEL_HINTS["text_encoder"]
                                ),
                                label=t["encoder"],
                            ),
                            label="encoder",
                        )
                    with gr.Row():
                        video_vae = tr(
                            gr.Dropdown(
                                choices=vae_opts,
                                value=pick_default(vae_opts, MODEL_HINTS["video_vae"]),
                                label=t["vvae"],
                            ),
                            label="vvae",
                        )
                        audio_vae = tr(
                            gr.Dropdown(
                                choices=vae_opts,
                                value=pick_default(vae_opts, MODEL_HINTS["audio_vae"]),
                                label=t["avae"],
                            ),
                            label="avae",
                        )
                    weight_dtype = tr(
                        gr.Dropdown(
                            choices=[
                                "default", "fp8_e4m3fn", "fp8_e4m3fn_fast", "fp8_e5m2"
                            ],
                            value="default",
                            label=t["dtype"],
                        ),
                        label="dtype",
                    )
                    label = tr(
                        gr.Textbox(label=t["label"], placeholder=t["label_ph"]),
                        label="label", placeholder="label_ph",
                    )

                add_btn = tr(
                    gr.Button(t["add"], variant="primary", size="lg"), value="add"
                )
                add_note = tr(gr.Markdown(f"<sub>{t['add_info']}</sub>"),
                              value="add_info")

            # ---------------- results ----------------
            with gr.Column(scale=2):
                status = gr.Markdown(t["idle"])
                result_video = tr(
                    gr.Video(label=t["result"], height=320), label="result"
                )
                queue_table = tr(
                    gr.Dataframe(
                        headers=["", "ID", "Job", "Status", "Time"],
                        datatype=["str"] * 5,
                        label=t["queue"],
                        interactive=False,
                        wrap=True,
                    ),
                    label="queue",
                )
                with gr.Row():
                    refresh_btn = tr(gr.Button(t["refresh"], size="sm"),
                                     value="refresh")
                    clear_btn = tr(gr.Button(t["clear"], size="sm"), value="clear")
                with gr.Row():
                    cancel_id = tr(
                        gr.Textbox(label=t["cancel_id"], scale=2), label="cancel_id"
                    )
                    cancel_btn = tr(
                        gr.Button(t["cancel"], size="sm", scale=1), value="cancel"
                    )
                join_btn = tr(gr.Button(t["join"]), value="join")
                join_note = tr(gr.Markdown(f"<sub>{t['join_info']}</sub>"),
                               value="join_info")
                joined_video = tr(
                    gr.Video(label=t["joined"], height=240, visible=False),
                    label="joined",
                )

        # ---------------- behaviour ----------------

        duration.change(
            lambda seconds, code: describe_frames(seconds, T[code]),
            [duration, lang_state],
            frames_note,
        )

        for tab, name in ((tab_t2v, "t2v"), (tab_i2v, "i2v"), (tab_r2v, "r2v")):
            tab.select(lambda n=name: n, None, mode_state)

        def snapshot(code: str):
            tt = T.get(code, T["en"])
            jobs = manager.jobs()
            rows = []
            for job in jobs:
                name = job.label or job.prompt[:38].replace("\n", " ") or job.mode
                when = f"{job.elapsed():.0f}s" if job.started_at else "-"
                rows.append(
                    [
                        STATUS_ICON.get(job.status, "?"),
                        job.id,
                        f"{job.mode} · {name}",
                        job.status,
                        when,
                    ]
                )
            latest = None
            for job in reversed(jobs):
                if job.status == "done" and job.output_path:
                    latest = job.output_path
                    break

            line = manager.status_line()
            if line == "Idle":
                line = tt["idle"]
            failed = [j for j in jobs if j.status == "error"]
            if failed:
                line += f"\n\n**{failed[-1].id} {tt['failed']}:** {failed[-1].error}"
            return line, rows, latest

        def enqueue(
            code, mode, prompt_text, seconds, resolution_key, steps_v, seed_v,
            sampler_v, scheduler_v, diffusion_v, encoder_v, vvae_v, avae_v, dtype_v,
            label_v, chain_v, start_v, end_v, ref_imgs_v, ref_vid_v, ref_aud_v,
            use_vid_audio_v, ref_size_v,
        ):
            tt = T.get(code, T["en"])
            if not (prompt_text or "").strip():
                raise gr.Error(tt["err_prompt"])
            if not all([diffusion_v, encoder_v, vvae_v, avae_v]):
                raise gr.Error(tt["err_models"])
            if mode == "i2v" and not chain_v and not start_v:
                raise gr.Error(tt["err_start"])
            if mode == "r2v" and not (ref_imgs_v or ref_vid_v or ref_aud_v):
                raise gr.Error(tt["err_refs"])

            width, height = RESOLUTION_PRESETS[resolution_key]
            ref_paths = [
                f.name if hasattr(f, "name") else str(f) for f in (ref_imgs_v or [])
            ]

            manager.add(
                Job(
                    mode=mode,
                    prompt=prompt_text,
                    width=width,
                    height=height,
                    length=snap_length(seconds),
                    models=ModelSet(
                        diffusion_model=diffusion_v,
                        text_encoder=encoder_v,
                        video_vae=vvae_v,
                        audio_vae=avae_v,
                        weight_dtype=dtype_v,
                    ),
                    sampling=SamplingSettings(
                        steps=int(steps_v),
                        sampler=sampler_v,
                        scheduler=scheduler_v,
                        seed=int(seed_v),
                    ),
                    first_frame=start_v if mode == "i2v" else None,
                    last_frame=end_v if mode == "i2v" else None,
                    ref_images=ref_paths if mode == "r2v" else [],
                    ref_video=ref_vid_v if mode == "r2v" else None,
                    ref_audio=ref_aud_v if mode == "r2v" else None,
                    use_video_audio=bool(use_vid_audio_v),
                    ref_image_size=ref_size_v,
                    chain_from_previous=bool(chain_v) and mode == "i2v",
                    label=(label_v or "").strip(),
                )
            )
            return snapshot(code)

        add_inputs = [
            lang_state, mode_state, prompt, duration, resolution, steps, seed,
            sampler, scheduler, diffusion_model, text_encoder, video_vae, audio_vae,
            weight_dtype, label, chain, start_frame, end_frame, ref_images,
            ref_video, ref_audio, use_video_audio, ref_size,
        ]
        live_outputs = [status, queue_table, result_video]

        add_btn.click(enqueue, add_inputs, live_outputs)
        refresh_btn.click(snapshot, lang_state, live_outputs)

        def do_cancel(job_id: str, code: str):
            if (job_id or "").strip():
                manager.cancel(job_id.strip())
            return snapshot(code)

        cancel_btn.click(do_cancel, [cancel_id, lang_state], live_outputs)

        def do_clear(code: str):
            manager.clear_finished()
            return snapshot(code)

        clear_btn.click(do_clear, lang_state, live_outputs)

        def do_join(code: str):
            tt = T.get(code, T["en"])
            done = manager.completed()
            if len(done) < 2:
                raise gr.Error(tt["err_join_few"])
            if not ffmpeg_available():
                raise gr.Error(tt["err_ffmpeg"])
            path = concat_videos(
                [j.output_path for j in done],
                Path(manager.output_dir) / "joined.mp4",
            )
            return gr.update(value=str(path), visible=True)

        join_btn.click(do_join, lang_state, joined_video)

        # ---------------- language switching ----------------

        def switch_language(code: str, seconds: float):
            code = code if code in T else "en"
            tt = T[code]
            updates = [gr.update(**build(tt)) for _, build in translated]
            notice = missing_notice(tt, code)
            return [
                code,
                f"## H3 Studio\n{tt['subtitle']}  ·  Powered by MiniMax H3",
                gr.update(value=notice, visible=bool(notice)),
                describe_frames(seconds, tt),
                *updates,
            ]

        language.change(
            switch_language,
            [language, duration],
            [lang_state, header, missing_md, frames_note]
            + [component for component, _ in translated],
        )

        # Keep the queue view live while a job renders.
        gr.Timer(2.0).tick(snapshot, lang_state, live_outputs)

    return demo


def main() -> int:
    config = load_config()
    parser = argparse.ArgumentParser(description="H3 Studio")
    parser.add_argument("--comfy-url", default=config["comfy_url"])
    parser.add_argument("--output-dir", default=config["output_dir"])
    parser.add_argument("--port", type=int, default=config["server_port"])
    parser.add_argument("--lang", choices=sorted(T), default=config["language"],
                        help="Starting interface language; switchable in the UI.")
    parser.add_argument("--listen", action="store_true",
                        help="Bind on the local network, not just this machine.")
    parser.add_argument(
        "--share", action="store_true", default=config["share"],
        help=(
            "Expose a temporary public Gradio URL that anyone on the internet "
            "can reach - not just your local network. WARNING: this app has no "
            "login, no region checks, and no content moderation built in, and "
            "the MiniMax H3 license imposes real obligations (safeguards, "
            "territorial limits, disclosures) on anyone who lets third parties "
            "generate Outputs through it. Making the model reachable like this "
            "without meeting those obligations yourself is on you. Not "
            "recommended for ordinary personal/local use."
        ),
    )
    args = parser.parse_args()

    if args.share:
        print(
            "\n"
            "[warning] --share will publish a public URL that anyone on the\n"
            "internet can open - this app has no login, no region checks, and\n"
            "no content moderation. If people outside the MiniMax H3 license's\n"
            "Applicable Territory can reach it, or if it is used as a public\n"
            "Hosted Service, you - not this project - are responsible for\n"
            "meeting the license's safeguard and territorial obligations.\n"
            "Prefer running locally (no --share) unless you understand and\n"
            "accept that.\n",
            file=sys.stderr,
        )

    client = ComfyClient(args.comfy_url)
    if not client.is_alive():
        print(
            f"Could not reach ComfyUI at {args.comfy_url}.\n"
            "Start ComfyUI first, or pass --comfy-url with the right address.",
            file=sys.stderr,
        )
        return 1
    if not client.has_h3_nodes():
        print(
            "This ComfyUI build does not have the MiniMax H3 nodes.\n"
            "Update ComfyUI to a version that includes them (0.30.0 or newer).",
            file=sys.stderr,
        )
        return 1
    if not ffmpeg_available():
        print("[warn] ffmpeg not found - clip chaining and joining will not work.")

    output_dir = Path(args.output_dir)
    manager = QueueManager(client, output_dir, output_dir / ".work")
    demo = build_ui(manager, client, args.lang)
    demo.queue().launch(
        server_name="0.0.0.0" if args.listen else "127.0.0.1",
        server_port=args.port,
        share=args.share,
        inbrowser=True,
        show_api=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
