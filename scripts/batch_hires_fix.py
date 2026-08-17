"""
Batch Hires-Fix tab
===================
A UI tab where you drag in multiple txt2img-generated images and run each
through hires-fix in batch, reusing Forge's existing pipeline. Runs SECOND in
the refine chain, after Batch ADetailer — the scan/save plumbing is the shared
core parameterized with this stage's record.
"""
import importlib
import os
import traceback
from contextlib import closing

import gradio as gr
from PIL import Image

from modules import images, processing, script_callbacks, scripts, shared
from modules_forge import main_thread

import batch_adetailer_shared as bshared
# scripts/*.py are re-executed on every in-process Reload UI, but root modules
# stay cached in sys.modules — reload so shared-code edits land too.
importlib.reload(bshared)

STAGE = bshared.HIRES_STAGE

# ──────────────────────────────────────────────
# Extension Settings (registered via add_option)
# ──────────────────────────────────────────────
def _register_settings():
    section = ("batch_hires_fix", "Batch Hires-Fix")

    shared.opts.add_option(
        "batch_hires_fix_output_dir",
        shared.OptionInfo("", "Output Directory", gr.Textbox, {}, section=section)
        .info("Leave empty to use the default txt2img output directory."),
    )

    shared.opts.add_option(
        "batch_hires_fix_scan_roots",
        shared.OptionInfo(
            "",
            "Test-folder scan roots (semicolon-separated)", gr.Textbox, {}, section=section)
        .info("Each root is searched (up to 3 levels deep) for Tests folders — so one "
              "root covers both <root>/<set>/Tests and <root>/Commissions/<set>/Tests. "
              "The same roots are used to find drag-dropped images back on disk. "
              "Non-existent roots are silently skipped."),
    )

    shared.opts.add_option(
        "batch_hires_fix_skip_errors",
        shared.OptionInfo(True, "Skip Failed Images and Continue", gr.Checkbox,
                          {}, section=section)
        .info("If an image fails during hires-fix, skip it and continue with the rest."),
    )

# ──────────────────────────────────────────────
# Test-folder scanning — shared core, parameterized by this stage's record.
#
# Hires-fix picks up -adetailer images that have no -hires successor, and saves
# two files per image at the same resolution: the hires-fix result
# (<stem>-hires.png) and a plain Lanczos upscale (<stem>-base.png) as the
# unedited bottom layer for the Krita edit stage.
# ──────────────────────────────────────────────
def _adetailer_images(folder):
    return bshared.stage_inputs(folder, STAGE)


def _pending_adetailer(folder):
    return bshared.pending_inputs(folder, STAGE)


# ──────────────────────────────────────────────
# Saving
# ──────────────────────────────────────────────
def _save_base_copy(img: Image.Image, geninfo: str | None, stem: str, outdir: str, size):
    """
    Plain Lanczos upscale of the source at the hires result's exact size,
    saved as <stem>-base.png next to it — the unedited bottom layer the Krita
    edit stage puts under the -hires layer. Carries the source's generation
    info. Skipped if it already exists (re-runs stay idempotent).
    """
    dest = os.path.join(outdir, f"{stem}-base.png")
    if os.path.exists(dest):
        return
    from PIL.PngImagePlugin import PngInfo
    meta = PngInfo()
    if geninfo:
        meta.add_text("parameters", geninfo)
    img.resize(size, Image.LANCZOS).save(dest, pnginfo=meta)


