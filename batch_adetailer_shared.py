"""
Shared core for the Batch ADetailer / Batch Hires-Fix tabs.

Both tabs work the same refine pipeline over commission/request sets:
    NrM.png -> NrM-adetailer.png -> NrM-adetailer-base.png / NrM-adetailer-hires.png
and share everything that isn't stage-specific: set scanning, revision picking,
dragged-file resolution, default script args, LoRA repair, infotext
inheritance, original-name saving, and batch cancelling. The stage differences
(what counts as an input, what marks it done, which settings prefix) live in
the two Stage records below.

Lives at the extension root: modules/scripts.py adds every extension basedir
to sys.path, so `import batch_adetailer_shared` works from scripts/*.py.
Unlike the scripts (re-executed by load_scripts on every in-process Reload
UI), this module stays cached in sys.modules — the scripts importlib.reload()
it so code edits still land, and the script-args cache below is keyed on the
runner's identity for the same reason.
"""
import filecmp
import os
import re
import sys
import tempfile
import weakref

import gradio as gr

from modules import images, shared
from modules.infotext_utils import parse_generation_parameters

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".jxl", ".avif", ".heif")
_VARIANT_TOKENS = ("-adetailer", "-hires", "-edited", "-base")


class Stage:
    """One pipeline stage's knobs — everything else in this module is shared."""

    def __init__(self, name, opt_prefix, revision_re, is_input, done_suffixes,
                 pending_label, empty_label):
        self.name = name                    # settings-section name, used in messages
        self.opt_prefix = opt_prefix        # settings keys: <prefix>_scan_roots, ...
        self.revision_re = revision_re      # `<n>r<rev>` naming for this stage's inputs
        self.is_input = is_input            # stem -> is one of this stage's inputs
        self.done_suffixes = done_suffixes  # any <stem><suffix> sibling = done
        self.pending_label = pending_label  # folder panel label while work is pending
        self.empty_label = empty_label      # ... when nothing is; .format(roots=...)


ADETAILER_STAGE = Stage(
    name="Batch ADetailer",
    opt_prefix="batch_adetailer",
    # `<image>r<revision>` naming: 1r1, 1r2, 10r13. Digits-only prefix on purpose —
    # a looser match would swallow names like flower2.png.
    revision_re=re.compile(r"^(\d+)r(\d+)$", re.IGNORECASE),
    # Pipeline outputs (-adetailer, -hires, -edited, -base) and their collision
    # copies are not bases.
    is_input=lambda stem: not any(tok in stem.lower() for tok in _VARIANT_TOKENS),
    # A base with a plain -hires sibling went through the old hires-first chain
    # and is left alone.
    done_suffixes=("-adetailer", "-hires"),
    pending_label="Sets with base images that have no -adetailer version yet",
    empty_label=(
        "Nothing pending — every set under {roots} already has -adetailer "
        "results. Load a folder by path below, or widen the roots in "
        "Settings → Batch ADetailer. (A set is a folder with a Tests "
        "subfolder, or any folder inside a Requests folder.)"
    ),
)

HIRES_STAGE = Stage(
    name="Batch Hires-Fix",
    opt_prefix="batch_hires_fix",
    # Same naming with this stage's input suffix: 1r1-adetailer, 10r13-adetailer.
    revision_re=re.compile(r"^(\d+)r(\d+)-adetailer$", re.IGNORECASE),
    # The endswith test also excludes this stage's own outputs (-adetailer-base,
    # -adetailer-hires) and collision copies (-adetailer-1).
    is_input=lambda stem: stem.endswith("-adetailer"),
    # One that's been hand-edited past this stage counts as done too.
    done_suffixes=("-hires", "-edited"),
    pending_label=("Sets with -adetailer images that have no -hires version yet — "
                   "results save back into the set's folder"),
    empty_label=(
        "Nothing pending — every -adetailer image under {roots} already has "
        "a -hires version. Load a folder by path below, or widen the roots "
        "in Settings → Batch Hires-Fix. (A set is a folder with a Tests "
        "subfolder, or any folder inside a Requests folder.)"
    ),
)

