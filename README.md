# miditrack

miditrack is a program that lets you convert retro video game music to MIDI and preview it.

This is a fork of [Nihondo/miditrack](https://github.com/Nihondo/miditrack) with full Windows compatibility, quality-of-life improvements, and conversion accuracy fixes.

## Features

- Converts NES (`.nsf`/`.nsfe`), SNES (`.spc`/`.spc2`), and VGM/VGZ (`.vgm`/`.vgz`) files to MIDI.
- Built-in interactive piano roll and multi-track audio auditioning.
- Automatic tempo and beat detection from rhythm events and hardware frame intervals.
- Musical grid alignment and hardware sound driver startup latency compensation.
- Sample-accurate audio stem synchronization for FM, DAC, and noise channels.
- Fast previewing and instrument switching with dynamic SoundFont sample loading.
- Native Windows desktop application with standalone executable bundling.

## Setup and Build (Windows)

### Prerequisites

- Python 3.10+
- Node.js 18+
- [FluidSynth](https://www.fluidsynth.org/) (ensure `fluidsynth.exe` is in your PATH or installed to `C:\bin\fluidsynth.exe`)

### Running from Source

1. Clone the repository:
   ```powershell
   git clone https://github.com/ItsPoofy/miditrack.git
   cd miditrack
   ```

2. Build the `vgm2midi` engine:
   ```powershell
   cd vgm2midi
   npm install
   npm run build
   cd ..
   ```

3. Launch the desktop application:
   ```powershell
   uv run run_desktop.py
   # or with standard python:
   python -m pip install -e ./miditrack
   python run_desktop.py
   ```

### Building the Standalone Executable

To bundle miditrack into a standalone Windows folder with `miditrack.exe`:

```powershell
python scripts/build_windows_exe.py
```

The output package will be generated at `dist/miditrack/`.

## Supported Formats

| Tool | Format | Output | Description |
|---|---|---|---|
| **vgm2midi** | Sega Genesis/MD, Arcade, SMS, GG, PC Engine (`.vgm`, `.vgz`) | Standard MIDI (`.mid`) + WAV stems | Auto tempo detection, startup preroll compensation, synchronized DAC/noise audio |
| **spc2midi** | Super Nintendo / Super Famicom (`.spc`, `.spc2`) | Standard MIDI (`.mid`) + Game SoundFont (`.sf2`) | Native MSVC build support, instrument envelope extraction |
| **nsf2midi** | NES / Famicom (`.nsf`, `.nsfe`) | Standard MIDI (`.mid`) | Native Windows support, multi-track export, MDF presets |
| **miditrack** | All formats above and `.mid` files | Editable project, stems ZIP, master WAV | Desktop window, low-latency auditioning, interactive piano roll |

## Command-Line Usage

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

# Fast MIDI-to-WAV rendering
python -m miditrack.midi2wav_cli -s SoundFont.sf2 song.mid -o song.wav
```

## Credits and License

- Original project by [Nihondo](https://github.com/Nihondo/miditrack) (MIT License).
- NES emulation core powered by [NotSoFatso](https://github.com/BleuBleu/FamiStudio).
- SNES sequence parsing powered by [VGMTrans](https://github.com/vgmtrans/vgmtrans).
- SoundFont synthesis powered by [FluidSynth](https://www.fluidsynth.org/).
- Real-time time stretching powered by [Rubber Band Library](https://breakfastquay.com/rubberband/).
