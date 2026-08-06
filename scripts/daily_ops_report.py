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
                db_metrics_result = db_metrics_module.collect(conn, target, elapsed_minutes=elapsed)
        except Exception:
            logger.warning("DB 집계 실패 — 로그 지표만으로 리포트를 낸다", exc_info=True)

    previous = None
    prev_path = out_dir / f"{log_metrics.previous_business_day(target).isoformat()}_지표.json"
    if prev_path.exists():
        try:
            previous = json.loads(prev_path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("전일 지표 사이드카 읽기 실패 — 델타를 생략한다", exc_info=True)

    hypothesis_results = None
    try:
        from mahdi.ops import hypotheses

        hypothesis_results = hypotheses.evaluate(
            hypotheses.load(PROJECT_ROOT / "docs" / "동작점검" / "hypotheses.yaml"),
            target,
            metrics,
            db_metrics_result,
        )
    except Exception:
        logger.warning("가설 검정 건너뜀", exc_info=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{target.isoformat()}_지표.json"
    payload = dict(metrics)
    if db_metrics_result:
        payload["db"] = db_metrics_result
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    md_path = out_dir / f"{target.isoformat()}_지표.md"
    md_path.write_text(
        report.render(metrics, previous=previous, db_metrics=db_metrics_result, hypotheses=hypothesis_results),
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
