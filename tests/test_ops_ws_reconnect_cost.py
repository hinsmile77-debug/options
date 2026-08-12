"""재연결을 **비용**으로 읽는다 + 사후 평가를 **체인 입력 출처**로 가른다 (2026-08-12 고도화 1·5).

## 고도화 1이 필요했던 이유

08-12에 WS가 31회 끊겼고 그것이 레짐 30분을 먹었다. 그런데 종전 지표로는 둘 다 안 보였다:

  · 단절은 `qualitative.ws_reconnect`(최초 1건)와 `failures`(재연결 후 재단절)로 **나뉘어**
    세어졌고, **시각이 없어** 「09~10시에 몰렸다」를 물을 수 없었다.
  · 레짐 결손은 **어디에도 없었다** — 봉은 멀쩡히 적재됐고 레짐만 조용히 빠졌다.

그 편중이 곧 진단이었다: 31회는 KIS가 아니라 09:13의 단 한 번이 연 **자기지속 루프**였다(§7-1).

## 고도화 5가 판정하는 것

정규장 판단의 96%가 stale이었다는 발견이 「위상을 옮기자」(Fix#10)로 이어졌는데,
**늙은 체인을 본 판단이 실제로 못 맞혔는지는 아무도 재지 않았다.** 차이가 없다면 그 레버의
가치는 우리가 생각한 것보다 작다 — 이 표가 그 질문에 답한다.
"""

from __future__ import annotations

from datetime import date

from mahdi.ops import log_metrics, report

TARGET = date(2026, 8, 12)


def _line(message: str, at: str = "09:13:30", level: str = "WARNING") -> str:
    return f"2026-08-12 {at},345 {level}:mahdi.main:{message}"


def _parse(*lines: str) -> dict:
    return log_metrics.parse_day(list(lines), TARGET)


# ===== 고도화 1 — 단절 계량 =====


def test_both_disconnect_lines_are_counted_on_one_axis():
    """최초 끊김과 재연결 후 재단절은 **같은 사건**이다 — 둘 다 재구독과 관측 공백을 만든다."""
    metrics = _parse(
        _line("WS 연결 끊김 — 5초 후 재연결 시도"),
        _line("WS 재연결 후 다시 끊김 — 5초 후 재시도", at="09:14:59"),
    )
    assert metrics["ws_disconnect"]["count"] == 2
    # 종전 지표를 깨지 않는다 — 「WS 연결 끊김」 마커는 그대로 세어진다.
    assert metrics["qualitative"]["ws_reconnect"] == 1


def test_a_retry_attempt_failure_is_not_a_new_disconnect():
    """이미 끊긴 상태의 재시도 실패는 새 단절이 아니다.

    게다가 그 줄은 `WarningThrottle`이 60초당 1건으로 누른다 — 세면 **억제 정책이 곧 지표가 된다.**
    """
    assert _parse(_line("WS 재연결 시도 실패 — 10초 후 재시도"))["ws_disconnect"]["count"] == 0


def test_disconnects_are_bucketed_by_hour_so_the_clustering_shows():
    ws = _parse(
        _line("WS 연결 끊김 — 5초 후 재연결 시도", at="09:13:30"),
        _line("WS 재연결 후 다시 끊김 — 5초 후 재시도", at="09:14:59"),
        _line("WS 재연결 후 다시 끊김 — 5초 후 재시도", at="10:09:59"),
    )["ws_disconnect"]

    assert ws["by_hour"] == {"09시": 2, "10시": 1}
    assert ws["busiest_hour"] == "09시"
    assert ws["first_at"] == "09:13"
    assert ws["last_at"] == "10:09"


def test_a_quiet_day_reports_zero_without_pretending_to_know_a_threshold():
    ws = _parse(_line("아무 일도 없었다", level="INFO"))["ws_disconnect"]
    assert ws["count"] == 0
    assert ws["by_hour"] == {}
    assert ws["first_at"] is None


