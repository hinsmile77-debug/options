"""지표 dict → 마크다운 (순수 렌더러, 파일 I/O 없음).

2026-08-01(운영점검보고서 2026-07-31 §5-2). **사람 보고서를 대신 쓰지 않는다** — 표와 전일
델타까지만 낸다. "왜 그런가"는 사람이 쓴다(§5-2 "하지 않을 것" 참고).

전일 지표(`previous`)가 없으면 델타 열을 **생략한다** — 지어내지 않는다.
"""

from __future__ import annotations

from typing import Any, Callable

from mahdi.broker.rest_client import SLOW_CALL_LOG_THRESHOLD_SECONDS
from mahdi.ops import db_metrics as db_metrics_module  # 임계를 리포트에 그대로 인용하기 위함

# 전일 대비 델타를 붙일 핵심 지표. (라벨, 지표 경로, 포맷, 개선 방향)
# 개선 방향: "down"이면 감소가 개선, "up"이면 증가가 개선, None이면 판정하지 않는다.
HEADLINE_METRICS: list[tuple[str, str, str, str | None]] = [
    ("총 REST 호출", "rest.total_calls", "{:,.0f}건", None),
    ("초당 수요", "rest.calls_per_second", "{:.3f}건/초", "down"),
    ("페이서 용량 대비", "rest.capacity_pct", "{:.1f}%", "down"),
    ("적자 시작 배율", "rest.deficit_threshold_multiplier", "{:.2f}배", "up"),
    ("옵션체인 사이클", "cycles.count", "{:,.0f}", "up"),
    ("REST수집 평균", "cycles.rest_seconds.mean", "{:.1f}초", "down"),
    ("60초 초과(밀림)", "overrun.count", "{:,.0f}건", "down"),
    ("최대 밀림", "overrun.max_seconds", "{:.1f}초", "down"),
    ("결손 분(회수 전)", "cycles.missing.count", "{:,.0f}분", "down"),
    ("결손 분(회수 후)", "cycles.missing.unrecovered_count", "{:,.0f}분", "down"),
    ("결손 회수", "catchups.count", "{:,.0f}건", None),
    ("비200 응답", "rest.non_200.count", "{:,.0f}건", "down"),
    ("백오프 최대 배율", "backoff.max_multiplier", "{:.2f}배", "down"),
    ("느린 REST 호출", "slow_calls.count", "{:,.0f}건", "down"),
    ("사람이 읽는 로그 줄", "log_volume.human_lines", "{:,.0f}줄", "down"),
]


def dig(metrics: dict, path: str) -> Any:
    """`"cycles.missing.count"` 같은 점 표기 경로로 중첩 dict를 꺼낸다(없으면 None)."""
    node: Any = metrics
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _fmt(value: Any, spec: str) -> str:
    if value is None:
        return "—"
    try:
        return spec.format(value)
    except (TypeError, ValueError):
        return str(value)


def _delta(current: Any, previous: Any, direction: str | None) -> str:
    if current is None or previous is None or not isinstance(current, (int, float)):
        return "—"
    if not isinstance(previous, (int, float)):
        return "—"
    diff = current - previous
    if abs(diff) < 1e-9:
        return "±0"
    arrow = "▲" if diff > 0 else "▼"
    mark = ""
    if direction is not None:
        improved = (diff < 0) if direction == "down" else (diff > 0)
        mark = " ✅" if improved else " ⚠"
    return f"{arrow}{abs(diff):,.3g}{mark}"


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["_(데이터 없음)_", ""]
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join(row) + " |" for row in rows]
    out.append("")
    return out


def _section(title: str, builder: Callable[[], list[str]]) -> list[str]:
    """지표 그룹마다 독립적으로 렌더링한다 — 하나가 죽어도 나머지는 계속 낸다."""
    try:
        return [f"## {title}", "", *builder()]
    except Exception as exc:  # noqa: BLE001 — 부분 결과라도 있는 편이 낫다
        return [f"## {title}", "", f"> 렌더링 실패: `{type(exc).__name__}: {exc}`", ""]


