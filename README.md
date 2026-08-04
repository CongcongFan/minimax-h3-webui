# H3 Studio

A small, friendly web UI for generating video with **MiniMax H3** through
ComfyUI. Text-to-video, image-to-video, and reference-to-video, with a queue
so you can line up the next shot while the current one renders.

Powered by MiniMax H3.

---

## ⚠️ Read this before you start: the model has territorial restrictions

**H3 Studio ships no model weights and never downloads them.** You obtain the
MiniMax H3 model yourself, and your use of it is governed by the
[MiniMax H3 Community License Agreement](https://huggingface.co/MiniMaxAI/MiniMax-H3),
not by this project's license.

That agreement defines an *Applicable Territory* of "worldwide, excluding the
Excluded Territories", and defines the Excluded Territories as:

> the European Union, the United Kingdom, the Republic of Korea and the United
> States of America

and states:

> You may not use, reproduce, modify, distribute, or display the MiniMax H3
> Works or any of their Outputs or results outside the Applicable Territory.

**If you are in the EU, the UK, South Korea, or the USA, you are not permitted
to use the open weights under that community license.** MiniMax directs users
in those regions to their hosted API, or to apply for a separate written
licence. Note also that the restriction covers *displaying generated outputs*,
which is worth thinking about before publishing clips to a global audience.

A few other terms worth knowing: commercial products and services built on the
model must display "MiniMax H3" in their interface, and organisations with more
than USD 20 million in related annual revenue need separate written
authorisation from MiniMax.

The license can change. **Read the current text yourself and make your own
decision** — the summary above is provided in good faith but is not legal
advice, and this project's authors are not responsible for how you use the
model.

**Please comply with the MiniMax H3 license.** Using this software does not
exempt you from any part of the MiniMax H3 Community License Agreement — you
are responsible for your own compliance, including the territorial
restrictions above.

I've done my best to get this right, but I could easily have missed
something. **If you notice anything here that doesn't comply with the MiniMax
H3 license, I'd genuinely appreciate it if you could reach out** at
**onigirikiller@proton.me** — sorry for the trouble, and I'll look into it and
fix it as soon as I've confirmed it.

**What this repository actually contains:** no MiniMax H3 model weights and no
MiniMax source code, and it does not download them. This project is
independently developed client software that communicates with a separately
installed ComfyUI instance over HTTP; it becomes MiniMax-H3-capable only once
you supply weights you obtained yourself. Whether that makes a given use of
this code compliant with the MiniMax H3 license is for you to review — see
[NOTICE](NOTICE) for the relevant license language, but form your own
judgment, especially for anything beyond casual personal use.

---

## What you need

| | |
|---|---|
| **ComfyUI** | v0.30.0 or newer — the MiniMax H3 nodes ship in ComfyUI core, so no custom nodes are required |
| **Python** | 3.10+ |
| **GPU** | 12 GB VRAM runs t2v/i2v comfortably at 864×480. Reference-to-video needs more (see *Known limits*) |
| **System RAM** | 32 GB is a practical minimum — the weights total roughly 40 GB and are streamed from RAM |
| **ffmpeg** | Optional, but required for clip chaining and joining |

## Getting the model

Download these four files from
[Comfy-Org/MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3) **only if
you are in the Applicable Territory**, and place them where ComfyUI can see
them:

```
ComfyUI/models/
├── diffusion_models/
│   ├── minimax_h3_fl2va_pruned_int8_convrot.safetensors   # t2v + i2v
│   └── minimax_h3_ref2va_pruned_int8_convrot.safetensors  # r2v (optional)
├── text_encoders/
│   └── qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors
└── vae/
    ├── minimax_h3_video_vae_fp16.safetensors
    └── minimax_h3_audio_vae_fp32.safetensors
```

These are large (~20 GB each for the diffusion models). If your ComfyUI lives
on a slow drive, ComfyUI's `extra_model_paths.yaml` lets you keep the weights
on a faster one:

```yaml
minimax_h3:
    base_path: D:/fast_drive/h3_models
    diffusion_models: diffusion_models
    text_encoders: text_encoders
    vae: vae
```

H3 Studio reads whatever ComfyUI reports as installed and offers it in the
model dropdowns. It will tell you if something is missing; it will not fetch
anything for you.

## Install and run

```bash
git clone https://github.com/onigirikiller/minimax-h3-webui.git
cd minimax-h3-webui
pip install -r requirements.txt
```

Start ComfyUI first, then:

```bash
python app.py
```

The UI opens at <http://127.0.0.1:7860>.

Useful flags:

```bash
python app.py --lang ja                        # Japanese interface
python app.py --comfy-url http://127.0.0.1:8188  # non-default ComfyUI address
python app.py --port 7861                      # different UI port
python app.py --listen                         # reachable from your LAN
```

Settings can also live in `config.json` (copy `config.example.json`).

#### ⚠️ `--share` publishes a public URL — think before you use it

There is also a `--share` flag, off by default, that asks Gradio to publish a
temporary public URL: not just your LAN, but anyone on the internet. This app
has **no login, no region checks, and no content moderation** built in.

If you turn it on, you are the one exposing MiniMax H3 to arbitrary third
parties - which is squarely the territory the MiniMax H3 license's Hosted
Service and safeguard obligations are about, and it becomes much harder to
keep usage inside the license's Applicable Territory once anyone with the
link can open it. **This is not recommended for ordinary personal/local use.**
If you have a real reason to use it, read the license sections on Hosted
Services and use restrictions first, and make sure you can actually meet
them. The app prints a warning to the console when `--share` is used, but
does not block or ask for confirmation before starting.

## Using it

**Text to Video** — write a prompt, pick a length and resolution, queue it.

**Image to Video** — supply a start frame to animate a still. Add an end frame
as well and the model interpolates between the two keyframes.

**Reference to Video** — supply reference images, a reference video, or
reference audio to carry a character, art style, motion, or voice into a new
shot. Refer to them positionally in the prompt as `<Picture 1>`, `<Video 1>`,
`<Audio 1>`. This mode uses the `ref2va` checkpoint, which you select under
*Advanced settings → Model files*.

### Writing prompts

H3 rewards detail. A bare description tends to produce a nearly static shot;
a second-by-second breakdown produces actual motion. Something like:

```
Anime-style rooftop at night, heavy rain, neon city skyline.

[0.0-3.0s] Rain intensifies; his necktie whips sideways in the wind; he keeps
           staring at the skyline, unaware. Slow push-in.
[3.0-6.0s] The stairwell light behind him flickers; distant heat-lightning
           washes the clouds white; his eyes narrow.
[6.0-9.0s] He turns his head over his shoulder, rain sheeting off his jaw.

Camera: one continuous shot, slow push-in, no cuts.
Audio: heavy rain, distant thunder, a single sustained piano note.
No 3D look, flat cel-shaded anime, no text overlays.
```

Describe the audio too — H3 generates picture and sound together in a single
pass, so the soundtrack responds to the prompt like the visuals do.

### The queue, and building longer scenes

Add jobs while one is rendering; they run in order. The real payoff is
**Continue from the previous clip** on the *Image to Video* tab: it takes the
final frame of the clip before it and uses it as the start frame, so you can
queue several clips up front and let a long scene build unattended. When they
are done, **Join all finished clips** concatenates them into one file.

Two 15-second clips chained this way join seamlessly, which is the practical
route to a 30-second piece.

### Bulk import from JSON (optional, LLM-friendly)

This is a plus-alpha feature tucked into a collapsed "📥 Bulk import (JSON)"
section next to the queue — it doesn't change or clutter the normal
single-clip workflow above.

The idea: instead of filling in the form once per clip, describe a whole
multi-scene sequence as one JSON document and hand it straight to the queue.
This is a good fit for having an LLM write the scene list for you — paste its
output straight into the text box, or save it as a file and upload that.

```json
{
  "defaults": { "resolution": "864x480", "steps": 20 },
  "scenes": [
    {
      "label": "scene1-turn",
      "mode": "i2v",
      "prompt": "A young man on a rain-soaked rooftop slowly turns...",
      "duration_sec": 15,
      "start_image": "assets/rooftop.png"
    },
    {
      "label": "scene2-reunion",
      "mode": "i2v",
      "prompt": "He finishes turning to face her, they reach out...",
      "duration_sec": 15
    }
  ]
}
```

Each scene needs only `mode` (`t2v` / `i2v` / `r2v`), `prompt`, and
`duration_sec`. Everything else falls back sensibly:

- **`resolution`** — a `"1344x768"`-style string, or omit it to use
  `defaults.resolution` or the app default. Snapped to a multiple of 32
  either way.
- **`start_image` / `end_image`** (`i2v`) — local file paths. Relative paths
  resolve against the uploaded JSON file's own folder, or against the
  current working directory when you paste JSON instead of uploading a file.
- **`chain_from_previous`** (`i2v`) — if you leave out `start_image`, this
  defaults to `true` automatically: the scene continues from the last frame
  of whichever clip finishes right before it in the queue, exactly like the
  checkbox on the Image to Video tab. Set it to `false` explicitly if a
  scene genuinely has no start frame and isn't meant to chain.
- **`ref_images` / `ref_video` / `ref_audio`** (`r2v`) — local file paths (a
  list for `ref_images`); at least one is required for an `r2v` scene.
- **`steps` / `sampler` / `scheduler` / `seed`** — fall back to `defaults`,
  then to the app's normal defaults (20 / `res_multistep` / `simple` / random).
- **`diffusion_model` / `text_encoder` / `video_vae` / `audio_vae`** — only
  needed if you want to force a specific file; otherwise the right checkpoint
  (`fl2va` for `t2v`/`i2v`, `ref2va` for `r2v`) is picked automatically from
  whatever ComfyUI has installed, the same way the dropdowns do.

The whole document is validated before anything is queued — if there's a
problem, every issue found is listed at once (wrong `mode`, a missing file, an
unknown sampler, etc.) so you can fix a bad JSON in one pass rather than
discovering issues one at a time. A worked example lives at
[`examples/scene_batch_example.json`](examples/scene_batch_example.json).

## Known limits

Measured on an RTX 3060 (12 GB VRAM) with 32 GB system RAM:

| Job | Time |
|---|---|
| 864×480, 5s (124 frames), 20 steps | ~15 min |
| 864×480, 15s (362 frames), 20 steps | ~54 min |

Render time grows faster than clip length — tripling the frame count roughly
3.6×'d the time — so a couple of long clips cost more than the same total
length in short ones, but produce fewer seams.

**Reference-to-video is VRAM hungry.** On a 12 GB card it consistently ran out
by roughly 1 GB no matter how far the resolution, step count, frame count, or
reference size were reduced; the overhead looks fixed rather than
resolution-dependent. 16 GB or more is likely needed. Text-to-video and
image-to-video are fine at 12 GB.

**On Windows, "not enough memory" may really mean "C: is full."** The pagefile
usually lives on C:, so a nearly-full system drive surfaces as a RAM
allocation failure — often in the final decode step, after the expensive
sampling has already finished. Free some space and re-run the same job:
ComfyUI caches completed nodes, so it picks up at the decode rather than
starting over. H3 Studio says as much when it sees that error.

Valid frame counts sit on a 17k+5 grid at 24 fps, and canvas dimensions must be
multiples of 32. The duration slider and resolution presets handle both for
you.

## How it fits together

```
H3 Studio (this app)  ──HTTP──>  ComfyUI  ──>  MiniMax H3 weights
   Gradio UI + queue              you run it       you supply them
```

H3 Studio builds ComfyUI API workflow graphs and submits them over HTTP. It
does not import, link against, or bundle any ComfyUI code, and it holds no
model weights.

**Network behaviour**: by default the UI only binds to `127.0.0.1` - nothing
is reachable outside your machine unless you pass `--listen` (your LAN) or
`--share` (a public URL, see the warning above). Gradio itself normally sends
small, anonymous startup telemetry (its own version, what component types
your app uses - never your prompts, files, or generated video) to
`api.gradio.app`; H3 Studio turns this off (`analytics_enabled=False`), so no
such requests are made at all.

## Licence

H3 Studio's own code is released under the [Apache License 2.0](LICENSE).

That covers **this application's code only**. MiniMax H3, ComfyUI, Gradio,
ffmpeg, and everything else this project uses or talks to are each governed
by their own license, not by this one — see [NOTICE](NOTICE) for the specific
terms and where to find the full text of each. If anything here ever seems to
say otherwise, the component's own license is what actually applies.
