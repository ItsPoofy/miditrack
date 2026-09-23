from __future__ import annotations

from miditrack.devices import list_system_devices


def test_list_system_devices_returns_expected_structure():
    devices = list_system_devices()
    assert isinstance(devices, dict)
    assert "audio_outputs" in devices
    assert "midi_outputs" in devices
    assert "midi_inputs" in devices

    assert isinstance(devices["audio_outputs"], list)
    assert len(devices["audio_outputs"]) >= 1
    assert devices["audio_outputs"][0]["id"] == "default"

    assert isinstance(devices["midi_outputs"], list)
    assert len(devices["midi_outputs"]) >= 1
    assert devices["midi_outputs"][0]["id"] == "none"

    assert isinstance(devices["midi_inputs"], list)
