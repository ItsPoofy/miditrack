"""miditrack内の midi2wav.sh を安全に呼び出し、.mid を .wav にレンダリングする。

このリポジトリのパス自体が "Chill & Relax GAME MUSIC" のようにスペースと '&' を
含むため、シェル経由の実行（シェル文字列の組み立て、shell=True）は確実に壊れる。
subprocess.run() に明示的なargvリストを shell=False で渡し、シェルを一切介さない。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .errors import RenderError
from .tooling import has_wave_audio, is_executable_file, stderr_tail

RENDER_TIMEOUT_SECONDS = 300
_STDERR_TAIL_LINES = 20
_SOUNDFONT_EXTENSIONS = (".sf2", ".sf3")


def default_soundfont_dirs() -> list[Path]:
    """midi2wav.sh の DEFAULT_SOUNDFONT_DIRS と同じ探索順を返す（同じディレクトリ・同じ順序）。"""
    dirs = [
        Path.home() / "Library/Audio/Sounds/Banks",
        Path("/Library/Audio/Sounds/Banks"),
        Path("/opt/homebrew/share/soundfonts"),
        Path("/usr/local/share/soundfonts"),
        Path("/opt/homebrew/share/fluid-synth/sf2"),
        Path("/usr/local/share/fluid-synth/sf2"),
    ]
    if os.name == "nt":
        user_profile = Path(os.environ.get("USERPROFILE", Path.home()))
        appdata = Path(os.environ.get("APPDATA", user_profile / "AppData" / "Roaming"))
        dirs.extend([
            user_profile / "SoundFonts",
            user_profile / "Sounds" / "Banks",
            appdata / "miditrack" / "soundfonts",
            Path("C:/Program Files/Image-Line/FL Studio 2026/Data/Patches/Soundfonts"),
            Path("C:/bin"),
        ])
    return dirs


def list_soundfonts(dirs: list[Path] | None = None) -> list[dict]:
    """探索ディレクトリ群から見つかる SoundFont (*.sf2/*.sf3) を一覧化する。

    midi2wav.sh の -S（対話選択）と同じ「ディレクトリ順、ディレクトリ内はファイル名順」
    で列挙する。存在しないディレクトリ（未マウントの外付けSSD等）は黙ってスキップする。

    シンボリックリンクはたどるが、実体（resolve()した絶対パス）が同じものは
    最初に見つかった1件のみを残し重複を除く。midi2wav.sh の
    collect_available_soundfonts() と同じ挙動。
    """
    search_dirs = dirs if dirs is not None else default_soundfont_dirs()
    results: list[dict] = []
    seen_real_paths: set[Path] = set()
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        matches = [
            p
            for p in sorted(directory.iterdir())
            if p.is_file() and p.suffix.lower() in _SOUNDFONT_EXTENSIONS
        ]
        for path in matches:
            real_path = path.resolve()
            if real_path in seen_real_paths:
                continue
            seen_real_paths.add(real_path)
            results.append(
                {
                    "path": str(path),
                    "name": path.name,
                    "dir": str(directory),
                    "sizeBytes": path.stat().st_size,
                }
            )
    return results


def resolve_midi2wav_bin() -> str:
    """midi2wav.sh の実行体を解決する。

    解決順:
      1. MIDI2WAV_BIN 環境変数 -- 設定されているのに実行できなければ致命的エラー
         （フォールバックしない）
      2. このファイルから見たmiditrackディレクトリの midi2wav.sh
         （src/miditrack/render.py から2階層上）
      3. PATH上の "midi2wav"（subprocessが自前でPATH解決するので、素のコマンド名を返す）
    """
    env_bin = os.environ.get("MIDI2WAV_BIN")
    if env_bin:
        if not is_executable_file(env_bin):
            raise RenderError(f"MIDI2WAV_BIN が実行可能ファイルではありません: {env_bin}")
        return env_bin

    # src/miditrack/render.py -> src/miditrack -> src -> miditrack
    miditrack_root = Path(__file__).resolve().parents[2]
    sibling = miditrack_root / "midi2wav.sh"
    if is_executable_file(sibling):
        return str(sibling)

    return "midi2wav"


def is_soundfont_file(path: Path) -> bool:
    """path が実在する .sf2/.sf3 ファイルかどうかを返す。"""
    return path.is_file() and path.suffix.lower() in _SOUNDFONT_EXTENSIONS


def render_wav(
    midi_path: Path,
    wav_path: Path,
    soundfont: Path | None = None,
    *,
    sample_rate: int = 44100,
) -> None:
    """midi_pathを指定サンプルレートのWAVへレンダリングする。失敗時はRenderError。"""
    bin_path = resolve_midi2wav_bin()

    argv = [bin_path, "-f"]
    if soundfont:
        argv += ["-s", str(soundfont)]
    argv += ["-r", str(sample_rate), "-o", str(wav_path), str(midi_path)]

    if os.name == "nt":
        # Windows: directly run fluidsynth without bash script or python -m
        fluidsynth_bin = os.environ.get("FLUIDSYNTH_BIN") or shutil.which("fluidsynth")
        if not fluidsynth_bin and getattr(sys, "frozen", False):
            helpers = Path(sys.executable).parent / "_internal" / "Helpers"
            if (helpers / "fluidsynth.exe").is_file():
                fluidsynth_bin = str(helpers / "fluidsynth.exe")
            elif (Path(sys.executable).parent / "fluidsynth.exe").is_file():
                fluidsynth_bin = str(Path(sys.executable).parent / "fluidsynth.exe")
        if not fluidsynth_bin and Path("C:/bin/fluidsynth.exe").is_file():
            fluidsynth_bin = "C:/bin/fluidsynth.exe"

        sf = soundfont
        if not sf:
            sf_list = list_soundfonts()
            if sf_list:
                sf = Path(sf_list[0]["path"])

        if not fluidsynth_bin or not sf:
            raise RenderError("fluidsynth or soundfont not available on Windows")

        gain = os.environ.get("MIDITRACK_FLUIDSYNTH_GAIN", "1.0")
        cmd = [
            str(fluidsynth_bin),
            "-ni",
            "-q",
            "-o", "synth.dynamic-sample-loading=1",
            "-o", "synth.cpu-cores=4",
            "-g", str(gain),
            "-F", str(wav_path),
            "-T", "wav",
            "-r", str(sample_rate),
            str(sf),
            str(midi_path),
        ]
        try:
            from .tooling import safe_subprocess_run
            res = safe_subprocess_run(cmd, capture_output=True, text=True, timeout=RENDER_TIMEOUT_SECONDS)
            if res.returncode != 0:
                tail = stderr_tail(res.stderr, _STDERR_TAIL_LINES)
                raise RenderError(f"fluidsynth error:\n{tail}")
        except subprocess.TimeoutExpired as error:
            raise RenderError(f"Rendering timed out after {RENDER_TIMEOUT_SECONDS}s") from error

        if not has_wave_audio(wav_path):
            raise RenderError("Failed to write WAV output")
        return

    try:
        result = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=RENDER_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise RenderError(
            f"midi2wav が見つかりません（{bin_path}）。MIDI2WAV_BIN 環境変数か "
            "PATH 上の midi2wav を確認してください"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise RenderError(
            f"midi2wav のレンダリングが {RENDER_TIMEOUT_SECONDS} 秒でタイムアウトしました"
        ) from error

    if result.returncode != 0:
        tail = stderr_tail(result.stderr, _STDERR_TAIL_LINES)
        raise RenderError(f"midi2wav の実行に失敗しました（exit={result.returncode}）:\n{tail}")

    if not has_wave_audio(wav_path):
        raise RenderError("WAVの書き出しに失敗しました（出力が空です）")
