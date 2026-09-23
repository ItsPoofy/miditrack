"""Real-time MIDI playback engine for VGMidi.

Provides sub-millisecond voice dispatch and instant 0ms track muting, soloing,
and volume control for both WinMM MIDI Output devices and live SoundFont synthesis.
"""

from __future__ import annotations

import ctypes
from ctypes import c_char_p, c_int, c_void_p, wintypes
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any

from . import midi, pianoroll


class WinMmOutput:
    """Live MIDI output via Windows Multimedia (WinMM) API."""

    def __init__(self, device_id: int = 0):
        self.device_id = device_id
        self.h_midi = wintypes.HMETAFILE()
        self.is_open = False
        if sys.platform == "win32":
            try:
                self.winmm = ctypes.windll.winmm
                self.open(device_id)
            except Exception:
                self.winmm = None
        else:
            self.winmm = None

    def open(self, device_id: int):
        if not self.winmm:
            return
        if self.is_open:
            self.close()
        self.device_id = device_id
        dev = device_id if device_id >= 0 else 0xFFFFFFFF
        res = self.winmm.midiOutOpen(ctypes.byref(self.h_midi), dev, 0, 0, 0)
        self.is_open = (res == 0)

    def send_short(self, status: int, data1: int, data2: int = 0):
        if not self.is_open:
            return
        msg = (data2 << 16) | (data1 << 8) | status
        self.winmm.midiOutShortMsg(self.h_midi, msg)

    def reset(self):
        if self.is_open:
            self.winmm.midiOutReset(self.h_midi)

    def close(self):
        if self.is_open:
            try:
                self.reset()
                self.winmm.midiOutClose(self.h_midi)
            except Exception:
                pass
            self.is_open = False


def find_fluidsynth_dll() -> Path | None:
    """Locate bundled or system libfluidsynth-3.dll."""
    if "LIBFLUIDSYNTH_PATH" in os.environ:
        p = Path(os.environ["LIBFLUIDSYNTH_PATH"])
        if p.is_file():
            return p
    if "FLUIDSYNTH_BIN" in os.environ:
        p = Path(os.environ["FLUIDSYNTH_BIN"]).parent / "libfluidsynth-3.dll"
        if p.is_file():
            return p
    helpers = os.environ.get("MIDITRACK_RESOURCE_ROOT")
    if helpers:
        p = Path(helpers) / "libfluidsynth-3.dll"
        if p.is_file():
            return p
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if directory:
            p = Path(directory) / "libfluidsynth-3.dll"
            if p.is_file():
                return p
    if Path(r"C:\bin\libfluidsynth-3.dll").is_file():
        return Path(r"C:\bin\libfluidsynth-3.dll")
    return None


class FluidSynthOutput:
    """Live SoundFont synthesizer output via libfluidsynth."""

    def __init__(self, dll_path: Path, soundfont_path: Path | None = None):
        self.is_open = False
        self.dll_path = dll_path
        self.sfont_id = None
        try:
            self.fs = ctypes.CDLL(str(dll_path))
            self._setup_types()
            self.settings = self.fs.new_fluid_settings()
            self.fs.fluid_settings_setstr(self.settings, b"audio.driver", b"dsound")
            self.synth = self.fs.new_fluid_synth(self.settings)
            self.adriver = self.fs.new_fluid_audio_driver(self.settings, self.synth)
            self.is_open = True
            if soundfont_path and Path(soundfont_path).is_file():
                self.load_soundfont(soundfont_path)
        except Exception as e:
            self.close()
            raise RuntimeError(f"Failed to initialize live FluidSynth: {e}") from e

    def _setup_types(self):
        self.fs.new_fluid_settings.restype = c_void_p
        self.fs.new_fluid_synth.restype = c_void_p
        self.fs.new_fluid_synth.argtypes = [c_void_p]
        self.fs.new_fluid_audio_driver.restype = c_void_p
        self.fs.new_fluid_audio_driver.argtypes = [c_void_p, c_void_p]
        self.fs.fluid_settings_setstr.argtypes = [c_void_p, c_char_p, c_char_p]
        self.fs.fluid_synth_sfload.restype = c_int
        self.fs.fluid_synth_sfload.argtypes = [c_void_p, c_char_p, c_int]
        self.fs.fluid_synth_noteon.argtypes = [c_void_p, c_int, c_int, c_int]
        self.fs.fluid_synth_noteoff.argtypes = [c_void_p, c_int, c_int]
        self.fs.fluid_synth_cc.argtypes = [c_void_p, c_int, c_int, c_int]
        self.fs.fluid_synth_program_change.argtypes = [c_void_p, c_int, c_int]
        self.fs.fluid_synth_pitch_bend.argtypes = [c_void_p, c_int, c_int]
        self.fs.fluid_synth_all_notes_off.argtypes = [c_void_p, c_int]
        self.fs.fluid_synth_all_sounds_off.argtypes = [c_void_p, c_int]
        self.fs.delete_fluid_audio_driver.argtypes = [c_void_p]
        self.fs.delete_fluid_synth.argtypes = [c_void_p]
        self.fs.delete_fluid_settings.argtypes = [c_void_p]

    def load_soundfont(self, path: Path | str):
        if not self.is_open:
            return
        p_bytes = str(path).encode("utf-8")
        sfont_id = self.fs.fluid_synth_sfload(self.synth, p_bytes, 1)
        if sfont_id != -1:
            self.sfont_id = sfont_id

    def send_short(self, status: int, data1: int, data2: int = 0):
        if not self.is_open:
            return
        cmd = status & 0xF0
        ch = status & 0x0F
        if cmd == 0x90:
            if data2 == 0:
                self.fs.fluid_synth_noteoff(self.synth, ch, data1)
            else:
                self.fs.fluid_synth_noteon(self.synth, ch, data1, data2)
        elif cmd == 0x80:
            self.fs.fluid_synth_noteoff(self.synth, ch, data1)
        elif cmd == 0xB0:
            self.fs.fluid_synth_cc(self.synth, ch, data1, data2)
        elif cmd == 0xC0:
            self.fs.fluid_synth_program_change(self.synth, ch, data1)
        elif cmd == 0xE0:
            pitch_val = ((data2 << 7) | data1)
            self.fs.fluid_synth_pitch_bend(self.synth, ch, pitch_val)

    def reset(self):
        if self.is_open:
            for ch in range(16):
                self.fs.fluid_synth_all_sounds_off(self.synth, ch)

    def close(self):
        if self.is_open:
            try:
                self.reset()
                self.fs.delete_fluid_audio_driver(self.adriver)
                self.fs.delete_fluid_synth(self.synth)
                self.fs.delete_fluid_settings(self.settings)
            except Exception:
                pass
            self.is_open = False


