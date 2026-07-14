"""
Batch ADetailer Extension for Forge Neo
=======================================
A UI tab where you drag in multiple images and run each through ADetailer
(detect + inpaint faces/hands/...) in batch — with per-image settings.

Each dropped image carries its own configuration: click a thumbnail on the right,
and that image's unit slots load on the left. Per slot you pick which of your
ADetailer units to use (which brings that unit's model and all its saved settings
along) and override just the things that vary per image: the ADetailer prompt and
negative prompt, detection confidence, inpaint denoising strength, and mask max
area ratio. Slot order is execution order, so a hand unit can be run before a face
unit on an image where the hand overlaps the face.

The processing trick: ADetailer is an alwayson script whose real work happens in
postprocess_image(). Its "skip img2img" flag neuters the base img2img pass (1 step,
Euler, 128x128 — nearly free) and makes it detect+inpaint on p.init_images[0] at
full resolution instead. So a batch run is just: build one
StableDiffusionProcessingImg2Img per image with init_images=[img], hand it
ADetailer's args with skip_img2img=True, and let process_images() do the rest.
"""
import copy as _copy
import os
import re
import sys
import traceback
from contextlib import closing
from functools import partial

import gradio as gr
from PIL import Image

from modules import images, processing, script_callbacks, scripts, shared
from modules.infotext_utils import parse_generation_parameters
from modules_forge import main_thread

ADETAILER_TITLE = "adetailer"

# The per-image overrides, in the order their controls appear in each slot.
OVERRIDE_ATTRS = [
    "ad_prompt",
    "ad_negative_prompt",
    "ad_confidence",
    "ad_denoising_strength",
    "ad_mask_max_ratio",
]
# preset dropdown + one control per override
CONTROLS_PER_SLOT = 1 + len(OVERRIDE_ATTRS)

PRESET_DISABLED = -1

# What right-click drops into a slot's prompt box. ADetailer's own placeholder for
# "the image's prompt" is [PROMPT] (adetailer/scripts/!adetailer.py :: _get_prompt);
# this is a friendlier spelling of it, translated back before the unit is handed over.
BASE_PROMPT_TOKEN = "[base prompt]"
_BASE_PROMPT_RE = re.compile(r"\[\s*base\s*prompt\s*\]", re.IGNORECASE)

DEFAULT_NUM_SLOTS = 4

# Safety net if ADetailer's pydantic model can't be reached to enumerate fields.
_FALLBACK_AD_FIELDS = {
    "ad_model", "ad_model_classes", "ad_tab_enable", "ad_hires_fix_only",
    "ad_prompt", "ad_negative_prompt", "ad_confidence", "ad_mask_filter_method",
    "ad_mask_k", "ad_mask_min_ratio", "ad_mask_max_ratio", "ad_dilate_erode",
    "ad_x_offset", "ad_y_offset", "ad_mask_merge_invert", "ad_mask_blur",
    "ad_denoising_strength", "ad_inpaint_only_masked",
    "ad_inpaint_only_masked_padding", "ad_use_inpaint_width_height",
    "ad_inpaint_width", "ad_inpaint_height", "ad_inpaint_scale", "ad_use_steps",
    "ad_steps", "ad_use_cfg_scale", "ad_cfg_scale", "ad_use_checkpoint",
    "ad_checkpoint", "ad_use_vae", "ad_vae", "ad_use_sampler", "ad_sampler",
    "ad_scheduler", "ad_use_noise_multiplier", "ad_noise_multiplier",
    "ad_use_clip_skip", "ad_clip_skip", "ad_restore_face", "ad_controlnet_model",
    "ad_controlnet_module", "ad_controlnet_weight", "ad_controlnet_guidance_start",
    "ad_controlnet_guidance_end",
}

# ──────────────────────────────────────────────
# Extension Settings (registered via add_option)
# ──────────────────────────────────────────────
def _register_settings():
    section = ("batch_adetailer", "Batch ADetailer")

    shared.opts.add_option(
        "batch_adetailer_output_dir",
        shared.OptionInfo("", "Output Directory", gr.Textbox, {}, section=section)
        .info("Leave empty to use the default img2img output directory."),
    )

    shared.opts.add_option(
        "batch_adetailer_max_images",
        shared.OptionInfo(50, "Max Images per Batch", gr.Slider,
                          {"minimum": 1, "maximum": 500, "step": 1}, section=section)
        .info("Maximum number of images that can be processed in one batch."),
    )

    shared.opts.add_option(
        "batch_adetailer_skip_errors",
        shared.OptionInfo(True, "Skip Failed Images and Continue", gr.Checkbox,
                          {}, section=section)
        .info("If an image fails during ADetailer, skip it and continue with the rest."),
    )

    shared.opts.add_option(
        "batch_adetailer_fix_lora_names",
        shared.OptionInfo(True, "Repair Unresolvable LoRA Names in Prompts", gr.Checkbox,
                          {}, section=section)
        .info(
            "A prompt read from an old image can name a LoRA that no longer exists under "
            "that name (a training epoch like 'mylora-000021' that was later renamed to "
            "'mylora'). Forge can't resolve it and renders without the LoRA. When on, such "
            "names are re-pointed at the matching file in your Lora folder."
        ),
    )

# ──────────────────────────────────────────────
# Talking to the installed ADetailer extension
# ──────────────────────────────────────────────
def _find_adetailer_script():
    """
    The installed ADetailer alwayson script object on the img2img runner, or
    None if the extension isn't installed/enabled for img2img. We need the
    object itself (not just its presence) for its args_from/args_to slice and
    for the gradio components it created.
    """
    try:
        return scripts.scripts_img2img.script(ADETAILER_TITLE)
    except Exception:
        return None


