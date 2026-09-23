"""libvgmトラックsidecarと選択レンダラの単体テスト。"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from miditrack import libvgm
from miditrack.errors import WebValidationError


class TestLibvgmMetadata(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "converted.libvgm.json"

    def _write(self) -> None:
        self.path.write_text(json.dumps({
            "version": 1,
            "sampleCount": 44100,
            "tracks": [
                {"trackIndex": 0, "libvgm": {
                    "deviceType": 2, "instance": 0, "mainMask": 64,
                    "linkedMask": 0, "groupId": "2:0:64:0",
                    "suggestedForHardwareMix": True,
                }, "fm": {
                    "model": "opn", "algorithm": 5,
                    "carrierOperators": [1, 2, 3], "suggestedProgram": 62,
                }, "pcm": {
                    "source": "ym2612-dac", "sampleId": "001234", "gmNote": 35,
                    "dataBlock": {
                        "bankType": 0, "bankInstance": 0, "blockId": 0,
                        "bankOffset": 0x1234, "blockOffset": 0x234,
                    },
                    "events": [{"type": "start", "sampleTime": 0, "durationSamples": 441}],
                }, "fmEvents": [{
                    "sampleTime": 441, "source": "ym2413-patch",
                    "timbre": {"model": "opll", "suggestedProgram": 16},
                }]},
                {"trackIndex": 1, "libvgm": {
                    "deviceType": 2, "instance": 0, "mainMask": 64,
                    "linkedMask": 0, "groupId": "2:0:64:0",
                    "suggestedForHardwareMix": True,
                }},
            ],
        }), encoding="utf-8")

    def test_loads_and_expands_shared_physical_channel(self) -> None:
        self._write()
        metadata = libvgm.load_metadata(self.path, 2)
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.sample_count, 44100)
        self.assertTrue(metadata.targets[0].suggested)
        self.assertEqual(
            libvgm.validate_sources(metadata, {0: "game"}),
            {0: "game", 1: "game"},
        )

    def test_rejects_out_of_range_track_index(self) -> None:
        self._write()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["tracks"][0]["trackIndex"] = 2
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(WebValidationError):
            libvgm.load_metadata(self.path, 2)


class TestLibvgmRender(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.helper = Path(self.temp.name) / "helper"
        self.helper.write_text("#!/bin/sh\n", encoding="utf-8")
        self.helper.chmod(0o755)
        self.addCleanup(os.environ.pop, "VGM2MIDI_STEMS_HELPER", None)
        os.environ["VGM2MIDI_STEMS_HELPER"] = str(self.helper)

    def test_resolves_bundled_helper_when_override_is_absent(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(libvgm, "DEFAULT_HELPER", self.helper),
        ):
            self.assertEqual(libvgm.resolve_helper(), self.helper)

    def test_combines_masks_for_the_same_device(self) -> None:
        output = Path(self.temp.name) / "selected.wav"
        targets = [
            libvgm.LibvgmTarget(2, 0, 1, 0, "a", False),
            libvgm.LibvgmTarget(2, 0, 4, 2, "b", True),
        ]

        def fake_run(argv, **_kwargs):
            output.write_bytes(b"R" * 100)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch("miditrack.libvgm.subprocess.run", side_effect=fake_run) as mocked:
            libvgm.render_selection(Path("song.vgm"), output, 22050, targets)
        argv = mocked.call_args.args[0]
        self.assertEqual(argv[-1], "2:0:5:2")
        self.assertEqual(argv[1], "--selection")

    def test_loads_start_sample_offset(self) -> None:
        sidecar = Path(self.temp.name) / "offset.libvgm.json"
        sidecar.write_text(json.dumps({
            "version": 1,
            "sampleCount": 44100,
            "startSampleOffset": 1500,
            "tracks": [
                {"trackIndex": 0, "libvgm": {
                    "deviceType": 2, "instance": 0, "mainMask": 64,
                    "linkedMask": 0, "groupId": "2:0:64:0",
                }},
            ],
        }), encoding="utf-8")
        metadata = libvgm.load_metadata(sidecar, 1)
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.start_sample_offset, 1500)

    def test_trims_start_sample_offset_accurately(self) -> None:
        import wave
        output = Path(self.temp.name) / "trimmed.wav"
        targets = [libvgm.LibvgmTarget(2, 0, 1, 0, "a", False)]

        def fake_run(argv, **_kwargs):
            # argv: helper --selection input temp_wav frames ...
            temp_path = Path(argv[3])
            frames = int(argv[4])
            # Write a valid 16-bit stereo 44.1kHz WAV with known sample numbers
            with wave.open(str(temp_path), "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(44100)
                # each frame is 4 bytes
                data = b"".join(int(i % 30000).to_bytes(2, "little", signed=True) * 2 for i in range(frames))
                w.writeframes(data)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch("miditrack.libvgm.subprocess.run", side_effect=fake_run) as mocked:
            libvgm.render_selection(Path("song.vgm"), output, 100, targets, start_sample_offset=50)

        argv = mocked.call_args.args[0]
        self.assertEqual(int(argv[4]), 150)  # 100 + 50
        with wave.open(str(output), "rb") as w:
            self.assertEqual(w.getnframes(), 100)
            # The first frame in output should have been frame 50 in input
            frame0 = w.readframes(1)
            expected_sample = int(50).to_bytes(2, "little", signed=True) * 2
            self.assertEqual(frame0, expected_sample)


if __name__ == "__main__":
    unittest.main()