# ──────────────────────────────────────────────
# Core Processing Logic — mirrors txt2img_upscale_function
# ──────────────────────────────────────────────
def _process_single_image(img: Image.Image, geninfo: str | None, hires_params: dict, save_opts: dict):
    """
    Process one image through hires-fix. Mirrors the logic in
    modules/txt2img.py :: txt2img_upscale_function().

    Key difference from the ✨ button: we do NOT set p.txt2img_upscale = True,
    because that flag causes Forge Neo to skip model loading (it assumes the ✨
    button already has a loaded model). Since our extension runs independently,
    we need normal model loading to occur inside process_images().

    Runs on Forge's main thread (see batch_hires_fix_process). Never raises:
    returns (images, infotexts, error_traceback_or_None, notes) so the full
    traceback reaches the status log instead of being swallowed by Gradio.
    """
    notes: list[str] = []
    try:
        p = processing.StableDiffusionProcessingTxt2Img(
            outpath_samples=(
                save_opts.get("output_dir")
                or getattr(shared.opts, "batch_hires_fix_output_dir", None)
                or shared.opts.outdir_samples
                or shared.opts.outdir_txt2img_samples
            ),
            outpath_grids=shared.opts.outdir_grids or shared.opts.outdir_txt2img_grids,
            prompt="",
            styles=[],
            negative_prompt="",
            batch_size=1,
            n_iter=1,
            cfg_scale=float(hires_params.get("cfg_scale", 7.0)),
            distilled_cfg_scale=float(hires_params.get("hr_distilled_cfg", 3.0)),
            width=img.size[0],
            height=img.size[1],
            enable_hr=True,
            denoising_strength=float(hires_params.get("denoising_strength", 0.6)),
            hr_scale=float(hires_params.get("hr_scale", 2.0)),
            hr_upscaler=hires_params.get("hr_upscaler"),
            hr_second_pass_steps=int(hires_params.get("hr_second_pass_steps", 0)),
            hr_resize_x=int(hires_params.get("hr_resize_x", 0)),
            hr_resize_y=int(hires_params.get("hr_resize_y", 0)),
            hr_checkpoint_name=None,
            hr_additional_modules=["Use same choices"],
            hr_sampler_name=(
                None if hires_params.get("hr_sampler_name") == "Use same sampler"
                else hires_params.get("hr_sampler_name")
            ),
            hr_scheduler=(
                None if hires_params.get("hr_scheduler") == "Use same scheduler"
                else hires_params.get("hr_scheduler")
            ),
            hr_prompt="",
            hr_negative_prompt="",
            hr_cfg=float(hires_params.get("hr_cfg", 6.0)),
            hr_distilled_cfg=float(hires_params.get("hr_distilled_cfg", 3.0)),
            override_settings={},
        )

        # Same pattern as txt2img_create_processing: assign scripts + real
        # default args and let the setters run setup_scripts() normally.
        p.scripts = scripts.scripts_txt2img
        p.script_args = bshared.get_default_script_args(scripts.scripts_txt2img, "txt2img").copy()

        if geninfo:
            # The ✨ button gets these for free from the live txt2img UI state;
            # we must recover them from the image's infotext.
            bshared.apply_source_image_parameters(p, geninfo)
            # Shift-based models (Qwen, Flux, ...): use the same shift for the
            # hires pass as the original generation, like "Use same" semantics.
            p.hr_distilled_cfg = p.distilled_cfg_scale

        # An old image's prompt can name a LoRA that no longer exists under
        # that name — the hires pass would silently render without it.
        p.prompt, found = bshared.fix_lora_names(p.prompt)
        notes += found
        p.negative_prompt, found = bshared.fix_lora_names(p.negative_prompt)
        notes += found
        for note in dict.fromkeys(notes):
            print(f"[Batch Hires-Fix] {note}")

        p.firstpass_image = img
        # Intentionally NOT setting p.txt2img_upscale — see docstring above.

        if shared.opts.txt2img_upscale_single_batch:
            p.batch_size = 1
            p.n_iter = 1

        p.override_settings["save_images_before_highres_fix"] = False

        if save_opts.get("use_original_name"):
            # We save manually afterwards with the original filename + suffix.
            p.do_not_save_samples = True

        with closing(p):
            processed = scripts.scripts_txt2img.run(p, *p.script_args)

            if processed is None:
                processed = processing.process_images(p)

        if shared.state.interrupted or shared.state.stopping_generation:
            # A cancelled sampling loop returns the partially-denoised image;
            # saving it would make the result look finished (and folder mode
            # would then never re-list the base) — drop it instead.
            return [], [], None, notes

        if save_opts.get("use_original_name"):
            bshared.save_with_original_name(processed, p, save_opts)

        if save_opts.get("base_copy") and processed.images:
            # The Krita edit stage layers -hires over an unedited upscale of
            # the same size; a twin failure shouldn't fail the image.
            try:
                _save_base_copy(img, geninfo, save_opts["stem"],
                                p.outpath_samples, processed.images[0].size)
            except Exception as e:
                print(f"[Batch Hires-Fix] -base copy failed: {e}")

        return processed.images, processed.infotexts, None, notes
    except Exception:
        tb = traceback.format_exc()
        print(f"[Batch Hires-Fix] Error processing image:\n{tb}")
        return [], [], tb, notes