def render(metrics: dict, previous: dict | None = None, db_metrics: dict | None = None,
           hypotheses: list[dict] | None = None) -> str:
    """
    입력: 오늘 로그 지표, (선택) 전일 지표, (선택) DB 지표, (선택) 가설 검정 결과.
    계산: 운영점검 보고서가 인용할 수 있는 표 묶음을 마크다운으로 낸다.
    실패 조건: 절 단위로 예외를 격리한다 — 한 절이 죽어도 나머지는 나온다.
    """
    date_label = metrics.get("date", "?")
    lines: list[str] = [
        f"# 마흐디 운영 지표 (자동 집계) — {date_label}",
        "",
        "> `scripts/daily_ops_report.py`가 장마감 후 자동 생성한다. **해석은 사람 보고서**",
        "> (`docs/동작점검/YYYY-MM-DD_마흐디_운영점검보고서.md`)의 몫이다 — 여기엔 표와 델타만 있다.",
        "",
    ]
    if hypotheses:
        lines += _section("0. 가설 검정 (구현 시점에 적어둔 예측 vs 오늘 실측)",
                          lambda: _render_hypotheses(hypotheses))
    lines += _section("1. 한눈에 (전일 대비)", lambda: _render_headline(metrics, previous))
    lines += _section("2. 시간대별 사이클/밀림", lambda: _render_by_hour(metrics))
    lines += _section("3. 시작분 mod10 — 폴러 충돌", lambda: _render_by_mod10(metrics))
    lines += _section("4. 결손 분", lambda: _render_missing(metrics))
    lines += _section("5. REST 수요/응답", lambda: _render_rest(metrics))
    lines += _section("6. 백오프", lambda: _render_backoff(metrics))
    lines += _section("7. 버스트 점유 시간", lambda: _render_bursts(metrics))
    lines += _section("8. 연속 지연 에피소드", lambda: _render_stalls(metrics))
    lines += _section("9. 느린 REST 호출 — 페이서 vs HTTP 귀속", lambda: _render_slow_calls(metrics))
    lines += _section("9-1. KIS 응답시간 — 서비스 품질 지표", lambda: _render_rest_latency(metrics))
    lines += _section("10. 폴러 실측 위상", lambda: _render_phase(metrics))
    lines += _section("11. 로그 볼륨/정성 항목", lambda: _render_log_volume(metrics))
    if db_metrics:
        lines += _section("12. DB 적재", lambda: _render_db_tables(db_metrics))
        lines += _section("13. 판단/레짐/피처", lambda: _render_db_judgement(db_metrics))
        lines += _section("14. 신호 도달률 — 데이터가 판단까지 갔는가",
                          lambda: _render_signal_reach(db_metrics))
        lines += _section("14-1. 앙상블 멤버별 가용성 — 어느 멤버가 왜 죽었는가",
                          lambda: _render_member_availability(db_metrics))
        lines += _section("14-2. 행사가 창 품질 — 수집한 행사가가 스팟을 감쌌는가",
                          lambda: _render_strike_window(db_metrics, metrics))
        lines += _section("15. 북별 감마 지형 (장 마지막 스냅샷)",
                          lambda: _render_book_gamma_map(db_metrics, previous))
        lines += _section("16. 매크로/안전장치", lambda: _render_db_misc(db_metrics))
    return "\n".join(lines).rstrip() + "\n"


def _render_headline(metrics: dict, previous: dict | None) -> list[str]:
    headers = ["지표", "오늘"]
    if previous:
        headers += [f"전일({previous.get('date', '?')})", "Δ"]
    rows = []
    for label, path, spec, direction in HEADLINE_METRICS:
        value = dig(metrics, path)
        row = [label, _fmt(value, spec)]
        if previous:
            prev_value = dig(previous, path)
            row += [_fmt(prev_value, spec), _delta(value, prev_value, direction)]
        rows.append(row)
    out = _table(headers, rows)
    if not previous:
        out += ["> 전일 지표 사이드카가 없어 델타를 생략했다.", ""]
    return out


def _render_by_hour(metrics: dict) -> list[str]:
    rows = [
        [
            f"{r['hour']:02d}시", str(r["cycles"]), f"{r['rest_mean']:.1f}", f"{r['rest_max']:.1f}",
            str(r["over_60s"]), f"{r['slip_max']:.1f}", str(r["foreign_sum"]),
        ]
        for r in dig(metrics, "cycles.by_hour") or []
    ]
    return _table(
        ["시간대", "사이클", "REST평균(초)", "REST최대(초)", "60초초과", "최대밀림(초)", "창안 타폴러호출"], rows
    )


def _render_by_mod10(metrics: dict) -> list[str]:
    rows = []
    for r in dig(metrics, "cycles.by_mod10") or []:
        by_group = ", ".join(f"{k} {v}" for k, v in sorted(r["foreign_by_group"].items(), key=lambda x: -x[1]))
        rows.append(
            [
                str(r["mod10"]), str(r["cycles"]), f"{r['rest_mean']:.1f}",
                f"{r['foreign_mean']:.1f}", by_group or "—", str(r["over_60s"]),
            ]
        )
    out = _table(["시작분 mod10", "사이클", "REST평균(초)", "창안 타폴러(평균)", "내역", "60초초과"], rows)
    out += [
        "> 창 안 타폴러 호출은 **httpx 타임스탬프를 사이클 수집창과 교차해 실측**한 값이다"
        "(로그의 `타폴러동시호출추정`은 페이서 카운터 역산이라 별개).",
        "",
    ]
    return out


def _render_missing(metrics: dict) -> list[str]:
    missing = dig(metrics, "cycles.missing") or {}
    out = [
        f"- 결손 **{missing.get('count', 0)}분** (홀수분 {missing.get('odd', 0)} / "
        f"짝수분 {missing.get('even', 0)})",
        f"- 캐치업 회수 **{missing.get('recovered_by_catchup', 0)}분** → "
        f"미회수 **{missing.get('unrecovered_count', 0)}분**",
        "",
    ]
    listed = missing.get("list") or []
    if listed:
        out += ["```", " ".join(listed), "```", ""]
    return out


