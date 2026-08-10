"""Ship the models that get loaded, and only those.

The buffalo_l pack has five models and `detect.py` loads two of them:
FaceAnalysis is constructed with `allowed_modules=["detection", "recognition"]`,
so the other three were downloaded by every user and then skipped at load with
"model ignore".

    1k3d68.onnx     143.6 MB   landmark_3d_68    never loaded
    2d106det.onnx     5.0 MB   landmark_2d_106   never loaded
    genderage.onnx    1.3 MB   genderage         never loaded

That is roughly 150 MB of a ~682 MB download, for nothing. v0.0.41 stopped
those models being LOADED (the 3D-landmark one was crashing on a missing
meanshape_68.pkl); it did not stop them being SHIPPED.

The risk in cutting them is the reverse mistake: adding a module to
allowed_modules later and not adding its model, so the sidecar asks for
something that is not in the bundle. These tests fail if the two lists drift.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = (ROOT / "sidecar/spotted_sidecar.spec").read_text()
DETECT = (ROOT / "facetag/detect.py").read_text()
BUILD = (ROOT / "sidecar/build.sh").read_text()

# Which file backs which InsightFace module in the buffalo_l pack. Nothing in
# either source states this, so it lives here, next to the check that uses it.
MODULE_MODEL = {
    "detection": "det_10g.onnx",
    "recognition": "w600k_r50.onnx",
    "landmark_3d_68": "1k3d68.onnx",
    "landmark_2d_106": "2d106det.onnx",
    "genderage": "genderage.onnx",
}


def _bundled() -> list[str]:
    m = re.search(r"BUNDLED_INSIGHTFACE_MODELS = \[(.*?)\]", SPEC, re.S)
    assert m, "BUNDLED_INSIGHTFACE_MODELS not found in the spec"
    return re.findall(r'"([^"]+)"', m.group(1))


def _allowed() -> list[str]:
    m = re.search(r'"allowed_modules":\s*\[(.*?)\]', DETECT, re.S)
    assert m, "allowed_modules not found in detect.py"
    return re.findall(r'"([^"]+)"', m.group(1))


def test_every_module_the_app_asks_for_is_in_the_bundle():
    """The failure this prevents is a sidecar that starts and then cannot
    detect a face, which the release gate would catch but only after a build."""
    missing = [
        MODULE_MODEL[mod] for mod in _allowed()
        if MODULE_MODEL.get(mod) and MODULE_MODEL[mod] not in _bundled()
    ]
    assert not missing, f"allowed_modules needs these models bundled: {missing}"


def test_nothing_is_bundled_that_is_never_loaded():
    """The point of the diet. A model here that no allowed module uses is
    ~150 MB of download doing nothing."""
    needed = {MODULE_MODEL[m] for m in _allowed() if m in MODULE_MODEL}
    extra = [f for f in _bundled() if f not in needed]
    assert not extra, f"bundled but never loaded: {extra}"


def test_the_two_known_loads_are_actually_the_ones_shipped():
    """Pins the current answer, so a change to either list is deliberate."""
    assert sorted(_allowed()) == ["detection", "recognition"]
    assert sorted(_bundled()) == ["det_10g.onnx", "w600k_r50.onnx"]


def test_the_whole_model_directory_is_not_bundled_wholesale():
    """It used to be one datas entry for the entire buffalo_l folder, which is
    how three unused models reached every user."""
    assert '"insightface_root/models/buffalo_l"),' not in SPEC.replace(
        '"insightface_root/models/buffalo_l",', ""
    ) or "BUNDLED_INSIGHTFACE_MODELS" in SPEC
    assert "for _m in BUNDLED_INSIGHTFACE_MODELS" in SPEC


def test_the_build_refuses_to_proceed_without_the_required_models():
    """A partial fetch would otherwise become a PyInstaller path error, or a
    sidecar that runs and cannot see faces."""
    for model in ("det_10g.onnx", "w600k_r50.onnx"):
        assert model in BUILD, f"build.sh does not check for {model}"
    assert "exit 1" in BUILD[BUILD.index("det_10g.onnx"):]


def test_transformers_is_still_gone():
    """The ROADMAP's stated diet — drop transformers for a slim tokenizer —
    was already done. If it comes back, ~50 MB comes with it."""
    assert "collect_all(\"transformers\")" not in SPEC
    assert "clip_tokenizer" in (ROOT / "facetag/clip.py").read_text()
