"""実機チップノイズWAV（nsf2midi --chip-wav / vgm2midi --noise-wav）と
fluidsynthのレンダリング結果を純Python/標準ライブラリ（wave/audioop）で合成する。

外部バイナリ（ffmpeg等）に依存せず、16-bit PCM WAVのゲイン調整、区間切り出し、
および複数ステムの単純加算（amix同等）を高速・安全に行う。
"""

from __future__ import annotations

import array
import os
import wave
from collections.abc import Sequence
from pathlib import Path

from .errors import MixError
from .tooling import has_wave_audio

# NOISE/DPCM は非線形TNDミックステーブル（nsf2midi/third_party/NotSoFatso/Wave_TND.h）
# 上で単独レンダリングされるため、TRI/DMCが同時に鳴っている実機の音より本来の寄与が
# 大きく出る。両入力に固定のヘッドルームを与え、amix(normalize=0) の純加算で
# クリップしないようにする（0.80+0.55=1.35 が理論上の最悪値で、両方が同時に
# フルスケール付近でなければクリップしない）。
DRY_GAIN = 0.40
STEM_GAIN = 1.0
# ゲーム由来SoundFontレンダリングとGM SoundFontレンダリングを合成する場合のゲイン。
# この2つは「1つの編曲を互いに素なトラック集合へ分割したもの」であり、単純加算すれば
# 分割前の1回レンダリングと同じ音量になる。ステムのような「別枠で足す音」ではないので
# DRY_GAINのようなヘッドルームは取らない。
SPLIT_GAIN = 1.0


def build_filter_complex(gains: Sequence[float], sample_rate: int = 44100) -> str:
    """各入力に個別のゲインを掛けてから単純加算(amix)する-filter_complex文字列を作る（後方互換用）。"""
    if len(gains) < 2:
        raise MixError("ミックスには2つ以上の入力が必要です")
    parts = []
    labels = []
    for index, gain in enumerate(gains):
        label = f"g{index}"
        labels.append(label)
        parts.append(
            f"[{index}:a]aformat=sample_fmts=fltp:sample_rates={sample_rate}:channel_layouts=stereo,"
            f"volume={gain}[{label}]"
        )
    joined_labels = "".join(f"[{label}]" for label in labels)
    parts.append(
        f"{joined_labels}amix=inputs={len(gains)}:duration=longest:"
        f"dropout_transition=0:normalize=0[out]"
    )
    return ";".join(parts)


def resolve_ffmpeg_bin() -> str | None:
    """旧インターフェース互換スタブ（ネイティブミキサー移行に伴い不要）。"""
    return os.environ.get("FFMPEG_BIN")


def _read_pcm_wav(path: Path) -> tuple[bytes, int, int]:
    """WAVファイルを開き、(pcm_bytes, framerate, channels) を返す。16-bit PCMを保証する。"""
    if not path.is_file():
        raise MixError(f"WAVファイルが見つかりません: {path}")
    try:
        with wave.open(str(path), "rb") as w:
            channels = w.getnchannels()
            width = w.getsampwidth()
            framerate = w.getframerate()
            nframes = w.getnframes()
            data = w.readframes(nframes)
    except Exception as e:
        raise MixError(f"WAVファイルの読み込みに失敗しました ({path}): {e}") from e

    if width != 2:
        try:
            import audioop
            data = audioop.lin2lin(data, width, 2)
        except Exception as e:
            raise MixError(f"サポート外のサンプル幅です ({width} bytes): {path}") from e

    return data, framerate, channels


def _format_pcm(data: bytes, in_rate: int, in_channels: int, target_rate: int) -> bytes:
    """PCMデータを16-bit ステレオ・指定サンプルレートへ正規化する。"""
    try:
        import audioop
        has_audioop = True
    except ImportError:
        has_audioop = False

    # チャンネル数変換 (mono -> stereo)
    if in_channels == 1:
        if has_audioop:
            data = audioop.tostereo(data, 2, 1, 1)
        else:
            samples = array.array("h", data)
            stereo_samples = array.array("h")
            for s in samples:
                stereo_samples.append(s)
                stereo_samples.append(s)
            data = stereo_samples.tobytes()
        channels = 2
    elif in_channels == 2:
        channels = 2
    else:
        raise MixError(f"サポート外のチャンネル数です: {in_channels}")

    # サンプルレート変換
    if in_rate != target_rate:
        if has_audioop:
            data, _ = audioop.ratecv(data, 2, channels, in_rate, target_rate, None)
        else:
            samples = array.array("h", data)
            in_frames = len(samples) // 2
            out_frames = int(in_frames * target_rate / in_rate)
            out_samples = array.array("h", [0] * (out_frames * 2))
            ratio = in_rate / target_rate
            for i in range(out_frames):
                src_pos = i * ratio
                idx = int(src_pos)
                frac = src_pos - idx
                idx2 = min(idx + 1, in_frames - 1)
                for ch in range(2):
                    s1 = samples[idx * 2 + ch]
                    s2 = samples[idx2 * 2 + ch]
                    interp = int((1.0 - frac) * s1 + frac * s2)
                    out_samples[i * 2 + ch] = max(-32768, min(32767, interp))
            data = out_samples.tobytes()

    return data


