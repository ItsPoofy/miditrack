"""Build standalone Windows executable package for VGMidi.

Creates dist/VGMidi containing:
- VGMidi.exe (native desktop GUI embedding WebView2)
- all web assets and backend logic
- bundled helpers: nsf2midi.exe, spc2midi.exe, vgm2midi, node.exe, fluidsynth.exe, ffmpeg.exe
- bundled SoundFont (GeneralUser-GS.sf2)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    script_dir = Path(__file__).resolve().parent
    repo_dir = script_dir.parent
    dist_dir = repo_dir / "dist"
    build_dir = repo_dir / "build" / "pyinstaller"

    print("=== Building VGMidi Windows Executable ===")

    # 1. Verify helpers
    nsf_bin = repo_dir / "nsf2midi" / "nsf2midi.exe"
    if not nsf_bin.is_file():
        sys.exit(f"Error: missing {nsf_bin}. Build it first.")

    spc_bin = repo_dir / "spc2midi" / "spc2midi.exe"
    if not spc_bin.is_file():
        cmake_spc = repo_dir / "spc2midi" / "build" / "Release" / "spc2midi.exe"
        if cmake_spc.is_file():
            shutil.copyfile(cmake_spc, spc_bin)

    vgm_cli = repo_dir / "vgm2midi" / "dist" / "cli.js"
    if not vgm_cli.is_file():
        sys.exit(f"Error: missing {vgm_cli}.")

    # 2. Run PyInstaller
    icon_path = repo_dir / "images" / "miditrack.ico"
    desktop_app_entry = repo_dir / "run_desktop.py"

    pyinstaller_args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name",
        "VGMidi",
        "--paths",
        str(repo_dir / "miditrack" / "src"),
        "--collect-data",
        "miditrack",
        "--collect-all",
        "webview",
        "--collect-all",
        "pythonnet",
        "--collect-all",
        "clr_loader",
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir / "work"),
        "--specpath",
        str(build_dir),
    ]

    if icon_path.is_file():
        pyinstaller_args.extend(["--icon", str(icon_path)])

    pyinstaller_args.append(str(desktop_app_entry))

    print("Running PyInstaller...")
    res = subprocess.run(pyinstaller_args, check=False)
    if res.returncode != 0:
        sys.exit(f"PyInstaller failed with code {res.returncode}")

    app_dist_dir = dist_dir / "VGMidi"
    print(f"PyInstaller finished. App directory: {app_dist_dir}")

    # 3. Assemble Helpers & Resources
    helpers_dir = app_dist_dir / "_internal" / "Helpers"
    helpers_dir.mkdir(parents=True, exist_ok=True)

    # NSF2MIDI
    nsf_bin = repo_dir / "nsf2midi" / "nsf2midi.exe"
    shutil.copyfile(nsf_bin, helpers_dir / "nsf2midi.exe")
    for mdf in ["gm.mdf", "default.mdf"]:
        mdf_src = repo_dir / "nsf2midi" / mdf
        if mdf_src.is_file():
            shutil.copyfile(mdf_src, helpers_dir / mdf)

    # SPC2MIDI
    if spc_bin.is_file():
        shutil.copyfile(spc_bin, helpers_dir / "spc2midi.exe")

    # Node.js & VGM2MIDI
    node_exe = shutil.which("node") or "C:\\Program Files\\nodejs\\node.exe"
    if Path(node_exe).is_file():
        shutil.copyfile(node_exe, helpers_dir / "node.exe")

    vgm_dest = helpers_dir / "vgm2midi"
    if vgm_dest.exists():
        shutil.rmtree(vgm_dest)
    shutil.copytree(
        repo_dir / "vgm2midi",
        vgm_dest,
        ignore=shutil.ignore_patterns(".git", "native", "src", "tests", "docs"),
    )

    # VGM2MIDI stems helper
    stems_exe = repo_dir / "vgm2midi" / "native" / "bin" / "vgm2midi_stems.exe"
    if stems_exe.is_file():
        shutil.copyfile(stems_exe, helpers_dir / "vgm2midi_stems.exe")
        native_bin = helpers_dir / "vgm2midi" / "native" / "bin"
        native_bin.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(stems_exe, native_bin / "vgm2midi_stems.exe")

    # FluidSynth
    for tool_name in ["fluidsynth.exe", "libfluidsynth-3.dll", "sndfile.dll", "SDL3.dll"]:
        tool_src = Path("C:/bin") / tool_name
        if tool_src.is_file():
            shutil.copyfile(tool_src, app_dist_dir / tool_name)
            shutil.copyfile(tool_src, helpers_dir / tool_name)

    # Copy SoundFont
    sf_src = Path("C:/Program Files/Image-Line/FL Studio 2026/Data/Patches/Soundfonts/GeneralUser-GS.sf2")
    if sf_src.is_file():
        sf_dest_dir = app_dist_dir / "soundfonts"
        sf_dest_dir.mkdir(exist_ok=True)
        shutil.copyfile(sf_src, sf_dest_dir / "GeneralUser-GS.sf2")

    # Ensure web_assets is directly available at _internal/miditrack/web_assets
    web_assets_src = repo_dir / "miditrack" / "src" / "miditrack" / "web_assets"
    web_assets_dest = app_dist_dir / "_internal" / "miditrack" / "web_assets"
    if web_assets_src.is_dir():
        if web_assets_dest.exists():
            shutil.rmtree(web_assets_dest)
        shutil.copytree(web_assets_src, web_assets_dest)

    downloads_dir = Path.home() / "Downloads" / "VGMidi"
    try:
        subprocess.run(["taskkill", "/F", "/IM", "VGMidi.exe"], capture_output=True)
        import time
        time.sleep(0.5)
    except Exception:
        pass
    if downloads_dir.exists():
        shutil.rmtree(downloads_dir)
    print(f"Deploying to {downloads_dir}...")
    shutil.copytree(app_dist_dir, downloads_dir)

    # Clean up redundant copies so there is ONLY 1 VGMidi.exe
    print("Cleaning up intermediate copies...")
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    if build_dir.exists():
        shutil.rmtree(build_dir)
    for exe_name in ["VGMidi.exe", "miditrack.exe"]:
        cli_exe = repo_dir / "miditrack" / ".venv" / "Scripts" / exe_name
        if cli_exe.is_file():
            try:
                cli_exe.unlink()
            except OSError:
                pass

    print("\n[+] Standalone Windows app successfully installed at:")
    print(f"  {downloads_dir / 'VGMidi.exe'}\n")


if __name__ == "__main__":
    main()
