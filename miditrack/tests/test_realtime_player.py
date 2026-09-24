from __future__ import annotations

from pathlib import Path
import pytest
from miditrack.realtime_player import RealtimeMidiPlayer, TimedMidiMessage


class MockOutput:
    def __init__(self):
        self.sent = []
        self.is_open = True

    def send_short(self, status: int, data1: int, data2: int = 0):
        self.sent.append((status, data1, data2))

    def reset(self):
        self.sent.append(("reset",))

    def close(self):
        self.is_open = False


def test_realtime_player_mute_solo_volume():
    player = RealtimeMidiPlayer()
    mock_out = MockOutput()
    player.output = mock_out

    # Track 0 on channel 0, Track 1 on channel 1
    player.track_channels = {0: 0, 1: 1}
    player.track_volumes = {0: 100, 1: 80}

    # 1. Test mute track 0 -> should send CC 7 = 0 and CC 123 (all notes off)
    mock_out.sent.clear()
    player.set_track_mute(0, True)
    assert 0 in player.muted_tracks
    assert (0xB0, 7, 0) in mock_out.sent
    assert (0xB0, 123, 0) in mock_out.sent

    # 2. Test unmute track 0 -> should restore volume
    mock_out.sent.clear()
    player.set_track_mute(0, False)
    assert 0 not in player.muted_tracks
    assert (0xB0, 7, 127) in mock_out.sent

    # 3. Test solo track 1 -> track 0 silenced, track 1 has volume
    mock_out.sent.clear()
    player.set_track_solo(1, True)
    assert player.solo_track == 1
    assert (0xB0, 7, 0) in mock_out.sent  # ch 0 silenced
    assert (0xB1, 7, round(80 * 127 / 100)) in mock_out.sent  # ch 1 active

    # 4. Test un-solo
    mock_out.sent.clear()
    player.set_track_solo(1, False)
    assert player.solo_track is None
    assert (0xB0, 7, 127) in mock_out.sent

    # 5. Test volume change
    mock_out.sent.clear()
    player.set_track_volume(0, 50)
    assert player.track_volumes[0] == 50
    assert (0xB0, 7, round(50 * 127 / 100)) in mock_out.sent


def test_realtime_player_configure():
    player = RealtimeMidiPlayer()
    player.configure(sound_source_type="midi", midi_device_id=0)
    assert player.sound_source_type == "midi"
    assert player.midi_device_id == 0


def test_realtime_routes():
    from miditrack.web_routes import create_app
    from miditrack.web_session import WebSession

    session = WebSession()
    app = create_app(token="test-token", session=session, require_token=False)
    client = app.test_client()

    # 1. Config endpoint
    res = client.post("/api/realtime/config", json={"soundSourceType": "midi", "midiDeviceId": 0})
    assert res.status_code == 200
    data = res.get_json()
    assert data["soundSourceType"] == "midi"
    assert data["midiDeviceId"] == 0

    # 2. Status endpoint
    res = client.get("/api/realtime/status")
    assert res.status_code == 200
    data = res.get_json()
    assert "playing" in data
    assert "currentTime" in data
    assert data["soundSourceType"] == "midi"

    # 3. Mute endpoint
    res = client.post("/api/realtime/mute", json={"trackIndex": 0, "muted": True})
    assert res.status_code == 200
    assert res.get_json()["muted"] is True

    # 4. Solo endpoint
    res = client.post("/api/realtime/solo", json={"trackIndex": 1, "solo": True})
    assert res.status_code == 200
    assert res.get_json()["solo"] is True

    # 5. Volume endpoint
    res = client.post("/api/realtime/volume", json={"trackIndex": 1, "volumePercent": 75})
    assert res.status_code == 200
    assert res.get_json()["volumePercent"] == 75

    # 6. Pause endpoint
    res = client.post("/api/realtime/pause")
    assert res.status_code == 200
    assert res.get_json()["playing"] is False


def test_realtime_player_stop_and_active_notes():
    player = RealtimeMidiPlayer()
    mock_out = MockOutput()
    player.output = mock_out

    # Simulate Note-On on ch 0, note 60
    msg_on = TimedMidiMessage(time=0.0, track_index=0, channel=0, status=0x90, data1=60, data2=100)
    player._dispatch_message(msg_on)
    assert (0, 60) in player.active_notes

    # Simulate Note-On on ch 1, note 72
    msg_on2 = TimedMidiMessage(time=0.1, track_index=1, channel=1, status=0x90, data1=72, data2=90)
    player._dispatch_message(msg_on2)
    assert (1, 72) in player.active_notes

    # Note-Off for note 60 via vel=0
    msg_off = TimedMidiMessage(time=0.2, track_index=0, channel=0, status=0x90, data1=60, data2=0)
    player._dispatch_message(msg_off)
    assert (0, 60) not in player.active_notes
    assert (1, 72) in player.active_notes

    # Call _all_notes_off: should send explicit note off for (1, 72) and reset output
    mock_out.sent.clear()
    player._all_notes_off()
    assert (0x81, 72, 0) in mock_out.sent
    assert ("reset",) in mock_out.sent
    assert len(player.active_notes) == 0

    # Test stop clears everything
    player.loaded_path = "C:/fake/path.mid"
    player.loaded_revision = 42
    player.events = [msg_on]
    player.event_times = [0.0]
    player.duration = 10.0
    player.stop()
    assert player.loaded_path is None
    assert player.loaded_revision is None
    assert len(player.events) == 0
    assert player.duration == 0.0


def test_seek_does_not_hold_sustain():
    player = RealtimeMidiPlayer()
    mock_out = MockOutput()
    player.output = mock_out

    # Song with CC 64 (Sustain ON) at 0.5s and Program Change 5 at 0.1s
    ev1 = TimedMidiMessage(time=0.1, track_index=0, channel=0, status=0xC0, data1=5, data2=0)
    ev2 = TimedMidiMessage(time=0.5, track_index=0, channel=0, status=0xB0, data1=64, data2=127)
    player.events = [ev1, ev2]
    player.event_times = [0.1, 0.5]
    player.duration = 2.0

    mock_out.sent.clear()
    player.seek(1.0)
    # Timbre (PC 5) should be restored
    assert (0xC0, 5, 0) in mock_out.sent
    # But CC 64 (Sustain pedal) must NOT be replayed as ON
    assert (0xB0, 64, 127) not in mock_out.sent


def test_session_reset_stops_player_engine():
    from miditrack.realtime_player import player_engine
    from miditrack.web_session import WebSession

    player_engine.loaded_path = "C:/fake/song1.mid"
    player_engine.loaded_revision = 5
    player_engine.events = [TimedMidiMessage(0.0, 0, 0, 0x90, 60, 100)]

    session = WebSession()
    session.reset_midi_state()

    assert player_engine.loaded_path is None
    assert player_engine.loaded_revision is None
    assert len(player_engine.events) == 0

