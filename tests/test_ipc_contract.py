"""The IPC checker itself needs tests: it is the only thing standing between a
Rust signature change and a runtime TypeError the type checker cannot see."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "check_ipc", ROOT / "scripts" / "check_ipc_contract.py"
)
ipc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ipc)


def test_parses_return_type_and_skips_injected_params():
    cmds = ipc.rust_commands('''
        #[tauri::command]
        async fn start_label_server(
            app: AppHandle,
            window: Window,
            port: u16,
            scope_paths: Option<Vec<String>>,
        ) -> Result<String, String> { }
    ''')
    c = cmds["start_label_server"]
    assert c["ret"] == "String"
    assert c["params"] == ["port", "scope_paths"]      # app/window injected
    assert "scope_paths" in c["optional"]              # Option<> is not required


def test_catches_the_v0_0_62_regression():
    """Rust returns the port; the frontend reads it as a URL string."""
    assert not ipc.compatible("u16", "string")
    assert ipc.compatible("String", "string")
    assert ipc.compatible("i32", "number")
    assert ipc.compatible("Option<String>", "string | null")
    assert ipc.compatible("Vec<String>", "string[]")
    # an untyped invoke asserts nothing, so it cannot be wrong
    assert ipc.compatible("u16", "unknown")


def test_reads_object_literal_keys_in_every_form():
    sites = ipc.ts_call_sites('''
        await invoke<number>("tag_videos", { excludeTags, overwrite: x, scope: p ?? null });
        await invoke("set_window_title", { title });
        await invoke("rename_person", { old, new: newName });
        await invoke<string>("fetch_status");
    ''')
    by = {s["name"]: s for s in sites}
    # shorthand, renamed, and `?? null` values must not be mistaken for keys
    assert by["tag_videos"]["keys"] == ["excludeTags", "overwrite", "scope"]
    assert by["set_window_title"]["keys"] == ["title"]
    assert by["rename_person"]["keys"] == ["old", "new"]
    assert by["fetch_status"]["keys"] == []
    assert by["fetch_status"]["declared"] == "string"


def test_the_real_repo_is_currently_consistent():
    assert ipc.main() == 0, "frontend and Rust commands disagree; see output above"
