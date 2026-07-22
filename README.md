# Batch ADetailer for Forge Neo

A batch-processing ADetailer extension for [Stable Diffusion WebUI Forge — Neo](https://github.com/Haoming02/sd-webui-forge-classic/tree/neo).

oooo this is really helpful for me! but you can drag in your images, and configure each image to have it's own adetailer settings / prompt all in one go! thank you claude

![Forge Neo](https://img.shields.io/badge/Forge-Neo-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## Requirements

The [ADetailer](https://github.com/Bing-su/adetailer) extension must be installed
and enabled (built against the `aadetailer-neoforge` fork). This extension drives
ADetailer's own pipeline — it does not reimplement detection or inpainting.

## How it works

Drop your images. Click a thumbnail on the right, and that image's **slots** load
on the left. Each slot picks one of your ADetailer units — which brings along that
unit's detection model **and every setting you saved for it** in the img2img
ADetailer panel (mask blur, dilate/erode, padding, steps, CFG, sampler, ...). You
only override the handful of things that actually vary per image:

- **ADetailer prompt / negative prompt** (empty = reuse that image's own prompt
  from its metadata; `[base prompt]` = that same prompt, but with room to add to it)
- **Detection confidence**
- **Inpaint denoising strength**
- **Mask max area ratio**

**Slot order is execution order.** So on an image where a hand overlaps a face,
put the hand unit in Slot 1 and the face unit in Slot 2, and the face pass runs
last — over the top of the hand — for maximum retention. Any image where you don't
care just keeps the defaults.

**Right-click a thumbnail** to drop `[base prompt]` into Slot 1's prompt for that
image. It stands for that image's own prompt, so leaving it alone inherits exactly
as an empty box would — and anything you write around it is *added* to that
prompt, e.g. `[base prompt], detailed eyes, looking at viewer`. It works in any
slot and in the negative box too (it's ADetailer's `[PROMPT]` placeholder under a
friendlier name).

**▶️ Run this image** (next to Run Batch) re-runs only the thumbnail you have
selected — for when a batch came out fine except for one or two. It saves
alongside the earlier result rather than overwriting it (`mypic-adetailer-1.png`),
so you can pick the one you prefer.

There's an **Apply these settings to all images** button for when one config suits
the whole batch. It copies the unit choice, confidence, denoising strength and
mask max ratio onto every image — but **not the prompts**, since those are the
part that's meant to differ per image. Every image keeps its own.

## Features

- **Per-image settings** — every dropped image remembers its own slots, prompts,
  and sliders
- **Inherits your ADetailer defaults** — models and all the fiddly settings come
  from what you configured in the img2img ADetailer panel (Settings → Defaults),
  so the batch tab stays uncluttered and always matches your setup
- **Reorderable units** — per image, choose which unit runs first
- **No base-image regeneration** — uses ADetailer's "skip img2img" path, so the
  base pass is a throwaway 1-step 128×128 render and only the detected regions are
  actually inpainted, at full resolution
- **Per-image prompt inheritance** — an empty ADetailer prompt falls back to the
  image's own prompt, read from its embedded generation info
- **LoRA name repair** — an old image's prompt often names a LoRA that has since
  been renamed (a training epoch like `mylora-000021` that's now just `mylora`).
  Forge can't resolve it and quietly renders without the LoRA; this re-points the
  name at the real file, and says so in the log when it can't
- **Keep your filenames** — results save as `<original name><suffix>.png`
  (e.g. `mypic-adetailer.png`) flat in the output folder (optional, on by default)
- **Roomy layout** — the drop zone spans the top, and the thumbnail gallery has a
  drag handle in its bottom-right corner for when four at a time isn't enough
- **Big preview** — the selected image shows large under the gallery, so you can
  see what you're configuring without opening it elsewhere
- **Suffix filter on the drop zone** — drag in a whole folder's worth of files and
  only the ones ending in the suffix (default `-hires`) load; the rest are skipped
- **Test-folder mode** — scans your work directories for `<set>/Tests` folders
  with `-hires` images that have no `-adetailer` version yet, loads them with one
  click, and saves each result back next to its own source image
- **Live progress** — the log fills in as each image finishes; the Cancel button
  aborts the image being worked on and stops the batch
- **Readable errors** — failures show the full traceback in the status log and
  skip to the next image (configurable)

## Installation

1. Clone this repository into your Forge Neo `extensions` folder:
   ```
   cd <your Forge Neo folder>/extensions
   git clone https://github.com/amiiari/batch-adetailer-neo
   ```
2. Restart Forge Neo (or Reload UI).
3. A new **Batch ADetailer** tab appears.

No extra dependencies are required.

## Usage

1. Set your ADetailer units up once, the way you like them, on the **img2img**
   tab, and save them as defaults (Settings → Defaults, or the `ui-config.json`
   mechanism). Unit 1 = faces, Unit 2 = hands, etc.
2. Open the **Batch ADetailer** tab and drop your images in.
3. Click a thumbnail; tune that image's slots on the left. Repeat for any image
   that needs different treatment.
4. Click **🚀 Run Batch ADetailer**.

> **Note:** the number of slots is capped by **Settings → ADetailer → Max models**.

### Test-folder mode

The **📁 Test Folders** panel at the top scans the roots configured in Settings
for `<set>/Tests` folders (e.g. `Commissions/Commission 137/Tests`). A set is
listed while its Tests folder has `-hires` images with no `-adetailer` or
`-edited` variant next to them.

Tick the sets you want and click **📥 Load Selected Folders** — the pending
images land in the tab like a normal drop, so you still configure prompts per
image before running. Loading also ticks **Save next to each source image**:
with it on, every result saves as `<name>-adetailer.png` into the same folder
its source came from (always png, suffix and output-dir settings ignored),
which is what the pending scan keys on — so re-running only ever does new work,
and the list rescans itself after each batch.

## Settings

Under **Settings → Batch ADetailer**:

- **Output Directory** — custom save location (empty = default img2img output dir)
- **Test-folder scan roots** — semicolon-separated directories scanned for
  `<set>/Tests` folders by the Test Folders panel
- **Max Images per Batch** — safety limit (default 50)
- **Skip Failed Images and Continue** — keep going when one image fails (default on)
- **Repair Unresolvable LoRA Names in Prompts** — re-point a renamed/epoch LoRA
  name at the matching file in your Lora folder (default on)

## License

MIT