def _render_rest(metrics: dict) -> list[str]:
    rest = metrics.get("rest") or {}
    out = [
        f"- 총 **{rest.get('total_calls', 0):,}건** / {rest.get('span_seconds', 0) / 60:.0f}분 "
        f"= **{_fmt(rest.get('calls_per_second'), '{:.3f}')}건/초** "
        f"(용량 대비 **{_fmt(rest.get('capacity_pct'), '{:.1f}')}%**, "
        f"적자 시작 배율 **{_fmt(rest.get('deficit_threshold_multiplier'), '{:.2f}')}배**)",
        "",
    ]
    out += _table(
        ["폴러 그룹", "호출 수"], [[k, f"{v:,}"] for k, v in (rest.get("by_group") or {}).items()]
    )
    out += _table(
        ["상태코드", "건수"], [[k, f"{v:,}"] for k, v in (rest.get("by_status") or {}).items()]
    )
    non200 = rest.get("non_200") or {}
    out += [
        f"- 비200 **{non200.get('count', 0)}건({non200.get('pct', 0)}%)** — "
        + (", ".join(f"{k} {v}" for k, v in (non200.get("by_group") or {}).items()) or "—"),
        "",
    ]
    return out


def _render_backoff(metrics: dict) -> list[str]:
    bo = metrics.get("backoff") or {}
    out = [
        f"- 확대 {bo.get('expand', 0)}건 / 회복 {bo.get('recover', 0)}건 / "
        f"최대 **{_fmt(bo.get('max_multiplier'), '{:.2f}')}배** / "
        f"시간가중 평균 {_fmt(bo.get('mean_multiplier'), '{:.3f}')}배",
        "",
    ]
    out += _table(
        ["시간대", "평균 배율"],
        [[f"{h}시", f"{v:.3f}"] for h, v in (bo.get("mean_multiplier_by_hour") or {}).items()],
    )
    return out


def _render_bursts(metrics: dict) -> list[str]:
    rows = []
    for group, b in (metrics.get("bursts") or {}).items():
        occ = b.get("occupancy_seconds") or {}
        rows.append(
            [
                group, str(b.get("burst_count", 0)), f"{b.get('calls_per_burst_median', 0):.0f}",
                _fmt(occ.get("median"), "{:.1f}"), _fmt(occ.get("max"), "{:.1f}"),
                ", ".join(f"{k}({v})" for k, v in (b.get("start_positions_mod10") or {}).items()),
            ]
        )
    out = _table(["그룹", "버스트 수", "콜/버스트(중앙)", "점유 중앙(초)", "점유 최대(초)", "시작 위치(10분창 분:초)"], rows)
    out += ["> 점유 시간이 60초를 넘으면 그 폴러는 다음 분의 옵션체인 사이클을 덮는다.", ""]
    return out


def _render_stalls(metrics: dict) -> list[str]:
    rows = [
        [s["at"], str(s["mod10_minute"]), str(s["gaps"]), f"{s['total_seconds']:.0f}", f"{s['mean_gap']:.1f}"]
        for s in metrics.get("stalls") or []
    ]
    out = _table(["시작", "분 mod10", "연속 지연 횟수", "총 초", "평균 간격(초)"], rows)
    out += [
        "> 페이서 배율로 설명되지 않는 지연 구간. 특정 `분 mod10`에 몰리면 그 시각의 폴러 배치를 의심한다.",
        "",
    ]
    return out


def _render_slow_calls(metrics: dict) -> list[str]:
    sc = metrics.get("slow_calls") or {}
    if not sc.get("count"):
        # 2026-08-04 §2-1: 임계값을 문자열에 박아두지 않는다 — 08-03에 3.0 → 5.0으로 올렸는데
        # 이 줄만 "임계(3초)"로 남아 있었다. 실제 상수를 그대로 인용한다.
        return [f"임계({SLOW_CALL_LOG_THRESHOLD_SECONDS:.0f}초) 초과 호출 없음.", ""]
    out = [
        f"- **{sc['count']}건** — 페이서대기 우세 **{sc.get('pacer_dominant', 0)}건** / "
        f"HTTP 우세 **{sc.get('http_dominant', 0)}건**",
        "",
        "| 구간 | 평균(초) | 중앙(초) | 최대(초) |",
        "|---|---|---|---|",
        f"| 전체 | {_fmt(dig(sc, 'total_seconds.mean'), '{:.2f}')} | "
        f"{_fmt(dig(sc, 'total_seconds.median'), '{:.2f}')} | {_fmt(dig(sc, 'total_seconds.max'), '{:.2f}')} |",
        f"| 페이서대기 | {_fmt(dig(sc, 'pacer_seconds.mean'), '{:.2f}')} | "
        f"{_fmt(dig(sc, 'pacer_seconds.median'), '{:.2f}')} | {_fmt(dig(sc, 'pacer_seconds.max'), '{:.2f}')} |",
        f"| HTTP | {_fmt(dig(sc, 'http_seconds.mean'), '{:.2f}')} | "
        f"{_fmt(dig(sc, 'http_seconds.median'), '{:.2f}')} | {_fmt(dig(sc, 'http_seconds.max'), '{:.2f}')} |",
        "",
        "> **페이서대기 우세** → 예약 큐 경합(다른 폴러와의 충돌). "
        "**HTTP 우세** → KIS 서버 또는 커넥션 풀. 둘 다 작은데 간격이 크면 이벤트 루프/스레드풀 블로킹.",
        "",
    ]
    if sc.get("samples"):
        out += _table(
            ["시각", "총(초)", "페이서(초)", "HTTP(초)", "배율", "엔드포인트"],
            [
                [s["at"], f"{s['total']:.2f}", f"{s['pacer']:.2f}", f"{s['http']:.2f}",
                 f"{s['multiplier']:.2f}", s["endpoint"]]
                for s in sc["samples"]
            ],
        )
    return out


