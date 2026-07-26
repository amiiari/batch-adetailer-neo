// Right-click a thumbnail in the Batch ADetailer source gallery -> select that
// image and fill Slot 1's ADetailer prompt with the first 3 lines of the image's
// own prompt (or, if it has none baked in, the live img2img prompt).
//
// Gradio has no contextmenu event, so this is the usual Forge dance: stash the
// clicked index (and the current img2img prompt, for the fallback) in hidden
// textboxes, dispatch `input` so gradio's frontend picks the values up, then
// click a hidden button whose python handler does the work.
//
// Also: ←/→ steps through the thumbnails without clicking each one — it just
// clicks the neighbour of the selected one, so gradio's own select event does
// everything a real click would.
(function () {
    "use strict";

    const INDEX_ID = "batch_adetailer_rclick";
    const PROMPT_ID = "batch_adetailer_rclick_prompt";
    const BUTTON_ID = "batch_adetailer_rclick_btn";
    const GALLERY_ID = "batch_adetailer_source";

    function onRightClick(event) {
        const gallery = event.currentTarget;
        const thumbs = Array.from(gallery.querySelectorAll(".thumbnail-item"));
        const clicked = event.target.closest(".thumbnail-item");
        const index = thumbs.indexOf(clicked);
        if (index < 0) {
            return;  // right-click on empty gallery space: leave the browser menu alone
        }

        event.preventDefault();

        const root = gradioApp();
        const field = root.querySelector(`#${INDEX_ID} textarea, #${INDEX_ID} input`);
        const button = root.querySelector(`#${BUTTON_ID}`);
        if (!field || !button) {
            return;
        }

        field.value = String(index);
        field.dispatchEvent(new Event("input", { bubbles: true }));

        // Stash the live img2img prompt so python can fall back to it when the
        // clicked image has no prompt of its own. #img2img_prompt lives on the
        // img2img tab but stays in the DOM even when that tab isn't showing.
        const promptField = root.querySelector(`#${PROMPT_ID} textarea, #${PROMPT_ID} input`);
        const img2imgPrompt = root.querySelector("#img2img_prompt textarea, #img2img_prompt input");
        if (promptField) {
            promptField.value = img2imgPrompt ? img2imgPrompt.value : "";
            promptField.dispatchEvent(new Event("input", { bubbles: true }));
        }

        // Let gradio's input handlers commit the values before the click reads them.
        setTimeout(() => button.click(), 30);
    }

    function onArrowKey(event) {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") {
            return;
        }
        if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) {
            return;
        }
        // Leave the arrows alone while typing/adjusting: prompt boxes, sliders,
        // dropdowns and the like all use them for their own cursor.
        const t = event.target;
        if (t && t.closest && t.closest("input, textarea, select, [contenteditable='true']")) {
            return;
        }

        const gallery = gradioApp().querySelector(`#${GALLERY_ID}`);
        if (!gallery || gallery.offsetParent === null) {
            return;  // Batch ADetailer tab isn't on screen
        }

        const thumbs = Array.from(gallery.querySelectorAll(".thumbnail-item"));
        if (!thumbs.length) {
            return;
        }

        const current = thumbs.findIndex((el) => el.classList.contains("selected"));
        const next = current < 0 ? 0 : current + (event.key === "ArrowRight" ? 1 : -1);
        if (next < 0 || next >= thumbs.length) {
            return;  // already at either end
        }

        event.preventDefault();
        thumbs[next].click();
        thumbs[next].scrollIntoView({ block: "nearest" });
    }

    onUiLoaded(function () {
        // The document survives a Reload UI, the gallery element does not —
        // hence one guard per listener.
        if (!document.body.dataset.badArrowNav) {
            document.body.dataset.badArrowNav = "1";
            document.addEventListener("keydown", onArrowKey);
        }

        const gallery = gradioApp().querySelector(`#${GALLERY_ID}`);
        if (!gallery || gallery.dataset.badRightClick) {
            return;
        }
        // Delegated on the gallery itself: the thumbnails are re-rendered on
        // every drop, the container is not.
        gallery.dataset.badRightClick = "1";
        gallery.addEventListener("contextmenu", onRightClick);
    });
})();
