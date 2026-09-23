import sys
import io
from pathlib import Path

if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

# Add package src to sys.path
src_dir = Path(__file__).resolve().parent / "miditrack" / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from miditrack.desktop_app import main

if __name__ == "__main__":
    main()