def batch_hires_fix_process(
    files,
    denoising_strength,
    hr_scale,
    hr_upscaler,
    hr_second_pass_steps,
    hr_resize_x,
    hr_resize_y,
    hr_sampler_name,
    hr_scheduler,
    hr_cfg,
    use_original_name,
    filename_suffix,
    save_base_copy=True,
    save_to_source=False,
):
    """
    Main batch processing function. Processes each image through hires-fix
    sequentially and collects all results.

    save_base_copy: also save a plain Lanczos upscale of each source as
    <stem>-base.png at the result's resolution (the unedited bottom layer for
    the Krita edit stage).
    save_to_source: each result is saved into the directory its source image
    came from (folder mode), instead of the configured output directory.
    """
    my_run = bshared.start_run(STAGE)

    if not files:
        yield [], "No images to process. Please drag and drop some images first."
        return

    skip_errors = shared.opts.batch_hires_fix_skip_errors

    hires_params = {
        "denoising_strength": float(denoising_strength),
        "hr_scale": float(hr_scale),
        "hr_upscaler": str(hr_upscaler) if hr_upscaler else None,
        "hr_second_pass_steps": int(hr_second_pass_steps),
        "hr_resize_x": int(hr_resize_x or 0),
        "hr_resize_y": int(hr_resize_y or 0),
        "hr_sampler_name": str(hr_sampler_name) if hr_sampler_name else None,
        "hr_scheduler": str(hr_scheduler) if hr_scheduler else None,
        "hr_cfg": float(hr_cfg),
    }

    total = len(files)
    all_results: list = []
    status_messages: list[str] = []
    failed_count = 0

    for idx, file_obj in enumerate(files):
        if bshared.cancel_requested(STAGE, my_run):
            status_messages.append(f"⏹️ Cancelled — {idx} of {total} images processed.")
            break

        if isinstance(file_obj, str):
            image_path = file_obj
        elif hasattr(file_obj, "name"):
            image_path = file_obj.name
        else:
            status_messages.append(
                f"❌ [{idx + 1}/{total}] Unknown file object at index {idx}"
            )
            failed_count += 1
            continue

        fname = os.path.basename(image_path)
        # status lines read "Commission 137 - M, Fluorite/3r1-adetailer.png"
        name = bshared.display_name(image_path) if save_to_source else fname

        try:
            img = Image.open(image_path)
            # Read infotext BEFORE converting — convert() can drop PNG info.
            geninfo, _items = images.read_info_from_image(img)
            img = img.convert("RGB")
        except Exception as e:
            status_messages.append(f"❌ [{idx + 1}/{total}] Failed to load {name}: {e}")
            failed_count += 1
            continue

        if not geninfo:
            status_messages.append(
                f"⚠️ [{idx + 1}/{total}] {name}: no generation info found in image — "
                f"hires pass will run with an empty prompt."
            )

        shared.total_tqdm.clear()

        save_opts = {
            "use_original_name": bool(use_original_name),
            "stem": os.path.splitext(fname)[0],
            "suffix": filename_suffix or "",
            "base_copy": bool(save_base_copy),
        }
        if save_to_source:
            # The downstream pipeline (and the pending scan) key on
            # <stem>-hires.png, so the name and format are forced here rather
            # than taken from the suffix box: a custom suffix would leave the
            # -adetailer image "pending" forever and reprocess it every run.
            # png also keeps the infotext, which jpg/jxl/... silently drop.
            save_opts["use_original_name"] = True
            save_opts["suffix"] = "-hires"
            save_opts["format"] = "png"
            if bshared.is_dragged_temp_copy(image_path):
                # A drag-dropped file whose original resolve_dropped_paths could
                # not find: dirname() is gradio's upload cache, so saving there
                # would bury the result in a folder gradio later wipes. Leave
                # output_dir unset so it falls back to the output dir, and say so.
                status_messages.append(
                    f"⚠️ [{idx + 1}/{total}] {name}: 'save into each image's own "
                    f"folder' is on, but this image wasn't found under your scan "
                    f"roots — its result goes to the output dir. Add its folder in "
                    f"Settings → Batch Hires-Fix → scan roots."
                )
            else:
                save_opts["output_dir"] = os.path.dirname(image_path)

        # state.begin() resets state.interrupted / stopping_generation, which
        # otherwise stay True forever after a UI reload (request_restart calls
        # interrupt()) and make process_images_inner return 0 images silently.
        # Real generations get this from the UI's wrap_gradio_gpu_call wrapper.
        shared.state.begin(job="batch_hires_fix")
        try:
            # GPU work must run on Forge's main thread, same as the ✨ button
            # (txt2img.py routes through main_thread.run_and_wait_result).
            result_images, _infotexts, error_tb, notes = main_thread.run_and_wait_result(
                _process_single_image, img, geninfo, hires_params, save_opts
            )
        finally:
            shared.state.end()

        shared.total_tqdm.clear()

        for note in dict.fromkeys(notes or []):
            status_messages.append(f"🔧 [{idx + 1}/{total}] {name}: {note}")

        if error_tb:
            status_messages.append(f"❌ [{idx + 1}/{total}] Error on {name}:\n{error_tb}")
            failed_count += 1
            if not skip_errors:
                break
            continue

        # Cancel/Interrupt pressed during this image: report and stop the batch.
        # (Checked before the next begin(), which would reset shared.state's flags.)
        if bshared.cancel_requested(STAGE, my_run) or shared.state.interrupted or shared.state.stopping_generation:
            status_messages.append(
                f"⏹️ [{idx + 1}/{total}] Cancelled during {name} — stopping batch."
            )
            failed_count += 1
            break

        if not result_images:
            status_messages.append(f"⚠️ [{idx + 1}/{total}] No output for {name}")
            failed_count += 1
            continue

        all_results.extend(result_images)
        status_messages.append(f"✅ [{idx + 1}/{total}] Done: {name}")

        # Stream partial results into the gallery as each image finishes.
        yield all_results, f"Processing... {idx + 1}/{total} done.\n\n" + "\n".join(status_messages)

    status_text = (
        f"Batch complete — {len(all_results)} succeeded, "
        f"{failed_count} failed/skipped out of {total}.\n\n"
        + "\n".join(status_messages)
    )

    yield all_results, status_text