# ──────────────────────────────────────────────
# Test-folder scanning
#
# Commission/request sets keep work-in-progress test images in a Tests/
# subfolder. A stage's input is "pending" while it has no done-marker sibling.
# Compositional edits are new revisions (1r2), never suffixes.
# ──────────────────────────────────────────────
def natural_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def display_name(path):
    """`<set>/file.png` for status lines. A Tests folder is collapsed away —
    every set has one, so it carries no information — but an image kept in the
    set folder itself still names its set rather than the folder above it."""
    parent = os.path.dirname(path)
    if os.path.basename(parent).lower() == "tests":
        parent = os.path.dirname(parent)
    return f"{os.path.basename(parent)}/{os.path.basename(path)}"


def is_dragged_temp_copy(path):
    """True if `path` lives in gradio's upload cache (a drag-dropped file's temp
    copy) rather than a real on-disk source. Save-to-source must never write next
    to one of these — the folder is temporary and gets wiped."""
    root = os.environ.get("GRADIO_TEMP_DIR") or os.path.join(tempfile.gettempdir(), "gradio")
    try:
        return os.path.realpath(path).startswith(os.path.realpath(root) + os.sep)
    except Exception:
        return False


def image_stems(folder):
    """{stem: filename} for every image in `folder`, in natural order."""
    try:
        files = sorted(os.listdir(folder), key=natural_key)
    except OSError:
        return {}
    return {os.path.splitext(f)[0]: f
            for f in files if os.path.splitext(f)[1].lower() in _IMAGE_EXTS}


def latest_revisions(paths, revision_re):
    """Only the highest `<n>r<rev>` revision of each image number; names outside
    that convention pass through untouched."""
    best: dict = {}
    for p in paths:
        m = revision_re.match(os.path.splitext(os.path.basename(p))[0])
        if m:
            key = int(m.group(1))
            best[key] = max(best.get(key, 0), int(m.group(2)))
    keep = []
    for p in paths:
        m = revision_re.match(os.path.splitext(os.path.basename(p))[0])
        if not m or int(m.group(2)) == best[int(m.group(1))]:
            keep.append(p)
    return keep


def stage_inputs(folder, stage):
    """Full paths of this stage's input images in `folder`; only the latest
    revision of each image number counts."""
    return latest_revisions(
        [os.path.join(folder, f) for stem, f in image_stems(folder).items()
         if stage.is_input(stem)],
        stage.revision_re)


def pending_inputs(folder, stage):
    """This stage's inputs in `folder` that have no done-marker sibling yet, in
    natural order (2r1 before 10r1)."""
    stems = image_stems(folder)
    return [p for p in stage_inputs(folder, stage)
            if not any(f"{os.path.splitext(os.path.basename(p))[0]}{tok}" in stems
                       for tok in stage.done_suffixes)]


def scan_roots(stage):
    """Existing directories from the stage's scan-roots setting."""
    roots = getattr(shared.opts, f"{stage.opt_prefix}_scan_roots", "") or ""
    return [r for r in (r.strip() for r in roots.split(";")) if r and os.path.isdir(r)]


def walk_roots(stage, max_depth=3):
    """(dirpath, dirnames, filenames) under every scan root, no deeper than
    `max_depth` levels — sets live at more than one depth (<root>/<set>/Tests and
    <root>/Commissions/<set>/Tests both exist), so one fixed level isn't enough."""
    for root in scan_roots(stage):
        for dirpath, dirnames, filenames in os.walk(root):
            if dirpath[len(root):].count(os.sep) >= max_depth:
                dirnames[:] = []
            yield dirpath, dirnames, filenames


