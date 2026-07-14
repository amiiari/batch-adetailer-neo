// Right-click a thumbnail in the Batch ADetailer source gallery -> select that
// image and put "[base prompt]" in Slot 1's ADetailer prompt (a placeholder for
// the image's own prompt, so it can be built on rather than only inherited).
//
// Gradio has no contextmenu event, so this is the usual Forge dance: stash the
// clicked index in a hidden textbox, dispatch `input` so gradio's frontend picks
// the value up, then click a hidden button whose python handler does the work.
(function () {
    "use strict";

    const INDEX_ID = "batch_adetailer_rclick";
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
        // Let gradio's input handler commit the value before the click reads it.
        setTimeout(() => button.click(), 30);
    }

    onUiLoaded(function () {
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
