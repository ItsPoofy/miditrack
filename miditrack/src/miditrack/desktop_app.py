"""Native Windows desktop application for miditrack.

Uses Microsoft Edge WebView2 (via pywebview) to run miditrack in a native desktop window,
identical to miditrack.app on macOS.
"""

from __future__ import annotations

import io
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()
from werkzeug.serving import make_server

import webview

try:
    from miditrack import __version__
    from miditrack.cli import build_parser
    from miditrack.web_routes import create_app
    from miditrack.web_session import WebSession
except ImportError:
    from . import __version__
    from .cli import build_parser
    from .web_routes import create_app
    from .web_session import WebSession


class DesktopApi:
    def __init__(self, app: MiditrackDesktopApp):
        self._app = app

    def save_file(self, filename: str, b64_data: str) -> bool:
        try:
            import base64
            data = base64.b64decode(b64_data)
            downloads_dir = str(Path.home() / "Downloads")
            res = self._app.window.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=downloads_dir,
                save_filename=filename,
            )
            if res:
                dest = Path(res if isinstance(res, str) else res[0])
                dest.write_bytes(data)
                return True
            return False
        except Exception as e:
            print(f"Error in save_file: {e}", file=sys.stderr)
            return False


class MiditrackDesktopApp:
    def __init__(self, initial_file: Path | None = None, soundfont: Path | None = None):
        self.initial_file = initial_file
        self.soundfont = soundfont
        self.token = secrets.token_urlsafe(32)
        self.session = WebSession()
        self.server = None
        self.server_thread = None
        self.temp_open_dir = Path(tempfile.mkdtemp(prefix="miditrack-open-"))
        self._configure_environment()

    def _configure_environment(self):
        os.environ.setdefault("MIDITRACK_FLUIDSYNTH_GAIN", "1.0")
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).parent
            helpers_dir = exe_dir / "_internal" / "Helpers"
            if not helpers_dir.is_dir():
                helpers_dir = exe_dir

            os.environ["MIDITRACK_RESOURCE_ROOT"] = str(helpers_dir)

            nsf_exe = helpers_dir / "nsf2midi.exe"
            if nsf_exe.is_file():
                os.environ["NSF2MIDI_BIN"] = str(nsf_exe)

            spc_exe = helpers_dir / "spc2midi.exe"
            if spc_exe.is_file():
                os.environ["SPC2MIDI_BIN"] = str(spc_exe)

            node_exe = helpers_dir / "node.exe"
            if node_exe.is_file():
                os.environ["MIDITRACK_NODE_BIN"] = str(node_exe)

            stems_exe = helpers_dir / "vgm2midi_stems.exe"
            if stems_exe.is_file():
                os.environ["VGM2MIDI_STEMS_HELPER"] = str(stems_exe)

            fluidsynth_exe = helpers_dir / "fluidsynth.exe"
            if not fluidsynth_exe.is_file():
                fluidsynth_exe = exe_dir / "fluidsynth.exe"
            if fluidsynth_exe.is_file():
                os.environ["FLUIDSYNTH_BIN"] = str(fluidsynth_exe)

            # Include exe_dir and helpers_dir in PATH for fluidsynth
            current_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{exe_dir};{helpers_dir};{current_path}"

    def start_server(self) -> str:
        app = create_app(
            token=self.token,
            session=self.session,
            soundfont=self.soundfont,
            require_token=True,
            local_open_dir=self.temp_open_dir,
        )
        self.server = make_server("127.0.0.1", 0, app, threaded=True)
        self.port = self.server.server_port
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        return f"http://127.0.0.1:{self.port}/?token={self.token}"

    def on_closed(self):
        if self.server:
            try:
                self.server.shutdown()
            except Exception:
                pass
        self.session.clear()
        shutil.rmtree(self.temp_open_dir, ignore_errors=True)

    def on_loaded(self):
        # If an initial file was specified on CLI/drag-and-drop, trigger loading via open-local
        if self.initial_file and self.initial_file.is_file():
            def _load():
                try:
                    time.sleep(0.3)
                    staged = self.temp_open_dir / self.initial_file.name
                    shutil.copyfile(self.initial_file, staged)
                    staged_path_str = str(staged).replace("\\", "\\\\")
                    js = f"""
                    fetch('/api/open-local', {{
                        method: 'POST',
                        headers: {{
                            'Content-Type': 'application/json',
                            'X-Miditrack-Token': '{self.token}'
                        }},
                        body: JSON.stringify({{paths: ['{staged_path_str}']}})
                    }}).then(res => res.json()).then(data => {{
                        if (window.dispatchEvent) {{
                            window.location.reload();
                        }}
                    }}).catch(err => console.error(err));
                    """
                    self.window.evaluate_js(js)
                except Exception as e:
                    print(f"Error opening initial file: {e}", file=sys.stderr)

            threading.Thread(target=_load, daemon=True).start()

    def run(self):
        url = self.start_server()

        # Find application icon
        icon_path = None
        possible_icons = [
            Path(__file__).resolve().parents[3] / "images" / "miditrack.ico",
            Path(__file__).resolve().parents[2] / "images" / "miditrack.ico",
            Path(__file__).resolve().parents[0] / "web_assets" / "favicon.ico",
        ]
        for p in possible_icons:
            if p.is_file():
                icon_path = str(p)
                break

        self.window = webview.create_window(
            title="VGMidi",
            url=url,
            width=1280,
            height=860,
            min_size=(800, 600),
            background_color="#20262f",
            text_select=True,
            js_api=DesktopApi(self),
        )
        self.window.events.closed += self.on_closed
        self.window.events.loaded += self.on_loaded

        # Use isolated user data folder so VGMidi doesn't collide with other pywebview apps
        cache_dir = Path.home() / "AppData" / "Local" / "VGMidi" / "webview2"
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Start native desktop window using Edge WebView2 (gui='edgechromium')
        webview.start(gui="edgechromium", debug=False, storage_path=str(cache_dir))


def main():
    parser = build_parser()
    args, unknown = parser.parse_known_args()

    initial_file = args.midi_file
    if not initial_file and unknown:
        for arg in unknown:
            p = Path(arg)
            if p.is_file():
                initial_file = p
                break

    app = MiditrackDesktopApp(initial_file=initial_file, soundfont=args.soundfont)
    try:
        app.run()
    finally:
        os._exit(0)


if __name__ == "__main__":
    main()