def set_folders(stage):
    """
    [(label, set_folder)] for every set under the scan roots.

    A *set* is a folder that has a Tests subfolder — that marker is what the
    workflow already maintains, and it's the only reliable way to tell a real
    set from the archives and scratch dirs sitting beside it (Reference,
    Tests/Finished, loose scratch folders). Request sets are the exception:
    they keep their images directly in the folder with no Tests subfolder, so
    there the *group* is the marker — anything inside a "Requests" folder is a
    set.
    """
    return [(os.path.basename(dirpath), dirpath)
            for dirpath, dirnames, _files in walk_roots(stage)
            if any(d.lower() == "tests" for d in dirnames)
            or os.path.basename(os.path.dirname(dirpath)).lower() == "requests"]


def set_scan_dirs(set_folder):
    """The folders one set entry covers: the set folder itself plus its Tests
    subfolder — images live in both, and one checkbox loads both."""
    try:
        subs = sorted(os.path.join(set_folder, d) for d in os.listdir(set_folder)
                      if d.lower() == "tests"
                      and os.path.isdir(os.path.join(set_folder, d)))
    except OSError:
        subs = []
    return [set_folder, *subs]


def scan_test_folders(stage):
    """[(label, set_folder)] for every set that still has pending inputs (in its
    own folder or its Tests folder) — the panel's to-do list."""
    choices = []
    for label, folder in set_folders(stage):
        n = sum(len(pending_inputs(d, stage)) for d in set_scan_dirs(folder))
        if n:
            choices.append((f"{label}  ({n} to do)", folder))
    return sorted(choices, key=lambda c: natural_key(c[0]))


def folder_choices(stage):
    """The Test Folders checkbox group, freshly scanned. An empty list is the
    normal state once everything is processed — say so in the label, otherwise
    the panel just renders blank and the load/run button looks broken."""
    choices = scan_test_folders(stage)
    label = stage.pending_label
    if not choices:
        roots = "; ".join(scan_roots(stage)) or "(none set)"
        label = stage.empty_label.format(roots=roots)
    return gr.CheckboxGroup(choices=choices, value=[], label=label)


def resolve_dropped_paths(paths, stage):
    """
    Drag-and-drop hands us gradio's upload cache copies: same filename and same
    bytes, but the original folder is lost, so "save into each image's own
    folder" has nowhere to go. Find the originals back under the scan roots —
    same name, same size, and byte-identical (`1r1.png` exists in every set, so
    the name alone is not an identity).

    -> (paths with the originals substituted in, notes)
    """
    dropped = [p for p in paths if is_dragged_temp_copy(p)]
    if not dropped:
        return paths, []

    wanted = {}
    for p in dropped:
        try:
            wanted[p] = (os.path.basename(p).lower(), os.path.getsize(p))
        except OSError:
            pass

    names = {name for name, _size in wanted.values()}
    candidates: dict = {}
    for dirpath, _dirnames, filenames in walk_roots(stage):
        for f in filenames:
            if f.lower() not in names:
                continue
            full = os.path.join(dirpath, f)
            if is_dragged_temp_copy(full):
                continue  # the upload cache itself sits under a scan root
            try:
                candidates.setdefault((f.lower(), os.path.getsize(full)), []).append(full)
            except OSError:
                pass

    resolved, notes, unresolved = [], [], 0
    for p in paths:
        key = wanted.get(p)
        matches = [m for m in candidates.get(key, []) if filecmp.cmp(m, p, shallow=False)] if key else []
        if len(matches) == 1:
            resolved.append(matches[0])
        else:
            resolved.append(p)
            if key:
                unresolved += 1
                if len(matches) > 1:
                    notes.append(f"`{os.path.basename(p)}` matches {len(matches)} files "
                                 f"under your scan roots — can't tell which folder it came from.")
    if unresolved:
        notes.append(f"{unresolved} dropped file(s) not found under your scan roots — those "
                     f"results go to the output dir. (Settings → {stage.name} → scan roots.)")
    return resolved, notes