def _get_num_slots():
    """
    How many ADetailer unit slots exist. Comes from ADetailer's `ad_max_models`
    setting, which decides how many gr.State unit slots its script_args slice has
    (2 leading bools + one slot per unit).
    """
    ad_script = _find_adetailer_script()
    if ad_script is None:
        return DEFAULT_NUM_SLOTS
    try:
        slots = (ad_script.args_to - ad_script.args_from) - 2
        return slots if slots >= 1 else DEFAULT_NUM_SLOTS
    except Exception:
        return DEFAULT_NUM_SLOTS


def _get_adetailer_defaults():
    """
    The user's *saved* img2img ADetailer defaults, one dict per unit — i.e. what
    they configured via Settings → Defaults (ui-config.json), not ADetailer's
    stock values.

    ADetailer builds one `gr.State(lambda: state_init(w))` per unit, where
    state_init reads `{attr: widget.value}` off its live widgets at call time.
    Gradio keeps that lambda in `component.load_event_to_attach`. Meanwhile
    Forge's UiLoadsave *mutates* those widgets' .value with the saved defaults
    (ui_loadsave.py: `setattr(obj, field, saved_value)`), but only after all
    script UI is built. So calling the lambda now re-reads the widgets and yields
    the user's defaults; `state.value` alone would only give the build-time
    (stock) values.

    MUST be called at event/click time — at UI-build time UiLoadsave hasn't run
    yet and this returns stock defaults.
    """
    ad_script = _find_adetailer_script()
    if ad_script is None or not getattr(ad_script, "controls", None):
        return []

    out = []
    for state in ad_script.controls[2:]:  # [ad_enable, ad_skip_img2img, *unit states]
        values = None

        load_event = getattr(state, "load_event_to_attach", None)
        if load_event:
            try:
                values = dict(load_event[0]())
            except Exception:
                values = None

        if not isinstance(values, dict) or not values:
            values = dict(getattr(state, "value", None) or {})

        values.pop("is_api", None)  # leave ADetailerArgs' own default in place
        out.append(values)

    return out


def _ad_field_names():
    """
    Valid ADetailerArgs field names. The model is `extra=Extra.forbid`, so a
    single stray key makes the whole unit fail validation and get dropped
    silently — everything we hand over gets filtered through this.
    """
    try:
        model = sys.modules["adetailer"].ADetailerArgs
        fields = set(getattr(model, "__fields__", None) or model.model_fields)
        if fields:
            return fields
    except Exception:
        pass
    return set(_FALLBACK_AD_FIELDS)


def _preset_choices(defaults):
    """
    Dropdown choices for "which of my ADetailer units does this slot use", as
    (label, value) pairs — the label shows the unit's model so the user can tell
    the face unit from the hand unit at a glance.
    """
    choices = [("— slot disabled —", PRESET_DISABLED)]
    for i, unit in enumerate(defaults):
        model = str(unit.get("ad_model", "None") or "None")
        label = f"Unit {i + 1} — {model}" if model != "None" else f"Unit {i + 1} — (no model set)"
        choices.append((label, i))
    return choices

# ──────────────────────────────────────────────
# Per-image config
#
# One image's config is a flat list of NUM_SLOTS * CONTROLS_PER_SLOT values,
# laid out slot by slot as [preset, prompt, negative, confidence, denoise,
# max_ratio] — matching the order of the controls built by _slot_controls().
# ──────────────────────────────────────────────
def _blank_slot_values():
    return [PRESET_DISABLED, "", "", 0.3, 0.4, 1.0]


def _slot_values_from_unit(preset_index, unit):
    return [
        preset_index,
        str(unit.get("ad_prompt", "") or ""),
        str(unit.get("ad_negative_prompt", "") or ""),
        float(unit.get("ad_confidence", 0.3)),
        float(unit.get("ad_denoising_strength", 0.4)),
        float(unit.get("ad_mask_max_ratio", 1.0)),
    ]


def _default_config(defaults, num_slots):
    """
    A fresh image's config: slot i uses unit i (same as ADetailer's own layout),
    with the overrides pre-filled from that unit's saved defaults.
    """
    config = []
    for i in range(num_slots):
        unit = defaults[i] if i < len(defaults) else None
        # A unit with no model set can't detect anything, so leave that slot
        # disabled rather than pre-selecting a unit that would never run.
        if unit and str(unit.get("ad_model", "None") or "None") != "None":
            config += _slot_values_from_unit(i, unit)
        else:
            config += _blank_slot_values()
    return config


def _config_to_unit_dicts(config, defaults, num_slots):
    """
    One image's config -> the list of ADetailer unit dicts to run, in slot order
    (which is execution order). Each dict is that preset's full saved defaults
    with the per-image overrides applied on top.
    """
    units = []

    for i in range(num_slots):
        chunk = config[i * CONTROLS_PER_SLOT : (i + 1) * CONTROLS_PER_SLOT]
        if len(chunk) < CONTROLS_PER_SLOT:
            continue

        preset = chunk[0]
        if preset is None or int(preset) < 0 or int(preset) >= len(defaults):
            continue

        unit = dict(defaults[int(preset)])
        if not unit:
            continue

        unit["ad_prompt"] = str(chunk[1] or "")
        unit["ad_negative_prompt"] = str(chunk[2] or "")
        unit["ad_confidence"] = float(chunk[3])
        unit["ad_denoising_strength"] = float(chunk[4])
        unit["ad_mask_max_ratio"] = float(chunk[5])

        # Without these, a unit can silently no-op: a preset whose tab default is
        # off would need_skip(), and hires-fix-only never matches our img2img pass.
        unit["ad_tab_enable"] = True
        unit["ad_hires_fix_only"] = False

        if str(unit.get("ad_model", "None") or "None") == "None":
            continue

        allowed = _ad_field_names()
        units.append({k: v for k, v in unit.items() if k in allowed})

    return units

