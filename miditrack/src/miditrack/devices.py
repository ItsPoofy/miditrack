from __future__ import annotations

import sys
from typing import Any


def list_system_devices() -> dict[str, list[dict[str, str]]]:
    """Dynamically enumerates real system Audio outputs, MIDI outputs, and MIDI inputs via WinMM.

    Zero subprocesses, zero console window popups, instantaneous in-process enumeration.
    """
    audio_outputs: list[dict[str, str]] = [{"id": "default", "name": "Default Audio Device"}]
    midi_outputs: list[dict[str, str]] = [{"id": "none", "name": "<none>"}]
    midi_inputs: list[dict[str, str]] = []

    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            winmm = ctypes.windll.winmm

            class WAVEOUTCAPSW(ctypes.Structure):
                _fields_ = [
                    ("wMid", wintypes.WORD),
                    ("wPid", wintypes.WORD),
                    ("vDriverVersion", wintypes.UINT),
                    ("szPname", wintypes.WCHAR * 32),
                    ("dwFormats", wintypes.DWORD),
                    ("wChannels", wintypes.WORD),
                    ("wReserved1", wintypes.WORD),
                    ("dwSupport", wintypes.DWORD),
                ]

            class MIDIOUTCAPSW(ctypes.Structure):
                _fields_ = [
                    ("wMid", wintypes.WORD),
                    ("wPid", wintypes.WORD),
                    ("vDriverVersion", wintypes.UINT),
                    ("szPname", wintypes.WCHAR * 32),
                    ("wTechnology", wintypes.WORD),
                    ("wVoices", wintypes.WORD),
                    ("wNotes", wintypes.WORD),
                    ("wChannelMask", wintypes.WORD),
                    ("dwSupport", wintypes.DWORD),
                ]

            class MIDIINCAPSW(ctypes.Structure):
                _fields_ = [
                    ("wMid", wintypes.WORD),
                    ("wPid", wintypes.WORD),
                    ("vDriverVersion", wintypes.UINT),
                    ("szPname", wintypes.WCHAR * 32),
                    ("dwSupport", wintypes.DWORD),
                ]

            # Enumerate Audio Output Devices
            num_wave = winmm.waveOutGetNumDevs()
            for i in range(num_wave):
                caps = WAVEOUTCAPSW()
                if winmm.waveOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
                    raw_name = caps.szPname.strip()
                    if raw_name:
                        audio_outputs.append({"id": str(i), "name": raw_name})

            # Enumerate MIDI Output Devices
            num_midi_out = winmm.midiOutGetNumDevs()
            for i in range(num_midi_out):
                caps = MIDIOUTCAPSW()
                if winmm.midiOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
                    raw_name = caps.szPname.strip()
                    if raw_name:
                        midi_outputs.append({"id": str(i), "name": raw_name})

            # Enumerate MIDI Input Devices
            num_midi_in = winmm.midiInGetNumDevs()
            for i in range(num_midi_in):
                caps = MIDIINCAPSW()
                if winmm.midiInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
                    raw_name = caps.szPname.strip()
                    if raw_name:
                        midi_inputs.append({"id": str(i), "name": raw_name})

        except Exception:
            pass

    return {
        "audio_outputs": audio_outputs,
        "midi_outputs": midi_outputs,
        "midi_inputs": midi_inputs,
    }