# ──────────────────────────────────────────────
# Drop-zone helpers
# ──────────────────────────────────────────────
def file_paths(files):
    """gr.File values -> plain path list."""
    paths = [f if isinstance(f, str) else getattr(f, "name", None) for f in (files or [])]
    return [p for p in paths if p]


def filter_suffix(paths, suffix_filter):
    """Keep only paths whose stem ends with the filter (blank = keep everything).
    -> (kept, skipped_count)"""
    suffix = (suffix_filter or "").strip().lower()
    if not suffix:
        return paths, 0
    kept = [p for p in paths
            if os.path.splitext(os.path.basename(p))[0].lower().endswith(suffix)]
    return kept, len(paths) - len(kept)


# ──────────────────────────────────────────────
# Default script args — mirrors modules/api/api.py :: init_default_script_args
# ──────────────────────────────────────────────
_script_args_cache: dict = {}  # role -> (runner weakref, args)


def get_default_script_args(runner, role):
    """
    Build a script_args list of the exact length `runner` (the img2img or
    txt2img ScriptRunner) expects, with position 0 = 0 (no selectable script)
    and every alwayson script's slice filled with that script's own UI default
    values.

    This is the same technique Forge Neo's API uses (init_default_script_args)
    when there is no live UI to source args from. Passing placeholder Nones
    instead breaks alwayson scripts (adetailer, dynamic prompts, ...) that
    index into their args expecting real values.

    Known-fragile: this re-invokes .ui() on the runner's live script
    singletons. Harmless today (ScriptRunner.infotext_fields was already
    flattened at page build, and the throwaway Blocks context swallows the new
    components), but a script whose ui() carries side effects re-runs them here.
    """
    last_arg_index = 1
    for script in runner.scripts:
        if last_arg_index < script.args_to:
            last_arg_index = script.args_to

    # Keyed on the runner's identity, not just length: an in-process Reload UI
    # builds new ScriptRunners with new script instances, and a same-length
    # cache from the old UI would serve stale defaults. Weakref so a dead
    # runner isn't kept alive by its own cache entry.
    ref, args = _script_args_cache.get(role, (None, None))
    if ref is not None and ref() is runner and len(args) == last_arg_index:
        return args

    script_args = [None] * last_arg_index
    script_args[0] = 0

    with gr.Blocks():  # script.ui() creates gradio components; needs a Blocks context
        for script in runner.scripts:
            ui_elems = script.ui(script.is_img2img)
            if ui_elems:
                script_args[script.args_from : script.args_to] = [elem.value for elem in ui_elems]

    _script_args_cache[role] = (weakref.ref(runner), script_args)
    return script_args


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


def lora_networks():
    """Forge's lora module (extensions-builtin/sd_forge_lora), or None."""
    nets = sys.modules.get("networks")
    return nets if hasattr(nets, "available_networks") else None


def resolve_lora_name(name: str, nets):
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
    # Tokens written by other tools can carry a subfolder and/or the file
    # extension — <lora:Misc\Dark_Slider_Anima.safetensors:0.8> — but Forge
    # indexes by bare filename stem, so strip both before any lookup.
    key = key.replace("\\", "/").rsplit("/", 1)[-1]
    key = re.sub(r"\.(safetensors|pt|ckpt)$", "", key)

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