def _apply_gain_to_bytes(data: bytes, gain: float) -> bytes:
    """16-bit PCMデータにゲインを乗算し、[-32768, 32767] でサチュレーションする。"""
    if gain == 1.0:
        return data
    try:
        import audioop
        return audioop.mul(data, 2, gain)
    except ImportError:
        samples = array.array("h", data)
        for i in range(len(samples)):
            samples[i] = max(-32768, min(32767, int(samples[i] * gain)))
        return samples.tobytes()


def _add_pcm_bytes(b1: bytes, b2: bytes) -> bytes:
    """2本の等長16-bit PCMデータを加算し、飽和クリッピングする。"""
    try:
        import audioop
        return audioop.add(b1, b2, 2)
    except ImportError:
        arr1 = array.array("h", b1)
        arr2 = array.array("h", b2)
        out = array.array("h", [0] * len(arr1))
        for i in range(len(arr1)):
            out[i] = max(-32768, min(32767, arr1[i] + arr2[i]))
        return out.tobytes()


def _write_pcm_wav(path: Path, data: bytes, sample_rate: int) -> None:
    """16-bit ステレオ PCM データをWAVファイルとして書き出す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp.wav")
    try:
        with wave.open(str(temp_path), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(data)
        temp_path.replace(path)
    except Exception as e:
        if temp_path.exists():
            temp_path.unlink()
        raise MixError(f"WAV書き出しに失敗しました ({path}): {e}") from e

    if not has_wave_audio(path):
        raise MixError(f"WAV書き出しに失敗しました（出力が空です）: {path}")


def apply_gain(
    input_wav: Path,
    out_wav: Path,
    gain: float,
    *,
    sample_rate: int = 44100,
) -> None:
    """input_wavへgainを掛けた結果をout_wavへ書く。失敗時は MixError。"""
    data, in_rate, in_ch = _read_pcm_wav(input_wav)
    formatted = _format_pcm(data, in_rate, in_ch, sample_rate)
    gained = _apply_gain_to_bytes(formatted, gain)
    _write_pcm_wav(out_wav, gained, sample_rate)


def trim_wav(
    input_wav: Path,
    out_wav: Path,
    start_seconds: float,
    duration_seconds: float,
    *,
    sample_rate: int = 44100,
) -> None:
    """input_wavの指定区間をWAVとして書き出す。失敗時は MixError。"""
    if start_seconds < 0 or duration_seconds <= 0:
        raise MixError("切り出し範囲は開始0秒以上・長さ0秒超で指定してください")

    data, in_rate, in_ch = _read_pcm_wav(input_wav)
    formatted = _format_pcm(data, in_rate, in_ch, sample_rate)
    bytes_per_frame = 4  # 16-bit stereo = 4 bytes per frame

    start_frame = int(start_seconds * sample_rate)
    duration_frames = int(duration_seconds * sample_rate)
    total_frames = len(formatted) // bytes_per_frame

    if start_frame >= total_frames:
        raise MixError("切り出し開始位置が音声の終端を超えています")

    end_frame = min(start_frame + duration_frames, total_frames)
    trimmed_data = formatted[start_frame * bytes_per_frame : end_frame * bytes_per_frame]

    if not trimmed_data:
        raise MixError("区間切り出し結果のWAV書き出しに失敗しました（出力が空です）")

    _write_pcm_wav(out_wav, trimmed_data, sample_rate)


def mix_wav(
    inputs: Sequence[tuple[Path, float]],
    out_wav: Path,
    *,
    sample_rate: int = 44100,
) -> None:
    """(WAVパス, ゲイン) の列を単純加算して out_wav に書き出す。失敗時は MixError。"""
    if len(inputs) < 2:
        raise MixError("ミックスには2つ以上の入力が必要です")

    processed: list[bytes] = []
    max_len = 0
    for path, gain in inputs:
        data, in_rate, in_ch = _read_pcm_wav(path)
        formatted = _format_pcm(data, in_rate, in_ch, sample_rate)
        gained = _apply_gain_to_bytes(formatted, gain)
        processed.append(gained)
        if len(gained) > max_len:
            max_len = len(gained)

    # duration=longest に従い、短いステムの末尾を無音（0バイト）でパディング
    padded = [p.ljust(max_len, b"\x00") for p in processed]

    # 単純加算 (normalize=0)
    result = padded[0]
    for nxt in padded[1:]:
        result = _add_pcm_bytes(result, nxt)

    _write_pcm_wav(out_wav, result, sample_rate)