# ──────────────────────────────────────────────
# Default script args — mirrors modules/api/api.py :: init_default_script_args
# ──────────────────────────────────────────────
_default_script_args_cache: list | None = None

def _get_default_script_args():
    """
    Build a script_args list of the exact length the img2img ScriptRunner
    expects, with position 0 = 0 (no selectable script) and every alwayson
    script's slice filled with that script's own UI default values.

    This is the same technique Forge Neo's API uses (init_default_script_args)
    when there is no live UI to source args from. Passing placeholder Nones
    instead breaks alwayson scripts that index into their args expecting real
    values.
    """
    global _default_script_args_cache

    runner = scripts.scripts_img2img

    last_arg_index = 1
    for script in runner.scripts:
        if last_arg_index < script.args_to:
            last_arg_index = script.args_to

    if _default_script_args_cache is not None and len(_default_script_args_cache) == last_arg_index:
        return _default_script_args_cache

    script_args = [None] * last_arg_index
    script_args[0] = 0

    with gr.Blocks():  # script.ui() creates gradio components; needs a Blocks context
        for script in runner.scripts:
            ui_elems = script.ui(script.is_img2img)
            if ui_elems:
                script_args[script.args_from : script.args_to] = [elem.value for elem in ui_elems]

    _default_script_args_cache = script_args
    return script_args


def _assemble_script_args(unit_dicts):
    """
    Full flat script_args array for the img2img runner, with ADetailer's slice
    replaced by [ad_enable, ad_skip_img2img, unit0, unit1, ...].

    Every unit slot must be overwritten: ADetailer's own UI defaults put gr.State
    *lambdas* in those positions, and while ADetailer ignores non-dict args, a
    leftover lambda would silently mean "one fewer unit than the user configured".
    Unused slots get an inert {"ad_model": "None"} (ADetailer's need_skip()).

    Returns (script_args, warning_or_None), or (None, error) if ADetailer is absent.
    """
    ad_script = _find_adetailer_script()
    if ad_script is None:
        return None, (
            "ADetailer not found on the img2img tab. Install/enable the ADetailer "
            "extension (aadetailer-neoforge) and reload the UI."
        )

    script_args = _get_default_script_args().copy()

    slice_len = ad_script.args_to - ad_script.args_from
    num_slots = slice_len - 2  # minus the two leading bools
    if num_slots < 1:
        return None, (
            f"Unexpected ADetailer args layout (slice length {slice_len}). "
            "Is the installed ADetailer version compatible?"
        )

    warning = None
    active = list(unit_dicts)
    if len(active) > num_slots:
        warning = (
            f"Only {num_slots} ADetailer unit slot(s) available — units beyond "
            f"#{num_slots} were ignored. Raise 'Max models' in Settings → ADetailer "
            f"and restart to use all {len(active)}."
        )
        active = active[:num_slots]

    ad_slice = [True, True]  # ad_enable, ad_skip_img2img
    ad_slice += active
    ad_slice += [{"ad_model": "None"}] * (num_slots - len(active))

    script_args[ad_script.args_from : ad_script.args_to] = ad_slice
    return script_args, warning

# ──────────────────────────────────────────────
# LoRA name repair
#
# A prompt inherited from an old image can name a LoRA that no longer exists
# under that name — typically a training epoch (`mylora-000021`) that was later
# renamed to `mylora`. Forge resolves <lora:NAME:w> against the filename stems in
# `networks.available_networks` and the aliases in `available_network_aliases`
# (an alias is the file's own `ss_output_name` metadata, which for a renamed
# epoch is neither the old *nor* the new filename). When NAME matches neither,
# load_networks logs `Failed to load LoRA` and the pass simply renders without
# it — the missing-LoRA look, with no error on the UI side.
# ──────────────────────────────────────────────
_LORA_TOKEN_RE = re.compile(r"<lora:([^:>]+)((?::[^>]*)?)>", re.IGNORECASE)
_EPOCH_SUFFIX_RE = re.compile(r"-\d{4,6}$")


def _lora_networks():
    """Forge's lora module (extensions-builtin/sd_forge_lora), or None."""
    nets = sys.modules.get("networks")
    return nets if hasattr(nets, "available_networks") else None


def _resolve_lora_name(name: str, nets):
    """
    None  -> the name already resolves, leave it alone
    ""    -> no such LoRA anywhere, nothing we can do
    str   -> the filename stem it should be pointed at instead
    """
    avail = getattr(nets, "available_networks", None) or {}
    aliases = getattr(nets, "available_network_aliases", None) or {}

    if name in avail or name in aliases:
        return None

    by_stem = {k.lower(): v for k, v in avail.items()}
    by_alias = {k.lower(): v for k, v in aliases.items()}
    key = name.lower()

    entry = by_stem.get(key) or by_alias.get(key)  # differs only in case
    if entry is None:
        # Epoch checkpoints: mylora-000021 -> mylora
        base = _EPOCH_SUFFIX_RE.sub("", key)
        entry = by_stem.get(base) or by_alias.get(base)
        if entry is None:
            # ...or the file kept an epoch number of its own: mylora-000023
            for stem, candidate in by_stem.items():
                if _EPOCH_SUFFIX_RE.sub("", stem) == base:
                    entry = candidate
                    break

    if entry is None:
        return ""
    return str(getattr(entry, "name", "") or "")


