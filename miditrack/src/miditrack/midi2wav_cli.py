"""Windows-compatible CLI replacement for midi2wav.sh.
Directly invokes fluidsynth to render MIDI to WAV.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .render import default_soundfont_dirs, list_soundfonts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render MIDI to WAV using FluidSynth")
    parser.add_argument("-f", "--force", action="store_true", help="Overwrite existing output file")
    parser.add_argument("-s", "--soundfont", type=str, default=None, help="SoundFont (.sf2/.sf3) path")
    parser.add_argument("-r", "--rate", type=int, default=44100, help="Sample rate in Hz")
    parser.add_argument("-o", "--output", type=str, default=None, help="Output WAV path")
    parser.add_argument("-g", "--gain", type=str, default=None, help="FluidSynth gain")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("input", type=str, help="Input MIDI file")

    args = parser.parse_args(argv)

    input_path = Path(args.input)
    if not input_path.is_file():
        sys.stderr.write(f"Input file not found: {input_path}\n")
        return 1

    output_path = Path(args.output) if args.output else input_path.with_suffix(".wav")
    if output_path.exists() and not args.force:
        sys.stderr.write(f"Output file already exists: {output_path}\n")
        return 1

    soundfont_path = args.soundfont
    if not soundfont_path:
        sf_list = list_soundfonts()
        if not sf_list:
            sys.stderr.write("No SoundFont found in standard directories.\n")
            return 1
        soundfont_path = sf_list[0]["path"]

    fluidsynth_bin = os.environ.get("FLUIDSYNTH_BIN") or shutil.which("fluidsynth") or "C:\\bin\\fluidsynth.exe"
    if not fluidsynth_bin or not (Path(fluidsynth_bin).is_file() or shutil.which(fluidsynth_bin)):
        sys.stderr.write(f"fluidsynth executable not found: {fluidsynth_bin}\n")
        return 1

    cmd = [
        str(fluidsynth_bin),
        "-ni",
    ]
    if not args.verbose:
        cmd.append("-q")
    cmd.extend([
        "-o",
        "synth.dynamic-sample-loading=1",
    ])
    gain_val = args.gain if args.gain is not None else "0.35"
    cmd.extend(["-g", str(gain_val)])
    cmd.extend([
        "-F",
        str(output_path),
        "-T",
        "wav",
        "-r",
        str(args.rate),
        str(soundfont_path),
        str(input_path),
    ])

    from .tooling import safe_subprocess_run
    result = safe_subprocess_run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        return result.returncode

    if not output_path.exists() or output_path.stat().st_size <= 44:
        sys.stderr.write(f"Failed to generate WAV audio: {output_path}\n")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