def _render_phase(metrics: dict) -> list[str]:
    rows = [
        [
            group, str(p["mode_second"]),
            ", ".join(str(k) for k in (p.get("minutes_mod10") or {}).keys()),
        ]
        for group, p in (metrics.get("poller_phase") or {}).items()
    ]
    out = _table(["그룹", "분 안의 초(최빈)", "발사 분(mod10)"], rows)
    out += [
        "> 설계 위상(`mahdi/main.py` \"폴러 위상 계획\")과 대조한다 — 어긋나면 격자 앵커가 깨진 것이다.",
        "",
    ]
    return out


def _render_log_volume(metrics: dict) -> list[str]:
    lv = metrics.get("log_volume") or {}
    out = [
        f"- 총 **{lv.get('total_bytes', 0) / 1048576:.2f}MB** / {lv.get('total_lines', 0):,}줄 — "
        f"httpx {lv.get('httpx_bytes', 0) / 1048576:.2f}MB({_fmt(lv.get('httpx_pct'), '{:.1f}')}%), "
        f"**사람이 읽는 줄 {lv.get('human_lines', 0):,}줄**",
        "",
    ]
    out += _table(["레벨", "건수"], [[k, str(v)] for k, v in (lv.get("by_level") or {}).items()])
    out += _table(["정성 항목", "건수"], [[k, str(v)] for k, v in (metrics.get("qualitative") or {}).items()])
    out += _render_parser_audit(metrics)
    out += _table(["실패 유형", "건수"], [[k, str(v)] for k, v in (metrics.get("failures") or {}).items()])
    return out


def _render_rest_latency(metrics: dict) -> list[str]:
    """
    2026-08-04 고도화#5 — §2-6이 밀림의 90%를 KIS 응답 지연으로 귀속시켰는데, 지금까지 그 지연은
    "우리 지표"(밀림 건수)로만 보였다. §9의 `slow_calls`는 임계(5초) 위쪽 꼬리만 보므로
    "오늘 KIS가 평소보다 느렸는가"에 답할 수 없다.
    """
    lat = metrics.get("rest_latency") or {}
    if not lat:
        return [
            "> 계측 전 — `poll_rest_latency_snapshot`(2026-08-04 고도화#5) 도입 이전 로그다. "
            "다음 거래일부터 5분 창마다 엔드포인트별 p50/p95/p99가 쌓인다.",
            "",
        ]
    out = _table(
        ["엔드포인트", "호출", "p50(초)", "p95(초)", "p99(초)", "최대(초)"],
        [
            [endpoint, f"{s['calls']:,}", f"{s['p50']:.2f}", f"{s['p95']:.2f}",
             f"{s['p99']:.2f}", f"{s['max']:.2f}"]
            for endpoint, s in (lat.get("endpoints") or {}).items()
        ],
    )
    grid = lat.get("p95_by_hour") or {}
    endpoints = sorted({e for row in grid.values() for e in row})
    if grid and endpoints:
        out += _table(
            ["시간대", *endpoints],
            [[f"{h}시", *[f"{row.get(e, 0):.2f}" if e in row else "—" for e in endpoints]]
             for h, row in grid.items()],
        )
        out += ["> 시간대별 **p95**(초). 매일 같은 시간대가 붉으면 KIS 쪽 혼잡 패턴이다.", ""]
    warnings = lat.get("warnings") or []
    threshold = lat.get("p95_warn_threshold")
    if warnings:
        hits = ", ".join(f"{w['hour']}시 {w['endpoint']} {w['p95']:.2f}초" for w in warnings)
        out += [
            f"- ⚠ p95가 임계({threshold}초)를 넘은 구간 **{len(warnings)}개** — {hits}",
            "",
            "> **사전 대응 규칙(`hypotheses.yaml` 2026-08-04-p5, 숫자 보기 전에 확정)**: "
            f"`inquire-price`의 p95가 {threshold}초를 넘는 시간대가 **이틀 연속 같은 시간대에** "
            "나타나면, 그 시간대에 한해 위클리 폴링을 2분 → 4분 격분으로 늘린다"
            "(먼슬리는 건드리지 않는다 — 판단 입력이다).",
            "> **발동은 사람이 한다.** 지연을 보고 폴링을 자동으로 줄이면 폴링이 줄어 지연이 낮아지고 "
            "다시 폴링이 느는 되먹임이 생긴다 — 2026-07-08에 페이서를 나눴다가 500 폭주로 "
            "203분을 잃은 전례가 있다.",
            "",
        ]
    else:
        out += [f"> ✅ p95가 임계({threshold}초)를 넘은 (시간대, 엔드포인트) 없음.", ""]
    return out