def batch_hires_fix_process_folders(
    folders,
    denoising_strength,
    hr_scale,
    hr_upscaler,
    hr_second_pass_steps,
    hr_resize_x,
    hr_resize_y,
    hr_sampler_name,
    hr_scheduler,
    hr_cfg,
    save_base_copy=True,
):
    """
    Folder mode: hires-fix every pending -adetailer image in the selected
    Tests folders, saving each result (plus its -base Lanczos twin) back into
    the folder it came from.
    """
    if not folders:
        yield [], "No folders selected — tick at least one (🔄 Rescan if the list is stale)."
        return

    # A ticked set covers its own folder AND its Tests folder.
    files = [f for folder in folders for d in bshared.set_scan_dirs(folder)
             for f in _pending_adetailer(d)]
    if not files:
        yield [], ("Nothing to do — every -adetailer image in the selected sets "
                   "already has a -hires (or later) version.")
        return

    # Original-name saving AND the "-hires" suffix are forced (the suffix
    # textbox is ignored here): <stem>-hires.png next to its source is what the
    # pending-scan and the content manager key on — a custom suffix would
    # leave -adetailer images "pending" forever and reprocess them every run.
    yield from batch_hires_fix_process(
        files,
        denoising_strength,
        hr_scale,
        hr_upscaler,
        hr_second_pass_steps,
        hr_resize_x,
        hr_resize_y,
        hr_sampler_name,
        hr_scheduler,
        hr_cfg,
        True,
        "-hires",
        save_base_copy=save_base_copy,
        save_to_source=True,
    )


