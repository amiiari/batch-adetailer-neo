# Change Log

## 2026-07-13 — Right-click to clear Slot 1's prompt (v0.2.3)

Right-clicking a thumbnail selects that image and blanks Slot 1's ADetailer prompt
(blank = inherit the image's own prompt). Nothing else in the slot changes.

- New file `javascript/batch_adetailer.js` — Forge auto-loads `javascript/*.js` from every
  extension (`ui_gradio_extensions.py:20`, `scripts.list_scripts("javascript", ".js")`).
  Gradio has no contextmenu event, so the JS delegates a listener on the gallery container
  (the thumbnails are re-rendered on each drop; the container isn't), writes the clicked
  index into a hidden textbox, dispatches `input` so gradio's frontend commits the value,
  and clicks a hidden button. `_on_right_click()` on the python side does the work.
- The source gallery got `elem_id="batch_adetailer_source"` — deliberately *not* ending in
  `_gallery`, since that suffix is what makes Forge's imageviewer.js attach its lightbox.
- Right-clicking empty gallery space leaves the normal browser menu alone.

## 2026-07-13 — LoRA name repair (v0.2.2)

The "my LoRA isn't applied" report turned out not to be a prompt-plumbing bug at all.
The source images' prompts name `<lora:amiiari-anima-v5.3-000021:1>`, but the Lora folder
now holds only `amiiari-anima-v5.3.safetensors` (whose own alias — `ss_output_name` —
is `amiiari-anima-v5`, matching neither name). Forge resolves LoRA tokens against
`networks.available_networks` (filename stems) and `available_network_aliases`; when a
name matches neither, `load_networks` logs `Failed to load LoRA` **to the console** and the
pass just renders without it. Nothing surfaces in the UI — it looks exactly like a prompt
that never arrived. Any renamed training epoch does this to every old image that references it.

- New `_fix_lora_names()` re-points unresolvable `<lora:...>` names at the matching file:
  exact → case-insensitive → epoch-suffix-stripped (`mylora-000021` → `mylora`) → a file
  carrying an epoch number of its own. Names that already resolve are never touched; a name
  with no candidate at all is left alone and *flagged* in the status log rather than
  silently ignored.
- Applied to the prompt inherited from the image AND to each slot's override prompts.
- Setting: **Repair Unresolvable LoRA Names in Prompts** (on by default).
- `_process_single_image` now returns a 4th value (notes) so repairs/misses reach the UI log.

## 2026-07-13 — First-test fixes (v0.2.1)

Three issues from the first live run.

- **Cancel button.** New `⏹️ Cancel` next to Run. `shared.state.interrupted` alone can't
  carry the request — `state.begin()` at the top of every image resets it, so a cancel
  landing *between* images would be wiped. Module-level `_cancel_requested` survives that
  and is cleared only when a batch starts; the button also calls `shared.state.interrupt()`
  to abort the image currently being sampled. Bound with `queue=False` so the click is
  served immediately instead of queueing behind the running batch.
- **Prompt boxes** are 5 rows at rest (`max_lines=20`), growing with the text.
- **LoRAs/styles were silently dropped from the inherited prompt.** `_apply_source_image_parameters`
  took `params["Prompt"]` straight from `parse_generation_parameters()` — but that function
  *subtracts* the text of any matching saved style from the prompt it returns and hands the
  style names back separately in `params["Styles array"]` (that's how the paste button
  repopulates the styles dropdown; `infotext_utils.py:_extract_styles`, gated on
  `opts.infotext_styles`, default "Apply if any"). We left `p.styles` empty, so anything
  living in a style — a `<lora:...>` token, for instance — never made it into `p.prompt`,
  and a slot with a blank ADetailer prompt (which falls back to `p.all_prompts`) inpainted
  without it. Now `p.styles` is set from `Styles array`; `process_images` folds the text
  back in via `apply_styles_to_prompt`. The resolved base prompt is printed to the console
  per image so this stays visible.

## 2026-07-13 — Per-image units (v0.2)

Redesign after v0.1 turned out to solve the wrong problem: v0.1 had one global set of
4 fully-configured units applied to every image. What was actually wanted: **each image
carries its own unit configuration**, inheriting everything else from the ADetailer
defaults already saved on the img2img panel.

### Changed
- **Per-image config store.** Three `gr.State`s (`paths`, `store = {path: [values]}`,
  `sel`). Click a thumbnail in the source gallery → that image's slots load on the left.
  Handlers: `_on_files` (seed configs for new images, keep existing), `_on_select_image`
  (gallery `.select` + `gr.SelectData`), `_on_control_change` (single `gr.on` across all
  override controls), `_on_preset_change` (per-slot, repopulates that slot's overrides),
  `_on_apply_to_all`.
- **Inherit the user's saved ADetailer defaults** — new `_get_adetailer_defaults()`.
  ADetailer builds `gr.State(lambda: state_init(w))` per unit; gradio keeps that lambda in
  `component.load_event_to_attach`, and Forge's UiLoadsave *mutates* the underlying widgets'
  `.value` with ui-config.json values (`ui_loadsave.py:84`) — but only after all script UI
  is built. So calling the lambda **at event time** re-reads the widgets and yields the
  user's saved defaults. `state.value` alone would only give build-time (stock) values.
  Rejected: re-calling `script.ui()` in a fresh Blocks (gets stock defaults, and re-binds
  handlers to the real img2img submit button); parsing ui-config.json by label (brittle —
  labels contain `/` and unicode arrows, and `Enable this tab (1st)` uses its own scheme).
- **Slots, not units.** Each slot chooses *which* unit it runs, so slot order = execution
  order and units can be reordered per image (hand before face when a hand overlaps it).
- **Only 5 per-image overrides** (`OVERRIDE_ATTRS`): ad_prompt, ad_negative_prompt,
  ad_confidence, ad_denoising_strength, ad_mask_max_ratio. Everything else inherited.
- **Forced on every unit**: `ad_tab_enable=True` and `ad_hires_fix_only=False` — otherwise a
  preset whose saved defaults have the tab off (or hires-only) silently no-ops in our
  img2img pass.
- **Field whitelist** now derived at runtime from `ADetailerArgs.__fields__` (Extra.forbid),
  with the hardcoded set as fallback.
- Layout per the request: images + results on the right, units on the left.

### Kept from v0.1 (unchanged)
`_register_settings`, `_get_default_script_args`, `_find_adetailer_script`,
`_assemble_script_args`, `_apply_source_image_parameters`, `_fix_infotext`,
`_save_with_original_name`, `_process_single_image`, and the batch loop body.

### Gotchas hit
- `functools.partial` with a bound keyword turns a trailing `evt: gr.SelectData` param into
  keyword-only, and gradio only scans *positional* params when deciding where to inject
  event data → the click event never arrives. Gallery/file handlers use closures instead.
- `sel_state` must be updated in the **same outputs batch** as the controls on thumbnail
  select; otherwise the change-storm from loading the controls writes the new image's values
  into the previously selected image's slot.
- A unit with no model set leaves its slot *disabled* rather than pre-selected (it could
  never run anyway).

---

## 2026-07-13 — Extension Implementation (v0.1)

### Files Created
- `scripts/batch_adetailer.py` — the whole extension:
  - `_register_settings()` — 3 settings under a "Batch ADetailer" section
  - `_find_adetailer_script()` / `_get_ad_model_choices()` — locate the installed ADetailer alwayson script on the img2img runner and read its live `model_mapping` for the model dropdowns
  - `_get_default_script_args()` — `init_default_script_args` technique from `modules/api/api.py`, for the **img2img** runner
  - `_build_ad_unit_dict()` / `_assemble_script_args()` — build ADetailerArgs-shaped dicts and splice `[True, True, unit0..unitN]` into ADetailer's `args_from:args_to` slice
  - `_apply_source_image_parameters()` — per-image infotext inheritance
  - `_fix_infotext()` — patch out the `Steps: 1 / Size: 128x128` artifacts of skip-img2img
  - `_process_single_image()` / `batch_adetailer_process()` — sequential batch loop on Forge's main thread
  - `_unit_controls()` / `_build_ui_tab()` — 4-unit tabbed UI + streaming gallery
- `install.py`, `README.md`, `LICENSE` (MIT), `CLAUDE.md`, `.gitignore`

### Architecture Decisions
- **Drive ADetailer, don't reimplement it.** Build a `StableDiffusionProcessingImg2Img` per image with
  `init_images=[img]` and pass ADetailer `ad_skip_img2img=True`, so the base pass is neutered to a
  throwaway 1-step 128×128 render and ADetailer inpaints the original full-res image in `postprocess_image`.
- **Copied wholesale from the batch-hires-fix template**: settings registration via `add_option`, main-thread
  dispatch (`main_thread.run_and_wait_result`), `shared.state.begin/end` per image, streaming gallery yields,
  original-filename saving with collision counters, tracebacks into the status log.
- **4 units**, units 2–4 default to model `None` (= disabled). Slot count is derived from ADetailer's
  `args_to - args_from` at runtime (it depends on the `ad_max_models` setting), with a warning on overflow.
- **Explicit field whitelist** in `_build_ad_unit_dict()` — `ADetailerArgs` is `extra=Extra.forbid`, so a stray
  key would make the unit fail validation and be silently dropped.
- **Every unit slot is overwritten** in `_assemble_script_args()` — ADetailer's UI defaults leave gr.State
  lambdas in those positions, which it ignores, so a half-filled slice would quietly lose units.
- `mask=None` on the processing object: ADetailer disables skip-img2img on inpaint objects.

### Pending
- Live test in Forge Neo (see README/CLAUDE.md verification steps).