def _render_member_availability(db: dict) -> list[str]:
    """2026-08-04 고도화#2 — `available_member_count` 숫자 하나로는 어느 멤버가 왜 죽었는지 모른다."""
    ma = db.get("member_availability") or {}
    if not ma.get("available"):
        return [f"> 계측 전 — {ma.get('reason', '사유 미상')}.", ""]
    out = _table(
        ["멤버", "가용 분", "가용률", "미가용 대표 사유"],
        [
            [
                m["member"] + ("" if m["implemented"] else " *(미구현)*"),
                f"{m['available_minutes']:,}",
                f"{m['available_pct']:.1f}%",
                m["top_unavailable_reason"] or "—",
            ]
            for m in ma.get("members") or []
        ],
    )
    return out + [
        f"> 분모 {ma.get('minutes', 0):,}분. 사유는 판단 시점에 `risk_gate_state.member_unavailable`로 "
        "남긴 값이다 — 2026-08-04에는 이 표가 없어 사람이 `signal_layer.py`를 읽어 역산했고, "
        "그 역산 끝에 `orderflow_ofi_vpin`이 **데이터가 있는데도** 죽어 있다는 것이 나왔다(§2-5).",
        "",
    ]


def _render_strike_window(db: dict, metrics: dict) -> list[str]:
    """
    2026-08-04 고도화#3 — §12 커버리지("데이터가 DB에 있는가")와 §14 신호 도달률("판단까지 갔는가")
    사이의 빈 칸: **"수집한 행사가가 애초에 맞는 행사가였는가."**
    """
    q = db.get("strike_window_quality") or {}
    rolls = metrics.get("atm_rolls") or {}
    out: list[str] = []
    if q.get("available"):
        out += _table(
            ["지표", "값", "읽는 법"],
            [
                ["ATM 정합률", f"**{q['atm_covered_pct']:.1f}%**",
                 "그 분의 ATM이 수집 행사가 안에 있었는가 — **핵심 지표**"],
                ["창 정합률", f"{q['window_covered_pct']:.1f}%",
                 "설계 창(ATM±2) 전부를 덮었는가 — 100% 밑이 정상(아래 주석)"],
                ["ATM 이탈 거리", f"중앙 {q['atm_offset_strikes_median']}칸 / 최대 {q['atm_offset_strikes_max']}칸",
                 "수집 창 중심이 진짜 ATM에서 몇 행사가 떨어졌는가"],
                ["창 폭 지터", f"{q['width_jitter']}배",
                 f"스냅샷({q['snapshot_window_minutes']}분 창) 행사가 {q['snapshot_strikes_median']:.0f}개 / 설계 {q['design_strikes']}개"],
            ],
        )
        out += [
            "> **창 정합률을 합격/불합격으로 읽지 말 것.** 재롤링은 선물 1분봉이 완성될 때 일어나고 "
            "그 분의 폴링은 이미 시작됐거나 끝났으므로 **구조적으로 한 틱 늦는다.** 게다가 "
            "ATM 히스테리시스(2026-08-04 Fix#6)는 **일부러** 창을 늦게 옮긴다 — 이 값만 보면 "
            "그 fix가 회귀로 보인다. 판정은 **ATM 정합률과 이탈 거리**로 한다.",
            "> 2026-08-03에 하루치 체인 전체가 스팟에서 5.5% 떨어진 외가격에서 수집됐는데 "
            "먼슬리 커버리지(§12)는 98.8%로 훌륭했다 — **이 표 하나면 그날 바로 잡혔다.**",
            "",
        ]
    else:
        out += [f"> 계측 전 — {q.get('reason', '사유 미상')}.", ""]

    if rolls.get("count") is not None:
        pct = rolls.get("round_trip_pct")
        out += [
            f"- ATM 롤링 **{rolls['count']}회** / 즉시 왕복 **{rolls['round_trips']}회**"
            + (f" (**{pct:.1f}%**)" if pct is not None else ""),
            "",
            "> 즉시 왕복 = `A→B` 다음 이벤트가 `B→A`. 히스테리시스가 없으면 스팟이 격자 중점 "
            "근처에서 진동할 때마다 창이 오간다(2026-08-04 실측 194회 중 70회, **36.1%**). "
            "이 값이 Fix#6의 유일한 직접 지표다.",
            "",
        ]
    return out