def _fix_lora_names(text: str):
    """Re-point unresolvable <lora:...> names at the real file. -> (text, notes)"""
    if not text or not getattr(shared.opts, "batch_adetailer_fix_lora_names", True):
        return text, []

    nets = _lora_networks()
    if nets is None:
        return text, []

    notes: list[str] = []

    def replace(match):
        name = match.group(1)
        resolved = _resolve_lora_name(name, nets)

        if resolved is None:
            return match.group(0)
        if not resolved:
            notes.append(f"LoRA `{name}` isn't in your Lora folder — this pass runs without it.")
            return match.group(0)

        notes.append(f"LoRA `{name}` → `{resolved}`.")
        return f"<lora:{resolved}{match.group(2)}>"

    return _LORA_TOKEN_RE.sub(replace, text), notes

# ──────────────────────────────────────────────
# Infotext extraction
# ──────────────────────────────────────────────
def _apply_source_image_parameters(p, geninfo: str):
    """
    Apply the source image's generation parameters (prompt, seed, sampler, ...)
    to the processing object. ADetailer's inpaint pass inherits these: an empty
    ad_prompt falls back to p.prompt, and with skip-img2img the steps/sampler it
    uses come from p (captured into p._ad_orig before the base pass is neutered).
    """
    params = parse_generation_parameters(geninfo, [])

    p.prompt = params.get("Prompt", "")
    p.negative_prompt = params.get("Negative prompt", "")
    # parse_generation_parameters *subtracts* any matching saved style's text from
    # the prompt it returns and hands back the style names instead (that's how the
    # paste button repopulates the styles dropdown). Leaving p.styles empty would
    # therefore silently drop whatever lives in those styles — LoRA tags included.
    # process_images folds them back into all_prompts via apply_styles_to_prompt.
    styles = params.get("Styles array") or []
    p.styles = list(styles) if isinstance(styles, (list, tuple)) else []
    p.seed = params.get("Seed", -1)
    p.subseed = params.get("Variation seed", -1)

    try:
        p.steps = int(params["Steps"])
    except (KeyError, ValueError):
        pass
    try:
        p.cfg_scale = float(params["CFG scale"])
    except (KeyError, ValueError):
        pass
    try:
        p.distilled_cfg_scale = float(params["Distilled CFG Scale"])
    except (KeyError, ValueError):
        pass

    if params.get("Sampler"):
        p.sampler_name = params["Sampler"]
    if params.get("Schedule type"):
        p.scheduler = params["Schedule type"]

# ──────────────────────────────────────────────
# Saving with the original filename + suffix
# ──────────────────────────────────────────────
def _fix_infotext(infotext: str | None, width: int, height: int, steps: int | None):
    """
    Undo the cosmetic damage skip-img2img does to the saved infotext: the base
    pass is neutered to 1 step at 128x128 *before* create_infotext runs, so the
    result would advertise "Steps: 1, Size: 128x128" for a full-res image.
    """
    if not infotext:
        return infotext

    infotext = re.sub(r"(?<=\bSize: )128x128\b", f"{width}x{height}", infotext)
    if steps:
        infotext = re.sub(r"(?<=\bSteps: )1(?=,|$)", str(steps), infotext)
    return infotext


def _save_with_original_name(processed, p, stem: str, suffix: str, orig_size, orig_steps):
    """
    Save result images as <original filename><suffix>.<ext> directly in the
    output directory (no dated subfolders, no [seed]-[prompt] naming pattern).
    Collisions get a -1, -2, ... counter instead of overwriting.
    """
    outdir = p.outpath_samples
    os.makedirs(outdir, exist_ok=True)
    extension = shared.opts.samples_format

    for i, image in enumerate(processed.images):
        base = f"{stem}{suffix}" if i == 0 else f"{stem}{suffix}-{i}"
        name = base
        n = 1
        while os.path.exists(os.path.join(outdir, f"{name}.{extension}")):
            name = f"{base}-{n}"
            n += 1

        infotext = processed.infotexts[i] if i < len(processed.infotexts) else None
        infotext = _fix_infotext(infotext, orig_size[0], orig_size[1], orig_steps)

        images.save_image(
            image, outdir, "",
            info=infotext,
            forced_filename=name,
            extension=extension,
            save_to_dirs=False,
            p=p,
        )

# ──────────────────────────────────────────────
# Cancelling a running batch
#
# shared.state.interrupted can't carry the request on its own: state.begin() at
# the top of every image resets it, so a cancel that lands between two images
# would be wiped. This flag survives that and is only cleared when a batch starts.
# ──────────────────────────────────────────────
_cancel_requested = False


def _request_cancel():
    """Cancel button: abort the image being sampled, then stop the batch."""
    global _cancel_requested
    _cancel_requested = True
    shared.state.interrupt()
    return "⏹️ Cancel requested — finishing the current image, then stopping."

