from __future__ import annotations

import json
from pathlib import Path

from interfaces import dashboard_api


def test_dashboard_config_is_cached_until_yaml_changes(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "dev.yaml"
    config_path.write_text("profile: dev\n", encoding="utf-8")
    calls = 0

    def load(_path: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"profile": "dev", "cameras": []}

    monkeypatch.setattr(dashboard_api, "CONFIG_PATH", config_path)
    monkeypatch.setattr(dashboard_api, "load_raw_config", load)
    monkeypatch.setattr(dashboard_api, "CONFIG_CACHE", None)

    assert dashboard_api._raw_config()["profile"] == "dev"
    assert dashboard_api._raw_config()["profile"] == "dev"
    assert calls == 1


def test_event_journal_reads_only_appended_lines(tmp_path) -> None:
    journal = tmp_path / "events.jsonl"
    first = {"record_type": "START", "event_id": "event-1"}
    second = {"record_type": "UPDATE", "event_id": "event-1"}
    journal.write_text(
        "\n".join(json.dumps(item) for item in (first, second)) + "\n",
        encoding="utf-8",
    )
    dashboard_api.JOURNAL_CACHE.clear()

    cursor, lines, event_count = dashboard_api._event_journal_snapshot(
        journal,
        after=0,
    )
    assert cursor == 2
    assert len(lines) == 2
    assert event_count == 1

    third = {"record_type": "START", "event_id": "event-2"}
    with journal.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(third) + "\n")

    cursor, lines, event_count = dashboard_api._event_journal_snapshot(
        journal,
        after=2,
    )
    assert cursor == 3
    assert [json.loads(line)["event_id"] for _sequence, line in lines] == ["event-2"]
    assert event_count == 2