# ──────────────────────────────────────────────
# Gradio UI Tab
# ──────────────────────────────────────────────
def _on_files(files, suffix_filter):
    """Files dropped/browsed: keep only those whose stem ends with the suffix
    filter (blank = keep everything), trade gradio's temp copies for the on-disk
    originals, and preview the survivors. Returns updates for
    [source_gallery, paths_state, file_input, status_text]."""
    paths = bshared.file_paths(files)
    paths, skipped = bshared.filter_suffix(paths, suffix_filter)

    # Rewrite the drop zone only when we actually filtered something out. That
    # rewrite retriggers this handler; the second pass skips nothing and returns
    # gr.update() with no value, which ends the loop (same guard as adetailer).
    file_update = gr.update(value=paths or None) if skipped else gr.update()

    # AFTER file_update: the run reads paths_state, not the drop zone, so the
    # originals never round-trip through gr.File (which would re-copy them into
    # gradio's cache and lose the folder again).
    paths, notes = bshared.resolve_dropped_paths(paths, STAGE)

    if not (files or []):
        status = ""
    else:
        note = (f" Skipped {skipped} not ending in `{(suffix_filter or '').strip()}`."
                if skipped else "")
        status = " ".join([f"Loaded {len(paths)} image(s)." + note, *notes])
    return gr.update(value=paths or None), paths, file_update, status


def _folder_choices():
    return bshared.folder_choices(STAGE)


def _load_folder_path(path):
    """📂 Load Folder: every -adetailer image in one folder, done or not, loaded
    with its real path so save-to-source lands results back in it. Bypasses the
    Test Folders panel, which is a to-do list and so can't show a finished set —
    or a set that keeps its images outside a Tests folder at all.
    Returns updates for [source_gallery, paths_state, save_to_source, status_text]."""
    folder = (path or "").strip().strip('"')
    files = _adetailer_images(folder) if folder else []
    if not files:
        return gr.update(), gr.update(), gr.update(), (
            "No -adetailer images in that folder — check the path. "
            "(This stage's inputs are <name>-adetailer.png files.)"
        )
    return gr.update(value=files), files, gr.update(value=True), (
        f"Loaded {len(files)} image(s) from {folder} — results save back into it as "
        f"<name>-hires.png. 🚀 Run Batch Hires-Fix when ready."
    )