# ──────────────────────────────────────────────
# Core Processing Logic
# ──────────────────────────────────────────────
def _process_single_image(img: Image.Image, geninfo: str | None, unit_dicts: list, save_opts: dict):
    """
    Run one image through ADetailer.

    Runs on Forge's main thread (see batch_adetailer_process). Never raises:
    returns (images, infotexts, error_traceback_or_None, notes) so the full
    traceback reaches the status log instead of being swallowed by Gradio.
    """
    notes: list[str] = []
    try:
        unit_dicts = [dict(u) for u in unit_dicts]
        for unit in unit_dicts:
            for key in ("ad_prompt", "ad_negative_prompt"):
                # "[base prompt]" -> ADetailer's own [PROMPT] placeholder, which it
                # substitutes with the image's prompt (p.all_prompts) at inpaint time.
                text = _BASE_PROMPT_RE.sub("[PROMPT]", unit.get(key, "") or "")
                unit[key], found = _fix_lora_names(text)
                notes += found

        script_args, _warning = _assemble_script_args(unit_dicts)
        if script_args is None:
            return [], [], _warning, notes

        p = processing.StableDiffusionProcessingImg2Img(
            outpath_samples=(
                getattr(shared.opts, "batch_adetailer_output_dir", None)
                or shared.opts.outdir_samples
                or shared.opts.outdir_img2img_samples
            ),
            outpath_grids=shared.opts.outdir_grids or shared.opts.outdir_img2img_grids,
            prompt="",
            negative_prompt="",
            styles=[],
            batch_size=1,
            n_iter=1,
            cfg_scale=7.0,
            init_images=[img],
            width=img.size[0],
            height=img.size[1],
            resize_mode=0,
            # The base img2img pass is neutered by ADetailer's skip-img2img
            # (1 step / 128x128), so this value never actually shapes the output —
            # but it must still be a legal denoising strength.
            denoising_strength=0.4,
            # mask must stay None: ADetailer disables skip-img2img on inpaint
            # processing objects (it calls that combination buggy).
            mask=None,
            mask_blur=4,
            inpainting_fill=1,
            inpaint_full_res=False,
            inpaint_full_res_padding=32,
            inpainting_mask_invert=0,
            override_settings={},
        )

        p.scripts = scripts.scripts_img2img
        p.script_args = script_args

        if geninfo:
            _apply_source_image_parameters(p, geninfo)

        # A slot with a blank ADetailer prompt inpaints with *this* prompt (ADetailer
        # falls back to p.all_prompts), so a LoRA that can't be resolved here is a
        # LoRA missing from the inpaint.
        p.prompt, found = _fix_lora_names(p.prompt)
        notes += found
        p.negative_prompt, found = _fix_lora_names(p.negative_prompt)
        notes += found

        print(f"[Batch ADetailer] base prompt: {p.prompt!r}")
        if p.styles:
            print(f"[Batch ADetailer] styles: {p.styles}")
        for note in dict.fromkeys(notes):
            print(f"[Batch ADetailer] {note}")

        # Captured before process_images(), because ADetailer's process() hook
        # overwrites p.steps/width/height with its 1-step/128x128 stand-ins.
        orig_size = (p.width, p.height)
        orig_steps = p.steps

        if save_opts.get("use_original_name"):
            # We save manually afterwards with the original filename + suffix.
            p.do_not_save_samples = True

        with closing(p):
            processed = scripts.scripts_img2img.run(p, *p.script_args)

            if processed is None:
                processed = processing.process_images(p)

        if save_opts.get("use_original_name"):
            _save_with_original_name(
                processed, p, save_opts["stem"], save_opts.get("suffix", ""),
                orig_size, orig_steps,
            )

        return processed.images, processed.infotexts, None, notes
    except Exception:
        tb = traceback.format_exc()
        print(f"[Batch ADetailer] Error processing image:\n{tb}")
        return [], [], tb, notes


