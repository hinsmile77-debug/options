"""장마감 후 하루치 운영 지표를 자동 집계해 마크다운 + JSON 사이드카로 남긴다.

2026-08-01(운영점검보고서 2026-07-31 §5-2). 10회째 손으로 재작성하던 집계를 고정한다 —
사람이 하는 일이 "표 만들기"에서 "델타가 왜 그런지 해석하기"로 옮겨간다.

실행:
    uv run python scripts/daily_ops_report.py [--date YYYY-MM-DD] [--no-db] [--out-dir DIR]

`stop_mahdi_marketclose.bat`이 taskkill 이후 한 줄로 호출한다 — 그 시점에 관측 루프는 이미
종료돼 **로그가 완결**돼 있고, DB/Redis는 의도적으로 계속 실행 중이라 SQL 집계가 가능하다.
**실패해도 종료 절차를 막으면 안 되므로** 최상위에서 예외를 삼키고 로그만 남긴다.

산출:
    docs/동작점검/auto/YYYY-MM-DD_지표.md    사람이 읽는 표
    docs/동작점검/auto/YYYY-MM-DD_지표.json  기계 판독 — 다음날 델타 계산에 쓴다

**전일 원본 로그가 아니라 전일 결과(JSON)를 보존하는 이유**: 로그는 10MB×10 로테이션이라 약
이틀치만 남는다. 어제 결과를 저장하는 편이 어제 원본을 보존하는 것보다 싸고 확실하다.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mahdi.config.settings import PROJECT_ROOT
from mahdi.ops import log_metrics, report

logger = logging.getLogger("mahdi.daily_ops_report")

LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_OUT_DIR = PROJECT_ROOT / "docs" / "동작점검" / "auto"

# 2026-08-07 고도화#5 — §14-3 멤버 부호 일치율을 나란히 볼 직전 영업일 수.
# 3인 이유: 하루치로는 "갈렸다"와 "부호가 뒤집혀 있다"를 구분할 수 없고, 그 이상 늘리면
# 표가 옆으로 넘쳐 사람이 안 읽는다. 판정 자체는 사람이 하므로 **보이기만 하면 된다.**
MEMBER_SIGN_HISTORY_DAYS = 3


def build(target: date, out_dir: Path, use_db: bool) -> Path:
    """
    계산: 로그 지표 → (선택) DB 지표 → (선택) 전일 델타/가설 검정 → 마크다운 + JSON 저장.
    실패 조건: DB 집계와 가설 검정은 **선택 사항**이라 실패해도 로그만 남기고 나머지를 낸다 —
              부분 결과라도 있는 편이 아무것도 없는 것보다 낫다(get_health_summary와 같은 원칙).
    """
    metrics = log_metrics.parse_day(log_metrics.iter_day_lines(LOG_DIR, target), target)

    db_metrics_result = None
    if use_db:
        try:
            from mahdi.data import db
            from mahdi.ops import db_metrics as db_metrics_module

            # 경과 분 = 사이클이 돈 분 + 결손 분 — 먼슬리 절대 커버리지의 분모다.
            elapsed = (metrics["cycles"]["count"] or 0) + (metrics["cycles"]["missing"]["count"] or 0)
            with db.get_connection() as conn:
                # 2026-08-06 고도화#5 — **집계보다 먼저 계산한다.** 사후 평가는 그날의 판단에
                # 스팟 궤적을 붙이는 일이라 장이 끝난 지금이 유일하게 온전한 시점이고,
                # 아래 `collect()`가 그 결과를 읽어 리포트에 싣는다.
                from mahdi.ops import decision_outcomes

                written = decision_outcomes.compute(conn, target)
                if written:
                    logger.info("판단 사후 평가 %d건 계산", written)
                # 2026-08-10 — `log_cycles`를 넘기면 DB 축의 「0행 분」이 원인별로 갈린다
                # (`db_metrics.attribute_zero_row_causes()`). 두 축을 다 가진 곳이 여기뿐이다.
                db_metrics_result = db_metrics_module.collect(
                    conn, target, elapsed_minutes=elapsed, log_cycles=metrics["cycles"],
                )
        except Exception:
            logger.warning("DB 집계 실패 — 로그 지표만으로 리포트를 낸다", exc_info=True)

    # 2026-08-07 고도화#5 — 전일 하나가 아니라 **직전 영업일들**을 읽는다.
    # `previous`(전일 대비 델타)와 `history`(추세 판정)는 다른 질문이다: 하루치 변화로는
    # 멤버 부호 일치율이 "갈렸다"인지 "뒤집혀 있다"인지 구분할 수 없다(§14-3).
    history: list[dict] = []
    day = target
    for _ in range(MEMBER_SIGN_HISTORY_DAYS):
        day = log_metrics.previous_business_day(day)
        path = out_dir / f"{day.isoformat()}_지표.json"
        if not path.exists():
            continue  # 공휴일/미가동일 — 건너뛰되 더 거슬러 올라가지는 않는다(창은 영업일 고정).
        try:
            history.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            logger.warning("%s 지표 사이드카 읽기 실패 — 그날은 비운다", day, exc_info=True)
    previous = history[0] if history and str(history[0].get("date")) == str(
        log_metrics.previous_business_day(target)
    ) else None

    # 2026-08-12 Fix#6(규약 H) — **가설 검정보다 먼저 읽는다.** 검정이 이 값을 받아야
    # "레버가 꺼진 채 판정하는" 08-12의 오독을 막을 수 있다.
    lever_state = None
    try:
        from mahdi.ops import levers

        lever_state = levers.collect(PROJECT_ROOT)
    except Exception:
        logger.warning("레버 상태 수집 건너뜀", exc_info=True)

    # 2026-08-12 Fix#8 — 워치독 자신의 로그. `log_metrics`와 **다른 파일**을 읽는다(§2-3).
    watchdog_result = None
    try:
        from mahdi.ops import watchdog_metrics

        watchdog_result = watchdog_metrics.collect(LOG_DIR, target)
    except Exception:
        logger.warning("워치독 지표 건너뜀", exc_info=True)

    # **가설이 참조할 수 있으려면 `metrics` 본체에 실려야 한다.** 사이드카에만 넣으면
    # `hypotheses._lookup`이 못 찾아 그 가설이 「경로 없음」으로 영원히 검정 불가가 된다
    # (08-06 §3-1이 정확히 그 사고였고, 도입 당일 `test_ops_hypotheses`가 이것을 잡았다).
    if lever_state:
        metrics["levers"] = lever_state
    if watchdog_result is not None:
        metrics["watchdog"] = watchdog_result

    hypothesis_results = None
    try:
        from mahdi.ops import hypotheses

        hypothesis_results = hypotheses.evaluate(
            hypotheses.load(PROJECT_ROOT / "docs" / "동작점검" / "hypotheses.yaml"),
            target,
            metrics,
            db_metrics_result,
            levers=lever_state,
        )
    except Exception:
        logger.warning("가설 검정 건너뜀", exc_info=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{target.isoformat()}_지표.json"
    # `metrics`에 이미 `levers`/`watchdog`이 실려 있으므로 사이드카에도 그대로 따라간다 —
    # **다음날 "어제는 그 레버가 켜져 있었나"를 물을 수 있어야** 규약 H가 하루짜리 장치로
    # 끝나지 않는다(레버는 그날의 코드 상태라 사후 복원이 불가능하다).
    payload = dict(metrics)
    if db_metrics_result:
        payload["db"] = db_metrics_result
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # 2026-08-18 — 검증 캠페인은 **위 json을 쓴 뒤에** 평가한다. 캠페인의 유일한 입력이
    # `auto/*_지표.json`이므로, 순서를 뒤집으면 **오늘치가 누적에서 빠진다** — 매일 하루씩
    # 뒤처진 표본으로 판정하게 되고 그 사실은 어디에도 안 드러난다.
    campaign_results = None
    try:
        from mahdi.ops import campaign as campaign_module

        campaign_results = campaign_module.evaluate(
            campaign_module.load(PROJECT_ROOT / "docs" / "동작점검" / "validation_campaign.yaml"),
            campaign_module.load_daily_metrics(out_dir, until=target),
        )
    except Exception:
        logger.warning("검증 캠페인 건너뜀", exc_info=True)

    md_path = out_dir / f"{target.isoformat()}_지표.md"
    md_path.write_text(
        report.render(
            metrics, previous=previous, db_metrics=db_metrics_result,
            hypotheses=hypothesis_results, history=history,
            levers=lever_state, watchdog=watchdog_result,
            campaign=campaign_results,
        ),
        encoding="utf-8",
    )
    return md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="마흐디 일일 운영 지표 자동 집계")
    parser.add_argument("--date", help="대상 날짜(YYYY-MM-DD, 기본값 오늘). 로그가 남아 있으면 과거 날짜도 가능")
    parser.add_argument("--no-db", action="store_true", help="Docker가 꺼진 상태에서 로그 집계만 뽑을 때")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
    target = log_metrics.resolve_target_date(args.date, datetime.now())
    path = build(target, args.out_dir, use_db=not args.no_db)
    logger.info("운영 지표 리포트 생성: %s", path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # 장마감 훅에서 호출되므로 여기서 죽어도 종료 절차를 막으면 안 된다.
        logging.getLogger("mahdi.daily_ops_report").warning("운영 지표 리포트 생성 실패", exc_info=True)
        sys.exit(0)