def _build_ui_tab():
    from modules import sd_samplers, sd_schedulers

    upscaler_choices = list(shared.latent_upscale_modes.keys()) + [x.name for x in shared.sd_upscalers]

    default_upscaler = "4xUltrasharp_4xUltrasharpV10"
    if default_upscaler not in upscaler_choices:
        default_upscaler = "Latent"

    with gr.Blocks(analytics_enabled=False) as block:
        gr.Markdown(
            "# Batch Hires-Fix\n"
            "Drag and drop images generated via txt2img to run them through hires-fix in batch."
        )

        # The run reads this, not the drop zone: dropped paths are gradio's temp
        # copies, and the originals must not round-trip back through gr.File.
        paths_state = gr.State([])

        # Test Folders panel spans the full width at the top, like the ADetailer tab.
        with gr.Accordion("📁 Test Folders — hires-fix in place", open=True):
            folder_select = _folder_choices()
            with gr.Row():
                folder_btn = gr.Button(
                    "🚀 Hires-Fix Selected Folders", variant="primary", scale=3
                )
                refresh_btn = gr.Button("🔄 Rescan", scale=1)

            # The panel above is a to-do list, so a finished set is invisible and
            # a set that keeps its images outside a Tests folder never appears at
            # all. This loads any folder as-is, done or not.
            with gr.Row():
                folder_path = gr.Textbox(
                    label="…or load every -adetailer image in one folder, done or not",
                    placeholder=r"C:\art\Commission 12 - Example",
                    max_lines=1,
                    scale=4,
                )
                load_path_btn = gr.Button("📂 Load Folder", scale=1)

        # The drop zone + filter also span the full width at the top: parked in
        # the narrow left column they crowd the settings.
        with gr.Row():
            file_input = gr.File(
                label="Drop images here (or click to browse)",
                file_count="multiple",
                file_types=["image"],
                type="filepath",
                scale=4,
            )
            suffix_filter = gr.Textbox(
                value="",
                label="Only load files ending with",
                info="Drag a whole folder in — anything else is skipped. "
                     "Empty = load everything.",
                max_lines=1,
                scale=1,
            )

        with gr.Row():
            # ── Left column: hires-fix controls ──
            with gr.Column(scale=1):
                gr.Markdown("### Hires-Fix Settings")

                with gr.Row():
                    denoising_strength = gr.Slider(
                        minimum=0.0, maximum=1.0, step=0.01,
                        value=0.3, label="Denoising Strength", scale=2,
                    )
                    hr_scale = gr.Slider(
                        minimum=1.0, maximum=8.0, step=0.05,
                        value=1.25, label="Upscale By", scale=2,
                    )

                hr_upscaler = gr.Dropdown(
                    choices=upscaler_choices,
                    value=default_upscaler,
                    label="Hires Upscaler",
                    allow_custom_value=True,
                )

                # hr_cfg=1.0 would make the pipeline drop the negative prompt
                # entirely, so keep this visible. The hires distilled CFG
                # (shift) is intentionally NOT exposed: it inherits each
                # image's own base shift from infotext.
                hr_cfg = gr.Slider(
                    minimum=1.0, maximum=24.0, step=0.5,
                    value=4.5, label="Hires CFG Scale",
                )

                with gr.Row():
                    hr_second_pass_steps = gr.Slider(
                        minimum=0, maximum=150, step=1,
                        value=0, label="Hires Steps (0 = same as image's steps)", scale=2,
                    )
                    hr_resize_x = gr.Number(
                        value=0, label="Resize to Width (0 = auto)",
                        min=0, precision=0, scale=2,
                    )

                hr_resize_y = gr.Number(
                    value=0, label="Resize to Height (0 = auto)",
                    min=0, precision=0,
                )

                # ── Sampler & Scheduler ──
                with gr.Row():
                    hr_sampler_name = gr.Dropdown(
                        choices=["Use same sampler"] + sd_samplers.visible_sampler_names(),
                        value="Use same sampler",
                        label="Hires sampling method",
                    )
                    hr_scheduler = gr.Dropdown(
                        choices=["Use same scheduler"] + [x.label for x in sd_schedulers.schedulers],
                        value="Use same scheduler",
                        label="Hires schedule type",
                    )

                # ── Output naming ──
                with gr.Row():
                    use_original_name = gr.Checkbox(
                        value=True,
                        label="Save as original filename + suffix",
                        scale=2,
                    )
                    filename_suffix = gr.Textbox(
                        value="-hires",
                        label="Filename suffix",
                        max_lines=1,
                        scale=1,
                    )

                save_base_copy = gr.Checkbox(
                    value=True,
                    label="Also save a plain Lanczos upscale (<name>-base.png, same size "
                          "as the result — the unedited layer for the Krita edit stage)",
                )

                # Ticked automatically by "📂 Load Folder". Forces <name>-hires.png,
                # so the suffix box above is ignored while this is on.
                save_to_source = gr.Checkbox(
                    value=False,
                    label="Save as <name>-hires.png into each image's own folder",
                )

                with gr.Row():
                    process_btn = gr.Button(
                        "🚀 Run Batch Hires-Fix", variant="primary", size="lg", scale=3
                    )
                    cancel_btn = gr.Button("⏹️ Cancel", variant="stop", size="lg", scale=1)

            # ── Right column: output & status ──
            with gr.Column(scale=2):
                # Mirrors the adetailer tab: a preview of the images the run will
                # actually process, so the suffix filter's effect is visible.
                source_gallery = gr.Gallery(
                    label="Loaded images (what will be processed)",
                    columns=[4],
                    height=200,
                    preview=False,
                )

                # elem_id must end in "_gallery" so Forge's lightbox modal
                # (javascript/imageviewer.js + ui.js all_gallery_buttons) picks
                # it up — that's what enables ←/→ arrow-key navigation in the
                # full-size preview. preview=True matches the txt2img gallery.
                output_gallery = gr.Gallery(
                    label="Results",
                    elem_id="batch_hires_fix_gallery",
                    columns=[4],
                    height="auto",
                    preview=True,
                )

                status_text = gr.TextArea(
                    label="Status / Log",
                    lines=10,
                    interactive=False,
                )

        refresh_btn.click(
            fn=_folder_choices,
            inputs=[],
            outputs=[folder_select],
            queue=False,
        )

        load_path_btn.click(
            fn=_load_folder_path,
            inputs=[folder_path],
            outputs=[source_gallery, paths_state, save_to_source, status_text],
            queue=False,
        )

        folder_btn.click(
            fn=batch_hires_fix_process_folders,
            inputs=[
                folder_select,
                denoising_strength,
                hr_scale,
                hr_upscaler,
                hr_second_pass_steps,
                hr_resize_x,
                hr_resize_y,
                hr_sampler_name,
                hr_scheduler,
                hr_cfg,
                save_base_copy,
            ],
            outputs=[output_gallery, status_text],
        ).then(  # the run consumed pending work — rescan so the list stays honest
            fn=_folder_choices,
            inputs=[],
            outputs=[folder_select],
        )

        # queue=False so the click is served straight away instead of queueing
        # behind the running batch — otherwise the cancel could never arrive.
        cancel_btn.click(
            fn=lambda: bshared.request_cancel(STAGE),
            inputs=[],
            outputs=[status_text],
            queue=False,
        )

        # Filter + preview dropped files. queue=False keeps it snappy and stops it
        # queueing behind a running batch. Set the suffix filter BEFORE dropping —
        # like adetailer, it only re-filters when the file list itself changes.
        file_input.change(
            fn=_on_files,
            inputs=[file_input, suffix_filter],
            outputs=[source_gallery, paths_state, file_input, status_text],
            queue=False,
        )

        process_btn.click(
            fn=batch_hires_fix_process,
            inputs=[
                paths_state,
                denoising_strength,
                hr_scale,
                hr_upscaler,
                hr_second_pass_steps,
                hr_resize_x,
                hr_resize_y,
                hr_sampler_name,
                hr_scheduler,
                hr_cfg,
                use_original_name,
                filename_suffix,
                save_base_copy,
                save_to_source,
            ],
            outputs=[output_gallery, status_text],
        ).then(  # a save-to-source run consumed pending work — keep the list honest
            fn=_folder_choices,
            inputs=[],
            outputs=[folder_select],
        )

    return block

def _on_ui_tabs():
    """Register the Batch Hires-Fix tab with Forge Neo's UI."""
    yield (_build_ui_tab(), "Batch Hires-Fix", "batch-hires-fix-tab")

# ──────────────────────────────────────────────
# Registration
# Runs unconditionally: Forge Neo loads each extension script once per launch.
# Do NOT guard on shared.opts attribute existence — saved values in config.json
# make the attribute exist before registration, which would skip tab
# registration entirely.
# ──────────────────────────────────────────────
_register_settings()
script_callbacks.on_ui_tabs(_on_ui_tabs)
