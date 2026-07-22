# Change Log

## 2026-07-22 — ADetailer moves to the front of the chain (v0.6)

The refine pipeline is now adetailer-first:
`1r1.png -> 1r1-adetailer.png -> 1r1-adetailer-base.png + 1r1-adetailer-hires.png`
(faces are repaired at base resolution — where ADetailer actually pays off —
and the hires pass afterwards re-sharpens its output in the artist's style).

- **Folder scan flipped**: `_pending_hires` → `_pending_bases`. A set is listed
  while it has base images (no variant suffix) without a `-adetailer` sibling.
  Bases with a plain `-hires` sibling are old-chain work and stay hidden, so
  released sets don't flood the list. Compositional edits are new revisions
  (1r2), never suffixes.
- **Infotext sampler repair**: skip-img2img also stamps `Sampler: Euler` into
  the saved infotext (on top of the 1-step/128x128 damage `_fix_infotext`
  already undid). Now repaired from the captured original — batch-hires-fix
  inherits per-image params from this infotext, so it must tell the truth.
- Drop-zone suffix filter default `-hires` → empty (inputs are plain bases now).
- The save-to-source checkbox label was vague; now:
  "Save as <name>-adetailer.png into each image's own folder".
- `test_scan.py` updated to the new rules.

## 2026-07-22 — Folder mode saves into the real source folders (v0.5.6)

- **Bug**: with save-to-source on, folder-loaded batches saved their -adetailer
  files into gradio's temp cache instead of the Tests folders. Loading routed
  the paths through the drop zone, and every value that round-trips a gr.File
  is copied into gradio's cache (blocks.py `move_resource_to_block_cache`) — so
  "the folder the image came from" was the cache copy's folder.
- **Fix**: 📥 Load Selected Folders now feeds the original paths straight into
  the tab's state (`_load_paths`, the shared tail of the drop handler): gallery,
  per-image configs, preview and prompts all work as before, but paths_state
  holds the real files, so results land next to them. The drop zone is
  deliberately left untouched (drag-drop keeps its cache-copy semantics and
  output-dir saving).

## 2026-07-22 — Batch size limit removed again (v0.5.5)

- The 50-image "Max Images per Batch" limit is gone (it had been removed once,
  then crept back in with the v0.4 work): setting, check, and README mention.
  Folder loads routinely exceed 50 and the batch is sequential anyway — there's
  nothing for a cap to protect.

## 2026-07-22 — Tag autocomplete in the prompt boxes (v0.5.4)

- The slot prompt/negative boxes now carry elem_ids with the real ADetailer's
  img2img prefixes (`script_img2img_adetailer_ad_prompt_batch_slot1`, ...).
  sd-webui-tagcomplete targets ADetailer boxes with the prefix selectors
  `[id^=script_img2img_adetailer_ad_prompt] textarea` /
  `[id^=..._ad_negative_prompt] textarea` (_textAreas.js, hasIds entries are
  global queries), so matching the prefix lights up autocomplete here with zero
  tagcomplete configuration — and tag insertion dispatches a normal `input`
  event, so the per-image config store picks the text up like typed input.

## 2026-07-22 — Preview in the editor column, whole image always visible (v0.5.3)

- **The preview moved to the left column**, above the "Editing:" label — it sits
  with the controls it belongs to, and the right column is just thumbnails + log.
  It starts at 400px (narrow column — it doesn't need to be big, it needs to show
  what's being configured) and keeps the drag handle.
- **The whole image is now always visible**, scaled to fit however small the box
  is dragged. The v0.5.1/0.5.2 attempts sized the image off `.image-container`,
  whose height comes from an intermediate wrapper — so the image rendered at its
  natural height and got cropped by the box's `overflow: hidden` (what looked like
  "not fully shown"). The container is now pinned to the box's bounds
  (`position: absolute; inset: 0`), taking every wrapper out of the height chain,
  with `object-fit: contain` on the image.

## 2026-07-22 — Preview shows the image at full width (v0.5.2)

- The preview no longer letterboxes the image into the box (`object-fit:
  contain` made the resize drag just rescale it). The image now renders at
  full column width in its natural aspect, the box scrolls vertically for
  whatever doesn't fit, and the drag handle sizes the *viewport*, not the
  image. Gradio's own `img { height: 100%; object-fit: contain }` rule is
  out-specificity'd via the elem_id.

## 2026-07-22 — Resizable preview + arrow-key navigation (v0.5.1)

- **The big preview is drag-resizable** like the thumbnail gallery: same
  `resize: vertical` CSS trick, starting at the old 640px (min 240px). The
  gradio `height=` pin is gone; the image scales to whatever height the drag
  gives it (`object-fit: contain` was already there).
- **←/→ steps through the thumbnails.** The keydown handler just clicks the
  selected thumbnail's neighbour, so gradio's own select event does everything a
  real click would (slots, preview, editing label). Inactive while the target is
  an input/textarea/select/contenteditable (prompt boxes and sliders keep their
  arrows), when the tab isn't on screen, and at either end of the strip. The
  listener sits on `document` (guarded separately from the gallery's, which is
  re-attached per Reload UI since the gallery element is replaced).

## 2026-07-22 — Test-Folder Mode: ADetailer In Place (v0.5)

- **New 📁 Test Folders panel**: scans configurable roots (Settings → "Test-folder
  scan roots", default Commissions + Requests) for `<set>/Tests` folders holding
  `-hires` images with no `-adetailer`/`-edited` successor, listed as checkboxes
  ("Commission 137 - M, Fluorite  (7 to do)").
- **📥 Load Selected Folders** drops the pending images into the tab through the
  normal file-input path — gallery, per-image configs and prompts work as usual —
  and ticks the new **Save next to each source image** checkbox. With it on, each
  result saves as `<stem>-adetailer.png` into its own source folder (png and the
  `-adetailer` suffix forced: that naming is what the pending scan and the content
  manager key on; a custom suffix would reprocess everything forever). The list
  rescans itself after each batch run.
- **Cancel mid-image no longer saves the partial result** (same fix as
  batch-hires-fix v0.5.0): an interrupted inpaint returns a partial image; saving
  it would look finished — and with save-to-source, permanently hide the image
  from the pending scan.
- `test_scan.py`: standalone self-check for the scan logic (Forge modules stubbed).

## 2026-07-22 — Big preview + suffix filter on the drop zone (v0.4)

- **Large preview of the selected image.** New `gr.Image` at the top of the right
  column, directly under the drop zone (640px tall) — updated on file drop (first
  image), thumbnail click, and right-click. The thumbnail gallery now sits *below*
  it as a strip (starting height 200px, still drag-resizable); the preview is where
  you actually look at the image you're configuring.
- **Suffix filter on the drop zone** (default `-hires`, empty = load everything). Drag a
  whole folder's worth of files in and only the ones whose filename stem ends with the
  suffix are loaded; the rest are skipped and counted in the editing label. The filtered
  list is written back into the file box so it matches what loaded — guarded with a bare
  `gr.update()` when nothing was skipped, since this runs inside the file box's own
  `.change` handler and an unconditional write would retrigger it forever (the one
  retrigger after a filtering pass filters nothing, and stops).

## 2026-07-14 — Run one image (v0.3.3)

**▶️ Run this image** next to Run Batch: re-runs only the selected thumbnail, for when a
batch is fine except for an image or two. `batch_adetailer_run_selected()` is a thin
generator that delegates to `batch_adetailer_process()` with `paths=[selected]` and `sel=0`,
so config lookup, saving, cancelling and the status log all behave identically — no second
code path to keep in sync. Re-runs don't overwrite the earlier result: `_save_with_original_name`
already adds a `-1`, `-2`, ... counter on collision. With nothing selected it just says so.

## 2026-07-14 — Gallery resize actually resizes (v0.3.2)

Dragging the gallery taller stopped working past a point: the *block* grew, but gradio caps
the thumbnail grid's own height, so the extra space was empty and the block's scrollbar took
over. The block is now a flex column with the grid as the flexing child (`max-height: none`),
so the grid follows the dragged height and is the only thing that scrolls.

## 2026-07-14 — Apply-to-all no longer touches prompts (v0.3.1)

"Apply these settings to all images" used to overwrite every image's whole config, prompts
included — which wiped exactly the thing the tab exists to keep per-image. It now copies
only the non-prompt values (unit choice, confidence, denoising strength, mask max ratio);
each image keeps its own positive and negative prompt, in every slot.

The selected image is the exception, in that its prompts come from the live control values
rather than the store — those *are* its prompts, and the store copy may not have caught up
with a `.change` yet.

## 2026-07-14 — Layout (v0.3)

Three things from using it on real batches:

- **Drop zone moved to the top**, full width. Parked in the left column it grew with the
  file list, pushing the unit controls out of reach on a big batch. Its list is now capped
  at 220px with its own scrollbar.
- **The thumbnail gallery is resizable** — drag the bottom-right corner. Gradio has no
  resizable gallery, but the block is just a div: a `<style>` block gives
  `#batch_adetailer_source` a starting height plus `resize: vertical; overflow: auto`.
  The gradio `height` prop was *removed* — it pins the inner grid and fights the resize.
- **Results gallery removed** (results are saved to disk anyway). `batch_adetailer_process`
  now yields a status string instead of `(images, status)`, and `process_btn.click` outputs
  only the log. The Status/Log stays: it carries per-image progress, tracebacks, and the
  LoRA-repair notes.

## 2026-07-13 — `[base prompt]` token (v0.2.4)

Right-click now *inserts* `[base prompt]` into Slot 1's prompt instead of emptying the box.
Left alone it behaves exactly like an empty box (inherit the image's prompt); typed around,
it appends — `[base prompt], detailed eyes`. Emptying the box gives you no way to *add* to
the inherited prompt, which was the limitation.

- `BASE_PROMPT_TOKEN` / `_BASE_PROMPT_RE` — the token is rewritten to ADetailer's own
  `[PROMPT]` placeholder (`!adetailer.py :: _get_prompt`, which substitutes `p.all_prompts`)
  on the way into the unit dict, so nothing new has to resolve it. Case- and
  spacing-tolerant, works in every slot and in the negative box.

## 2026-07-13 — Right-click to clear Slot 1's prompt (v0.2.3)

Right-clicking a thumbnail selects that image and blanks Slot 1's ADetailer prompt
(blank = inherit the image's own prompt). Nothing else in the slot changes.
*(Superseded by v0.2.4: it inserts a `[base prompt]` token instead of clearing.)*

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