def batch_adetailer_process(store, paths, sel, use_original_name, filename_suffix, *control_values):
    """
    Main batch processing function. Each image is processed with its own config
    from the store, sequentially.

    control_values are the live values of the currently-visible slot controls. We
    fold them back into the store first, so an edit that hasn't landed as a
    .change event yet still counts — the same defence ADetailer uses in its own
    on_generate_click.
    """
    global _cancel_requested
    _cancel_requested = False

    num_slots = _get_num_slots()

    if not paths:
        yield "No images to process. Please drag and drop some images first."
        return

    if _find_adetailer_script() is None:
        yield (
            "❌ ADetailer not found on the img2img tab.\n\n"
            "This extension drives the ADetailer extension (aadetailer-neoforge) — "
            "install/enable it and reload the UI."
        )
        return

    max_images = shared.opts.batch_adetailer_max_images
    skip_errors = shared.opts.batch_adetailer_skip_errors

    if len(paths) > max_images:
        yield f"Too many images ({len(paths)}). Max is {max_images}."
        return

    # Snapshot the store: the running generator holds the gr.State by reference,
    # and a stray .change event could otherwise mutate it mid-run.
    store = _copy.deepcopy(dict(store or {}))
    if sel is not None and 0 <= int(sel) < len(paths):
        store[paths[int(sel)]] = list(control_values)

    defaults = _get_adetailer_defaults()
    if not defaults:
        yield (
            "❌ Could not read your ADetailer unit defaults from the img2img panel.\n\n"
            "Open the img2img tab once, then come back and try again."
        )
        return

    total = len(paths)
    all_results: list = []
    status_messages: list[str] = []
    failed_count = 0

    for idx, image_path in enumerate(paths):
        name = os.path.basename(image_path)

        if _cancel_requested:
            status_messages.append(f"⏹️ Cancelled — {idx} of {total} images processed.")
            break

        config = store.get(image_path)
        if not config:
            config = _default_config(defaults, num_slots)

        unit_dicts = _config_to_unit_dicts(config, defaults, num_slots)
        if not unit_dicts:
            status_messages.append(
                f"⚠️ [{idx + 1}/{total}] {name}: no enabled unit slots — skipped."
            )
            failed_count += 1
            continue

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
                f"slots with an empty ADetailer prompt will inpaint with no prompt."
            )

        shared.total_tqdm.clear()

        save_opts = {
            "use_original_name": bool(use_original_name),
            "stem": os.path.splitext(name)[0],
            "suffix": filename_suffix or "",
        }

        # state.begin() resets state.interrupted / stopping_generation, which
        # otherwise stay True forever after a UI reload (request_restart calls
        # interrupt()) and make process_images_inner return 0 images silently.
        # Real generations get this from the UI's wrap_gradio_gpu_call wrapper.
        shared.state.begin(job="batch_adetailer")
        try:
            # GPU work must run on Forge's main thread, same as img2img()
            # (img2img.py routes through main_thread.run_and_wait_result).
            result_images, _infotexts, error_tb, notes = main_thread.run_and_wait_result(
                _process_single_image, img, geninfo, unit_dicts, save_opts
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
        if _cancel_requested or shared.state.interrupted or shared.state.stopping_generation:
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
        models = ", ".join(str(u.get("ad_model")) for u in unit_dicts)
        status_messages.append(f"✅ [{idx + 1}/{total}] Done: {name}  ({models})")

        # Stream progress into the log as each image finishes.
        yield f"Processing... {idx + 1}/{total} done.\n\n" + "\n".join(status_messages)

    yield (
        f"Batch complete — {len(all_results)} succeeded, "
        f"{failed_count} failed/skipped out of {total}.\n\n"
        + "\n".join(status_messages)
    )

# ──────────────────────────────────────────────
# UI event handlers
# ──────────────────────────────────────────────
def _editing_label(paths, sel):
    if not paths:
        return "### No images loaded\nDrop images above to configure them."
    if sel is None or not (0 <= int(sel) < len(paths)):
        return "### Click a thumbnail to edit that image's units"
    return (
        f"### Editing: `{os.path.basename(paths[int(sel)])}`"
        f"  ({int(sel) + 1}/{len(paths)})"
    )


def _control_updates(config, num_slots, choices=None):
    """gr.updates for every slot control, from one image's config."""
    updates = []
    for i in range(num_slots):
        chunk = config[i * CONTROLS_PER_SLOT : (i + 1) * CONTROLS_PER_SLOT]
        if len(chunk) < CONTROLS_PER_SLOT:
            chunk = _blank_slot_values()
        if choices is not None:
            updates.append(gr.update(choices=choices, value=chunk[0]))
        else:
            updates.append(gr.update(value=chunk[0]))
        updates += [gr.update(value=v) for v in chunk[1:]]
    return updates


def _on_files(files, store, num_slots):
    """
    Files dropped: seed a config for each new image from the user's saved
    ADetailer defaults, keep configs for images that were already loaded, and
    show the first image's config.
    """
    paths = [f if isinstance(f, str) else getattr(f, "name", None) for f in (files or [])]
    paths = [p for p in paths if p]

    store = dict(store or {})
    defaults = _get_adetailer_defaults()
    choices = _preset_choices(defaults)

    if not paths:
        blank = _default_config(defaults, num_slots)
        return [
            gr.update(value=None),        # source gallery
            [],                           # paths_state
            {},                           # store_state
            None,                         # sel_state
            _editing_label([], None),     # editing markdown
            *_control_updates(blank, num_slots, choices),
        ]

    store = {
        path: store.get(path) or _default_config(defaults, num_slots)
        for path in paths
    }

    return [
        gr.update(value=paths, selected_index=0),
        paths,
        store,
        0,
        _editing_label(paths, 0),
        *_control_updates(store[paths[0]], num_slots, choices),
    ]


def _on_select_image(store, paths, num_slots, evt: gr.SelectData):
    """
    Thumbnail clicked: load that image's config. sel_state is returned in the
    SAME outputs batch as the controls — if it were updated in a later event, the
    change-storm from setting the controls would write the newly loaded values
    back into the previously selected image's slot and corrupt it.
    """
    idx = int(evt.index)
    paths = list(paths or [])
    if not (0 <= idx < len(paths)):
        return [gr.update()] * (2 + num_slots * CONTROLS_PER_SLOT)

    config = (store or {}).get(paths[idx]) or _default_config(
        _get_adetailer_defaults(), num_slots
    )

    return [idx, _editing_label(paths, idx), *_control_updates(config, num_slots)]


def _on_right_click(store, paths, index, num_slots):
    """
    Thumbnail right-clicked (via javascript/batch_adetailer.js, which puts the
    index in a hidden textbox and clicks a hidden button): select that image and
    put "[base prompt]" in Slot 1's ADetailer prompt. Left alone it behaves like
    an empty box (the image's own prompt); anything typed around it is appended to
    that prompt. Everything else about the slot is left as it is.
    """
    paths = list(paths or [])
    try:
        idx = int(index)
    except (TypeError, ValueError):
        idx = -1

    blank = [gr.update()] * (2 + num_slots * CONTROLS_PER_SLOT)
    if not (0 <= idx < len(paths)):
        return [*blank, store]

    store = dict(store or {})
    config = list(
        store.get(paths[idx]) or _default_config(_get_adetailer_defaults(), num_slots)
    )
    config[1] = BASE_PROMPT_TOKEN  # slot 1's prompt — position 0 is its preset dropdown
    store[paths[idx]] = config

    return [idx, _editing_label(paths, idx), *_control_updates(config, num_slots), store]


def _on_control_change(store, paths, sel, *control_values):
    """Any slot control edited: persist it into the selected image's config."""
    if sel is None or not paths or not (0 <= int(sel) < len(paths)):
        return store
    store = dict(store or {})
    store[paths[int(sel)]] = list(control_values)
    return store


def _on_preset_change(store, paths, sel, preset, slot, num_slots):
    """
    Slot's unit preset changed: repopulate that slot's overrides from the chosen
    unit's saved defaults, and persist. Bound separately from the generic control
    handler so the two never race on the same trigger.
    """
    defaults = _get_adetailer_defaults()

    preset_idx = int(preset) if preset is not None else PRESET_DISABLED
    if 0 <= preset_idx < len(defaults):
        values = _slot_values_from_unit(preset_idx, defaults[preset_idx])
    else:
        values = _blank_slot_values()
        values[0] = preset_idx

    store = dict(store or {})
    if sel is not None and paths and 0 <= int(sel) < len(paths):
        path = paths[int(sel)]
        config = list(store.get(path) or _default_config(defaults, num_slots))
        config[slot * CONTROLS_PER_SLOT : (slot + 1) * CONTROLS_PER_SLOT] = values
        store[path] = config

    return [store, *[gr.update(value=v) for v in values[1:]]]


def _on_apply_to_all(store, paths, sel, *control_values):
    """
    Copy the currently-shown config onto every loaded image — except the prompts.
    Those are the per-image part (that's the whole point of the tab), so each image
    keeps its own; everything else (unit choice, confidence, denoise, max ratio)
    is overwritten.
    """
    if not paths:
        return store, "No images loaded."

    source = list(control_values)
    num_slots = len(source) // CONTROLS_PER_SLOT
    store = dict(store or {})
    sel_path = paths[int(sel)] if sel is not None and 0 <= int(sel) < len(paths) else None

    for path in paths:
        config = list(source)
        # The selected image's prompts are the live control values themselves, so
        # only the *other* images have prompts of their own to preserve.
        if path != sel_path:
            existing = list(store.get(path) or source)
            for i in range(num_slots):
                prompt_at = i * CONTROLS_PER_SLOT + 1  # [preset, prompt, negative, ...]
                if prompt_at + 1 < len(existing):
                    config[prompt_at] = existing[prompt_at]
                    config[prompt_at + 1] = existing[prompt_at + 1]
        store[path] = config

    src = os.path.basename(paths[int(sel)]) if sel is not None and 0 <= int(sel) < len(paths) else "current"
    return store, (
        f"Applied `{src}` settings to all {len(paths)} images "
        f"(each image kept its own prompts)."
    )

# ──────────────────────────────────────────────
# Gradio UI Tab
# ──────────────────────────────────────────────
def _slot_controls(slot_index, num_slots):
    """
    One execution slot's controls. Order must match the config layout:
    [preset, prompt, negative, confidence, denoise, max_ratio].
    """
    # Real labels (with each unit's model) are filled in on file drop, once
    # ADetailer's saved defaults are readable.
    choices = [("— slot disabled —", PRESET_DISABLED)] + [
        (f"Unit {i + 1}", i) for i in range(num_slots)
    ]

    preset = gr.Dropdown(
        choices=choices,
        value=slot_index if slot_index < num_slots else PRESET_DISABLED,
        label="ADetailer unit (brings its model + your saved settings)",
    )

    # lines = rows shown at rest; the box grows with the text up to max_lines.
    prompt = gr.Textbox(
        label="ADetailer prompt",
        placeholder=(
            "Empty = reuse this image's own prompt from its metadata. "
            "Or write [base prompt] to build on it: '[base prompt], detailed eyes'"
        ),
        lines=5,
        max_lines=20,
    )
    negative_prompt = gr.Textbox(
        label="ADetailer negative prompt",
        placeholder="Empty = reuse this image's own negative prompt ([base prompt] works here too)",
        lines=5,
        max_lines=20,
    )

    with gr.Row():
        confidence = gr.Slider(
            minimum=0.0, maximum=1.0, step=0.01,
            value=0.3, label="Detection confidence",
        )
        denoising_strength = gr.Slider(
            minimum=0.0, maximum=1.0, step=0.01,
            value=0.4, label="Inpaint denoising strength",
        )

    mask_max_ratio = gr.Slider(
        minimum=0.0, maximum=1.0, step=0.001,
        value=1.0, label="Mask max area ratio (ignore detections bigger than this)",
    )

    return [preset, prompt, negative_prompt, confidence, denoising_strength, mask_max_ratio]


def _build_ui_tab():
    num_slots = _get_num_slots()

    with gr.Blocks(analytics_enabled=False) as block:
        gr.Markdown(
            "# Batch ADetailer\n"
            "Drop images, click a thumbnail to configure **that image's** units, then run the batch. "
            "Each slot pulls its model and settings from one of your ADetailer units "
            "(as saved in the img2img panel) — you only override what varies per image. "
            "Slot order is the order the units run in.\n\n"
            "*Right-click a thumbnail to drop `[base prompt]` into Slot 1's prompt — it stands "
            "for that image's own prompt, so leaving it alone inherits, and anything you add "
            "around it is appended.*"
        )

        gr.HTML(
            """
            <style>
            /* Drag the gallery's bottom-right corner to see more than one row of
               thumbnails. Gradio has no resizable gallery, but the block is just a
               div — `resize` + `overflow` is all it takes. */
            #batch_adetailer_source {
                height: 340px;
                min-height: 140px;
                resize: vertical;
                overflow: auto;
            }
            /* The drop zone's file list grows with every image dropped. */
            #batch_adetailer_files { max-height: 220px; overflow-y: auto; }
            </style>
            """
        )

        paths_state = gr.State([])
        store_state = gr.State({})
        sel_state = gr.State(None)

        # Driven from javascript/batch_adetailer.js on right-click: gradio has no
        # contextmenu event, so the JS writes the thumbnail index here and clicks
        # the button.
        rclick_index = gr.Textbox(visible=False, elem_id="batch_adetailer_rclick")
        rclick_btn = gr.Button(visible=False, elem_id="batch_adetailer_rclick_btn")

        # The drop zone spans the full width at the top: parked in the left column it
        # grows with the file list and pushes the unit controls off the screen.
        file_input = gr.File(
            label="Drop images here (or click to browse)",
            elem_id="batch_adetailer_files",
            file_count="multiple",
            file_types=["image"],
            type="filepath",
        )

        with gr.Row():
            # ── Left column: per-image unit editor ──
            with gr.Column(scale=1):
                editing_md = gr.Markdown(_editing_label([], None))

                slot_controls: list = []
                with gr.Tabs():
                    for i in range(num_slots):
                        with gr.Tab(f"Slot {i + 1}"):
                            slot_controls.append(_slot_controls(i, num_slots))

                controls = [c for slot in slot_controls for c in slot]
                presets = [slot[0] for slot in slot_controls]
                overrides = [c for slot in slot_controls for c in slot[1:]]

                apply_all_btn = gr.Button(
                    "📋 Apply these settings to all images (keeps each image's prompts)"
                )

                with gr.Row():
                    use_original_name = gr.Checkbox(
                        value=True,
                        label="Save as original filename + suffix",
                        scale=2,
                    )
                    filename_suffix = gr.Textbox(
                        value="-adetailer",
                        label="Filename suffix",
                        max_lines=1,
                        scale=1,
                    )

                with gr.Row():
                    process_btn = gr.Button(
                        "🚀 Run Batch ADetailer", variant="primary", size="lg", scale=3
                    )
                    cancel_btn = gr.Button("⏹️ Cancel", variant="stop", size="lg", scale=1)

            # ── Right column: the images ──
            with gr.Column(scale=2):
                # No `height`: the CSS below gives the block a starting height and a
                # drag handle, and the thumbnails scroll inside it. A gradio `height`
                # would pin the inner grid and fight the resize.
                #
                # allow_preview=False keeps a click on a thumbnail a *selection*
                # instead of popping open the full-size viewer.
                source_gallery = gr.Gallery(
                    label="Images — click to edit its units, right-click for Slot 1's [base prompt]",
                    # Deliberately NOT suffixed "_gallery": that suffix is what makes
                    # Forge's imageviewer.js attach its lightbox, which we don't want
                    # on the source thumbnails.
                    elem_id="batch_adetailer_source",
                    columns=[4],
                    allow_preview=False,
                    show_download_button=False,
                    interactive=False,
                )

                status_text = gr.TextArea(
                    label="Status / Log",
                    lines=10,
                    interactive=False,
                )

        # ── wiring ──
        # These are closures rather than functools.partial: binding a keyword arg
        # with partial turns the trailing `evt: gr.SelectData` parameter into a
        # keyword-only one, and gradio only scans *positional* params when it
        # decides where to inject the event data — so the click event would never
        # be passed. Closures keep the signatures clean.
        def on_files(files, store):
            return _on_files(files, store, num_slots)

        def on_select_image(store, paths, evt: gr.SelectData):
            return _on_select_image(store, paths, num_slots, evt)

        def on_right_click(store, paths, index):
            return _on_right_click(store, paths, index, num_slots)

        file_input.change(
            fn=on_files,
            inputs=[file_input, store_state],
            outputs=[source_gallery, paths_state, store_state, sel_state, editing_md, *controls],
            queue=False,
        )

        source_gallery.select(
            fn=on_select_image,
            inputs=[store_state, paths_state],
            outputs=[sel_state, editing_md, *controls],
            queue=False,
        )

        rclick_btn.click(
            fn=on_right_click,
            inputs=[store_state, paths_state, rclick_index],
            outputs=[sel_state, editing_md, *controls, store_state],
            queue=False,
            show_progress="hidden",
        )

        # One dependency for every override control, rather than N separate ones.
        # The preset dropdowns are deliberately NOT in here — they have their own
        # handler below, which also rewrites the overrides.
        gr.on(
            triggers=[c.change for c in overrides],
            fn=_on_control_change,
            inputs=[store_state, paths_state, sel_state, *controls],
            outputs=store_state,
            queue=False,
        )

        for i, preset in enumerate(presets):
            preset.change(
                fn=partial(_on_preset_change, slot=i, num_slots=num_slots),
                inputs=[store_state, paths_state, sel_state, preset],
                outputs=[store_state, *slot_controls[i][1:]],
                queue=False,
                show_progress="hidden",
            )

        apply_all_btn.click(
            fn=_on_apply_to_all,
            inputs=[store_state, paths_state, sel_state, *controls],
            outputs=[store_state, status_text],
            queue=False,
        )

        # queue=False so the click is served straight away instead of queueing
        # behind the running batch — otherwise the cancel could never arrive.
        cancel_btn.click(
            fn=_request_cancel,
            inputs=[],
            outputs=[status_text],
            queue=False,
        )

        process_btn.click(
            fn=batch_adetailer_process,
            inputs=[
                store_state, paths_state, sel_state,
                use_original_name, filename_suffix,
                *controls,
            ],
            outputs=[status_text],
        )

    return block


def _on_ui_tabs():
    """Register the Batch ADetailer tab with Forge Neo's UI."""
    yield (_build_ui_tab(), "Batch ADetailer", "batch-adetailer-tab")

# ──────────────────────────────────────────────
# Registration
# Runs unconditionally: Forge Neo loads each extension script once per launch.
# Do NOT guard on shared.opts attribute existence — saved values in config.json
# make the attribute exist before registration, which would skip tab
# registration entirely.
# ──────────────────────────────────────────────
_register_settings()
script_callbacks.on_ui_tabs(_on_ui_tabs)
