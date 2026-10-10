"""YAML models: values a writer types unquoted in story.yaml still validate."""

from __future__ import annotations

import yaml

from vantage.config import CompareChapter, GalleryChapter, ScrubChapter, Story, TimelineChapter

# Every date and year below is unquoted, so YAML hands the models a date or an int, not a string.
UNQUOTED = """
vantages:
  - id: overview
    name: Overview
    picks:
      2025-06-14: {source: 2025-06-14/DJI_0007.JPG}
captures:
  - date: 2025-06-14
chapters:
  - type: scrub
    vantage: overview
    from: 2025-06-14
    to: 2026-09-12
    steps:
      - capture: 2025-06-14
        text: Clearing.
    hotspots:
      - {id: pond, x: 0.5, y: 0.5, label: Pond, from: 2025-06-14, to: 2026-01-31}
  - type: compare
    vantage: overview
    before: 2025-06-14
    after: 2026-09-12
  - type: gallery
    vantage: overview
    captures: [2025-06-14, 2026-09-12]
    images:
      - {file: archive/postcard.jpg, date: 1955}
  - type: timeline
    items:
      - {date: 1970, title: Opened}
      - {date: 2025-10-23, title: Reopened, capture: 2026-01-31}
"""


def test_unquoted_dates_and_years_validate_as_text() -> None:
    story = Story.model_validate(yaml.safe_load(UNQUOTED))
    scrub, compare, gallery, timeline = story.chapters
    assert isinstance(scrub, ScrubChapter) and isinstance(compare, CompareChapter)
    assert isinstance(gallery, GalleryChapter) and isinstance(timeline, TimelineChapter)
    assert list(story.vantages[0].picks) == ["2025-06-14"]
    assert (scrub.from_, scrub.to, scrub.steps[0].capture) == ("2025-06-14", "2026-09-12", "2025-06-14")
    assert (scrub.hotspots[0].from_, scrub.hotspots[0].to) == ("2025-06-14", "2026-01-31")
    assert (compare.before, compare.after) == ("2025-06-14", "2026-09-12")
    assert gallery.captures == ["2025-06-14", "2026-09-12"] and gallery.images[0].date == "1955"
    assert [(i.date, i.capture) for i in timeline.items] == [("1970", None), ("2025-10-23", "2026-01-31")]