def fix_lora_names(text: str):
    """Re-point unresolvable <lora:...> names at the real file. -> (text, notes)
    One setting gates both tabs — the repair is about the Lora folder, not a stage."""
    if not text or not getattr(shared.opts, "batch_adetailer_fix_lora_names", True):
        return text, []

    nets = lora_networks()
    if nets is None:
        return text, []

    notes: list[str] = []

    def replace(match):
        name = match.group(1)
        resolved = resolve_lora_name(name, nets)

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
def apply_source_image_parameters(p, geninfo: str):
    """
    Apply the source image's generation parameters (prompt, seed, sampler, ...)
    to the processing object, so the pass is conditioned the same way the
    original generation was. Returns the parsed params dict for stage-specific
    extras (the hires tab reuses the source's shift).
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

    for attr, key, cast in (
        ("steps", "Steps", int),
        ("cfg_scale", "CFG scale", float),
        ("distilled_cfg_scale", "Distilled CFG Scale", float),
        # ADetailer's inpaint pass forwards these to its own processing object
        # (!adetailer.py i2i()), and the hires pass reads them directly.
        ("subseed_strength", "Variation seed strength", float),
        ("seed_resize_from_w", "Seed resize from-1", int),
        ("seed_resize_from_h", "Seed resize from-2", int),
    ):
        try:
            setattr(p, attr, cast(params[key]))
        except (KeyError, ValueError):
            pass

    # parse_generation_parameters always pops "Clip skip" (its skip-fields list
    # appends it unconditionally), so recover it from the raw infotext.
    m = re.search(r"\bClip skip: (\d+)", geninfo)
    if m:
        p.override_settings["CLIP_stop_at_last_layers"] = int(m.group(1))

    if params.get("Sampler"):
        p.sampler_name = params["Sampler"]
    if params.get("Schedule type"):
        p.scheduler = params["Schedule type"]

    return params


# ──────────────────────────────────────────────
# Saving with the original filename + suffix
# ──────────────────────────────────────────────
def save_with_original_name(processed, p, save_opts: dict, fix_info=None):
    """
    Save result images as <original filename><suffix>.<ext> directly in the
    output directory (no dated subfolders, no [seed]-[prompt] naming pattern).
    Collisions get a -1, -2, ... counter instead of overwriting. `fix_info`
    (optional) rewrites each infotext before saving — the adetailer tab uses it
    to undo skip-img2img's cosmetic damage.
    """
    outdir = p.outpath_samples
    os.makedirs(outdir, exist_ok=True)
    extension = save_opts.get("format") or shared.opts.samples_format
    stem, suffix = save_opts["stem"], save_opts.get("suffix", "")

    for i, image in enumerate(processed.images):
        base = f"{stem}{suffix}" if i == 0 else f"{stem}{suffix}-{i}"
        name = base
        n = 1
        while os.path.exists(os.path.join(outdir, f"{name}.{extension}")):
            name = f"{base}-{n}"
            n += 1

        infotext = processed.infotexts[i] if i < len(processed.infotexts) else None
        if fix_info is not None:
            infotext = fix_info(infotext)

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
# would be wiped. A bare module flag was worse still — any new run start reset
# it, silently un-cancelling a concurrent run. Per-run tokens instead:
# start_run() hands each batch its own sequence number, cancel kills every run
# started so far, and a run started *after* the cancel is untouched.
#
# Counted per stage, not globally: both tabs' outer loops can be in flight at
# once (only the GPU work is serialized, on main_thread's single worker), and
# one global counter let the ADetailer tab's Cancel stop a Hires-Fix batch the
# user never touched.
#
# importlib.reload() re-executes this body but reuses the module __dict__, so
# carry the live counters over — a reload mid-batch must not renumber a token
# that a running loop still holds.
# ──────────────────────────────────────────────
_runs = globals().get("_runs", {})  # stage key -> [issued, cancelled_through]


def start_run(stage):
    """A new batch's cancel token."""
    counters = _runs.setdefault(stage.opt_prefix, [0, 0])
    counters[0] += 1
    return counters[0]


def cancel_requested(stage, token):
    return token <= _runs.get(stage.opt_prefix, (0, 0))[1]


def request_cancel(stage):
    """Cancel button: abort the image being sampled, then stop the batch."""
    counters = _runs.setdefault(stage.opt_prefix, [0, 0])
    counters[1] = counters[0]
    shared.state.interrupt()
    return "⏹️ Cancel requested — finishing the current image, then stopping."
