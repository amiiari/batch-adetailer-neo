"""Self-check for the test-folder scan logic. Run: python test_scan.py
(Forge modules are stubbed out — this only exercises the pure filesystem code.)
"""
import os
import sys
import tempfile
from unittest.mock import MagicMock

for _m in ("gradio", "PIL", "modules", "modules.infotext_utils", "modules_forge"):
    sys.modules[_m] = MagicMock()

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import batch_adetailer as bad


def touch(*parts):
    path = os.path.join(*parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").close()


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

    touch(root, "Commission 2 - B", "Tests", "1r1.png")         # 1 pending
    done = os.path.join(root, "Commission 3 - C", "Tests")
    touch(done, "1r1.png"); touch(done, "1r1-adetailer.png")    # done -> hidden
    touch(root, "Commission 4 - D", "readme.txt")               # no Tests dir -> hidden

    bad.shared.opts.batch_adetailer_scan_roots = root + ";" + os.path.join(root, "missing")
    choices = bad._scan_test_folders()
    assert [c[1] for c in choices] == [tests, os.path.join(root, "Commission 2 - B", "Tests")], choices
    assert "(2 to do)" in choices[0][0] and "(1 to do)" in choices[1][0], choices

print("ok")
