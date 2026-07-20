from datetime import datetime

from scripts import log_marketclose_stop


def test_log_gap_writes_marker_and_no_gap_line_when_none_exists(monkeypatch, tmp_path):
    fake_log_file = tmp_path / "logs" / "premarket_startup.log"
    fake_marker = tmp_path / "logs" / ".last_marketclose_stop.txt"
    monkeypatch.setattr(log_marketclose_stop, "LOG_FILE", fake_log_file)
    monkeypatch.setattr(log_marketclose_stop, "LAST_STOP_MARKER_FILE", fake_marker)

    now = datetime(2026, 7, 20, 15, 45, 0)
    monkeypatch.setattr(log_marketclose_stop.db, "local_now", lambda: now)

    log_marketclose_stop.log_gap_and_update_marker()

    assert "직전 정상 종료 기록 없음" in fake_log_file.read_text(encoding="utf-8")
    assert fake_marker.read_text(encoding="utf-8") == now.isoformat()


def test_log_gap_reports_elapsed_hours_and_updates_marker(monkeypatch, tmp_path):
    # 07-17(금) 15:45 장마감 자동 종료가 스케줄대로 실행되지 못했던 사례처럼, 예약 실행이
    # 하루 이상 건너뛰면 다음 정상 종료 시점에 경과 시간이 그대로 로그에 남아야 한다.
    fake_log_file = tmp_path / "logs" / "premarket_startup.log"
    fake_marker_dir = tmp_path / "logs"
    fake_marker_dir.mkdir()
    fake_marker = fake_marker_dir / ".last_marketclose_stop.txt"
    last = datetime(2026, 7, 16, 15, 45, 0)
    fake_marker.write_text(last.isoformat(), encoding="utf-8")
    monkeypatch.setattr(log_marketclose_stop, "LOG_FILE", fake_log_file)
    monkeypatch.setattr(log_marketclose_stop, "LAST_STOP_MARKER_FILE", fake_marker)

    now = datetime(2026, 7, 20, 15, 45, 0)  # 4일(96시간) 뒤 — 목요일 이후 정상 종료가 없었던 경우
    monkeypatch.setattr(log_marketclose_stop.db, "local_now", lambda: now)

    log_marketclose_stop.log_gap_and_update_marker()

    logged = fake_log_file.read_text(encoding="utf-8")
    assert "직전 정상 종료: 2026-07-16 15:45:00 (96.0시간 전)" in logged
    assert fake_marker.read_text(encoding="utf-8") == now.isoformat()


def test_log_gap_handles_corrupted_marker_and_recovers(monkeypatch, tmp_path):
    fake_log_file = tmp_path / "logs" / "premarket_startup.log"
    fake_marker_dir = tmp_path / "logs"
    fake_marker_dir.mkdir()
    fake_marker = fake_marker_dir / ".last_marketclose_stop.txt"
    fake_marker.write_text("이건 타임스탬프가 아님", encoding="utf-8")
    monkeypatch.setattr(log_marketclose_stop, "LOG_FILE", fake_log_file)
    monkeypatch.setattr(log_marketclose_stop, "LAST_STOP_MARKER_FILE", fake_marker)

    now = datetime(2026, 7, 20, 15, 45, 0)
    monkeypatch.setattr(log_marketclose_stop.db, "local_now", lambda: now)

    log_marketclose_stop.log_gap_and_update_marker()

    assert "직전 정상 종료 기록 파싱 실패" in fake_log_file.read_text(encoding="utf-8")
    assert fake_marker.read_text(encoding="utf-8") == now.isoformat()  # 손상된 마커도 복구됨


def test_log_gap_appends_without_overwriting_existing_log_content(monkeypatch, tmp_path):
    fake_log_dir = tmp_path / "logs"
    fake_log_dir.mkdir()
    fake_log_file = fake_log_dir / "premarket_startup.log"
    fake_log_file.write_text("이전 로그 줄\n", encoding="utf-8")
    fake_marker = fake_log_dir / ".last_marketclose_stop.txt"
    monkeypatch.setattr(log_marketclose_stop, "LOG_FILE", fake_log_file)
    monkeypatch.setattr(log_marketclose_stop, "LAST_STOP_MARKER_FILE", fake_marker)

    now = datetime(2026, 7, 20, 15, 45, 0)
    monkeypatch.setattr(log_marketclose_stop.db, "local_now", lambda: now)

    log_marketclose_stop.log_gap_and_update_marker()

    logged = fake_log_file.read_text(encoding="utf-8")
    assert logged.startswith("이전 로그 줄\n")
    assert "직전 정상 종료 기록 없음" in logged
