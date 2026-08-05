"""이미 있는 지표끼리 **서로 모순되는지** 본다 (2026-08-05 고도화#3).

## 왜 이 모듈이 생겼는가

2026-08-05 리포트는 같은 문서 안에서 **논리적으로 모순되는 두 답**을 냈다:

  §14  감마플립 산출률 4.5% (22/494분)
  §15  광폭 감마플립 — 세 북 모두 **없음** (방문 행사가 전 폭 ±4.2%로 탐색)

광폭으로 훑어도 부호 전환이 없는데 좁은 창을 쓰는 라이브 판단이 22번 flip을 냈다면, 그 22건은
**시장 구조가 아니라 계산 결함**이다. 실제로 전수 대조해 보니 21건이 수집 행사가 창 밖의
외삽이었다(§2-5). **그 대조를 사람이 손으로 하기 전까지 아무도 몰랐다** — 리포트는 두 값을
나란히 인쇄해 놓고 서로 비교하지는 않았다.

## 이 모듈의 입장

**자동 리포트의 다음 진화 방향은 지표를 더 늘리는 것이 아니다.** 지표는 이미 17개 절로 충분히
많고, 08-05의 실패는 지표가 없어서가 아니라 **있는 지표를 서로 안 맞춰봐서** 났다.

## 무엇을 여기 두고 무엇을 안 두는가

여기 두는 것은 **서로 다른 절의 지표를 맞춰보는 규칙**이다. 한 절 안에서 끝나는 대조
(예: §4의 "로그 기준 결손 vs DB 기준 0행")는 그 절의 렌더러가 직접 하는 편이 읽기 좋다 —
맥락이 바로 옆에 있기 때문이다.

판정하지 않고 **모순을 지적만 한다.** 어느 쪽이 틀렸는지는 사람이 정한다(README 규약:
"도구는 판정하지 않는다").
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Finding:
    """교차 점검 1건. `sections`는 이 모순에 관여한 리포트 절 번호다."""

    id: str
    sections: tuple[str, ...]
    summary: str
    detail: str


# §12 먼슬리 커버리지를 "충분하다"고 볼 선 — 이 위인데 레그가 얇으면 그 둘이 어긋난 것이다.
_COVERAGE_OK_PCT = 95.0
# 먼슬리 레그가 설계값 미만인 분의 비율이 이 값을 넘으면 "얇다"고 본다.
_THIN_LEG_PCT = 25.0


def _dig(source: dict | None, path: str):
    from mahdi.ops.report import dig

    return dig(source or {}, path)


def evaluate(metrics: dict | None, db_metrics: dict | None) -> list[Finding]:
    """
    입력: 로그 지표 dict, DB 지표 dict(둘 다 없을 수 있다).
    계산: 아래 규칙을 순서대로 적용해 **모순이 성립하는 것만** 돌려준다.
    해석: 결과가 비어 있으면 "오늘은 지표끼리 어긋나지 않았다"이지 "전부 정상"이 아니다 —
         각 절의 개별 경고는 그대로 읽어야 한다.
    실패 조건: 없음 — 입력이 없는 규칙은 조용히 건너뛴다(계측 전인 날에 거짓 경고를 만들지
              않는다).
    """
    out: list[Finding] = []
    for rule in (_gamma_flip_vs_wide_landscape, _coverage_vs_leg_thickness, _backoff_vs_kis_latency):
        finding = rule(metrics, db_metrics)
        if finding is not None:
            out.append(finding)
    return out


def _gamma_flip_vs_wide_landscape(metrics: dict | None, db_metrics: dict | None) -> Finding | None:
    """§14 라이브 감마플립 산출률 vs §15 광폭 OI 지형의 flip 가능성.

    **08-05을 그대로 잡는 규칙이다.** 광폭(방문 행사가 전 폭)으로 훑어도 GEX 부호가 안 바뀌는데
    좁은 창(ATM±2)을 쓰는 라이브 판단이 flip을 냈다면, 그 flip은 레그가 없는 구간의 외삽이다.
    """
    flip_count = _dig(db_metrics, "signal_reach.gamma_flip_count")
    landscape = (db_metrics or {}).get("wide_oi_landscape") or []
    if not flip_count or not landscape:
        return None
    # `wide_gamma_flip`이 None인 북 = 그 북은 방문 전 구간에서 부호가 안 바뀐다.
    if any(book.get("wide_gamma_flip") is not None for book in landscape):
        return None

    books = ", ".join(str(b.get("expiry")) for b in landscape)
    pct = _dig(db_metrics, "signal_reach.gamma_flip_pct")
    out_of_range = _dig(db_metrics, "signal_reach.gamma_flip_out_of_range_count")
    detail = (
        f"§15는 **{len(landscape)}개 북 전부**({books})에서 광폭 탐색으로도 부호 전환이 "
        f"없다고 보고했는데, §14는 라이브 판단이 **{flip_count}분**({pct}%)에 감마플립을 냈다고 "
        "보고했다. 좁은 창(ATM±2)이 광폭보다 더 많은 전환을 볼 수는 없다 — "
        "**그 flip은 시장 구조가 아니라 레그 없는 구간의 외삽일 가능성이 높다.**"
    )
    if out_of_range:
        detail += f" §14의 「범위 밖」 {out_of_range}건이 그 증거다."
    return Finding(
        id="gamma-flip-vs-wide-landscape",
        sections=("14", "15"),
        summary=f"광폭 탐색은 flip 없음인데 라이브는 {flip_count}분에 flip을 냈다",
        detail=detail,
    )


def _coverage_vs_leg_thickness(metrics: dict | None, db_metrics: dict | None) -> Finding | None:
    """§12 먼슬리 커버리지 vs §12 먼슬리 레그 완전성 — "있다"와 "충분하다"는 다르다.

    커버리지는 *그 분에 먼슬리 행이 있는가*만 본다. 08-05은 커버리지 98.8%인데 레그 10개 미만이
    38.2%였다 — 데이터는 거의 매 분 있었고 **매번 얇았다.**
    """
    coverage = _dig(db_metrics, "monthly_coverage.coverage_pct")
    thin_pct = _dig(db_metrics, "monthly_leg_completeness.below_design_pct")
    if coverage is None or thin_pct is None:
        return None
    if coverage < _COVERAGE_OK_PCT or thin_pct < _THIN_LEG_PCT:
        return None

    design = _dig(db_metrics, "monthly_leg_completeness.design_legs")
    below_min = _dig(db_metrics, "monthly_leg_completeness.below_flip_minimum_count")
    return Finding(
        id="coverage-vs-leg-thickness",
        sections=("12",),
        summary=f"커버리지 {coverage:.1f}%인데 먼슬리 레그가 {thin_pct}% 분에서 설계값 미만이다",
        detail=(
            f"커버리지는 *그 분에 먼슬리 행이 있는가*만 잰다 — **몇 개인지는 안 본다.** "
            f"설계 {design}레그 미만이 **{thin_pct}%**이고 그중 BS 최소 미달이 "
            f"**{below_min}분**이라면, 커버리지 {coverage:.1f}%는 *데이터가 있다*는 뜻일 뿐 "
            "*판단 입력이 충분하다*는 뜻이 아니다. GEX/감마플립은 이 얇은 체인으로 계산됐다."
        ),
    )


def _backoff_vs_kis_latency(metrics: dict | None, db_metrics: dict | None) -> Finding | None:
    """§6 백오프 vs §9-1 KIS 응답시간 — 밀림/타임아웃의 책임이 우리인가 KIS인가.

    08-04 §2-6이 **미리 적어둔 판정표**의 자동화다: 페이서 대기가 크면 예약 큐 경합(우리 책임),
    HTTP가 크면 KIS 서버. 우리 쪽 압력(백오프)이 낮은데 KIS p95 경고가 떠 있으면 그날의 손실은
    스케줄링으로 줄일 수 없다 — **폴링 폭을 줄이는 것 말고는 우리가 할 수 있는 게 없다.**
    """
    latency_warnings = _dig(metrics, "rest_latency.warnings") or []
    if not latency_warnings:
        return None
    backoff_max = _dig(metrics, "backoff.max_multiplier")
    timeouts = _dig(metrics, "qualitative.read_timeout") or 0
    if backoff_max is None or backoff_max > 2.5 or not timeouts:
        return None

    return Finding(
        id="backoff-vs-kis-latency",
        sections=("6", "9-1"),
        summary=(
            f"우리 쪽 압력은 낮은데(백오프 최대 {backoff_max:.2f}배) "
            f"KIS 지연 경고 {len(latency_warnings)}건 · ReadTimeout {timeouts:,}건"
        ),
        detail=(
            "08-04 §2-6이 미리 적어둔 판정표대로라면 이 조합은 **KIS 귀속**이다 — 페이서가 "
            "한가한데 타임아웃이 나는 것은 예약 큐 경합이 아니다. **스케줄링 최적화로는 줄지 "
            "않는다**(08-04에 이미 페이서 대기가 1,661 → 229초로 소진됐다). 남은 선택지는 "
            "타임아웃/재시도 정책이나 호출 총량이며, 둘 다 대가가 있으므로 며칠 값을 쌓고 "
            "사람이 정한다(`hypotheses.yaml` 2026-08-04-p5의 사전 대응 규칙)."
        ),
    )
