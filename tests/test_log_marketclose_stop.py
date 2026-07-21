import subprocess
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


# 2026-07-21 이상점 대응(운영점검보고서 §3-1/§4): taskkill(창 제목 기반)이 사고 대응 중 수동
# 재시작된 프로세스를 못 찾고도 조용히 넘어간 사례 — 종료 후 실제로 프로세스가 남아있는지
# 커맨드라인 기준으로 재확인하고, 남아있으면 로그+Slack으로 알린다.


def test_count_remaining_parses_powershell_stdout(monkeypatch):
    captured_cmd = []

    def fake_run(cmd, capture_output, text, timeout, check):
        captured_cmd.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="3\n", stderr="")

    monkeypatch.setattr(log_marketclose_stop.subprocess, "run", fake_run)

    assert log_marketclose_stop._count_remaining_mahdi_processes() == 3
    assert captured_cmd[0] == log_marketclose_stop._REMAINING_PROCESS_CHECK_COMMAND


def test_count_remaining_treats_blank_stdout_as_zero(monkeypatch):
    def fake_run(cmd, capture_output, text, timeout, check):
        return subprocess.CompletedProcess(cmd, 0, stdout="\n", stderr="")

    monkeypatch.setattr(log_marketclose_stop.subprocess, "run", fake_run)

    assert log_marketclose_stop._count_remaining_mahdi_processes() == 0


def test_check_remaining_processes_noop_when_none_remain(monkeypatch, tmp_path):
    fake_log_file = tmp_path / "logs" / "premarket_startup.log"
    monkeypatch.setattr(log_marketclose_stop, "LOG_FILE", fake_log_file)
    monkeypatch.setattr(log_marketclose_stop, "_count_remaining_mahdi_processes", lambda: 0)

    alerted = []
    monkeypatch.setattr(log_marketclose_stop.notify, "notify_sync", lambda *a, **k: alerted.append((a, k)))

    log_marketclose_stop.check_remaining_processes_and_alert()

    assert not fake_log_file.exists()
    assert alerted == []


def test_check_remaining_processes_logs_and_alerts_when_processes_remain(monkeypatch, tmp_path):
    fake_log_dir = tmp_path / "logs"
    fake_log_dir.mkdir()
    fake_log_file = fake_log_dir / "premarket_startup.log"
    monkeypatch.setattr(log_marketclose_stop, "LOG_FILE", fake_log_file)
    monkeypatch.setattr(log_marketclose_stop, "_count_remaining_mahdi_processes", lambda: 2)

    now = datetime(2026, 7, 21, 15, 45, 5)
    monkeypatch.setattr(log_marketclose_stop.db, "local_now", lambda: now)

    alerted = []
    monkeypatch.setattr(log_marketclose_stop.notify, "notify_sync", lambda *a, **k: alerted.append((a, k)))

    log_marketclose_stop.check_remaining_processes_and_alert()

    logged = fake_log_file.read_text(encoding="utf-8")
    assert "경고" in logged
    assert "2개" in logged
    assert len(alerted) == 1
    args, kwargs = alerted[0]
    assert "2개" in args[0]
    assert kwargs.get("level") == "WARNING"


def test_check_remaining_processes_swallows_check_failure(monkeypatch, tmp_path):
    fake_log_file = tmp_path / "logs" / "premarket_startup.log"
    monkeypatch.setattr(log_marketclose_stop, "LOG_FILE", fake_log_file)

    def broken_count():
        raise subprocess.TimeoutExpired(cmd="powershell", timeout=20)

    monkeypatch.setattr(log_marketclose_stop, "_count_remaining_mahdi_processes", broken_count)
    alerted = []
    monkeypatch.setattr(log_marketclose_stop.notify, "notify_sync", lambda *a, **k: alerted.append((a, k)))

    log_marketclose_stop.check_remaining_processes_and_alert()  # 예외가 전파되면 이 줄에서 실패

    assert not fake_log_file.exists()
    assert alerted == []