def _render_parser_audit(metrics: dict) -> list[str]:
    """
    계산: `log_metrics._parser_audit()`가 찾은 "엄격 0 · 느슨 >0" 항목을 경고로 낸다.
    해석: 2026-08-04 §2-1 / 고도화#1 규약 C — **0건 보고는 증명을 동반한다.**
         08-03에 로그 세 곳을 바꾸면서 파서 셋이 조용히 죽었고, 08-04 리포트는 그것을
         `느린 REST 호출 0건 (▼933 ✅)` 이라는 **개선**으로 표시했다. 이 절이 있었다면
         같은 표 아래에 `⚠ slow_calls: 파서 0건 / 로그 실재 362건`이 떴을 것이다.
    실패 조건: 없음 — 감사 결과가 없으면 한 줄짜리 정상 확인만 남긴다(침묵하지 않는다).
    """
    audit = metrics.get("parser_audit") or {}
    blind = audit.get("blind") or {}
    if not blind:
        return ["> ✅ 계측 감사: 0건으로 보고된 항목 중 로그에 실재하는 것 없음(파서 정상).", ""]
    return [
        "> ⚠ **계측 감사 실패 — 아래 지표를 믿지 말 것.** 파서는 0을 냈는데 로그에는 실재한다. "
        "로그 문구/레벨/예외 처리를 바꾸고 `mahdi/ops/log_metrics.py`를 안 고쳤을 때 이렇게 된다 "
        "(2026-08-03에 실제로 3건 발생, 08-04 §2-1).",
        "",
        *_table(
            ["항목", "파서(엄격)", "로그 실재(느슨)"],
            [[k, str(v["strict"]), f"**{v['loose']}**"] for k, v in blind.items()],
        ),
    ]


def _render_db_tables(db: dict) -> list[str]:
    rows = [
        [r["table"], f"{r['rows']:,}", f"{r['minutes']:,}" if r.get("minutes") is not None else "—",
         r.get("note") or ""]
        for r in db.get("tables") or []
    ]
    out = _table(["테이블", "행", "DISTINCT 분", "비고"], rows)
    coverage = db.get("book_coverage") or []
    out += _table(
        ["북(series)", "만기", "적재 분", "커버리지"],
        [
            [c["series"], str(c["expiry"]), f"{c['minutes']:,}",
             _fmt(c.get("coverage_pct"), "{:.1f}%")]
            for c in coverage
        ],
    )
    out += [
        "> 위 커버리지의 분모는 **그날 옵션체인이 실제로 돈 분 수**다(북 사이 상대 비교용 — "
        "위클리는 설계상 격분이라 50% 근처가 정상).",
        "",
    ]
    monthly = db.get("monthly_coverage")
    if monthly:
        out += [
            f"- **먼슬리 절대 커버리지: {_fmt(monthly.get('coverage_pct'), '{:.1f}%')}** "
            f"({monthly.get('minutes')}분 / 경과 {monthly.get('elapsed_minutes')}분, "
            f"만기 {monthly.get('expiry') or monthly.get('reason')})",
            "> **이것이 GEX/감마플립 입력의 1분 연속성**이다 — 인프라 지표(밀림·백오프)가 좋아져도 "
            "이 값이 나빠질 수 있으므로 반드시 나란히 읽는다(2026-07-31: 밀림 83→46건인데 "
            "커버리지 95.0%→90.5%).",
            "",
        ]
    return out


def _render_db_judgement(db: dict) -> list[str]:
    out = _table(
        ["decision", "conviction", "reject_reason", "건수"],
        [[r["decision"], r["conviction"], r["reject_reason"] or "—", f"{r['count']:,}"]
         for r in db.get("signal_decisions") or []],
    )
    out += [
        f"- `risk_gate_state` 고유값 **{db.get('risk_gate_distinct', '—')}종** "
        "(1~2종이면 판단이 사실상 고정 출력이다)",
        "",
    ]
    out += _table(
        ["레짐", "오늘", "전체 이력", "영업일"],
        [[r["regime"], f"{r['today']:,}", f"{r['total']:,}", str(r["days"])]
         for r in db.get("regime") or []],
    )
    fs = db.get("feature_store") or {}
    out += [
        f"- `feature_store` 오늘 {fs.get('today', 0):,}건 / 누적 **{fs.get('total', 0):,}건** "
        f"(HMM 임계 {fs.get('hmm_threshold', 8000):,} 대비 {_fmt(fs.get('hmm_progress_pct'), '{:.1f}%')})",
        "",
    ]
    out += _table(
        ["피처", "중립값 탈출 비율"],
        [[k, _fmt(v, "{:.1f}%")] for k, v in (fs.get("non_neutral_pct") or {}).items()],
    )
    return out