def test_report_prints_the_reconnect_cost_and_the_lost_regime_minutes():
    """§11-2 — **재연결 1회당 잃은 분**이 Fix#3의 유일한 직접 지표다."""
    out = report.render(
        {"date": "2026-08-12", "ws_disconnect": {
            "count": 31, "by_hour": {"09시": 22, "10시": 9},
            "busiest_hour": "09시", "busiest_hour_count": 22,
            "first_at": "09:13", "last_at": "10:09",
        }},
        db_metrics={"regime_vs_futures_bars": {
            "available": True, "futures_symbol": "A01609",
            "futures_bar_minutes": 406, "regime_minutes": 376, "gap": 30,
            "minutes": ["09:13", "09:14"],
        }},
    )
    assert "## 11-2. WS 재연결" in out
    assert "31회" in out
    assert "0.97" in out          # 30분 / 31회
    assert "임계를 걸지 않는다" in out


def test_report_says_it_cannot_judge_when_there_were_no_reconnects():
    """**재연결이 0인 날에는 gap == 0이 「fix가 일했다」가 아니다** — 일할 일이 없었다."""
    out = report.render(
        {"date": "2026-08-13", "ws_disconnect": {
            "count": 0, "by_hour": {}, "busiest_hour": None, "busiest_hour_count": 0,
            "first_at": None, "last_at": None,
        }},
        db_metrics={"regime_vs_futures_bars": {
            "available": True, "futures_symbol": "A01609",
            "futures_bar_minutes": 400, "regime_minutes": 400, "gap": 0, "minutes": [],
        }},
    )
    assert "재연결 0회" in out
    assert "fix를 검정하지 못한다" in out


# ===== 고도화 5 — 사후 평가 × 체인 입력 출처 =====


def test_report_splits_outcomes_by_chain_input_source():
    """이 표가 **Fix#10(위상 레버)을 켤 가치 자체**를 판정한다."""
    out = report.render(
        {"date": "2026-08-12"},
        db_metrics={"decision_outcomes": {"by_chain_input": {
            "available": True,
            "sources": {
                "current": {"entries": 11, "horizons": {
                    "5m": {"sample": 11, "hit_pct": 54.5, "abs_move_pct": 0.289},
                    "15m": {"sample": 10, "hit_pct": 70.0, "abs_move_pct": 0.557},
                }},
                "stale": {"entries": 258, "horizons": {
                    "5m": {"sample": 250, "hit_pct": 54.8, "abs_move_pct": 0.190},
                    "15m": {"sample": 250, "hit_pct": 62.0, "abs_move_pct": 0.351},
                }},
            },
        }}},
    )
    assert "## 14-4. 사후 평가 × 체인 입력 출처" in out
    assert "54.5% (11)" in out and "62.0% (250)" in out
    assert "하루로 결론 내지 않는다" in out


def test_the_split_table_is_not_broken_by_pipes_in_its_header():
    """`|이동폭|`을 헤더에 쓰면 마크다운 표가 깨진다 — 도입 당일 실제로 그랬다."""
    out = report.render(
        {"date": "2026-08-12"},
        db_metrics={"decision_outcomes": {"by_chain_input": {
            "available": True,
            "sources": {"stale": {"entries": 1, "horizons": {
                "5m": {"sample": 1, "hit_pct": 100.0, "abs_move_pct": 0.1},
            }}},
        }}},
    )
    lines = out.splitlines()
    header_index = next(i for i, line in enumerate(lines) if line.startswith("| 체인 입력"))
    assert lines[header_index].count("|") == lines[header_index + 1].count("|")


def test_missing_chain_input_split_says_so_instead_of_faking_a_table():
    out = report.render(
        {"date": "2026-08-12"},
        db_metrics={"decision_outcomes": {"by_chain_input": {
            "available": False, "reason": "chain_input_source 미기록",
        }}},
    )
    assert "집계 없음" in out
