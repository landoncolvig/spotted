from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUST_SOURCE = ROOT / "app" / "src-tauri" / "src" / "lib.rs"
FRONTEND_SOURCE = ROOT / "app" / "src" / "main.ts"


def test_every_sidecar_launch_uses_the_hardened_command_builder() -> None:
    source = RUST_SOURCE.read_text()

    # Keep sidecar construction centralized. A raw launch elsewhere would
    # inherit macOS's external TMPDIR and reintroduce the PyInstaller failure.
    assert source.count('.sidecar("spotted-sidecar")') == 1
    assert "fn sidecar_command(app: &AppHandle)" in source
    assert source.count("sidecar_command(&app)?") == 5
    for variable in ("TMPDIR", "TEMP", "TMP"):
        assert f'.env("{variable}", &temp_dir)' in source


def test_pyinstaller_temp_failure_has_a_plain_language_explanation() -> None:
    source = FRONTEND_SOURCE.read_text()

    assert 'r.includes("could not create temporary directory")' in source
    assert "Spotted couldn't prepare its local working folder." in source