def _render_signal_reach(db: dict) -> list[str]:
    """2026-08-03 §5-1 — "데이터가 DB에 있는가"가 아니라 "판단까지 도달했는가"를 낸다."""
    reach = db.get("signal_reach") or {}
    if not reach.get("available"):
        return [
            "> 이 지표는 마이그레이션 022(`signal_decisions`의 체인 입력 컬럼) 적용 이후부터 나온다.",
            "",
        ]
    out = _table(
        ["지표", "값", "경고 임계"],
        [
            [
                "앙상블 최대 가용 멤버",
                # 2026-08-04 §2-5: 종전에는 "이론 최대 3개"가 하드코딩돼 있었고, 그 3이
                # `orderflow_ofi_vpin`이 죽어 있다는 사실을 분모 안에 숨기고 있었다.
                # 구현된 멤버 수를 아는 쪽(fusion.signal_layer)에서 가져온다.
                f"{reach['member_count_max']}개 / 이론 최대 "
                f"{db_metrics_module.SIGNAL_REACH_WARNINGS['member_count_max_min']}개",
                f"< {db_metrics_module.SIGNAL_REACH_WARNINGS['member_count_max_min']}",
            ],
            [
                "감마플립 산출률",
                f"{reach['gamma_flip_pct']}% ({reach['gamma_flip_count']:,}/{reach['decisions']:,}분)",
                f"< {db_metrics_module.SIGNAL_REACH_WARNINGS['gamma_flip_pct_min']}%",
            ],
            [
                "체인 스냅샷 레그 수",
                f"중앙 {_fmt(reach.get('chain_leg_median'), '{:.0f}')} / "
                f"최대 {_fmt(reach.get('chain_leg_max'), '{:,.0f}')}",
                "북수 x (ATM±N)x2 에서 크게 벗어나면",
            ],
            [
                "체인 스냅샷 최고령 레그",
                f"중앙 {_fmt(_minutes(reach.get('chain_age_seconds_median')), '{:.1f}분')} / "
                f"최대 {_fmt(_minutes(reach.get('chain_age_seconds_max')), '{:.1f}분')}",
                f"> {db_metrics_module.SIGNAL_REACH_WARNINGS['chain_age_seconds_max'] / 60:.0f}분",
            ],
        ],
    )
    for warning in reach.get("warnings") or []:
        out.append(f"- ⚠ {warning}")
    if not reach.get("warnings"):
        out.append("- 경고 없음")
    out += [
        "",
        "> **커버리지(§12)와 반드시 나란히 읽는다.** 2026-08-03에 먼슬리 커버리지는 98.8%였는데 "
        "감마플립 산출률은 **0%**였다 — 커버리지는 *데이터가 DB에 있는가*만 재고 *그 데이터가 "
        "신호까지 도달했는가*는 재지 않기 때문이다.",
        "",
    ]
    return out


def _minutes(seconds: float | None) -> float | None:
    return None if seconds is None else seconds / 60.0


def _render_db_misc(db: dict) -> list[str]:
    macro = db.get("macro") or {}
    out = _table(
        ["컬럼", "non-null", "고유값"],
        [[k, str(v.get("non_null")), str(v.get("distinct"))] for k, v in macro.items()],
    )
    halt = db.get("market_halt") or {}
    out += [
        f"- `market_halt_status`: 하트비트 `updated_at` **{halt.get('updated_at') or '—'}**, "
        f"최근 장운영정보 `last_message_at` **{halt.get('last_message_at') or '—'}**",
        f"- `shutdown_check_log` 잔존 프로세스 **{db.get('remaining_processes', '—')}**",
        "",
    ]
    rl = db.get("rate_limiter") or {}
    out += [
        f"- `rate_limiter_status_history` {rl.get('rows', 0):,}행 / 밀림 **{rl.get('overrun_rows', 0)}건** / "
        f"최대 배율 {_fmt(rl.get('max_multiplier'), '{:.2f}')}배",
        "",
    ]
    return out


def _render_hypotheses(results: list[dict]) -> list[str]:
    out: list[str] = []
    # 2026-08-03 §5-4 — 예정일이 지났는데 아직 `상태: pending`인 항목을 **표 위로** 띄운다.
    # 규약상 `상태`는 사람이 손으로 확정해야 하는데, 확정 안 된 것이 표에 섞여 들어가면 놓치기
    # 쉽고 그렇게 쌓이면 "예측 → 실측 검정" 규약 자체가 무력해진다.
    overdue = sorted({(r["id"], r.get("검증예정일")) for r in results if r.get("overdue")})
    if overdue:
        out += [
            f"> ⚠ **확정 대기 {len(overdue)}건** — 검증예정일이 지났는데 `hypotheses.yaml`의 "
            "`상태`가 아직 `pending`이다. 오늘 보고서를 쓰면서 손으로 확정할 것:",
            "",
        ]
        out += [f"> - `{hid}` (예정일 {due or '미지정'})" for hid, due in overdue]
        out.append("")

    rows = [
        [r["id"], r["가설"], r["metric"], _fmt(r.get("actual"), "{}"), r["expect"], r["verdict"]]
        for r in results
    ]
    out += _table(["id", "가설", "지표", "실측", "예측", "판정"], rows)
    out += [
        "> 판정은 참고값이다 — **`hypotheses.yaml`의 `상태`는 자동으로 바뀌지 않는다.** "
        "사람이 보고서를 쓰면서 손으로 확정한다(자동 판정이 틀렸을 때 조용히 덮이는 것을 막는다).",
        "",
    ]
    return out