class TimedMidiMessage:
    __slots__ = ("time", "track_index", "channel", "status", "data1", "data2", "is_state")

    def __init__(
        self,
        time: float,
        track_index: int,
        channel: int,
        status: int,
        data1: int,
        data2: int = 0,
        is_state: bool = False,
    ):
        self.time = time
        self.track_index = track_index
        self.channel = channel
        self.status = status
        self.data1 = data1
        self.data2 = data2
        self.is_state = is_state


class RealtimeMidiPlayer:
    """Threaded real-time MIDI sequencer and live voice controller."""

    def __init__(self):
        self.lock = threading.RLock()
        self.output: WinMmOutput | FluidSynthOutput | None = None
        self.sound_source_type = "soundfont"  # "soundfont" or "midi"
        self.midi_device_id = 0
        self.soundfont_path: Path | None = None

        self.events: list[TimedMidiMessage] = []
        self.duration: float = 0.0
        self.current_time: float = 0.0
        self.event_index: int = 0
        self.is_playing: bool = False
        self.start_perf_time: float = 0.0
        self.loaded_revision: int | None = None

        self.playback_thread: threading.Thread | None = None
        self.stop_event = threading.Event()

        # Track mix properties
        self.muted_tracks: set[int] = set()
        self.solo_track: int | None = None
        self.track_volumes: dict[int, int] = {}  # track_index -> percent (0-100)
        self.track_channels: dict[int, int] = {}  # track_index -> channel
        self.track_assignments: dict[int, int] = {}  # track_index -> program

    def configure(
        self,
        sound_source_type: str | None = None,
        midi_device_id: int | None = None,
        soundfont_path: Path | None = None,
    ):
        with self.lock:
            need_reopen = False
            if sound_source_type is not None and sound_source_type != self.sound_source_type:
                self.sound_source_type = sound_source_type
                need_reopen = True
            if midi_device_id is not None and midi_device_id != self.midi_device_id:
                self.midi_device_id = midi_device_id
                if self.sound_source_type == "midi":
                    need_reopen = True
            if soundfont_path is not None and soundfont_path != self.soundfont_path:
                self.soundfont_path = soundfont_path
                if self.sound_source_type == "soundfont":
                    need_reopen = True

            if need_reopen or self.output is None:
                was_playing = self.is_playing
                pos = self.get_current_time()
                if self.is_playing:
                    self.pause()
                self._open_output()
                if was_playing:
                    self.seek(pos)
                    self.play(pos)

    def _open_output(self):
        if self.output is not None:
            self.output.close()
            self.output = None

        if self.sound_source_type == "midi":
            self.output = WinMmOutput(self.midi_device_id)
        else:
            dll = find_fluidsynth_dll()
            if dll:
                try:
                    self.output = FluidSynthOutput(dll, self.soundfont_path)
                except Exception:
                    self.output = WinMmOutput(self.midi_device_id)
            else:
                self.output = WinMmOutput(self.midi_device_id)

    def load_midi(
        self,
        midi_path: Path,
        speed: float = 1.0,
        transpose: int = 0,
        assignments: dict[int, int] | None = None,
        volumes: dict[int, int] | None = None,
        channels: dict[int, int] | None = None,
    ):
        with self.lock:
            was_playing = self.is_playing
            if self.is_playing:
                self.pause()

            self.track_assignments = dict(assignments or {})
            self.track_volumes = dict(volumes or {})
            self.track_channels = dict(channels or {})
            self.muted_tracks.clear()
            self.solo_track = None

            mido = midi.import_mido()
            try:
                mf = mido.MidiFile(midi_path)
            except Exception:
                return

            tempo_map, _end_ticks = pianoroll._build_tempo_map(mf, speed)

            events: list[TimedMidiMessage] = []
            for track_idx, track in enumerate(mf.tracks):
                tick = 0
                assigned_prog = self.track_assignments.get(track_idx)
                assigned_ch = self.track_channels.get(track_idx)

                for msg in track:
                    tick += msg.time
                    sec = tempo_map.to_seconds(tick)
                    ch = assigned_ch if assigned_ch is not None else getattr(msg, "channel", 0)
                    if track_idx not in self.track_channels:
                        self.track_channels[track_idx] = ch

                    if msg.type == "note_on":
                        note = msg.note
                        if ch != midi.PERCUSSION_CHANNEL:
                            note = max(0, min(127, note + transpose))
                        status = 0x90 | (ch & 0x0F)
                        events.append(
                            TimedMidiMessage(
                                sec, track_idx, ch, status, note, msg.velocity, is_state=False
                            )
                        )
                    elif msg.type == "note_off":
                        note = msg.note
                        if ch != midi.PERCUSSION_CHANNEL:
                            note = max(0, min(127, note + transpose))
                        status = 0x80 | (ch & 0x0F)
                        events.append(
                            TimedMidiMessage(sec, track_idx, ch, status, note, 0, is_state=False)
                        )
                    elif msg.type == "program_change":
                        prog = assigned_prog if assigned_prog is not None else msg.program
                        status = 0xC0 | (ch & 0x0F)
                        events.append(
                            TimedMidiMessage(sec, track_idx, ch, status, prog, 0, is_state=True)
                        )
                    elif msg.type == "control_change":
                        val = msg.value
                        if msg.control == 7 and track_idx in self.track_volumes:
                            val = round(self.track_volumes[track_idx] * 127 / 100)
                        status = 0xB0 | (ch & 0x0F)
                        events.append(
                            TimedMidiMessage(
                                sec, track_idx, ch, status, msg.control, val, is_state=True
                            )
                        )
                    elif msg.type == "pitchwheel":
                        status = 0xE0 | (ch & 0x0F)
                        pitch_val = max(0, min(16383, msg.pitch + 8192))
                        lsb = pitch_val & 0x7F
                        msb = (pitch_val >> 7) & 0x7F
                        events.append(
                            TimedMidiMessage(sec, track_idx, ch, status, lsb, msb, is_state=True)
                        )

            events.sort(key=lambda ev: ev.time)
            self.events = events
            self.duration = events[-1].time if events else 0.0
            self.current_time = 0.0
            self.event_index = 0

            if self.output is None:
                self._open_output()

            if was_playing:
                self.play(0.0)

    def play(self, start_seconds: float = 0.0):
        with self.lock:
            if not self.events:
                return
            if self.is_playing:
                self.pause()

            if self.output is None:
                self._open_output()

            self.current_time = max(0.0, min(self.duration, start_seconds))
            # Find starting event index
            import bisect
            times = [ev.time for ev in self.events]
            self.event_index = bisect.bisect_left(times, self.current_time)

            # Send state messages up to start_seconds so instruments & controls are accurate
            self._send_state_up_to(self.current_time)
            self._apply_all_track_volumes()

            self.is_playing = True
            self.start_perf_time = time.perf_counter() - self.current_time
            self.stop_event.clear()

            self.playback_thread = threading.Thread(target=self._sequencer_loop, daemon=True)
            self.playback_thread.start()

    def pause(self):
        with self.lock:
            if not self.is_playing:
                return
            self.is_playing = False
            self.stop_event.set()
            if self.playback_thread and self.playback_thread.is_alive():
                self.playback_thread.join(timeout=0.2)
            self.playback_thread = None

            if self.output:
                self.output.reset()

    def seek(self, seconds: float):
        with self.lock:
            seconds = max(0.0, min(self.duration, seconds))
            was_playing = self.is_playing
            if self.is_playing:
                self.pause()

            self.current_time = seconds
            import bisect
            times = [ev.time for ev in self.events]
            self.event_index = bisect.bisect_left(times, self.current_time)

            if self.output:
                self.output.reset()
                self._send_state_up_to(self.current_time)
                self._apply_all_track_volumes()

            if was_playing:
                self.play(seconds)

    def get_current_time(self) -> float:
        with self.lock:
            if self.is_playing:
                cur = time.perf_counter() - self.start_perf_time
                return min(self.duration, cur)
            return self.current_time

    def set_track_mute(self, track_index: int, muted: bool):
        with self.lock:
            if muted:
                self.muted_tracks.add(track_index)
                ch = self.track_channels.get(track_index)
                if ch is not None and self.output:
                    # Instant 0ms mute: CC7 = 0 and CC123 All Notes Off
                    self.output.send_short(0xB0 | (ch & 0x0F), 7, 0)
                    self.output.send_short(0xB0 | (ch & 0x0F), 123, 0)
            else:
                self.muted_tracks.discard(track_index)
                ch = self.track_channels.get(track_index)
                if ch is not None and self.output:
                    vol = self._effective_channel_volume(track_index)
                    self.output.send_short(0xB0 | (ch & 0x0F), 7, vol)

    def set_track_solo(self, track_index: int, solo: bool):
        with self.lock:
            if solo:
                self.solo_track = track_index
                for t_idx, ch in self.track_channels.items():
                    if t_idx != track_index:
                        # Instant 0ms silence non-soloed tracks
                        self.output.send_short(0xB0 | (ch & 0x0F), 7, 0)
                        self.output.send_short(0xB0 | (ch & 0x0F), 123, 0)
                    else:
                        vol = self._effective_channel_volume(t_idx)
                        self.output.send_short(0xB0 | (ch & 0x0F), 7, vol)
            else:
                if self.solo_track == track_index:
                    self.solo_track = None
                self._apply_all_track_volumes()

    def set_track_volume(self, track_index: int, volume_percent: int):
        with self.lock:
            self.track_volumes[track_index] = volume_percent
            ch = self.track_channels.get(track_index)
            if ch is not None and self.output:
                vol = self._effective_channel_volume(track_index)
                self.output.send_short(0xB0 | (ch & 0x0F), 7, vol)

    def _effective_channel_volume(self, track_index: int) -> int:
        if track_index in self.muted_tracks:
            return 0
        if self.solo_track is not None and self.solo_track != track_index:
            return 0
        pct = self.track_volumes.get(track_index, 100)
        return max(0, min(127, round(pct * 127 / 100)))

    def _apply_all_track_volumes(self):
        if not self.output:
            return
        for t_idx, ch in self.track_channels.items():
            vol = self._effective_channel_volume(t_idx)
            self.output.send_short(0xB0 | (ch & 0x0F), 7, vol)

    def _send_state_up_to(self, target_time: float):
        """Send Program Change and CC states up to target_time so timbre is correct."""
        if not self.output:
            return
        latest_pc: dict[int, int] = {}
        latest_cc: dict[tuple[int, int], int] = {}

        for ev in self.events:
            if ev.time > target_time:
                break
            cmd = ev.status & 0xF0
            ch = ev.channel
            if cmd == 0xC0:
                latest_pc[ch] = ev.data1
            elif cmd == 0xB0 and ev.data1 != 7:  # Preserve volume control
                latest_cc[(ch, ev.data1)] = ev.data2

        for ch, prog in latest_pc.items():
            self.output.send_short(0xC0 | (ch & 0x0F), prog, 0)
        for (ch, ctrl), val in latest_cc.items():
            self.output.send_short(0xB0 | (ch & 0x0F), ctrl, val)

    def _sequencer_loop(self):
        """High-precision scheduling loop."""
        num_events = len(self.events)
        while not self.stop_event.is_set():
            now = time.perf_counter() - self.start_perf_time
            if now >= self.duration + 0.5:
                # Song ended
                with self.lock:
                    self.is_playing = False
                    self.current_time = self.duration
                    if self.output:
                        self.output.reset()
                break

            with self.lock:
                idx = self.event_index
                while idx < num_events:
                    ev = self.events[idx]
                    if ev.time > now:
                        break
                    idx += 1

                    # Check mute / solo for note events
                    cmd = ev.status & 0xF0
                    if cmd == 0x90 and ev.data2 > 0:
                        if ev.track_index in self.muted_tracks:
                            continue
                        if self.solo_track is not None and self.solo_track != ev.track_index:
                            continue

                    if self.output:
                        self.output.send_short(ev.status, ev.data1, ev.data2)

                self.event_index = idx
                self.current_time = now

            time.sleep(0.001)


# Global singleton instance
player_engine = RealtimeMidiPlayer()
