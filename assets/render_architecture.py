#!/usr/bin/env python3
"""Render assets/architecture-diagram.html to assets/architecture.png.

Devpost requires the architecture diagram as an uploaded file (png/jpg/pdf), not
as a description. The diagram is hand-authored HTML rather than Mermaid so the
control plane, the data input, and the model dependency can sit on visually
distinct planes, and so it matches the dark cover style used for the article.

    python3 assets/render_architecture.py

Needs Google Chrome and a network connection (the page loads Inter from Google Fonts).
"""

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGE = ROOT / "assets" / "architecture-diagram.html"
OUT_PNG = ROOT / "assets" / "architecture.png"

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Must match .cover in architecture-diagram.html: Chrome captures exactly this window.
WIDTH, HEIGHT = 1600, 1600


def main() -> None:
    if not pathlib.Path(CHROME).exists():
        sys.exit(f"Chrome not found at {CHROME}")
    if not PAGE.exists():
        sys.exit(f"Missing {PAGE}")

    subprocess.run(
        [
            CHROME,
            "--headless",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--screenshot={OUT_PNG}",
            f"--window-size={WIDTH},{HEIGHT}",
            "--force-device-scale-factor=2",
            "--virtual-time-budget=15000",
            PAGE.as_uri(),
        ],
        check=True,
        capture_output=True,
    )

    if not OUT_PNG.exists():
        sys.exit("Chrome produced no screenshot")
    print(f"Wrote {OUT_PNG} ({OUT_PNG.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