def _render_book_gamma_map(db: dict, previous: dict | None = None) -> list[str]:
    """2026-08-03 §5-5 — 합산하면 만기별 정보가 서로를 덮는다. 북마다 나눠 본다."""
    books = db.get("book_gamma_map") or []
    out = _table(
        ["만기", "레그", "GEX", "감마플립", "핀 행사가", "핀 집중도", "비고"],
        [
            [
                str(b["expiry"]),
                str(b["legs"]),
                _fmt(b.get("gex"), "{:,.0f}"),
                _fmt(b.get("gamma_flip"), "{:.2f}"),
                _fmt(b.get("pin_strike"), "{:.1f}"),
                _fmt(b.get("pin_concentration_pct"), "{:.1f}%"),
                "**만기 당일**" if b.get("expiry_today") else "",
            ]
            for b in books
        ],
    )
    out += [
        "> 만기 당일 북은 잔존만기 0이라 **감마플립이 정의되지 않는다**(`—`가 정상) — 대신 "
        "핀 리스크(v6 §A3 만기 Pinning)가 그 북에서만 의미를 갖는다. 먼슬리(최근월)가 "
        "GEX/감마플립의 주 입력이고(v6 §11.4 게이트), 위클리는 핀 리스크 전용으로 읽는다.",
        f"> 위 표는 **장 마지막 {db_metrics_module.db.CHAIN_SNAPSHOT_MAX_AGE_MINUTES}분 창**의 "
        "스냅샷이다 — 라이브 판단과 같은 함수(`db.option_chain_as_of()`)를 쓴다. "
        "2026-08-04 §2-7 이전에는 시각 경계가 없어 **그날 방문한 전 행사가**를 합쳐 놓고 "
        "\"장 마지막\"이라고 적었다(핀 행사가가 5시간 전 값이었다).",
        "",
    ]
    out += _render_wide_oi_landscape(db, previous)
    return out


def _render_wide_oi_landscape(db: dict, previous: dict | None = None) -> list[str]:
    """
    2026-08-04 §2-3 / 고도화#4 — "오늘 방문한 전 행사가"의 콜−풋 OI 지형.

    이 표가 08-04에 「GEX 광폭 체인」 결정을 뒤집었다: ATM 지터가 우연히 만든 25행사가(±3%)
    구간에서도 먼슬리 C−P 부호가 안 바뀌어, **행사가를 넓혀도 감마플립은 안 나온다**는 것이
    확인됐다. Fix#6이 지터를 줄이면 그 관측이 사라지므로 매일 자동으로 남긴다(추가 REST 0건).
    """
    books = db.get("wide_oi_landscape") or []
    if not books:
        return []
    out = _table(
        ["만기", "행사가 수", "범위", "탐색폭", "C−P 합", "C편중", "P편중", "광폭 감마플립"],
        [
            [
                str(b["expiry"]),
                str(b["strikes"]),
                f"{b['strike_min']:.1f}~{b['strike_max']:.1f}",
                f"±{b['search_pct']:.1f}%",
                f"{b['net_call_put_oi']:,}",
                str(b["call_heavy_strikes"]),
                str(b["put_heavy_strikes"]),
                f"**{b['wide_gamma_flip']:.2f}**" if b["flip_possible"] else "없음",
            ]
            for b in books
        ],
    )
    # 2026-08-04 고도화#4 — **이 표의 존재 이유는 하루치 표가 아니라 "바뀌는 날"이다.**
    # 광폭 감마플립이 '없음'에서 벗어나는 날이 「GEX 광폭 체인」 안건을 다시 꺼낼 첫 근거이므로,
    # 사람이 매일 두 리포트를 나란히 놓고 비교하지 않아도 되게 전일과 대조해 콜아웃을 낸다.
    prev_books = ((previous or {}).get("db") or {}).get("wide_oi_landscape") or []
    prev_flip = {str(b["expiry"]): b.get("flip_possible") for b in prev_books}
    changed = [
        (str(b["expiry"]), prev_flip[str(b["expiry"])], b["flip_possible"])
        for b in books
        if str(b["expiry"]) in prev_flip and prev_flip[str(b["expiry"])] != b["flip_possible"]
    ]
    if changed:
        out += [
            "- 🔔 **광폭 감마플립 가능 여부가 전일 대비 바뀐 북이 있다** — "
            + ", ".join(
                f"{expiry} {'불가→**가능**' if now else '가능→불가'}" for expiry, _was, now in changed
            ),
            "",
            "> 「GEX 광폭 체인」은 2026-08-04에 **폐기**됐다(§2-3 — 딜러 포지션이 전 구간 한 방향이라 "
            "행사가를 넓혀도 flip이 안 나온다). **그 폐기의 재개 조건이 바로 이 줄이다.**",
            "",
        ]

    return out + [
        "> **광폭 감마플립 '없음' = 그 북은 방문한 행사가 전 구간에서 GEX 부호가 안 바뀐다**"
        "(딜러 포지션이 한 방향). 이때의 `감마플립 산출률 0%`(§14)는 결함이 아니라 시장 구조이며, "
        "**행사가 창을 넓혀도 해결되지 않는다** — 2026-08-04에 ATM 지터가 만든 25행사가(±3%)로 "
        "실측 확인했고, 그 결과로 「GEX 광폭 체인」 안건이 폐기됐다(§2-3).",
        "> 이 값이 '없음'에서 벗어나는 날이 그 안건을 다시 꺼낼 첫 근거다. "
        "주의: 행사가별 C−P 부호가 국소적으로 바뀌는 것과 GEX(S) 부호가 바뀌는 것은 다른 사건이다 "
        "— 국소 부호로 판정하면 08-04 먼슬리가 '가능'으로 잘못 나온다.",
        "",
    ]
