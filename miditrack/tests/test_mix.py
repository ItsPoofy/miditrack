"""miditrack.mix のテスト。

ネイティブWAVミキサー（16-bit PCM WAVのゲイン調整、区間切り出し、複数ステム加算）の
正確性、エラー処理、および境界条件を実WAVデータを用いて検証する。
"""

from __future__ import annotations

import struct
import tempfile
import unittest
import wave
from pathlib import Path

from miditrack import mix
from miditrack.errors import MixError


def create_test_wav(
    path: Path,
    *,
    channels: int = 2,
    sample_rate: int = 44100,
    duration_seconds: float = 1.0,
    sample_value: int = 1000,
) -> Path:
    """指定仕様の16-bit PCM WAVファイルを生成する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    nframes = int(sample_rate * duration_seconds)
    frame = struct.pack("<" + "h" * channels, *([sample_value] * channels))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(frame * nframes)
    return path


def read_test_wav_samples(path: Path) -> tuple[list[tuple[int, ...]], int, int]:
    """WAVファイルを読み込み、(frames_samples, sample_rate, channels) を返す。"""
    with wave.open(str(path), "rb") as w:
        channels = w.getnchannels()
        rate = w.getframerate()
        nframes = w.getnframes()
        data = w.readframes(nframes)
    total_samples = nframes * channels
    raw_samples = struct.unpack("<" + "h" * total_samples, data)
    frames = [
        raw_samples[i * channels : (i + 1) * channels]
        for i in range(nframes)
    ]
    return frames, rate, channels


class TestMixWav(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # スペースと '&' を含むパスの正常動作を検証
        self.dry_path = Path(self.tmp.name) / "a & b.dry.wav"
        create_test_wav(self.dry_path, duration_seconds=1.0, sample_value=10000)
        self.stem_path = Path(self.tmp.name) / "a & b.chip.wav"
        create_test_wav(self.stem_path, duration_seconds=1.0, sample_value=5000)
        self.out_path = Path(self.tmp.name) / "a & b.out.wav"

    def test_two_inputs_scaled_and_summed(self) -> None:
        mix.mix_wav([(self.dry_path, 0.80), (self.stem_path, 0.55)], self.out_path)
        frames, rate, channels = read_test_wav_samples(self.out_path)
        self.assertEqual(rate, 44100)
        self.assertEqual(channels, 2)
        self.assertEqual(len(frames), 44100)
        # 10000 * 0.80 + 5000 * 0.55 = 8000 + 2750 = 10750
        first_frame = frames[0]
        self.assertEqual(first_frame, (10750, 10750))

    def test_longest_duration_pads_with_silence(self) -> None:
        short_path = Path(self.tmp.name) / "short.wav"
        create_test_wav(short_path, duration_seconds=1.0, sample_value=10000)
        long_path = Path(self.tmp.name) / "long.wav"
        create_test_wav(long_path, duration_seconds=2.0, sample_value=4000)

        mix.mix_wav([(short_path, 0.8), (long_path, 0.5)], self.out_path)
        frames, rate, channels = read_test_wav_samples(self.out_path)
        self.assertEqual(len(frames), 44100 * 2)

        # 0.5秒地点: 両方の合成 (10000*0.8 + 4000*0.5 = 8000 + 2000 = 10000)
        mid_idx = int(0.5 * 44100)
        self.assertEqual(frames[mid_idx], (10000, 10000))

        # 1.5秒地点: shortが終わりlongのみ (4000*0.5 = 2000)
        late_idx = int(1.5 * 44100)
        self.assertEqual(frames[late_idx], (2000, 2000))

    def test_three_inputs_mix(self) -> None:
        third_path = Path(self.tmp.name) / "third.wav"
        create_test_wav(third_path, duration_seconds=1.0, sample_value=2000)
        mix.mix_wav(
            [(self.dry_path, 0.80), (self.stem_path, 0.55), (third_path, 1.0)],
            self.out_path,
        )
        frames, _, _ = read_test_wav_samples(self.out_path)
        # 8000 + 2750 + 2000 = 12750
        self.assertEqual(frames[0], (12750, 12750))

    def test_mono_input_converted_to_stereo(self) -> None:
        mono_path = Path(self.tmp.name) / "mono.wav"
        create_test_wav(mono_path, channels=1, duration_seconds=1.0, sample_value=3000)
        mix.mix_wav([(self.dry_path, 0.8), (mono_path, 1.0)], self.out_path)
        frames, rate, channels = read_test_wav_samples(self.out_path)
        self.assertEqual(channels, 2)
        # 10000*0.8 + 3000 = 11000
        self.assertEqual(frames[0], (11000, 11000))

    def test_sample_rate_conversion(self) -> None:
        wav_22k = Path(self.tmp.name) / "stem_22k.wav"
        create_test_wav(wav_22k, sample_rate=22050, duration_seconds=1.0, sample_value=4000)
        mix.mix_wav([(self.dry_path, 0.8), (wav_22k, 0.5)], self.out_path, sample_rate=22050)
        frames, rate, _ = read_test_wav_samples(self.out_path)
        self.assertEqual(rate, 22050)
        self.assertEqual(len(frames), 22050)

    def test_single_input_raises_mix_error(self) -> None:
        with self.assertRaises(MixError):
            mix.mix_wav([(self.dry_path, 1.0)], self.out_path)

    def test_missing_input_raises_mix_error(self) -> None:
        missing = Path(self.tmp.name) / "nonexistent.wav"
        with self.assertRaises(MixError):
            mix.mix_wav([(self.dry_path, 0.8), (missing, 0.5)], self.out_path)


class TestApplyGain(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.in_path = Path(self.tmp.name) / "a & b.in.wav"
        create_test_wav(self.in_path, duration_seconds=1.0, sample_value=10000)
        self.out_path = Path(self.tmp.name) / "a & b.out.wav"

    def test_gain_applied_correctly(self) -> None:
        mix.apply_gain(self.in_path, self.out_path, 0.55)
        frames, rate, channels = read_test_wav_samples(self.out_path)
        self.assertEqual(rate, 44100)
        self.assertEqual(channels, 2)
        # 10000 * 0.55 = 5500
        self.assertEqual(frames[0], (5500, 5500))

    def test_sample_rate_resampling(self) -> None:
        mix.apply_gain(self.in_path, self.out_path, 1.0, sample_rate=22050)
        frames, rate, _ = read_test_wav_samples(self.out_path)
        self.assertEqual(rate, 22050)
        self.assertEqual(len(frames), 22050)

    def test_missing_file_raises_mix_error(self) -> None:
        missing = Path(self.tmp.name) / "does_not_exist.wav"
        with self.assertRaises(MixError):
            mix.apply_gain(missing, self.out_path, 0.55)


class TestTrimWav(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.in_path = Path(self.tmp.name) / "a & b.in.wav"
        create_test_wav(self.in_path, duration_seconds=5.0, sample_value=6000)
        self.out_path = Path(self.tmp.name) / "a & b.out.wav"

    def test_trim_slice(self) -> None:
        mix.trim_wav(self.in_path, self.out_path, 1.0, 2.0, sample_rate=44100)
        frames, rate, channels = read_test_wav_samples(self.out_path)
        self.assertEqual(rate, 44100)
        self.assertEqual(channels, 2)
        self.assertEqual(len(frames), 44100 * 2)
        self.assertEqual(frames[0], (6000, 6000))

    def test_trim_with_sample_rate_conversion(self) -> None:
        mix.trim_wav(self.in_path, self.out_path, 1.0, 2.0, sample_rate=22050)
        frames, rate, _ = read_test_wav_samples(self.out_path)
        self.assertEqual(rate, 22050)
        self.assertEqual(len(frames), 22050 * 2)

    def test_rejects_invalid_range(self) -> None:
        with self.assertRaises(MixError):
            mix.trim_wav(self.in_path, self.out_path, -0.1, 1.0)
        with self.assertRaises(MixError):
            mix.trim_wav(self.in_path, self.out_path, 0.0, 0.0)

    def test_start_beyond_eof_raises_mix_error(self) -> None:
        with self.assertRaises(MixError):
            mix.trim_wav(self.in_path, self.out_path, 10.0, 1.0)


class TestBuildFilterComplex(unittest.TestCase):
    def test_raises_for_fewer_than_two_gains(self) -> None:
        with self.assertRaises(MixError):
            mix.build_filter_complex([1.0])

    def test_labels_are_indexed_by_input_order(self) -> None:
        filter_str = mix.build_filter_complex([0.8, 0.55, 1.0])
        self.assertIn("[0:a]", filter_str)
        self.assertIn("[1:a]", filter_str)
        self.assertIn("[2:a]", filter_str)
        self.assertIn("amix=inputs=3", filter_str)


if __name__ == "__main__":
    unittest.main()
