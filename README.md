# miditrack (Fork)

> A modern chiptune-to-MIDI workstation for Windows, macOS, and Linux.

This is a fork of [Nihondo/miditrack](https://github.com/Nihondo/miditrack) with **full Windows compatibility**, **native desktop app support**, **automatic tempo detection**, **musical grid alignment**, and major audio engine QOL improvements.

Convert NES (`.nsf`/`.nsfe`), SNES (`.spc`/`.spc2`), and VGM/VGZ (`.vgm`/`.vgz`) into clean, editable MIDI with synchronized audio stems and an interactive piano roll.

---

## ⚡ What's New in this Fork

- **Windows Desktop App & Standalone Executable**:
  - Run natively on Windows via embedded WebView2 desktop GUI (`run_desktop.py`).
  - Automated build script (`scripts/build_windows_exe.py`) packages everything into a standalone `miditrack.exe` with bundled helper binaries and default SoundFont.
  - Native Windows CLI replacement for MIDI-to-WAV rendering (`midi2wav_cli.py`).
- **Auto-Tempo & Beat Detection**:
  - Intelligent BPM detection algorithm analyzing note onsets, rhythm events, and VGM frame deltas (`tempo-detect.ts`).
  - No more manual tempo guesswork or off-tempo imports.
- **Musical Grid Alignment & Hardware Preroll Compensation**:
  - Compensates for retro sound engine driver startup latency (e.g. Genesis/YM2612 initialization delays).
  - Downbeats land accurately on Bar 1 / Tick 0 rather than being locked into awkward off-grid fractions.
- **Sample-Accurate Stem Synchronization**:
  - Synced raw DAC and noise channel audio stems with FluidSynth rendered output.
  - Exported per-track stems align sample-accurately without phase drift.
- **Instant SoundFont Switching & Auditioning**:
  - Dynamic sample loading enabled by default in FluidSynth, eliminating sluggish loading freezes when previewing or switching SoundFonts while preserving full 44.1 kHz audio quality.
- **Cross-Platform Converter Portability**:
  - Native Windows compilation fixes for `nsf2midi` (Windows module path detection) and `spc2midi` (MSVC build compatibility).

---

## 🚀 Quick Start

### Windows

#### 1. Run from Source
**Prerequisites**: Python 3.10+, Node.js 18+, and [FluidSynth](https://www.fluidsynth.org/) (ensure `fluidsynth.exe` is in your PATH or installed to `C:\bin\fluidsynth.exe`).

```powershell
# Clone the repository
git clone https://github.com/ItsPoofy/miditrack.git
cd miditrack

# Build vgm2midi
cd vgm2midi
npm install
npm run build
cd ..

# Launch the desktop app
uv run run_desktop.py
# or with standard python:
python -m pip install -e ./miditrack
python run_desktop.py
```

#### 2. Build the Standalone Windows Executable
To bundle `miditrack` into a single, standalone Windows folder with `miditrack.exe`:
```powershell
python scripts/build_windows_exe.py
```
The output package will be generated at `dist/miditrack/`.

---

### macOS & Linux

**Prerequisites**: Python 3.10+, Node.js 18+, FluidSynth, and ffmpeg (`brew install fluid-synth ffmpeg rubberband uv`).

```bash
# Clone the repository
git clone https://github.com/ItsPoofy/miditrack.git
cd miditrack

# Build vgm2midi
cd vgm2midi
npm install
npm run build
cd ..

# Launch the server
cd miditrack
uv run miditrack
```

---

## 🎹 Supported Formats & Toolkit

| Tool | Format | Typical Output | Key Enhancements in this Fork |
|---|---|---|---|
| **vgm2midi** | Genesis/MD, Arcade, SMS, GG, PC Engine (`.vgm`, `.vgz`) | Standard MIDI (`.mid`) + WAV stems | Auto tempo detection, startup preroll compensation, synchronized DAC/noise audio |
| **spc2midi** | Super Nintendo / Super Famicom (`.spc`, `.spc2`) | Standard MIDI (`.mid`) + Game SoundFont (`.sf2`) | Native MSVC Windows build support, accurate instrument envelope extraction |
| **nsf2midi** | NES / Famicom (`.nsf`, `.nsfe`) | Standard MIDI (`.mid`) | Native Windows executable path resolution, MDF instrument presets |
| **miditrack** | All formats above & `.mid` files | Editable project, stems ZIP, master WAV | Native desktop window, low-latency auditioning, responsive piano roll |

---

## 🛠️ CLI Usage

Converters can also be run directly from the command line:

```bash
# VGM/VGZ conversion with auto-tempo detection
vgm2midi song.vgz --auto-tempo

# Disable grid snapping or preroll trimming if needed
vgm2midi song.vgz --no-snap --no-preroll-trim

# NES conversion
nsf2midi song.nsf song.mid

# SNES conversion with SoundFont extraction
spc2midi song.spc song.mid --sf2

# Fast MIDI-to-WAV rendering (Windows & POSIX)
python -m miditrack.midi2wav_cli -s GeneralUser-GS.sf2 song.mid -o song.wav
```

---

## 📦 SoundFonts

Place any General MIDI `.sf2` or `.sf3` SoundFont into one of the standard search directories, or select it directly from the UI:
- **Windows**: `%USERPROFILE%\SoundFonts`, `C:\SoundFonts`, or inside `soundfonts/`
- **macOS**: `~/Library/Audio/Sounds/Banks` or `/Library/Audio/Sounds/Banks`
- **Linux**: `/usr/share/soundfonts` or `/usr/local/share/soundfonts`

---

## 📜 Credits & License

- Original project by [Nihondo](https://github.com/Nihondo/miditrack) (MIT License).
- NES emulation core powered by [NotSoFatso](https://github.com/BleuBleu/FamiStudio).
- SNES sequence parsing powered by [VGMTrans](https://github.com/vgmtrans/vgmtrans).
- SoundFont synthesis powered by [FluidSynth](https://www.fluidsynth.org/).
- Real-time time stretching powered by [Rubber Band Library](https://breakfastquay.com/rubberband/).
