"""Self-check for both stages' scan logic and the shared core. Run: python test_scan.py
(Forge modules are stubbed out — this only exercises the pure filesystem code.)
"""
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock

for _m in ("gradio", "PIL", "modules", "modules.infotext_utils", "modules_forge"):
    sys.modules[_m] = MagicMock()

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)                            # batch_adetailer_shared
sys.path.insert(0, os.path.join(_here, "scripts"))
import batch_adetailer_shared as bshared
import batch_adetailer as bad
import batch_hires_fix as bhf


def touch(*parts, data=b""):
    path = os.path.join(*parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


# ── ADetailer stage: bases pending until a -adetailer sibling exists ──
with tempfile.TemporaryDirectory() as root:
    tests = os.path.join(root, "Commission 1 - A", "Tests")
    touch(tests, "1r1.png")                                     # pending base
    touch(tests, "2r1.png"); touch(tests, "2r1-adetailer.png")  # detailed -> done
    touch(tests, "3r1.png"); touch(tests, "3r1-adetailer.png")
    touch(tests, "3r1-adetailer-base.png")                      # later-stage outputs -> not bases
    touch(tests, "3r1-adetailer-hires.png")
    touch(tests, "4r1.png"); touch(tests, "4r1-hires.png")      # old hires-first chain -> left alone
    touch(tests, "10r1.png")                                    # pending; after 1r1, natural order
    touch(tests, "2r1-adetailer-1.png")                         # collision copy -> ignored
    touch(tests, "notes.txt")                                   # not an image -> ignored
    pending = [os.path.basename(p) for p in bad._pending_bases(tests)]
    assert pending == ["1r1.png", "10r1.png"], pending

    # "Load Folder" takes every base, done or not — but still never a variant.
    bases = [os.path.basename(p) for p in bad._base_images(tests)]
    assert bases == ["1r1.png", "2r1.png", "3r1.png", "4r1.png", "10r1.png"], bases

    touch(root, "Commission 2 - B", "Tests", "1r1.png")         # 1 pending
    done = os.path.join(root, "Commission 3 - C", "Tests")
    touch(done, "1r1.png"); touch(done, "1r1-adetailer.png")    # done -> hidden
    touch(root, "Commission 4 - D", "readme.txt")               # no Tests dir -> hidden

    # A set nested one level deeper (root/Commissions/<set>), as the real tree
    # has: sets sit both directly under the root and under a group folder.
    nested = os.path.join(root, "Commissions", "Commission 5 - E")
    touch(nested, "Tests", "1r1.png")
    touch(nested, "9r1.png")

    # One entry per set: its own folder + its Tests folder, counted together.
    # Folders without a Tests marker (scratch, references) aren't sets; work
    # below Tests (Finished archives) isn't scanned.
    set_a = os.path.join(root, "Commission 1 - A")
    touch(set_a, "8r1.png")                # + the 2 pending in its Tests -> 3
    touch(tests, "Finished", "7r1.png")
    touch(root, "random", "junk.png")
    set_b = os.path.join(root, "Commission 2 - B")   # pending only in Tests

    # Request sets have no Tests subfolder — being inside Requests is the marker.
    req = os.path.join(root, "Requests", "Request 1 - X")
    touch(req, "1r1.png"); touch(req, "2r1.png")
    done_req = os.path.join(root, "Requests", "Request 2 - Y")
    touch(done_req, "1r1.png"); touch(done_req, "1r1-adetailer.png")  # done -> hidden

    # Only the highest rN revision of each image number counts, double digits
    # included (1r2 < 1r13); names outside the NrM convention pass through.
    touch(req, "1r2.png"); touch(req, "1r13.png")
    touch(req, "flower2.png")
    latest = [os.path.basename(p) for p in bad._base_images(req)]
    assert latest == ["1r13.png", "2r1.png", "flower2.png"], latest

    bshared.shared.opts.batch_adetailer_scan_roots = root + ";" + os.path.join(root, "missing")
    choices = bshared.scan_test_folders(bad.STAGE)
    assert [c[1] for c in choices] == [set_a, set_b, nested, req], choices
    assert "(3 to do)" in choices[0][0], choices
    assert "(1 to do)" in choices[1][0] and "(2 to do)" in choices[2][0], choices
    assert "(3 to do)" in choices[3][0], choices  # 1r13 + 2r1 + flower2

    # Drag-dropped copies resolve back to their originals: same name in two sets,
    # told apart by content; a name that exists nowhere stays a temp path.
    a = touch(tests, "9r1.png", data=b"AAA")
    b = touch(nested, "9r1.png", data=b"BBB")
    cache = os.path.join(root, "gradio-cache")
    bshared.is_dragged_temp_copy = lambda p: p.startswith(cache)
    drops = [touch(cache, "h1", "9r1.png", data=b"BBB"),      # -> b
             touch(cache, "h2", "9r1.png", data=b"AAA"),      # -> a
             touch(cache, "h3", "nowhere.png", data=b"CCC")]  # -> unresolved
    resolved, notes = bshared.resolve_dropped_paths(drops, bad.STAGE)
    assert resolved == [b, a, drops[2]], resolved
    assert len(notes) == 1 and "1 dropped file(s) not found" in notes[0], notes
    assert "Batch ADetailer" in notes[0], notes  # the note names this tab's settings

    # Export → import round-trip: configs follow the filename, not the path.
    cfg = [0, "prompt A", "neg A", 0.3, 0.4, 1.0] * 2
    msg = bad._export_prompts({a: cfg}, [a], root)
    assert "1 image(s)" in msg, msg
    exported = [f for f in os.listdir(root) if f.endswith(".json")]
    assert len(exported) == 1, exported
    new_home = touch(root, "elsewhere", "9r1.png")
    store, *_, msg = bad._import_prompts(
        os.path.join(root, exported[0]), {new_home: ["x"] * len(cfg)},
        [new_home], None, 2)
    assert store[new_home] == cfg, store
    assert "1 of 1" in msg, msg


# ── Hires stage: -adetailer inputs pending until a -hires/-edited sibling ──
with tempfile.TemporaryDirectory() as root:
    tests = os.path.join(root, "Commission 1 - A", "Tests")
    touch(tests, "1r1.png"); touch(tests, "1r1-adetailer.png")     # pending
    touch(tests, "2r1.png"); touch(tests, "2r1-adetailer.png")
    touch(tests, "2r1-adetailer-hires.png")                        # hires-fixed -> done
    touch(tests, "2r1-adetailer-base.png")                         # lanczos twin -> not an input
    touch(tests, "3r1.png"); touch(tests, "3r1-adetailer.png")
    touch(tests, "3r1-adetailer-edited.png")                       # edited past -> done
    touch(tests, "4r1.png")                                        # base only -> not ready yet
    touch(tests, "10r1.png"); touch(tests, "10r1-adetailer.jpg")   # pending; after 1r1, natural order
    touch(tests, "1r1-adetailer-1.png")                            # collision copy -> ignored
    touch(tests, "notes.txt")                                      # not an image -> ignored
    pending = [os.path.basename(p) for p in bhf._pending_adetailer(tests)]
    assert pending == ["1r1-adetailer.png", "10r1-adetailer.jpg"], pending

    # "Load Folder" takes every -adetailer input, done or not — but never an
    # output (-adetailer-hires/-base/-edited) or a collision copy.
    inputs = [os.path.basename(p) for p in bhf._adetailer_images(tests)]
    assert inputs == ["1r1-adetailer.png", "2r1-adetailer.png", "3r1-adetailer.png",
                      "10r1-adetailer.jpg"], inputs

    touch(root, "Commission 2 - B", "Tests", "1r1-adetailer.png")  # 1 pending
    done = os.path.join(root, "Commission 3 - C", "Tests")
    touch(done, "1r1-adetailer.png"); touch(done, "1r1-adetailer-hires.png")  # done -> hidden
    touch(root, "Commission 4 - D", "readme.txt")                  # no Tests dir -> hidden

    nested = os.path.join(root, "Commissions", "Commission 5 - E")
    touch(nested, "Tests", "1r1-adetailer.png")
    touch(nested, "9r1-adetailer.png")

    set_a = os.path.join(root, "Commission 1 - A")
    touch(set_a, "8r1-adetailer.png")      # + the 2 pending in its Tests -> 3
    touch(tests, "Finished", "7r1-adetailer.png")
    touch(root, "random", "junk-adetailer.png")
    set_b = os.path.join(root, "Commission 2 - B")   # pending only in Tests

    req = os.path.join(root, "Requests", "Request 1 - X")
    touch(req, "2r1.png"); touch(req, "2r1-adetailer.png")
    done_req = os.path.join(root, "Requests", "Request 2 - Y")
    touch(done_req, "1r1-adetailer.png")
    touch(done_req, "1r1-adetailer-hires.png")                     # done -> hidden

    touch(req, "1r1-adetailer.png"); touch(req, "1r2-adetailer.png")
    touch(req, "1r13-adetailer.png")
    touch(req, "flower2-adetailer.png")
    latest = [os.path.basename(p) for p in bhf._adetailer_images(req)]
    assert latest == ["1r13-adetailer.png", "2r1-adetailer.png",
                      "flower2-adetailer.png"], latest

    bshared.shared.opts.batch_hires_fix_scan_roots = root + ";" + os.path.join(root, "missing")
    choices = bshared.scan_test_folders(bhf.STAGE)
    assert [c[1] for c in choices] == [set_a, set_b, nested, req], choices
    assert "(3 to do)" in choices[0][0], choices
    assert "(1 to do)" in choices[1][0] and "(2 to do)" in choices[2][0], choices
    assert "(3 to do)" in choices[3][0], choices  # 1r13 + 2r1 + flower2

    a = touch(tests, "9r1-adetailer.png", data=b"AAA")
    b = touch(nested, "9r1-adetailer.png", data=b"BBB")
    cache = os.path.join(root, "gradio-cache")
    bshared.is_dragged_temp_copy = lambda p: p.startswith(cache)
    drops = [touch(cache, "h1", "9r1-adetailer.png", data=b"BBB"),  # -> b
             touch(cache, "h2", "9r1-adetailer.png", data=b"AAA"),  # -> a
             touch(cache, "h3", "nowhere-adetailer.png", data=b"C")]  # -> unresolved
    resolved, notes = bshared.resolve_dropped_paths(drops, bhf.STAGE)
    assert resolved == [b, a, drops[2]], resolved
    assert len(notes) == 1 and "1 dropped file(s) not found" in notes[0], notes
    assert "Batch Hires-Fix" in notes[0], notes


# ── LoRA tokens from other tools carry a subfolder and/or extension; Forge
# indexes by bare stem, so those must resolve to it. ──
class Net:
    def __init__(self, name): self.name = name
nets = MagicMock()
nets.available_networks = {"amiiari-anima-v5.3": Net("amiiari-anima-v5.3"),
                           "Dark_Slider_Anima": Net("Dark_Slider_Anima")}
nets.available_network_aliases = {}
assert bshared.resolve_lora_name("amiiari-anima-v5.3", nets) is None  # already fine
assert bshared.resolve_lora_name("amiiari-anima-v5.3.safetensors", nets) == "amiiari-anima-v5.3"
assert bshared.resolve_lora_name(r"Misc\Dark_Slider_Anima.safetensors", nets) == "Dark_Slider_Anima"
assert bshared.resolve_lora_name("gone.safetensors", nets) == ""


# ── Infotext inheritance: styles restored, Clip skip recovered from the raw
# text (parse always pops it), subseed strength / seed resize applied. ──
bshared.parse_generation_parameters = lambda text, skip: {
    "Prompt": "a prompt", "Negative prompt": "bad",
    "Styles array": ["My Style"],
    "Seed": "123", "Variation seed": "456",
    "Steps": "30", "CFG scale": "6.5",
    "Variation seed strength": "0.7",
    "Seed resize from-1": "640", "Seed resize from-2": "960",
    "Sampler": "Euler a", "Schedule type": "Karras",
}
p = SimpleNamespace(override_settings={})
bshared.apply_source_image_parameters(p, "a prompt\nSteps: 30, Clip skip: 2")
assert p.styles == ["My Style"], p.styles
assert p.steps == 30 and p.cfg_scale == 6.5
assert p.subseed_strength == 0.7, p.subseed_strength
assert (p.seed_resize_from_w, p.seed_resize_from_h) == (640, 960)
assert p.override_settings["CLIP_stop_at_last_layers"] == 2, p.override_settings
assert p.sampler_name == "Euler a" and p.scheduler == "Karras"


# ── Per-run cancel tokens: a cancel kills runs already started, never one
# started after (the old global flag was reset by any new run start). ──
AD, HR = bshared.ADETAILER_STAGE, bshared.HIRES_STAGE
t1 = bshared.start_run(AD)
assert not bshared.cancel_requested(AD, t1)
bshared.request_cancel(AD)
assert bshared.cancel_requested(AD, t1)
t2 = bshared.start_run(AD)
assert not bshared.cancel_requested(AD, t2)
assert bshared.cancel_requested(AD, t1)  # the old run stays cancelled

# ...and one tab's Cancel never stops the other tab's batch.
hr_run = bshared.start_run(HR)
bshared.request_cancel(AD)
assert not bshared.cancel_requested(HR, hr_run)
bshared.request_cancel(HR)
assert bshared.cancel_requested(HR, hr_run)

print("ok")
