# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Persistenter Lauf-Speicher (runs.jsonl) und gezieltes Löschen im Verlauf.

Die Bilanz (Kacheln, Rückgewinn-Leiste, Diagramm) darf `Verlauf löschen` überleben.
Deshalb wird je abgeschlossenem Lauf eine Zusammenfassung mitgeschrieben, die von
den Einzeleinträgen unabhängig ist.
"""
import json
import os

import pytest


def _write_jsonl(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _read_jsonl(path):
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def _entry(run_id, name, before, after, ts, **kw):
    e = {"run_id": run_id, "name": name, "path": "/media/" + name, "src": "", "rel_dir": "",
         "size_before": before, "size_after": after, "method": "GPU", "target": "h265",
         "quality": "balanced", "timestamp": ts, "duration_s": 1, "speed": 2.0}
    e.update(kw)
    return e


# ─── Schreiben und Lesen ─────────────────────────────────────────────────────
def test_append_run_writes_one_line_per_run(server, tmp_path, monkeypatch):
    f = tmp_path / "runs.jsonl"
    monkeypatch.setattr(server, "RUNS_FILE", str(f))
    server.append_run({"run_id": "r1", "timestamp": 100, "target": "h265",
                       "count": 2, "bytes_before": 900, "bytes_after": 400})
    server.append_run({"run_id": "r2", "timestamp": 200, "target": "h264",
                       "count": 1, "bytes_before": 100, "bytes_after": 60})
    assert [r["run_id"] for r in _read_jsonl(f)] == ["r1", "r2"]


def test_load_runs_returns_newest_first(server, tmp_path, monkeypatch):
    f = tmp_path / "runs.jsonl"
    _write_jsonl(f, [{"run_id": "alt", "timestamp": 10, "count": 1, "bytes_before": 5, "bytes_after": 2},
                     {"run_id": "neu", "timestamp": 99, "count": 1, "bytes_before": 5, "bytes_after": 2}])
    monkeypatch.setattr(server, "RUNS_FILE", str(f))
    assert [r["run_id"] for r in server.load_runs()] == ["neu", "alt"]


def test_load_runs_skips_broken_lines(server, tmp_path, monkeypatch):
    f = tmp_path / "runs.jsonl"
    f.write_text('{"run_id":"ok","timestamp":1,"count":1,"bytes_before":9,"bytes_after":3}\nkaputt\n\n')
    monkeypatch.setattr(server, "RUNS_FILE", str(f))
    assert [r["run_id"] for r in server.load_runs()] == ["ok"]


def test_load_runs_without_file_is_empty(server, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNS_FILE", str(tmp_path / "fehlt.jsonl"))
    assert server.load_runs() == []


# ─── Migration ───────────────────────────────────────────────────────────────
def test_migrate_runs_builds_runs_from_existing_history(server, tmp_path):
    hist, runs, stats = tmp_path / "h.jsonl", tmp_path / "r.jsonl", tmp_path / "s.json"
    _write_jsonl(hist, [_entry("r1", "a.mkv", 1000, 400, 100),
                        _entry("r1", "b.mkv", 2000, 900, 110),
                        _entry("r2", "c.mkv", 3000, 1000, 200)])
    assert server.migrate_runs(str(hist), str(runs), str(stats)) == 2
    out = {r["run_id"]: r for r in _read_jsonl(runs)}
    assert out["r1"]["count"] == 2 and out["r1"]["bytes_before"] == 3000 and out["r1"]["bytes_after"] == 1300
    assert out["r2"]["count"] == 1 and out["r2"]["timestamp"] == 200


def test_migrate_runs_is_idempotent(server, tmp_path):
    hist, runs, stats = tmp_path / "h.jsonl", tmp_path / "r.jsonl", tmp_path / "s.json"
    _write_jsonl(hist, [_entry("r1", "a.mkv", 1000, 400, 100)])
    assert server.migrate_runs(str(hist), str(runs), str(stats)) == 1
    assert server.migrate_runs(str(hist), str(runs), str(stats)) == 0


def test_migrate_runs_recovers_cleared_history_from_stats(server, tmp_path):
    """Der Auslöser: Verlauf gelöscht, stats.json hat die Bilanz noch."""
    hist, runs, stats = tmp_path / "h.jsonl", tmp_path / "r.jsonl", tmp_path / "s.json"
    hist.write_text("")
    stats.write_text(json.dumps({"runs": 10, "files_converted": 84, "files_skipped": 0,
                                 "bytes_before": 196373814586, "bytes_after": 56409484408,
                                 "last_run": 1789030285.4}))
    assert server.migrate_runs(str(hist), str(runs), str(stats)) == 1
    r = _read_jsonl(runs)[0]
    assert r["count"] == 84 and r["bytes_before"] == 196373814586 and r["bytes_after"] == 56409484408
    assert r["timestamp"] == pytest.approx(1789030285.4)
    assert r["runs"] == 10          # der Sammeleintrag weiß, wie viele Läufe er zusammenfasst


def test_migrate_runs_adds_only_the_gap_stats_knows_about(server, tmp_path):
    """Verlauf teilweise vorhanden: der Sammeleintrag trägt nur die Differenz."""
    hist, runs, stats = tmp_path / "h.jsonl", tmp_path / "r.jsonl", tmp_path / "s.json"
    _write_jsonl(hist, [_entry("r1", "a.mkv", 1000, 400, 500)])
    stats.write_text(json.dumps({"runs": 3, "files_converted": 5, "bytes_before": 9000,
                                 "bytes_after": 3400, "last_run": 500}))
    assert server.migrate_runs(str(hist), str(runs), str(stats)) == 2
    gap = [r for r in _read_jsonl(runs) if r["run_id"] != "r1"][0]
    assert gap["count"] == 4 and gap["bytes_before"] == 8000 and gap["bytes_after"] == 3000
    assert gap["timestamp"] < 500          # der Sammeleintrag liegt vor dem ältesten echten Lauf


def test_migrate_runs_without_stats_and_history_writes_nothing(server, tmp_path):
    hist, runs, stats = tmp_path / "h.jsonl", tmp_path / "r.jsonl", tmp_path / "s.json"
    hist.write_text("")
    assert server.migrate_runs(str(hist), str(runs), str(stats)) == 0
    assert not runs.exists()


def test_migrate_runs_ignores_stats_smaller_than_history(server, tmp_path):
    """stats.json hinkt hinterher – dann gibt es keinen Sammeleintrag mit negativen Bytes."""
    hist, runs, stats = tmp_path / "h.jsonl", tmp_path / "r.jsonl", tmp_path / "s.json"
    _write_jsonl(hist, [_entry("r1", "a.mkv", 5000, 2000, 100)])
    stats.write_text(json.dumps({"runs": 1, "files_converted": 1, "bytes_before": 10, "bytes_after": 5}))
    assert server.migrate_runs(str(hist), str(runs), str(stats)) == 1
    assert [r["run_id"] for r in _read_jsonl(runs)] == ["r1"]


# ─── Gezieltes Löschen ───────────────────────────────────────────────────────
def test_delete_history_entry_removes_exactly_one(server, tmp_path, monkeypatch):
    f = tmp_path / "h.jsonl"
    _write_jsonl(f, [_entry("r1", "a.mkv", 1000, 400, 100),
                     _entry("r1", "b.mkv", 2000, 900, 110),
                     _entry("r2", "a.mkv", 3000, 1000, 200)])
    monkeypatch.setattr(server, "HISTORY_FILE", str(f))
    assert server.delete_history_entry("r1", "/media/a.mkv", 100) == 1
    left = [(e["run_id"], e["name"]) for e in _read_jsonl(f)]
    assert left == [("r1", "b.mkv"), ("r2", "a.mkv")]


def test_delete_history_entry_needs_all_three_keys(server, tmp_path, monkeypatch):
    """Gleicher Name in zwei Läufen: der Zeitstempel entscheidet, nichts wird versehentlich doppelt gelöscht."""
    f = tmp_path / "h.jsonl"
    _write_jsonl(f, [_entry("r1", "a.mkv", 1000, 400, 100),
                     _entry("r2", "a.mkv", 3000, 1000, 200)])
    monkeypatch.setattr(server, "HISTORY_FILE", str(f))
    assert server.delete_history_entry("r1", "/media/a.mkv", 999) == 0
    assert len(_read_jsonl(f)) == 2


def test_delete_history_run_removes_all_its_entries(server, tmp_path, monkeypatch):
    f = tmp_path / "h.jsonl"
    _write_jsonl(f, [_entry("r1", "a.mkv", 1000, 400, 100),
                     _entry("r1", "b.mkv", 2000, 900, 110),
                     _entry("r2", "c.mkv", 3000, 1000, 200)])
    monkeypatch.setattr(server, "HISTORY_FILE", str(f))
    assert server.delete_history_run("r1") == 2
    assert [e["name"] for e in _read_jsonl(f)] == ["c.mkv"]


def test_deleting_entries_leaves_the_run_summary_alone(server, tmp_path, monkeypatch):
    """Die Festlegung aus dem Design: Löschen räumt die Liste, nie die Bilanz."""
    h, r = tmp_path / "h.jsonl", tmp_path / "r.jsonl"
    _write_jsonl(h, [_entry("r1", "a.mkv", 1000, 400, 100)])
    _write_jsonl(r, [{"run_id": "r1", "timestamp": 100, "count": 1, "bytes_before": 1000, "bytes_after": 400}])
    monkeypatch.setattr(server, "HISTORY_FILE", str(h))
    monkeypatch.setattr(server, "RUNS_FILE", str(r))
    server.delete_history_run("r1")
    assert _read_jsonl(h) == []
    assert [x["run_id"] for x in server.load_runs()] == ["r1"]
