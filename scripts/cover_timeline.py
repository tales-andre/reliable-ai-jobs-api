"""Copy the safeguarded run's timeline SVG out of report/chaos.html, for docs/cover.html.

    python scripts/cover_timeline.py        # writes docs/cover-timeline.svg

The cover crops it with CSS. Using the SVG itself keeps the cover sharp at 1600x1200.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

html = (ROOT / "report" / "chaos.html").read_text(encoding="utf-8")
# The report shows the safeguarded run first.
svg = re.search(r"<svg .*?</svg>", html, re.S).group(0)
(ROOT / "docs" / "cover-timeline.svg").write_text(svg, encoding="utf-8")
print("wrote docs/cover-timeline.svg")
