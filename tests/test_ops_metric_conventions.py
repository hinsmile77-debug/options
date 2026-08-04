"""지표 규약의 **기계적 강제** — 2026-08-04(운영점검보고서 고도화#1).

## 왜 규약이 필요한가

같은 종류의 결함이 반복해서 났고, 매번 개별 수정으로 끝냈다:

  **로그 계약 파손** (규약 A)
    08-03에 `rest_client`의 로그 세 곳을 바꾸자 `log_metrics` 파서 셋이 조용히 죽었다.
    08-04 리포트는 그것을 `느린 REST 호출 0건(▼933 ✅)`이라는 **개선으로 표시**했다.

  **체인 조회에 시각 경계 누락** (규약 B) — **세 번 반복됐다**
    1) 08-03: `latest_option_chain()` — 시간 범위도 만기 필터도 없어 246레그 중 오늘
       수집분이 10개, 최고령이 4주였다.
    2) 08-04: `latest_expiry_liquidity()` — 같은 패턴. COCKPIT이 어제 만기를 표시했다.
    3) 08-04: `book_gamma_map()` — **08-03 수정과 같은 날 작성된 새 파일에서 재발.**
       "장 마지막 스냅샷"이라 적어놓고 하루치 전 행사가를 합쳐 놓았다.

개별 수정으로는 네 번째가 온다. 그래서 규약을 테스트로 못박는다.

  규약 A  로그 문구는 emit 측 모듈 상수다 → `test_ops_log_metrics_contract.py`
  규약 B  체인 스냅샷은 `db._chain_snapshot()` 하나만 만든다 → **이 파일**
  규약 C  "0건" 보고는 증명을 동반한다 → `log_metrics._parser_audit()`
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAHDI = PROJECT_ROOT / "mahdi"

# 체인 스냅샷의 지문: (만기, 행사가, 콜/풋)별 최신 1건. 이 형태를 쓰는 SQL은 곧 체인 스냅샷이다.
_CHAIN_SNAPSHOT_SQL = re.compile(
    r"DISTINCT\s+ON\s*\(\s*expiry\s*,\s*strike\s*,\s*option_type\s*\)", re.IGNORECASE
)

# 규약 B 허용목록 — 여기 없는 파일이 체인 스냅샷 SQL을 쓰면 테스트가 막는다.
_CHAIN_SNAPSHOT_ALLOWLIST = {
    # 유일한 정본. 신선도 창(CHAIN_SNAPSHOT_MAX_AGE_MINUTES)과 만기 경계가 여기 한 곳에 있다.
    "data/db.py",
    # 2026-08-04 §2-3/고도화#4 — **의도적인 예외**: "그날 방문한 전 행사가"를 보는 것이
    # 이 함수의 존재 이유다(신선도 창을 적용하면 광폭 실험 자체가 불가능하다). 그래서
    # 함수명·리포트 표기 둘 다 "광폭"임을 명시하고, 시각 경계가 없다는 사실을 docstring에 적었다.
    "ops/db_metrics.py",
}


def _python_sources() -> list[Path]:
    return [p for p in MAHDI.rglob("*.py") if "__pycache__" not in p.parts]


def _without_comments(path: Path) -> str:
    """주석 줄을 뺀 소스.

    아래 "이 문자열이 있으면 안 된다" 류의 검사는 **렌더되는 리터럴**을 겨눈다. 그런데 이
    리포지터리는 고친 결함을 주석에 그대로 인용하는 습관이 있어(그게 다음 사람을 살린다),
    주석까지 훑으면 *결함을 설명한 문장* 때문에 테스트가 깨진다 — 기록을 지우게 만드는
    테스트는 잘못된 테스트다.
    """
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def test_chain_snapshot_sql_lives_in_one_place():
    """규약 B: 체인 스냅샷 SQL은 `db._chain_snapshot()` 하나만 만든다.

    이 결함은 2026-08-03~04에 **세 번** 재발했다(모듈 docstring 참고). 새로 SQL을 쓰는 대신
    `db.latest_option_chain()` / `db.option_chain_as_of()`를 쓰면 신선도 창과 만기 경계가
    공짜로 따라온다 — 그리고 라이브 판단과 리포트가 **같은 체인**을 보게 된다.
    """
    offenders = sorted(
        str(path.relative_to(MAHDI)).replace("\\", "/")
        for path in _python_sources()
        if _CHAIN_SNAPSHOT_SQL.search(_without_comments(path))
    )
    unexpected = [p for p in offenders if p not in _CHAIN_SNAPSHOT_ALLOWLIST]
    assert not unexpected, (
        f"체인 스냅샷 SQL이 새 위치에 생겼다: {unexpected}. "
        "db.latest_option_chain()/option_chain_as_of()를 쓰거나, 정말 예외라면 "
        "_CHAIN_SNAPSHOT_ALLOWLIST에 **이유와 함께** 추가할 것(2026-08-04 고도화#1 규약 B)."
    )


def test_chain_snapshot_has_both_freshness_and_expiry_boundaries():
    """정본 SQL이 두 경계를 실제로 걸고 있는지 — 규약이 가리키는 대상 자체를 지킨다."""
    from mahdi.data import db

    sql = db._CHAIN_SNAPSHOT_SQL
    assert "timestamp <= %s" in sql, "as_of 상한이 없다"
    assert "timestamp >= %s" in sql, "신선도 하한이 없다(2026-08-03 §2-2에서 4주치가 섞였다)"
    assert "expiry >= %s" in sql, "만기 경계가 없다(만기 지난 레그가 t_years=0으로 통과한다)"
    assert db.CHAIN_SNAPSHOT_MAX_AGE_MINUTES > 0


def test_slow_call_threshold_is_not_duplicated_as_a_literal():
    """규약 A의 부수 규칙: 임계값을 문서 문자열에 박아두지 않는다.

    08-03에 임계를 3.0 → 5.0으로 올렸는데 `report.py`의 "임계(3초) 초과 호출 없음" 문자열만
    3초로 남아 08-04 리포트 §9가 **틀린 임계를 인쇄**했다. 상수를 인용하면 갈라질 수 없다.
    """
    text = _without_comments(MAHDI / "ops" / "report.py")
    assert "임계(3초)" not in text
    assert "SLOW_CALL_LOG_THRESHOLD_SECONDS" in text


def test_theoretical_member_ceiling_is_derived_not_hardcoded():
    """규약 A의 부수 규칙: "이론 최대 N개"를 손으로 적지 않는다.

    08-04까지 `db_metrics`가 3으로 하드코딩하고 주석에 *"orderflow는 파이프라인 미구현"* 이라고
    적어뒀는데 그 전제가 사실이 아니었다(`market_raw_1m.ofi`는 선물 410분 전부 채워져 있었다).
    그 결과 죽은 멤버 하나가 **지표의 분모 안으로 숨었다.**
    """
    from mahdi.fusion.signal_layer import IMPLEMENTED_MEMBER_FIELDS
    from mahdi.ops import db_metrics

    assert db_metrics.SIGNAL_REACH_WARNINGS["member_count_max_min"] == len(IMPLEMENTED_MEMBER_FIELDS)
    report_text = _without_comments(MAHDI / "ops" / "report.py")
    assert "이론 최대 3개" not in report_text


def test_chain_age_warning_threshold_follows_the_snapshot_window():
    """신선도 창을 바꾸면 경고 임계도 따라와야 한다 — 2026-08-04 Fix#6b(10분 → 5분)."""
    from mahdi.data import db
    from mahdi.ops import db_metrics

    assert (
        db_metrics.SIGNAL_REACH_WARNINGS["chain_age_seconds_max"]
        == db.CHAIN_SNAPSHOT_MAX_AGE_MINUTES * 60 * 1.5
    )


def test_strike_window_design_matches_the_live_poller():
    """`db_metrics`의 설계 창 상수가 관측 루프의 실제 값과 같아야 한다(고도화#3).

    두 값이 갈라지면 "창 정합률"이 조용히 틀린 기준으로 계산된다.
    """
    from mahdi import main
    from mahdi.ops import db_metrics

    assert db_metrics.STRIKE_WINDOW_EACH_SIDE == main.STRIKES_EACH_SIDE
    assert db_metrics.KOSPI200_STRIKE_INTERVAL == main.KOSPI200_OPTION_STRIKE_INTERVAL
