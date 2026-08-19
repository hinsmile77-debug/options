"""지표 dict → 마크다운 (순수 렌더러, 파일 I/O 없음).

2026-08-01(운영점검보고서 2026-07-31 §5-2). **사람 보고서를 대신 쓰지 않는다** — 표와 전일
델타까지만 낸다. "왜 그런가"는 사람이 쓴다(§5-2 "하지 않을 것" 참고).

전일 지표(`previous`)가 없으면 델타 열을 **생략한다** — 지어내지 않는다.
"""

from __future__ import annotations

from typing import Any, Callable

from mahdi.broker.rest_client import SLOW_CALL_LOG_THRESHOLD_SECONDS
from mahdi.ops import db_metrics as db_metrics_module  # 임계를 리포트에 그대로 인용하기 위함
from mahdi.ops import crosscheck
from mahdi.ops import levers as levers_module  # 그날 실제로 걸려 있던 레버 값(2026-08-14 Fix#3)
from mahdi.ops import log_metrics  # 임계 상수를 리포트에 그대로 인용하기 위함
from mahdi.ops import hypotheses as hypotheses_module  # 역할 상수를 같은 곳에서 가져온다

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
    # 2026-08-06 §3-5 / Fix#6 — 위 두 줄에서 **미가동분을 떼어 따로 세운다.**
    # 08-06에 결손 21분 중 20분이 프로세스 정지 구간이었는데, 이 표는 `▲20 ⚠`을 냈고
    # 그 숫자를 인프라 악화로 읽으면 틀린다. 인프라 결손은 실제로 1분이었다.
    ("└ 인프라 결손", "cycles.missing.infra_count", "{:,.0f}분", "down"),
    ("└ 관측 루프 미가동", "cycles.missing.downtime_count", "{:,.0f}분", "down"),
    ("결손 회수", "catchups.count", "{:,.0f}건", None),
    # 2026-08-06 고도화#1 — 먼슬리 레그 재시도로 살린 레그. **판단 주입력의 두께**다.
    # 0이면 재시도가 안 돌았거나(예산 없음) 놓친 레그가 없었던 것 — §12의 레그 완전성과 함께 읽는다.
    ("먼슬리 레그 회복", "priority_retry.recovered", "{:,.0f}레그", "up"),
    ("비200 응답", "rest.non_200.count", "{:,.0f}건", "down"),
    ("백오프 최대 배율", "backoff.max_multiplier", "{:.2f}배", "down"),
    ("느린 REST 호출", "slow_calls.count", "{:,.0f}건", "down"),
    ("사람이 읽는 로그 줄", "log_volume.human_lines", "{:,.0f}줄", "down"),
]


# ===== 2026-08-14 §1 / 고도화 1 — **판단 입력을 같은 화면에 올린다** =====
#
# 08-14의 §1은 밀림 0건 · 결손 0분 · 초당 수요 ▼0.082 ✅ · 적자 배율 ▲0.6 ✅ 를 인쇄했다.
# 같은 날 GEX 입력이 **78분** 없었고 먼슬리 절대 커버리지는 **82.6%**(전날까지 95% 이상)였으며
# 옵션체인이 51분 연속 비어 있었다. **§1만 읽으면 완벽한 하루로 보인다.**
#
# 위 `HEADLINE_METRICS`는 전부 «우리 인프라가 잘 돌았는가»를 잰다. 그것이 전부 초록인 채로
# 판단 입력의 5분의 1이 사라질 수 있다는 것이 08-14의 발견이고, 그래서 아래 세 줄을
# **같은 절에** 올린다 — 다른 절에 두면 그날처럼 아무도 겹쳐 읽지 않는다.
#
# 경로가 `db.`로 시작하지 않는 이유: 이 값들은 `db_metrics` dict에서 직접 꺼낸다.
# 전일 값은 사이드카의 `db` 하위에 있으므로 조회할 때만 접두사를 붙인다.
HEADLINE_DB_METRICS: list[tuple[str, str, str, str | None]] = [
    ("**먼슬리 절대 커버리지**", "monthly_coverage.coverage_pct", "{:.1f}%", "up"),
    ("**GEX 입력 없던 분**", "signal_reach.gex_input_missing_minutes", "{:,.0f}분", "down"),
    ("**최장 연속 0행 구간**", "chain_minute_coverage.zero_row_longest_run.length", "{:,.0f}분", "down"),
]


# 2026-08-06 §3-1 / Fix#3 — **리스트 절을 자연 키로 색인한다.**
#
# 08-05 `p6`이 적은 경로는 `db.tables.underlying_spot_1m.rows`였다. `db.tables`는 표를 그리기
# 위한 **리스트**라 그 경로는 영원히 None이었고, 그 가설은 주장 지표를 못 받은 채 하루를 갔다.
# 사람이 그렇게 적는 것이 자연스럽다 — 리포트에 `underlying_spot_1m`이라는 행이 실제로 있고,
# 그 행에 `rows` 칸이 있다. 리스트인지 dict인지는 렌더링 사정이지 지표의 의미가 아니다.
#
# 그래서 리스트 노드를 만나면 각 원소에서 아래 필드 중 하나를 찾아 키로 쓴다. 후보를 좁게
# 두는 이유: 아무 문자열 필드나 키로 삼으면 서로 다른 두 행이 같은 이름을 갖는 순간 조용히
# 첫 행이 이긴다. 여기 있는 것은 전부 그 절 안에서 유일한 식별자다.
_LIST_INDEX_FIELDS = ("table", "member", "id", "name")


def dig(metrics: dict, path: str) -> Any:
    """`"cycles.missing.count"` 같은 점 표기 경로로 중첩 구조를 꺼낸다(없으면 None).

    dict는 키로, **dict의 리스트는 자연 키(`_LIST_INDEX_FIELDS`)로** 색인한다.
    """
    node: Any = metrics
    for key in path.split("."):
        if isinstance(node, dict):
            if key not in node:
                return None
            node = node[key]
            continue
        if isinstance(node, list):
            match = _index_list(node, key)
            if match is None:
                return None
            node = match
            continue
        return None
    return node


def _hhmmss(seconds_of_day: float) -> str:
    total = int(seconds_of_day)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _index_list(rows: list, key: str) -> Any:
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field in _LIST_INDEX_FIELDS:
            value = row.get(field)
            if value is not None and str(value) == key:
                return row
    return None


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
           hypotheses: list[dict] | None = None, history: list[dict] | None = None,
           levers: dict | None = None, watchdog: dict | None = None,
           campaign: list[dict] | None = None, crash: dict | None = None) -> str:
    """
    입력: 오늘 로그 지표, (선택) 전일 지표, (선택) DB 지표, (선택) 가설 검정 결과,
         (선택) **직전 영업일들의 지표**(최신순, 2026-08-07 고도화#5).
    계산: 운영점검 보고서가 인용할 수 있는 표 묶음을 마크다운으로 낸다.
    해석: `previous`는 하루 전 하나이고 `history`는 그 이상이다 — 둘을 합치지 않은 이유는
         전일 대비 델타(§1·§9-1·§15)와 **추세 판정**(§14-3 부호 일치율)이 서로 다른 질문이기
         때문이다. 하루치 변화로는 "갈렸다"와 "부호가 뒤집혀 있다"를 구분할 수 없다.
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
    if levers:
        lines += _section("0. 오늘의 레버 상태 (규약 H — 무엇이 실제로 실행됐는가)",
                          lambda: _render_levers(levers))
    if hypotheses:
        lines += _section("0-1. 가설 검정 (구현 시점에 적어둔 예측 vs 오늘 실측)",
                          lambda: _render_hypotheses(hypotheses))
    if campaign:
        lines += _section("0-2. 검증 캠페인 (표본이 찰 때까지 판정하지 않는다)",
                          lambda: _render_campaign(campaign))
    lines += _section("1. 한눈에 (전일 대비) — 인프라와 **판단 입력**을 같은 화면에",
                      lambda: _render_headline(metrics, previous, db_metrics))
    lines += _section("2. 시간대별 사이클/밀림 (전일 같은 시간대 대비)",
                      lambda: _render_by_hour(metrics, previous))
    lines += _section("3. 시작분 mod10 — 폴러 충돌", lambda: _render_by_mod10(metrics))
    lines += _section("4. 결손 분 — 로그 기준과 DB 기준", lambda: _render_missing(metrics, db_metrics))
    lines += _section("5. REST 수요/응답", lambda: _render_rest(metrics, db_metrics, previous))
    lines += _section("6. 백오프", lambda: _render_backoff(metrics))
    lines += _section("7. 버스트 점유 시간", lambda: _render_bursts(metrics))
    lines += _section("8. 연속 지연 에피소드", lambda: _render_stalls(metrics))
    lines += _section("9. 느린 REST 호출 — 페이서 vs HTTP 귀속", lambda: _render_slow_calls(metrics))
    lines += _section("9-1. KIS 응답시간 — 서비스 품질 지표",
                      lambda: _render_rest_latency(metrics, previous))
    lines += _section("9-2. 옵션체인 컷 — 세 원인과 데드라인 라벨 (독립 행)",
                      lambda: _render_chain_cuts(metrics))
    lines += _section("10. 폴러 실측 위상", lambda: _render_phase(metrics))
    lines += _section("11. 로그 볼륨/정성 항목", lambda: _render_log_volume(metrics))
    if watchdog is not None:
        lines += _section("11-1. 워치독 — 감시자가 돌기는 했는가",
                          lambda: _render_watchdog(watchdog))
    if crash is not None:
        lines += _section("11-1-1. 관측 루프는 **왜** 죽었는가",
                          lambda: _render_crash(crash, metrics))
    lines += _section("11-2. WS 재연결 — 비용으로 읽는다",
                      lambda: _render_ws_disconnects(metrics, db_metrics))
    if db_metrics:
        lines += _section("12. DB 적재", lambda: _render_db_tables(db_metrics))
        lines += _section("13. 판단/레짐/피처", lambda: _render_db_judgement(db_metrics))
        lines += _section("14. 신호 도달률 — 데이터가 판단까지 갔는가",
                          lambda: _render_signal_reach(db_metrics))
        lines += _section("14-1. 앙상블 멤버별 가용성 — 어느 멤버가 왜 죽었는가",
                          lambda: _render_member_availability(db_metrics))
        lines += _section("14-2. 행사가 창 품질 — 수집한 행사가가 스팟을 감쌌는가",
                          lambda: _render_strike_window(db_metrics, metrics))
        lines += _section("14-3. 판단 품질 — 멤버가 무엇을 말했는가",
                          lambda: _render_member_scores(db_metrics, history))
        lines += _section("14-4. 사후 평가 × 체인 입력 출처 — 늙은 체인이 실제로 못 맞혔는가",
                          lambda: _render_outcomes_by_chain_input(db_metrics))
        lines += _section("15. 북별 감마 지형 (장 마지막 스냅샷)",
                          lambda: _render_book_gamma_map(db_metrics, previous))
        lines += _section("16. 매크로/안전장치", lambda: _render_db_misc(db_metrics))
        lines += _section("16-1. 보유 포지션 — 브로커가 무엇을 들고 있다고 말했는가",
                          lambda: _render_positions(db_metrics))
    lines += _section("17. 교차 점검 — 지표끼리 모순되는가",
                      lambda: _render_crosschecks(metrics, db_metrics))
    return "\n".join(lines).rstrip() + "\n"


def _render_levers(levers: dict) -> list[str]:
    """2026-08-12 §1-1 / Fix#6 — **레버 상태를 사람의 기억에서 떼어낸다.**

    08-12에 「오늘 단 하나만 켤 것」으로 지정된 레버가 안 켜졌고, 그런데도 §0-1이 그것을 켜진
    전제로 판정해 멀쩡한 fix를 반증처럼 보이게 했다. 그날 필요했던 질문(「오늘 F를 켰던가?」)의
    답은 저장소 안에 있었다 — 아무도 묻지 않았을 뿐이다.
    """
    rows = []
    for lever in levers.get("levers", []):
        on = lever.get("on")
        state = "**ON**" if on else ("OFF" if on is False else "⛔ 못 읽음")
        rows.append([
            f"`{lever['key']}`", lever.get("위치", "—"),
            f"`{lever.get('value')!r}`", f"`{lever.get('default')!r}`", state,
        ])
    out = _table(["레버", "위치", "오늘 값", "꺼진 값", "상태"], rows)
    head = levers.get("git_head")
    out.append(f"> 오늘 돌던 코드: `{head}`" if head else "> 오늘 돌던 코드: (git HEAD를 못 읽었다)")
    out += [
        "> **규약 H — 레버가 꺼진 날의 숫자로 그 레버의 가설을 판정하지 않는다.** "
        "`hypotheses.yaml`의 `전제레버`에 위 이름을 적으면 §0-1이 그 항목을 「미실행」으로 낸다. "
        "규약 F(건수는 구조 변수에 비례)·G(어떤 값은 그날 시장에 비례)의 셋째다 — "
        "**어떤 값은 그 코드가 실제로 돌았는가에 비례한다.**",
        "> `⛔ 못 읽음`은 **OFF가 아니다.** 「꺼져 있었다」와 「상태를 모른다」는 조치가 다르다 — "
        "후자는 이 표가 그 레버를 놓친 것이므로 `mahdi/ops/levers.py`를 고쳐야 한다.",
        "",
    ]
    return out


def _render_watchdog(watchdog: dict) -> list[str]:
    """2026-08-12 §2-3 / Fix#8 — **감시자의 침묵**을 다음 날이 보게 한다.

    08-12에 워치독은 10:14:01에 판정하고 재기동했는데, 재기동 호출이 상속된 파이프에 물려
    15:45:02까지 막혔고 그동안 매분 실행이 전부 무시됐다(`MultipleInstances=IgnoreNew`).
    스케줄러는 `State: Ready` / `LastTaskResult: 0` / `NumberOfMissedRuns: 0`이었다 —
    **프로세스 생존은 기능 생존의 증거가 아니다.**
    """
    silence = watchdog.get("max_silence_minutes")
    warn_at = watchdog.get("silence_warn_minutes") or 20.0
    rows = [
        ["판정 줄 수", _fmt(watchdog.get("checks"), "{}")],
        ["재기동(RESTART)", _fmt(watchdog.get("restarts"), "{}")],
        ["재기동 실패 보고", _fmt(watchdog.get("restart_failures"), "{}")],
        ["상한 도달(ALERT_ONLY)", _fmt(watchdog.get("alert_only"), "{}")],
        # 2026-08-14 Fix#2 — **재기동 건수와 같은 표에 두되 같은 줄에 섞지 않는다.**
        # RESTART는 「조치했다」이고 DEGRADED는 「조치하지 않기로 했다」이다(원인이 KIS 쪽이면
        # 재기동은 관측만 끊는다). 둘을 합치면 다음날 "워치독이 몇 번 개입했나"에 답할 수 없다.
        ["적재 정지(DEGRADED)", _fmt(watchdog.get("degraded_checks"), "{}")],
        ["첫 판정 / 마지막 판정",
         f"{watchdog.get('first_at') or '—'} / {watchdog.get('last_at') or '—'}"],
        ["**최장 무판정 구간**",
         f"**{_fmt(silence, '{:.0f}')}분** ({watchdog.get('max_silence_window') or '—'})"],
    ]
    out = _table(["지표", "값"], rows)
    if watchdog.get("checks") == 0:
        out.append(
            "> ⛔ **그날 워치독이 한 줄도 안 남겼다** — 미등록이거나 통째로 안 돈 날이다. "
            "08-06~08-11에 6영업일 연속 그랬고 아무도 몰랐다(등록 절차는 "
            "`docs/dev_memory/CURRENT_STATE.md`)."
        )
    elif watchdog.get("degraded_checks"):
        out.append(
            f"> ⛔ **적재 정지 판정 {watchdog['degraded_checks']}건** — 관측 루프는 살아 있는데 "
            "직전 10분 적재가 0분이었다. **재기동은 하지 않는다**(08-14의 원인은 KIS 지연이었고, "
            "재기동은 아무것도 안 고치고 관측만 끊는다). §4의 「최장 연속 0행 구간」·§9-1의 "
            "「p50 ÷ read timeout」과 함께 읽어 원인을 우리 쪽/KIS 쪽으로 가를 것."
        )
    elif silence is not None and silence > warn_at:
        out.append(
            f"> ⚠ **{silence:.0f}분 동안 워치독이 판정하지 않았다**(경고선 {warn_at:.0f}분 = "
            "정상 기록 주기 10분의 2배). 그 구간에 관측 루프가 죽었다면 아무도 되살리지 않는다. "
            "08-12에 이 값이 **331분**이었다."
        )
    out += [
        "> 「재기동 실패 보고」는 **로그 문구를 센 것이지 실패를 센 것이 아니다.** 08-12의 1건은 "
        "`capture_output` 때문에 뜬 오보였고 재기동은 실제로 성공했다(§2-3). 판정은 "
        "`premarket_startup.log`와 함께 사람이 한다.",
        "> 실시간 소비자는 이 표가 아니라 COCKPIT 배지다(`logs/.watchdog_last_check.json`) — "
        "**다음 날 읽는 지표로는 그날을 못 구한다.**",
        "",
    ]
    return out


def _render_crash(crash: dict, metrics: dict | None = None) -> list[str]:
    """2026-08-19 — 재기동 옆에 **사유**를 놓는다.

    08-19에 워치독이 두 번 재기동했고(09:54 · 10:36) 리포트는 그 사실까지만 말했다.
    첫 번째의 사유(`psycopg.OperationalError` — DB 컨테이너가 09:50:56에 재시작됐다)는
    `logs/observation_loop_crash.log`에만 있었고 **그 파일을 읽는 코드가 없었다.**
    `observation_loop.log`에는 `OperationalError`가 0건이다 — 예외가 로깅을 거치지 않고
    프로세스를 끝냈기 때문이다. 08-18 §3-2와 같은 계열의 결함이다.

    **기동 수와 사유 수를 나란히 낸다.** 둘이 다르면 「몇 번은 사유가 안 남았다」가 드러난다.
    """
    if not crash.get("marker_present"):
        return [
            "> ⚠ **크래시 로그에 그날의 기동 표식이 없다** — 사유를 날짜에 귀속할 수 없다. "
            "`start_mahdi_premarket.bat`이 2026-08-19부터 표식을 남기므로, 그 이전 날짜이거나 "
            "그 커밋이 안 실린 기동이다. **「크래시가 없었다」가 아니라 「셀 수 없었다」이다.**",
            "",
        ] + _render_crash_unattributed(crash)
    starts, crashes = crash.get("starts", 0), crash.get("crashes", 0)
    out = [f"- 기동 **{starts}회** · 사유가 남은 죽음 **{crashes}건**", ""]
    if crashes:
        out += _table(
            ["기동 시각", "사유", "마지막 프레임", "상세"],
            [[e.get("at") or "—", f"`{e['cause']}`", e.get("last_frame") or "—",
              (e.get("detail") or "—")[:80]]
             for e in crash.get("events") or []],
        )
        out += ["> **시각은 그 프로세스가 «뜬» 시각이다** — 죽은 시각이 아니다. 죽은 시각은 "
                "`observation_loop.log`의 공백과 워치독의 `RESTART` 줄이 답한다(§11-1).", ""]
    # **기동은 여러 번인데 사유가 그보다 적으면** 그 차이가 곧 「모르는 죽음」이다.
    # 정상 종료(장마감 taskkill)도 사유를 안 남기므로 그 1건은 빼고 읽는다.
    silent = starts - crashes - 1
    if silent > 0:
        out += [f"> ⚠ **사유 없이 끝난 기동 {silent}건** — 정상 종료 1건을 뺀 값이다. "
                "그 프로세스는 예외 없이(외부 종료·행) 사라졌다. 08-19 10:32가 그 형태였고, "
                "3분 공백 뒤 워치독이 되살렸다.", ""]
    if crash.get("causes"):
        out += ["> 사유별: " + " · ".join(f"`{k}` {v}건" for k, v in crash["causes"].items()), ""]
    return out + _render_crash_unattributed(crash)


def _render_crash_unattributed(crash: dict) -> list[str]:
    """날짜를 모르는 트레이스백 — **0으로 접지 않는다.**

    표식을 넣기 전(2026-08-19 이전)에 쌓인 것들이다. 이 값이 0이 될 때까지는 그날 표가
    「그날 전부」라고 단정할 수 없다(규약 C).
    """
    n = crash.get("unattributed") or 0
    if not n:
        return []
    return [f"> ⚠ **날짜를 모르는 트레이스백 {n}건**(상한) — 기동 표식 **이전**에 쌓인 것이다"
            "(이 파일은 2026-07-19부터 타임스탬프 없이 append돼 왔다). "
            "「오늘 것이 아니다」가 아니라 **「어느 날 것인지 모른다」**이다. "
            "예외 연쇄(`During handling of ...`)가 있으면 실제 죽음보다 많이 세어지므로 상한이다.", ""]


def _render_ws_disconnects(metrics: dict, db_metrics: dict | None) -> list[str]:
    """2026-08-12 고도화 1 — **재연결을 비용으로 읽는다.**

    종전에 재연결은 로그 줄일 뿐 지표가 아니었다. 08-12에 31회가 레짐 30분을 먹었는데
    그 환율을 아무도 세지 않았고, 31회가 09~10시에 몰렸다는 사실도 사람이 손으로 훑어서 알았다.
    """
    ws = dig(metrics, "ws_disconnect") or {}
    count = ws.get("count", 0)
    out = [f"- WS 단절 **{count}회**" + (
        f" — 첫 {ws.get('first_at')} / 마지막 {ws.get('last_at')}" if count else " (조용한 날)"
    )]
    by_hour = ws.get("by_hour") or {}
    if by_hour:
        out += _table(
            ["시간대", "단절"],
            [[hour, str(n)] for hour, n in sorted(by_hour.items())],
        )
        out.append(
            f"> 가장 몰린 시간대: **{ws.get('busiest_hour')} {ws.get('busiest_hour_count')}회**. "
            "**편중이 곧 진단이다** — 08-12는 09~10시에 31회가 몰렸고, 그것은 KIS가 아니라 "
            "09:13의 단 한 번이 연 **자기지속 루프**였다(§7-1, 유지 풀이 죽은 클라이언트를 쥐고 있었다)."
        )

    gap = dig(db_metrics or {}, "regime_vs_futures_bars") or {}
    if gap.get("available"):
        bars, regimes, missing = gap["futures_bar_minutes"], gap["regime_minutes"], gap["gap"]
        out += _table(
            ["지표", "값"],
            [
                ["선물봉 분", str(bars)],
                ["레짐 분", str(regimes)],
                ["**봉은 있는데 레짐이 없는 분**", f"**{missing}**"],
                ["재연결 1회당 잃은 분", f"{missing / count:.2f}" if count else "— (재연결 0회)"],
            ],
        )
        if missing:
            out.append(
                f"> ⚠ **{missing}분의 선물봉이 레짐을 못 남겼다.** 정상이면 두 값은 같아야 한다 — "
                "`regime_state`는 선물봉 완성 시에만 쓰이기 때문이다. 08-12에 376 vs 406(−30)이었고, "
                "원인은 봉 핸들러가 `적재 → WS 재롤링 → 레짐` 순서라 끊긴 소켓에서 레짐이 "
                "**도달조차 못 한 것**이었다(Fix#3이 순서를 뒤집었다)."
            )
            if gap.get("minutes"):
                out.append("> 해당 분: `" + " ".join(gap["minutes"]) + "`")
        else:
            out.append(
                "> 두 값이 같다 — 봉이 생긴 분에는 레짐도 남았다. "
                "⚠ **재연결이 0인 날에는 이것이 fix를 검정하지 못한다**(경로가 안 돈다)."
            )
    out += [
        "> **임계를 걸지 않는다.** 정상 재연결 횟수의 분포를 모른다 — 표본이 08-04 1회 / "
        "08-11 1회 / 08-12 31회로 셋뿐이다. 모르는 채 임계를 정하면 그 임계가 곧 결론이 된다.",
        "",
    ]
    return out


def _delta_baseline_banner(metrics: dict) -> list[str]:
    """§1 표 **머리**에 붙는 배너 — 기준일이 무엇이고 오늘이 거래일인가 (2026-08-19 / Fix#3).

    08-18에 이 배너가 없어서 「REST수집 평균 ▲18.7 ⚠」·「느린 REST 호출 ▲2,840 ⚠」·「사람이
    읽는 로그 ▲5,770 ⚠」·「비200 응답 ▲35 ⚠」 **넷이 전부 거짓으로 인쇄됐다.** 전일이
    광복절 대체휴일이었고, 그 표는 거래일과 휴장일을 뺀 값이었다(규약 G).

    **없는 정보는 인쇄하지 않는다.** `delta_baseline` 절 자체가 없으면(구버전 사이드카)
    빈 목록이다 — 침묵이 「정상」으로 읽히는 것보다 낫다(그 반대는 08-18이 이미 보여 줬다).
    """
    baseline = dig(metrics, "delta_baseline")
    if not isinstance(baseline, dict):
        return []
    lines: list[str] = []
    # 오늘 자신이 비거래일이면 **그 사실이 먼저다.** 이 산출물 전체가 시장 없는 하루의 것이다.
    if baseline.get("target_is_trading_day") == 0:
        name = baseline.get("target_holiday_name") or "주말"
        lines.append(f"> 🚫 **오늘은 비거래일이다({name}).** 아래 값 전부가 시장 없는 하루의 것이고, "
                     "**다음 거래일의 기준선으로 쓰면 안 된다** — 08-17이 그렇게 쓰여 08-18의 ⚠ 넷을 만들었다.")
    skipped = baseline.get("skipped_non_trading_days")
    if baseline.get("date") is None:
        lines.append("> ⚠ **직전 거래일을 못 찾았다** — 달력이 답을 못 준다. 델타를 생략했고 "
                     "**0으로 접지 않았다**(「모름」은 「변화 없음」이 아니다).")
    elif skipped:
        lines.append(f"> ⚠ **직전 거래일 {baseline['date']} 기준**이다 — 그 사이 비거래일 {skipped}일을 "
                     "건너뛰었다. 달력상 «어제»가 아니라 **시장이 마지막으로 열린 날**과 비교한 값이다.")
    gap = baseline.get("calendar_coverage_gap_days")
    if gap is None or (isinstance(gap, (int, float)) and gap > 0):
        lines.append("> ⚠ **휴장일 달력이 오늘을 못 덮는다**(`covered_through` 만료 또는 판독 불가) — "
                     "위 기준일 판정은 **주말만 반영한 값**일 수 있다. 달력을 갱신할 것.")
    return lines + [""] if lines else []


def _render_headline(metrics: dict, previous: dict | None, db_metrics: dict | None = None) -> list[str]:
    headers = ["지표", "오늘"]
    if previous:
        headers += [f"전일({previous.get('date', '?')})", "Δ"]

    def build_rows(source: dict | None, spec_list, prev_prefix: str):
        out_rows = []
        for label, path, spec, direction in spec_list:
            value = dig(source or {}, path)
            row = [label, _fmt(value, spec)]
            if previous:
                prev_value = dig(previous, prev_prefix + path)
                row += [_fmt(prev_value, spec), _delta(value, prev_value, direction)]
            out_rows.append(row)
        return out_rows

    # 배너는 **표보다 먼저** 온다 — 표를 읽고 나서 「그런데 기준일이 휴장일이었다」를 알면
    # 이미 읽은 ⚠ 넷을 사람이 되돌려야 하고, 08-18에 그 되돌리기가 일어나지 않았다.
    out = _delta_baseline_banner(metrics) + _table(headers, build_rows(metrics, HEADLINE_METRICS, ""))
    if not previous:
        out += ["> 전일 지표 사이드카가 없어 델타를 생략했다.", ""]

    # 2026-08-14 고도화 1 — 상세 근거는 `HEADLINE_DB_METRICS` 주석.
    #
    # **값이 없는 줄은 아예 안 그린다.** 「82.6%」가 있어야 할 자리에 「—」를 그리면 그 대시를
    # 「나쁘지 않았다」로 읽게 된다 — 규약 C가 금지하는 바로 그 형태다. 대신 무엇이 빠졌는지를
    # 문장으로 남겨 「측정하지 않았다」와 「측정했는데 좋았다」를 가른다.
    if db_metrics:
        available = [spec for spec in HEADLINE_DB_METRICS if dig(db_metrics, spec[1]) is not None]
        # 빠진 줄은 **사람 라벨이 아니라 집계 키**로 적는다 — 라벨을 적으면 「그 지표를 말하지
        # 않는다」는 다른 절의 계약(§12의 절대 커버리지 줄)과 문자열이 충돌하고, 무엇보다
        # 「무엇을 고쳐야 이 줄이 생기는가」에 답하는 것은 키 쪽이다.
        missing = [f"`db.{spec[1]}`" for spec in HEADLINE_DB_METRICS if spec not in available]
        if not available:
            return out + [
                f"> **판단 입력 {len(missing)}행이 이 집계에 없다** — {', '.join(missing)}. "
                "「좋았다」가 아니라 **「재지 않았다」**이다(규약 C).",
                "",
            ]
        out += ["**판단 입력** — 위 표가 전부 초록이어도 여기가 비면 그날 판단은 눈을 감고 났다.", ""]
        out += _table(headers, build_rows(db_metrics, available, "db."))
        if missing:
            out += [f"> ⚠ 이 집계에 없어 뺀 줄: {', '.join(missing)} — 「좋았다」가 아니라 「재지 않았다」이다.", ""]
        out += [
            "> 위 표는 «우리 인프라가 잘 돌았는가»를, 이 표는 «판단이 볼 것을 봤는가»를 잰다. "
            "**둘은 같은 날 반대 방향으로 갈 수 있다** — 08-14가 그랬다: 밀림 0건 · 결손 0분 · "
            "REST 수요 전일의 80%인 채로 GEX 입력이 78분 사라졌고 먼슬리 커버리지가 82.6%로 내려갔다. "
            "그날 §1만 읽은 대시보드는 그 하루를 **완벽한 하루**로 인쇄했다.",
            "",
        ]
    return out


def _render_by_hour(metrics: dict, previous: dict | None = None) -> list[str]:
    """2026-08-14 장중 §3 / Fix#3 — **전일 같은 시간대를 나란히 놓는다.**

    08-14의 유일한 신규 발견(수집 소요가 시간마다 계단식으로 오른다)은 사람이 이틀치 로그를
    손으로 겹쳐 읽어서 나왔다. 단독으로 보면 「12시 47.2초」는 어제의 29.2초와 구별되지 않는다 —
    같은 표에 두 날이 있으면 0초에 보인다. 전일 사이드카(`auto/<전일>_지표.json`)는 이미 있다.
    """
    prev_by_hour = {r["hour"]: r for r in (dig(previous or {}, "cycles.by_hour") or [])}
    hours = dig(metrics, "cycles.by_hour") or []
    # 2026-08-14 장중 §3 / 고도화 2 — 위상은 **절대 초가 아니라 그날 첫 시간대 대비 이동**으로
    # 읽는다. :19라는 숫자 자체에는 의미가 없고(폴러 기동 시각이 정하는 상수다), 하루 안에서
    # 그 값이 **얼마나 밀렸는가**가 예산 초과·컷·전멸을 선행한다.
    base_phase = next((r.get("end_second_median") for r in hours if r.get("end_second_median") is not None), None)
    rows = []
    for r in hours:
        prev = prev_by_hour.get(r["hour"])
        if prev is None:
            delta = "—"
        else:
            diff = r["rest_mean"] - prev["rest_mean"]
            # 부호를 항상 붙인다 — 「29.2」와 「+29.2」가 표에서 같아 보이면 델타가 아니다.
            delta = f"{prev['rest_mean']:.1f} ({diff:+.1f})"
        phase = r.get("end_second_median")
        if phase is None:
            phase_cell = "—"          # 구버전 집계 — 「:00에 끝났다」가 아니다(규약 C)
        elif base_phase is None:
            phase_cell = f":{phase:04.1f}"
        else:
            phase_cell = f":{phase:04.1f} ({phase - base_phase:+.0f}초)"
        rows.append([
            f"{r['hour']:02d}시", str(r["cycles"]), f"{r['rest_mean']:.1f}", delta,
            f"{r['rest_max']:.1f}", str(r["over_60s"]), phase_cell,
            f"{r['slip_max']:.1f}", str(r["foreign_sum"]),
        ])
    out = _table(
        ["시간대", "사이클", "REST평균(초)", "전일 같은 시간대", "REST최대(초)", "60초초과",
         "사이클 종료 위상", "최대밀림(초)", "창안 타폴러호출"],
        rows,
    )
    if prev_by_hour:
        out += [
            "> **전일 열은 「같은 시간대」끼리 비교한 값이다.** 하루 평균끼리 비교하면 시간대 분포가 "
            "다른 두 날이 같은 숫자를 낸다 — 08-14가 그랬다(하루 평균은 평범했고, 오후만 갈렸다).",
        ]
    if base_phase is not None:
        out += [
            "> **사이클 종료 위상**은 사이클이 분 안의 몇 초에 끝나는가의 **중앙값**이고, 괄호는 "
            "그날 첫 시간대 대비 이동이다. 08-14는 :19 → :50으로 31초 밀렸고 **예산 초과 138건 · "
            "조기 포기 26건 · 전멸 86분이 전부 그 곡선 위에 올라앉아 있었다** — 지금까지 우리는 "
            "결과 셋을 각각 세면서 공통 원인인 이 한 줄은 안 쟀다.",
            "> 평균이 아니라 중앙값인 이유: 60초를 넘긴 사이클 하나가 평균을 다음 분으로 넘겨 "
            "**위상이 거꾸로 돈 것처럼**(:55 → :05) 보이게 만든다.",
        ]
    return out + [""] if out and out[-1] != "" else out


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


def _render_missing(metrics: dict, db_metrics: dict | None = None) -> list[str]:
    """2026-08-05 §2-6 — 결손을 **로그 축과 DB 축 두 줄로** 낸다.

    로그 축은 *"사이클이 돌았는가"* 를, DB 축은 *"그 분에 행이 남았는가"* 를 잰다. 08-05에는
    전자가 1분, 후자가 4분이었다 — 사이클이 정상 실행되고도 `rows=0`으로 끝난 분(14:31, KIS가
    53초간 전 레그 타임아웃)을 로그 축은 구조적으로 못 본다. **두 값의 차이 자체가 신호다.**
    """
    missing = dig(metrics, "cycles.missing") or {}
    log_count = missing.get("count", 0)
    downtime = missing.get("downtime_count", 0)
    infra = missing.get("infra_count", 0)
    recovered = missing.get("recovered_by_catchup", 0)
    out = [
        f"- **로그 기준** 결손 **{log_count}분** (홀수분 {missing.get('odd', 0)} / "
        f"짝수분 {missing.get('even', 0)})",
        # 2026-08-06 §3-5 / Fix#6 — **세 축으로 가른다.**
        # 08-06 §1은 `결손 분 21분 ▲20 ⚠`을 냈고, 그것을 인프라 악화로 읽으면 틀린다:
        # 20분이 프로세스 정지 구간이고 사이클이 돌면서 놓친 것은 1분뿐이었다.
        f"- 내역: **미가동 {downtime}분** · **인프라 결손 {infra}분** · 회수 {recovered}분",
    ]
    if downtime:
        out.append(
            f"> ⚠ **{downtime}분은 관측 루프가 아예 안 돌던 구간**이다(재기동 사이). "
            "인프라가 나빠진 것이 아니라 시스템이 꺼져 있었다 — 이 둘을 같은 분모로 섞으면 "
            "「결손 ▲20 ⚠」이 인프라 회귀로 오독된다. 정지 자체는 §11의 프로세스 기동 횟수와 "
            "함께 읽을 것."
        )
    down_listed = missing.get("downtime_list") or []
    if down_listed:
        out += ["", "미가동:", "```", " ".join(down_listed), "```"]
    infra_listed = missing.get("infra_list") or []
    if infra_listed:
        out += ["", "인프라 결손:", "```", " ".join(infra_listed), "```"]
    if not down_listed and not infra_listed:
        listed = missing.get("list") or []
        if listed:
            out += ["", "```", " ".join(listed), "```"]

    coverage = (db_metrics or {}).get("chain_minute_coverage") or {}
    if not coverage.get("available"):
        out += ["", "> DB 기준 0행 분은 DB 집계가 있을 때만 나온다.", ""]
        return out

    zero_count = coverage["zero_row_count"]
    out += [
        "",
        f"- **DB 기준** 옵션체인 0행 **{zero_count}분** "
        f"(관측 구간 {coverage['span_minutes']:,}분 중 행이 있는 분 {coverage['minutes_with_rows']:,})",
    ]
    if coverage["zero_row_minutes"]:
        out += ["", "```", " ".join(coverage["zero_row_minutes"]), "```"]
    out += _render_zero_row_run(coverage)
    out += _render_zero_row_causes(coverage.get("zero_row_by_cause"))
    if coverage["over_design_count"]:
        over = ", ".join(f"{t}({n}행)" for t, n in coverage["over_design_minutes"])
        out += [
            "",
            f"- ⚠ 설계 상한({db_metrics_module.CHAIN_LEGS_PER_CYCLE_DESIGN}행) 초과 "
            f"**{coverage['over_design_count']}분** — {over}. "
            "한 사이클의 행이 **이웃 분 라벨로 들어갔다**는 뜻이고, 0행 분과 같은 사건의 반대쪽이다.",
        ]
    if zero_count != log_count:
        out += [
            "",
            f"- ⚠ **두 축이 어긋난다 — 로그 {log_count}분 vs DB {zero_count}분.** "
            "로그 축은 *사이클이 돌았는가*, DB 축은 *행이 남았는가*를 잰다 — "
            "**정상적으로는 로그 ≤ DB**다(사이클이 돌고도 행이 0일 수 있으므로).",
            "> **로그 축이 DB 축보다 크면 파서를 먼저 의심한다.** 2026-08-10에 이 자리가 "
            "「로그 3분 vs DB 1분」을 냈고 종전 문구는 그것을 *「`rows=0` 사이클은 로그 축에 "
            "안 잡힌다」* 로 설명했는데 **사실이 아니었다** — `rows=0`이어도 사이클 줄은 남으므로 "
            "정상적으로 잡힌다. 진짜 원인은 (a) 정규식이 `, 재시도함` 변형을 못 읽어 사이클이 "
            "통째로 사라진 것과 (b) 결손 축이 파생 start를 써서 반올림으로 앞 분에 귀속된 것, "
            "**둘 다 계측 쪽**이었다. 지금은 둘 다 고쳤고 계약 테스트가 지킨다.",
        ]
    # 2026-08-07(§2-1 / Fix#3) — **덮어쓴 분.** 빈 분보다 나쁘다: 행 수가 정상이라 위 두 축
    # 어디에도 안 잡히고, 그 분의 데이터는 실제로 **다음 분에 수집된 값**이다.
    dup = (metrics.get("cycles") or {}).get("duplicate_poll_minutes") or {}
    if dup.get("labelled"):
        if dup.get("count"):
            out += [
                "",
                f"- ⚠ **같은 분 라벨로 두 번 적재된 분 {dup['count']}개** — {', '.join(dup['list'])}. "
                "그 분의 데이터는 **다음 분에 수집된 값으로 덮여 있다**(UPSERT라 행 수는 정상이다). "
                "사이클이 분 경계 직전에 깨어 `poll_time`이 내려깎인 것이다 — 08-07 Fix#3의 대상.",
            ]
        else:
            out += ["", f"- 같은 분 라벨 중복 **0건** (라벨이 실린 사이클 {dup['labelled']:,}개)"]
    else:
        # 규약 C — 0을 "없었다"로 읽지 않는다. 08-07 이전 로그에는 라벨 자체가 없다.
        out += ["", "- 같은 분 라벨 중복: **측정 불가** — 이 로그에는 `분=` 라벨이 없다(08-07 Fix#3 이전)"]
    out.append("")
    return out


def _render_zero_row_run(coverage: dict) -> list[str]:
    """2026-08-14 §2-1 / Fix#3 — **0행 분이 몇 개인가**가 아니라 **몇 분 붙어 있었는가**.

    08-14에 이 자리가 낸 것은 「DB 기준 0행 86분」 한 줄이었다. 그 86분 중 84분이
    14:00~15:23에 연속으로 붙어 있었고 — 정규장 마지막 80분, 감마가 가장 중요한 구간이다 —
    그 사실은 사람이 0행 분 목록을 눈으로 훑어서 알았다. 흩어진 86분이었다면 그날은
    아무 일도 없는 날이다. **같은 숫자가 두 사건을 가린다.**
    """
    run = coverage.get("zero_row_longest_run")
    if not run:
        # 규약 C — 이 키가 없는 것은 「연속 구간이 없었다」가 아니라 「구버전 집계다」이다.
        return ["", "- 최장 연속 0행 구간: **측정 불가** — 이 집계에는 그 키가 없다(08-14 Fix#3 이전)"]
    length = run.get("length") or 0
    threshold = coverage.get("zero_row_run_alert_minutes") or db_metrics_module.ZERO_ROW_RUN_ALERT_MINUTES
    if not length:
        return ["", f"- 최장 연속 0행 구간: **0분** (임계 {threshold}분)"]
    span = f"{run.get('start')}~{run.get('end')}"
    if length >= threshold:
        return [
            "",
            f"- ⛔ **최장 연속 0행 구간 {length}분** ({span}) — 임계 {threshold}분 초과.",
            "> 흩어진 0행 분과 **완전히 다른 사건이다.** 체인 신선도 창은 5분이므로 6분째부터 "
            "그 분의 판단은 체인을 아예 못 본다(`chain_input_source = none`) — 이 구간의 길이가 "
            "곧 **판단이 감마 지형 없이 간 시간**이다. §14의 「GEX 입력 없던 분」과 나란히 읽을 것.",
        ]
    return ["", f"- 최장 연속 0행 구간: **{length}분** ({span}, 임계 {threshold}분)"]


_ZERO_ROW_CAUSE_LABELS = (
    ("collection_wiped", "수집 전멸", "사이클은 돌았는데 적재 0행 — 전 레그 실패/예산 소진"),
    ("no_cycle", "사이클 없음", "그 분에 옵션체인 사이클 자체가 없었다"),
    ("written_elsewhere", "이웃 분 적재", "사이클도 행도 있었는데 라벨이 옆 분으로 갔다"),
)


def _render_zero_row_causes(causes: dict | None) -> list[str]:
    """2026-08-10 — 0행 분을 **원인별로** 낸다.

    이 분해가 없으면 세 원인이 한 칸에서 만나고, **원인을 주장하는 fix를 검정할 수 없다.**
    08-10에 `zero_row_count`가 1이 되어 08-07 Fix#3(분 경계 스냅)의 불변식이 반증으로 찍혔는데,
    그 1건은 `collection_wiped`(KIS 지연으로 예산 소진)였다 — **그 fix와는 무관한 사건**이다.
    """
    if not causes:
        return []
    rows = [
        [label, str(len(causes.get(key) or [])), " ".join(causes.get(key) or []) or "—", why]
        for key, label, why in _ZERO_ROW_CAUSE_LABELS
    ]
    out = ["", "0행 분 원인:", ""]
    out += _table(["원인", "분", "해당 분", "무엇인가"], rows)
    if not causes.get("labelled"):
        out += [
            "> ⚠ **이 분해는 파생 start 기반이다** — 그날 로그에 `분=` 라벨이 없다(08-07 이전). "
            "라벨 축보다 정확도가 낮으니 경계 근처 1분은 의심할 것.",
        ]
    return out


def _book_count(db: dict | None) -> int | None:
    """그날 옵션체인이 실제로 수집한 **만기 북 수**(1~3).

    2026-08-10 — 옵션체인 REST 호출 수의 가장 큰 구조 변수다. 08-10에 이 값이 3→2로 줄어
    `rest.by_group.옵션체인`이 9,241 → 7,489로 떨어졌는데, 그것을 08-07 유지 풀 fix의 공로로
    읽을 뻔했다(`2026-08-07-e1`의 대가 지표 `<= 9,500`이 "확인"으로 찍혔다).

    규약 F는 **주장 역할**의 건수 지표만 막고 대가는 의도적으로 면제한다("대가는 얼마나
    늘었나가 본질이라 건수가 맞는 경우가 많다"). 그 판단은 그대로 두고, 대신 **구조 변수를
    값 옆에 인쇄**한다 — 읽는 사람이 나눗셈을 할 수 있으면 규약을 조일 필요가 없다.
    """
    books = (db or {}).get("book_coverage") or []
    return len(books) or None


def _render_rest(metrics: dict, db: dict | None = None, previous: dict | None = None) -> list[str]:
    rest = metrics.get("rest") or {}
    out = [
        f"- 총 **{rest.get('total_calls', 0):,}건** / {rest.get('span_seconds', 0) / 60:.0f}분 "
        f"= **{_fmt(rest.get('calls_per_second'), '{:.3f}')}건/초** "
        f"(용량 대비 **{_fmt(rest.get('capacity_pct'), '{:.1f}')}%**, "
        f"적자 시작 배율 **{_fmt(rest.get('deficit_threshold_multiplier'), '{:.2f}')}배**)",
        "",
    ]
    books = _book_count(db)
    prev_books = _book_count((previous or {}).get("db"))
    group_headers = ["폴러 그룹", "호출 수"] + (["북당(옵션체인만)"] if books else [])
    group_rows = []
    for name, calls in (rest.get("by_group") or {}).items():
        row = [name, f"{calls:,}"]
        if books:
            row.append(f"{calls / books:,.0f}" if name == "옵션체인" else "—")
        group_rows.append(row)
    out += _table(group_headers, group_rows)
    if books:
        note = f"> **오늘 북 {books}개**"
        if prev_books and prev_books != books:
            note += (
                f" — 전일 {prev_books}개에서 **바뀌었다.** 옵션체인 호출 수는 북 수에 거의 비례하므로 "
                "이 표의 전일 비교는 **북당 열로만** 읽을 것(2026-08-10에 3→2 변화를 fix 효과로 "
                "읽을 뻔했다)"
            )
        elif prev_books:
            note += " (전일과 같다 — 총계를 그대로 비교해도 된다)"
        out += [note, ""]
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
    return out + _render_censored_calls(sc)


# 옵션체인 사이클이 잘리는 **세 원인**. 원인이 다르면 조치가 다르므로 한 지표로 합치지 않는다
# (`log_metrics`의 `budget_exceeded`/`timeout_abort`/`failure_budget_abort` 주석이 근거다).
_CHAIN_CUT_CAUSES: list[tuple[str, str, str]] = [
    ("budget_exceeded", "예산 초과(벽시계)", "우리가 느렸다 — 50초 안에 못 끝냈다"),
    ("timeout_abort", "연속 타임아웃", "KIS가 read timeout 천장에 닿았다"),
    ("failure_budget_abort", "실패 예산 소진", "성공과 실패가 섞여 절반이 죽었다"),
]


def _render_chain_cuts(metrics: dict) -> list[str]:
    """2026-08-19 (08-18 보고서 §2-4 / Fix#5) — **컷을 지연 게이지의 종속 지표로 두지 않는다.**

    08-18에 컷 라벨이 3건 켜졌는데 그 시각의 `p50 ÷ timeout`은 **0.74로 경고선(0.80) 아래**였다.
    먼저 울린 종은 예산 대비 104%였고, 지연 표만 읽은 회차는 그 세 건을 못 봤다. 그래서
    **독립 절**로 올린다 — 두 축은 같은 날 반대 방향으로 갈 수 있다.

    ## `데드라인이먼슬리에서끝남` 열을 원인별로 갈라 읽어야 하는 이유

    이 라벨의 옛 이름은 `우선순위위반`이었고, 그 이름이 08-18의 오진을 만들었다.
    **경로마다 뜻이 다르다**:

      `budget_exceeded`  벽시계가 먼슬리 구간에서 끝났다는 뜻이다. 위클리는 언제나 뒤에
                         있으므로 이 값은 **위반이 아니라 「먼슬리가 예산을 다 썼다」**이다.
                         읽어야 할 것은 그 분의 **먼슬리 두께**이지 순서가 아니다.
      `timeout_abort`    여기서는 **진짜 순서 축**이다. 스코프 컷은 1단계 접기(위클리 선제
                         강등)를 거치므로 `아니오`가 정보를 담는다 — 0이 아니면 Fix#4가 깨진 것이다.
    """
    out: list[str] = []
    rows = []
    for key, label, meaning in _CHAIN_CUT_CAUSES:
        node = metrics.get(key)
        if not isinstance(node, dict):
            continue
        rows.append([
            label,
            f"{node.get('count', 0):,}",
            f"{node.get('skipped_legs_total', 0):,}",
            _fmt(node.get("priority_cut_minutes"), "{:,.0f}"),
            _fmt(node.get("priority_before_others_minutes"), "{:,.0f}"),
            meaning,
        ])
    if not rows:
        return ["> 계측 전 — 이 로그에는 컷 절이 없다. **「컷이 없었다」가 아니라 「안 셌다」**이다.", ""]
    out += _table(
        ["원인", "건수", "포기 레그", "먼슬리에 닿은 분", "데드라인이 먼슬리에서 끝난 분", "뜻"],
        rows,
    )
    before = dig(metrics, "timeout_abort.priority_before_others_minutes")
    if before is None:
        out += ["> ⚠ `timeout_abort`의 라벨이 **없다**(구버전 로그) — 「순서를 지켰다」가 아니라 "
                "「못 쟀다」이다(규약 C).", ""]
    elif before:
        out += [f"> 🚨 **`timeout_abort` 경로에서 {before}분** — 이쪽은 진짜 순서 축이다. "
                "스코프 컷은 1단계 접기를 거치므로 0이어야 하고, 0이 아니면 `main.py`의 "
                "선제 강등(`dropped_non_priority_first`)이 깨진 것이다.", ""]
    else:
        out += ["> ✅ `timeout_abort` 경로 **0분** — 스코프 컷은 위클리를 먼저 버렸다(Fix#4 정상).", ""]
    out += [
        "> **`budget_exceeded` 쪽의 같은 열을 위반으로 읽지 말 것.** 레그 순서가 이미 먼슬리 "
        "우선이라(`books[0]`) 위클리는 언제나 뒤에 있고, 벽시계가 먼슬리 구간에서 끝나면 그 "
        "열은 **구조적으로 켜진다** — 그 분에 위클리는 한 건도 안 불렸다. 08-18 보고서가 이것을 "
        "「불변식이 처음 깨졌다」로 읽어 P1을 잘못 냈다. **읽어야 할 것은 그 분의 먼슬리 두께**이고, "
        "그 축은 §12 `monthly_leg_completeness`와 §9-1의 검열 건수에 있다.",
        "",
    ]
    return out


def _render_censored_calls(sc: dict) -> list[str]:
    """2026-08-19 (08-18 보고서 §2-5 / Fix#6) — **타임아웃에 잘린 호출을 따로 인쇄한다.**

    08-18에 판단이 죽은 넉 분(전부 `:01`)을 `p50 ÷ timeout` 게이지가 **원리적으로** 못 봤다.
    그 분들의 `4.02`초는 응답시간이 아니라 read timeout에 잘린 값(우측 검열)이고, 검열된
    표본의 분위수는 천장에 눌려 위쪽 꼬리를 잃기 때문이다. 그래서 **분위수가 아니라 건수**를
    같은 줄에서 센다 — 이 절이 §7-1(정각·30분에 무엇이 겹치는가)의 유일한 입력이다.

    **여기서 판정하지 않는다.** 점유율과 균등선을 나란히 놓고 원인 후보를 적을 뿐이다 —
    08-18 13:40 회차가 관측 2회로 단정하지 않은 것과 같은 이유이고, 그 유보가 옳았다.
    """
    cen = sc.get("censored")
    if not isinstance(cen, dict):
        return [
            "> 계측 전 — 검열(HTTP ≥ read timeout) 집계는 2026-08-19 Fix#6부터 쌓인다. "
            "그 이전 로그에서는 **「검열이 없었다」가 아니라 「안 셌다」**이다.",
            "",
        ]
    if not cen.get("count"):
        return ["> ✅ read timeout에 잘린 호출(우측 검열) **0건** — 오늘 p50/p95는 천장에 "
                "안 눌렸으므로 그 게이지를 액면 그대로 읽어도 된다.", ""]
    share, base = cen.get("phase_concentration"), cen.get("phase_baseline")
    minutes = cen.get("phase_minutes") or []
    out = [
        f"- ⚠ **검열 {cen['count']}건** (HTTP ≥ 그 엔드포인트의 read timeout) — "
        "이 호출들의 응답시간은 **실제 값이 아니다**(천장에 잘렸다). "
        "**p50·p95는 이 건들을 못 본다.**",
        "",
    ]
    if cen.get("by_endpoint"):
        out += _table(
            ["엔드포인트", "검열 건수", "그 통로의 천장(초)"],
            [[e, f"{n:,}", f"{log_metrics.read_timeout_for_label(e):.1f}"]
             for e, n in cen["by_endpoint"].items()],
        )
        out += ["> 통로마다 천장이 다르다 — 08-18 실측: `inquire-price`(4.0초)는 12시부터 눌렸고 "
                "`inquire-balance`(10.0초)는 최대 7.62초로 **안 닿았다**. 같은 임계로 세면 틀린다.", ""]
    if share is not None and base is not None:
        # 2026-08-19 — **판정어를 뺐다. 배수만 인쇄한다.**
        #
        # 종전 이 자리에는 `점유율 >= 균등선 x 2`로 「위상 문제다 / 균등선 근처다」를 단정하는
        # 문장이 있었다. 08-19 실측 22.4%(1.68배)가 그 임계 밑으로 떨어져 **「균등선 근처다 —
        # 특정 분에 몰린 것이 아니다」**로 인쇄됐는데, 13.3%의 1.68배는 「근처」가 아니다.
        # 뭉툭한 임계 하나로 연속량을 이분한 것이고, 그 문장은 **매일 틀린 채로 인쇄된다.**
        ratio = dig(sc, "censored.phase_ratio")
        ratio_text = f" → **{ratio:.2f}배**" if isinstance(ratio, (int, float)) else ""
        out += [
            f"- 정각·30분 창(`:{minutes[0]:02d}~` 등 {len(minutes)}분) 점유율 **{share:.1%}** "
            f"/ 균등선 {base:.1%}{ratio_text}",
            "",
            "> **배수만 인쇄하고 판정하지 않는다.** 이틀 실측이 그 이유다 — 08-18 **2.54배** → "
            "08-19 **1.68배**. 같은 축이 이틀 만에 크게 움직였으므로 하루치로는 위상 문제라고도 "
            "아니라고도 말할 수 없다. 임계 하나로 이분하면 **경계 근처의 날이 매번 틀리게 인쇄된다** "
            "(08-19가 그랬다).",
            "> **왜 이 창인가**: 08-18에 `감마플립 산출 불가` 4건이 **전부 `:01`**이었고, HTTP ≥ 4.0초 "
            "333건 중 103건(31%)이 이 여덟 분에 있었다. 그 분들의 페이서 대기는 1.0~1.8초로 "
            "평범했다 — 우리 백오프가 아니라 KIS 응답이다.",
            "> ⚠ **그 관측은 08-19에 재현되지 않았다**: 산출 불가 **24건 중 위상창 6건(25%)**이다. "
            "08-18의 「전부 `:01`」은 표본이 **넷**이었다. 오늘은 훨씬 나쁜 날이었는데 **덜** "
            "위상적이었다(검열 290 → 747건, 점유율 33.8% → 22.4%).",
            "> **원인은 여기서 확정하지 않는다.** 그 분에 우리가 무엇을 더 쏘는지(만기유동성 폴러의 "
            "`startup_offset`, 5분 주기 매크로, 300초 창 인쇄)를 엔드포인트별 초 단위로 펼쳐야 "
            "갈린다 — 겹치지 않았는데 HTTP가 천장이면 **KIS 쪽**이고, 그때 레버 E는 이 문제에 "
            "듣지 않는다(레버 E는 우리 레그 수 축이다).",
            "",
        ]
    if cen.get("samples"):
        out += _table(
            ["시각", "HTTP(초)", "페이서(초)", "엔드포인트"],
            [[s["at"], f"{s['http']:.2f}", f"{s['pacer']:.2f}", s["endpoint"]]
             for s in cen["samples"]],
        )
    return out



def _render_latency_streak(warnings: list[dict], previous: dict | None, threshold) -> list[str]:
    """2026-08-05 고도화#5 — 사전 대응 규칙의 **발동 조건("이틀 연속 같은 시간대")을 자동 판정**한다.

    08-04 고도화#5가 이 규칙을 숫자 보기 전에 적어뒀는데, 그 발동 조건은 어제 리포트와 오늘
    리포트를 **사람이 손으로 대조**해야만 확인할 수 있었다. 대조를 안 하면 규칙은 적어둔 채로
    영영 발동하지 않는다 — 07-30에 "예측치를 못 적겠으면 근거가 부족한 것"이라고 정한 규약이
    같은 이유로 자동 대조를 얻은 것과 같은 자리다.

    **발동 자체는 여전히 사람이 한다.** 이 함수는 *"조건이 성립했다"* 까지만 말한다 — 지연을
    보고 폴링을 자동으로 줄이면 되먹임이 생긴다(2026-07-08에 203분을 잃은 그 구조).
    """
    prev_warnings = dig(previous or {}, "rest_latency.warnings") or []
    if not prev_warnings:
        return [
            "> 전일 §9-1 계측이 없어 **연속 판정을 못 한다** — 이 규칙은 이틀치가 있어야 성립한다.",
            "",
        ]

    def key(w):
        return (w["hour"], w["endpoint"])

    repeated = sorted(set(map(key, warnings)) & set(map(key, prev_warnings)))
    prev_date = (previous or {}).get("date", "전일")
    if not repeated:
        return [
            f"> 연속 판정: **해당 없음** — 오늘 넘은 시간대 중 {prev_date}에도 넘은 것이 없다. "
            "규칙은 발동하지 않는다.",
            "",
        ]
    listed = ", ".join(f"{hour}시 `{endpoint}`" for hour, endpoint in repeated)
    return [
        f"- 🔔 **연속 판정 성립: {len(repeated)}개 구간** — {listed} (오늘 + {prev_date} 모두 "
        f"{threshold}초 초과)",
        "",
        "> **사전 대응 규칙의 발동 조건이 충족됐다.** `inquire-price`가 포함돼 있으면 그 시간대에 "
        "한해 위클리 폴링을 2분 → 4분 격분으로 늘리는 것이 미리 정해둔 조치다. "
        "**적용 여부는 사람이 결정하고, 결정하면 그 자리에서 `hypotheses.yaml`에 예측치를 적는다.**",
        "",
    ]


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
    total = lv.get("total_lines", 0)
    httpx_lines = lv.get("httpx_lines", 0)
    human = lv.get("human_lines", 0)
    tb = lv.get("traceback_lines", 0)
    out = [
        f"- 총 **{lv.get('total_bytes', 0) / 1048576:.2f}MB** / {total:,}줄 — "
        f"httpx {lv.get('httpx_bytes', 0) / 1048576:.2f}MB({_fmt(lv.get('httpx_pct'), '{:.1f}')}%), "
        f"**사람이 읽는 줄 {human:,}줄**",
        # 2026-08-05 §2-4 — 항등식을 눈으로 확인할 수 있게 찍는다. 08-05에는 이 줄이 없어
        # 트레이스백 16,577줄이 `human_lines`에 섞였고, 그 값으로 가설 p4가 **거짓 반증**됐다.
        f"- 줄 구성: httpx **{httpx_lines:,}** + 사람 **{human:,}** + "
        f"트레이스백 **{tb:,}** = **{httpx_lines + human + tb:,}**"
        + ("" if httpx_lines + human + tb == total else f" ⚠ 총계 {total:,}과 불일치"),
        "",
    ]
    if total and tb / total > 0.25:
        out += [
            f"- ⚠ 트레이스백이 로그의 **{tb / total * 100:.0f}%** — 반복 예외가 표본 상한을 "
            "넘겨 새는지 확인할 것(`logutil.TRACEBACK_SAMPLES_PER_EXCEPTION_TYPE`).",
            "",
        ]
    # 2026-08-06 §3-3 / Fix#4 — 그날 프로세스가 몇 번 떴는가.
    # `오늘 N번째` 트레이스백 카운터는 **프로세스 단위**라, 재기동이 있으면 되감긴다(08-06에는
    # 세 번 떠서 같은 번호가 로그에 두 번 나왔다). 이 줄이 없으면 그 사실이 안 보인다.
    starts = metrics.get("process_starts") or []
    if len(starts) > 1:
        out += [
            f"- ⚠ **관측 루프가 오늘 {len(starts)}번 떴다** "
            f"({', '.join(_hhmmss(s) for s in starts)}) — `오늘 N번째` 카운터가 "
            f"{len(starts) - 1}번 되감겼고, 트레이스백 표본도 프로세스마다 새로 열렸다. "
            "재기동 구간은 §4의 「미가동」과 함께 읽을 것.",
            "",
        ]

    out += _table(["레벨", "건수"], [[k, str(v)] for k, v in (lv.get("by_level") or {}).items()])

    # 2026-08-06 §3-2 / Fix#4 — 「줄」과 「억제」를 나란히 낸다.
    # 08-06 실측: `read_timeout` 126건으로 보고됐지만 실제는 205건이었다(WarningThrottle이
    # 81건의 줄을 통째로 삼켰고, 그 숫자는 `(최근 60초간 M건 추가 억제됨)`에만 남아 있었다).
    suppressed = metrics.get("qualitative_suppressed") or {}
    qual_rows = []
    for key, total_count in (metrics.get("qualitative") or {}).items():
        hidden = suppressed.get(key, 0)
        qual_rows.append([
            key, str(total_count),
            f"{total_count - hidden:,} + {hidden:,}" if hidden else "—",
        ])
    out += _table(["정성 항목", "건수", "줄 + 억제"], qual_rows)
    if suppressed:
        out += [
            "> 「줄 + 억제」의 뒤쪽은 **로그에 줄이 아예 안 남은** 건수다(`WarningThrottle`이 60초 "
            "창당 1건만 남긴다). 건수 열은 둘의 합이다 — 08-06까지는 앞쪽만 세어 "
            "`read_timeout`을 실제의 61%로 보고했다. 억제분은 그 요약을 실은 줄의 예외 유형에 "
            "합산한 **근사**다(창이 60초라 유형이 바뀌는 경우는 드물다).",
            "",
        ]
    out += _render_parser_audit(metrics)
    # 2026-08-06 §3-4 / Fix#5 — 실패를 **원인 축으로** 갈라 낸다.
    # 08-06에 `만기 유동성 폴링 실패 7건`이 08-05 `p2`를 반증했는데, 7건이 전부 EGW00201이고
    # ReadTimeout 기인은 0건이었다 — 그 가설의 주장은 오히려 맞았고 지표가 그것을 못 봤다.
    by_cause = metrics.get("failures_by_cause") or {}
    causes = sorted({c for row in by_cause.values() for c in row})
    if by_cause and causes:
        out += _table(
            ["실패 유형", "총계", *causes],
            [
                [kind, str(total), *[str(by_cause.get(kind, {}).get(c, 0) or "—") for c in causes]]
                for kind, total in (metrics.get("failures") or {}).items()
            ],
        )
        other_total = sum(row.get("other", 0) for row in by_cause.values())
        grand_total = sum((metrics.get("failures") or {}).values())
        out += [
            "> **주장이 원인을 말하면 지표도 원인으로 잘려야 한다**(규약 E의 다음 칸). "
            "총계만 보면 08-06처럼 «맞은 fix가 반증으로» 나온다.",
            f"> `other` **{other_total}건 / {grand_total}건** — 트레이스백이 살아 있는 예외는 "
            "실패 줄에 유형이 안 실려 여기로 떨어진다(예산이 유형당 3건이라 정상 범위는 10건 "
            "안쪽이다). 크게 늘면 분류를 늘릴 때다.",
            "",
        ]
    else:
        out += _table(
            ["실패 유형", "건수"],
            [[k, str(v)] for k, v in (metrics.get("failures") or {}).items()],
        )
    return out


def _render_rest_latency(metrics: dict, previous: dict | None = None) -> list[str]:
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
    out += _render_p50_timeout_cross(metrics, lat)
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
        out += _render_latency_streak(warnings, previous, threshold)
    else:
        out += [f"> ✅ p95가 임계({threshold}초)를 넘은 (시간대, 엔드포인트) 없음.", ""]
    return out


# 이 교차표가 보는 엔드포인트. 옵션체인 20레그를 실제로 부르는 호출이고, 08-14에 절벽을
# 만든 것도 이것이다. 다른 엔드포인트는 타임아웃이 따로(10초) 걸려 있어 같은 줄에 놓으면 틀린다
# (`rest_client._ENDPOINT_READ_TIMEOUT_SECONDS`).
_CHAIN_ENDPOINT = "inquire-price"


def _render_p50_timeout_cross(metrics: dict, lat: dict) -> list[str]:
    """2026-08-14 §2-2 / Fix#3 — **p50과 read timeout을 한 줄에 놓는다.**

    08-14의 절벽을 예고할 수 있었던 유일한 값이다. 그날 13:36 창에서 p50/timeout = 0.77로
    임계(0.8)에 닿았고, 24분 뒤 84분 전멸이 시작됐다. 같은 시각 p95 임계는 이미 다섯 시간째
    붉었으므로 **아무것도 구별해 주지 못했다.**

    임계값은 「그날 실제로 걸려 있던 타임아웃」이어야 한다 — 레버가 켜진 날은 전역값과 다르다.
    """
    grid = lat.get("p50_by_hour") or {}
    if not grid:
        return []
    fallback = lat.get("global_read_timeout_seconds") or log_metrics.GLOBAL_HTTP_READ_TIMEOUT_SECONDS
    lever = levers_module.lever_value(metrics.get("levers"), "OPTION_CHAIN_READ_TIMEOUT_SECONDS")
    # 이 레버는 **꺼진 값이 `None`**(= 전역값 사용)이라 None을 폴백으로 접는 것이 정확하다.
    timeout = float(lever) if lever is not None else float(fallback)
    source = "레버" if lever is not None else "전역"
    ratio_warn = lat.get("p50_timeout_ratio_warn") or log_metrics.REST_LATENCY_P50_TIMEOUT_RATIO_WARN

    # **판정은 창 최대값으로 한다.** 시간대 가중평균은 절벽을 눌러 없앤다(08-14 13시:
    # 평균 2.18초 → 비율 0.55로 조용한데, 그 안의 창들은 3.08~3.53초 = 0.77~0.88로 이미
    # 경고선이었고 그 20~60분 뒤 전멸이 시작됐다). 평균은 «그 시간대가 전반적으로 어땠나»에만 답한다.
    grid_max = lat.get("p50_max_by_hour") or {}
    rows, breached, approached = [], [], []
    for hour, row in grid.items():
        p50 = row.get(_CHAIN_ENDPOINT)
        if p50 is None:
            continue
        p50_max = (grid_max.get(hour) or {}).get(_CHAIN_ENDPOINT, p50)
        ratio = p50_max / timeout if timeout else 0.0
        mark = "⛔" if ratio >= 1.0 else ("⚠" if ratio >= ratio_warn else "")
        rows.append([
            f"{hour}시", f"{p50:.2f}", f"{p50_max:.2f}", f"{timeout:.2f}", f"{ratio:.2f}", mark or "—",
        ])
        if ratio >= 1.0:
            breached.append((hour, p50_max, ratio))
        elif ratio >= ratio_warn:
            approached.append((hour, p50_max, ratio))
    if not rows:
        return []

    out = _table(
        ["시간대", f"`{_CHAIN_ENDPOINT}` p50 평균(초)", "창 최대 p50(초)",
         f"read timeout(초, {source})", "최대/timeout", ""],
        rows,
    )
    out += [
        f"> **창 최대 p50 ÷ read timeout.** 이 비율이 1.0에 닿는 순간 «그 창의 호출 절반 이상이 "
        "타임아웃»이고, 20레그 순차 수집의 기대 성공 수는 0에 수렴한다 — 수집이 "
        f"**느려지는 것이 아니라 비어 버린다.** 경고선 {ratio_warn}.",
        "> **평균이 아니라 최대로 판정하는 이유**: 08-14 13시는 평균 2.18초(비율 0.55)로 조용했는데 "
        "그 안의 창들은 3.08~3.53초(0.77~0.88)였고 **그 20~60분 뒤 84분 전멸이 시작됐다.** "
        "평균은 선행 신호를 평탄화해 없앤다.",
        "",
    ]
    if breached:
        hits = ", ".join(f"{h}시 {p:.2f}초(비율 {r:.2f})" for h, p, r in breached)
        out += [
            f"- ⛔ **중앙값 호출이 타임아웃을 넘긴 시간대 {len(breached)}개** — {hits}.",
            "> 이 시간대의 옵션체인은 «얇았던» 것이 아니라 **비어 있었을** 가능성이 높다. "
            "§4의 「최장 연속 0행 구간」과 §14의 「GEX 입력 없던 분」을 반드시 함께 읽을 것.",
            "",
        ]
    elif approached:
        hits = ", ".join(f"{h}시 {p:.2f}초(비율 {r:.2f})" for h, p, r in approached)
        out += [
            f"- ⚠ **경고선({ratio_warn})을 넘은 시간대 {len(approached)}개** — {hits}. "
            "중앙값 호출이 타임아웃 한 뼘 앞이다.",
            "",
        ]
    else:
        out += [f"> ✅ 비율이 경고선({ratio_warn})을 넘은 시간대 없음.", ""]
    return out


def _render_member_availability(db: dict) -> list[str]:
    """2026-08-04 고도화#2 — `available_member_count` 숫자 하나로는 어느 멤버가 왜 죽었는지 모른다."""
    ma = db.get("member_availability") or {}
    if not ma.get("available"):
        return [f"> 계측 전 — {ma.get('reason', '사유 미상')}.", ""]
    members = ma.get("members") or []
    out = _table(
        # 2026-08-05 §2-8 — "구조적" 열은 시장 구조상 불가피한 미가용(종가 단일가)이다.
        # 가용률에서 빼지 않고 **나란히** 둔다: 빼면 전일 대비 델타의 의미가 조용히 바뀐다.
        ["멤버", "가용 분", "가용률", "그중 구조적", "미가용 대표 사유"],
        [
            [
                m["member"] + ("" if m["implemented"] else " *(미구현)*"),
                f"{m['available_minutes']:,}",
                f"{m['available_pct']:.1f}%",
                f"{m.get('structural_minutes', 0):,}",
                m["top_unavailable_reason"] or "—",
            ]
            for m in members
        ],
    )
    out += [
        f"> 분모 {ma.get('minutes', 0):,}분. 사유는 판단 시점에 `risk_gate_state.member_unavailable`로 "
        "남긴 값이다 — 2026-08-04에는 이 표가 없어 사람이 `signal_layer.py`를 읽어 역산했고, "
        "그 역산 끝에 `orderflow_ofi_vpin`이 **데이터가 있는데도** 죽어 있다는 것이 나왔다(§2-5).",
    ]
    structural_total = sum(m.get("structural_minutes", 0) for m in members)
    if structural_total:
        out.append(
            "> **구조적 미가용은 결함이 아니다** — 종가 단일가(15:35~15:45)에는 연속 체결이 없어 "
            "WS 1분봉이 안 만들어지고 OFI도 없다. 08-05에는 이 구분이 없어 9분이 가용률에 녹아들었고, "
            "**08-04 §2-10이 '가치가 높다'고 판정한 종가 형성 구간에서 앙상블이 4→3으로 얇아지는 것이 "
            "안 보였다.** 이 값이 9분보다 크게 늘면 단일가 밖에서도 체결이 끊긴 것이다."
        )
    out.append("")
    return out


def _member_pair_history(history: list[dict] | None) -> tuple[list[str], dict[tuple[str, str], list]]:
    """
    입력: 직전 영업일들의 지표 사이드카(최신순).
    계산: (날짜 라벨 목록, 멤버 쌍 -> 그 날짜들의 일치율 목록)을 만든다. 값이 없는 날은 None.
    해석: 2026-08-07 고도화#5. 쌍이 그날 없었으면 자리를 비우되 열은 유지한다 — 열이 사라지면
         "그날은 안 쟀다"와 "그날은 0%였다"가 화면에서 같아진다.
    실패 조건: 없다 — 이력이 없으면 빈 결과.
    """
    labels: list[str] = []
    series: dict[tuple[str, str], list] = {}
    for day in history or []:
        labels.append(str(day.get("date", "?")))
        by_pair = {
            (p["a"], p["b"]): p.get("same_sign_pct")
            for p in ((day.get("db") or {}).get("member_score_quality") or {}).get("pairs") or []
        }
        for key in set(series) | set(by_pair):
            series.setdefault(key, [None] * (len(labels) - 1)).append(by_pair.get(key))
    return labels, series


def _render_member_scores(db: dict, history: list[dict] | None = None) -> list[str]:
    """2026-08-05 고도화#4 — 가용성(§14-1)이 "누가 살아 있었나"라면 여기는 "뭐라고 했나"다."""
    mq = db.get("member_score_quality") or {}
    if not mq.get("available"):
        return [f"> 계측 전 — {mq.get('reason', '사유 미상')}.", ""]

    out = _table(
        ["멤버", "산출 분", "평균 점수", "강세(+)", "약세(−)", "중립(0)"],
        [
            [
                m["member"], f"{m['scored_minutes']:,}", _fmt(m.get("mean"), "{:+.4f}"),
                f"{m['positive']:,}", f"{m['negative']:,}", f"{m['zero']:,}",
            ]
            for m in mq.get("members") or []
        ],
    )
    pairs = mq.get("pairs") or []
    if pairs:
        # 2026-08-07 고도화#5 — 일치율에 **직전 영업일들의 값을 나란히** 붙인다.
        past_labels, past_series = _member_pair_history(history)
        out += _table(
            ["멤버 쌍", "둘 다 비영인 분", "부호 일치", "일치율", "전일 대비"] + past_labels,
            [
                [
                    f"{p['a']} ↔ {p['b']}", f"{p['both_nonzero_minutes']:,}",
                    f"{p['same_sign_minutes']:,}", _fmt(p.get("same_sign_pct"), "{:.1f}%"),
                    # 2026-08-07 고도화#B — **변화폭이 판정 축이다.** 아래 주석 참고.
                    _fmt(
                        _pct_points(
                            p.get("same_sign_pct"),
                            (past_series.get((p["a"], p["b"])) or [None])[0],
                        ),
                        "{:+.1f}pt",
                    ),
                ]
                + [
                    _fmt(v, "{:.1f}%")
                    for v in past_series.get((p["a"], p["b"]), [None] * len(past_labels))
                ]
                for p in pairs
            ],
        )
        out += [
            "> **판정 축은 고정 임계가 아니라 「전일 대비 변화폭」이다**(2026-08-07 고도화#B). "
            "08-07에 `flow_position ↔ options_flow`가 08-06의 **74.6% → 13.1%** 로 하루 만에 "
            "뒤집혔다(−61.5pt). 그 전날 낮에는 22.3%를 보고 *「부호 규약이 뒤집혀 있을 가능성」* 을 "
            "의심했는데, **그 가설은 기각된다** — 규약 버그라면 매일 낮아야 한다. "
            "**흔들린다는 것이 앙상블이 살아 있다는 증거다.**",
            "> **여전히 임계를 걸지 않는다.** 정상 변동폭을 모르는 상태에서 임계를 먼저 정하면 그 "
            "임계가 곧 결론이 된다(08-05 스팟 소스 괴리율에서 같은 실수를 했다). 며칠 쌓아 "
            "「하루에 60pt 흔들리는 것이 정상인가」부터 안 뒤에 정한다. "
            "**고착**(며칠 연속 같은 방향으로 50%에서 멀리)이면 그때 `signal_layer`의 부호 규약을 본다.",
            "> 0은 중립이지 동의가 아니므로 **둘 다 비영인 분만** 분모로 센다.",
        ]
    out += [
        "> **일치율이 이 표의 핵심이다.** 멤버가 항상 같은 부호면 앙상블은 **실질 1멤버**이고, "
        "그때 `available_member_count` 4는 판단이 4개 축을 본다는 뜻이 아니다(가중치를 바꿔도 "
        "답이 안 바뀐다). 0은 중립이지 동의가 아니므로 **둘 다 비영인 분만** 분모로 센다.",
        "> 08-05의 `SMALL_TEST` 41건(`conflict_resolution:no_clear_consensus`)이 멤버가 실제로 "
        "갈렸다는 첫 증거였고, 그중 36건이 14~15시(Charm 경로가 열리는 시각)에 몰려 있었다.",
        "> **진입이 없어도 잴 수 있는 지표다** — ADVISORY 전용을 이유로 미루면 실거래 전환 "
        "시점에 비교할 기준선이 없다.",
        "",
    ]
    return out


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
                ["ATM 정합률(지수 기준)", f"**{q['atm_covered_pct']:.1f}%**",
                 "그 분의 ATM이 수집 행사가 안에 있었는가 — **핵심 지표**"],
                # 2026-08-05 고도화#1 / 규약 D — 같은 지표를 **독립 소스**로 한 번 더 잰다.
                ["ATM 정합률(선물 기준)",
                 f"{_fmt(q.get('atm_covered_pct_by_futures'), '{:.1f}%')} "
                 f"(같은 분 지수 기준 {_fmt(q.get('atm_covered_pct_by_index_same_minutes'), '{:.1f}%')})",
                 f"독립 소스 교차 검증 — 공통 {q.get('futures_cross_check_minutes', 0):,}분"],
                ["**소스 간 격차**", f"**{_fmt(q.get('atm_source_gap_pt'), '{:+.1f}pt')}**",
                 "두 스팟 소스가 만든 판정 차이 — **이것이 규약 D의 결론이다**"],
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
            "> **소스 간 격차가 왜 여기 있는가(2026-08-05 고도화#1 / 규약 D)**: 「ATM 정합률」의 "
            "스팟은 이 지표가 **감시하는 파이프라인이 적재한 값**이다 — 감시자와 감시 대상이 "
            "입력을 공유한다. 08-05에 그 결합이 90분짜리 사고를 통과시켰다: 지수가 전일 종가에 "
            "얼어붙어 **스팟도 행사가도 틀렸는데 둘이 서로 일치해서** 정합으로 세어졌고 지표는 "
            "88.1%를 냈다. 선물 WS는 완전히 다른 경로로 들어오므로 **격차가 곧 오염의 크기**다 "
            "(08-04 +4.2pt → 08-05 **+9.4pt**). 어느 쪽이 옳은지는 §16의 스팟 소스 괴리와 함께 "
            "사람이 읽는다.",
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


# 2026-08-06(Fix#3) — 먼슬리 북을 무엇으로 식별했는지. 1순위(`expiry_liquidity`)일 때는 아무
# 표시도 하지 않는다(정상이 조용해야 이상이 눈에 띈다). 폴백일 때만 출처를 드러낸다 —
# 08:31 전에는 폴백이 정상이지만, **장 끝난 뒤 리포트에 이 꼬리가 붙어 있으면 만기유동성 폴러가
# 하루 종일 죽어 있었다는 뜻**이라 그 자체가 경보다.
_MONTHLY_EXPIRY_SOURCE_LABEL = {
    "signal_decisions": " — 출처 `signal_decisions.gex_expiry` 폴백(만기유동성 미적재)",
}


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
            f"만기 {monthly.get('expiry') or monthly.get('reason')}"
            # 2026-08-06 Fix#3 — 폴백이 조용히 쓰이면 1순위(만기유동성 폴러)가 죽은 것을 모른다.
            f"{_MONTHLY_EXPIRY_SOURCE_LABEL.get(monthly.get('expiry_source'), '')})",
            "> **이것이 GEX/감마플립 입력의 1분 연속성**이다 — 인프라 지표(밀림·백오프)가 좋아져도 "
            "이 값이 나빠질 수 있으므로 반드시 나란히 읽는다(2026-07-31: 밀림 83→46건인데 "
            "커버리지 95.0%→90.5%).",
            "",
        ]
    # 2026-08-05 §2-7 — 커버리지 바로 아래에 둔다. 커버리지는 "그 분에 행이 있는가"만 보고
    # **몇 개인지는 안 본다**. 08-05는 커버리지 98.8%인데 레그 10개 미만이 38.2%였다.
    legs = db.get("monthly_leg_completeness") or {}
    if legs.get("available"):
        out += [
            f"- **먼슬리 레그 완전성**: 설계 {legs['design_legs']}레그 미만 "
            f"**{legs['below_design_count']:,}분 / {legs['minutes']:,}분 "
            f"({legs['below_design_pct']}%)** · 중앙 {_fmt(legs.get('legs_median'), '{:.0f}')}레그 "
            f"· 최소 {legs.get('legs_min')}레그 · "
            f"BS 최소({db_metrics_module.GAMMA_FLIP_MIN_LEGS}) 미달 "
            f"**{legs['below_flip_minimum_count']:,}분**",
            "> 커버리지가 *데이터가 있는가*라면 이것은 **판단 주입력의 두께**다. 먼슬리는 "
            "GEX/감마플립의 유일한 입력이므로(v6 §11.4, 08-04 Fix#5) 레그가 얇아지면 커버리지가 "
            "100%라도 신호가 얇아진다. BS 최소 미달 분은 감마플립을 **산출 시도조차 못 한 분**이다.",
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
    out += _render_effective_members(db)
    out += _render_entry_cutoff(db)
    out += _render_decision_outcomes(db)
    # 2026-08-11 Fix#4 — `db.regime`이 list에서 dict로 바뀌었다(`visits` + `trend_minutes`).
    # 구 JSON(08-10 이전)을 읽을 때를 위해 list도 그대로 받는다 — 전일 대비 델타가 깨지면
    # 리포트가 조용히 빈 표를 낸다.
    regime = db.get("regime") or {}
    regime_visits = regime.get("visits", []) if isinstance(regime, dict) else regime
    out += _table(
        ["레짐", "오늘", "전체 이력", "영업일"],
        [[r["regime"], f"{r['today']:,}", f"{r['total']:,}", str(r["days"])] for r in regime_visits],
    )
    if isinstance(regime, dict) and regime.get("today_total"):
        trend = regime.get("trend_minutes", 0)
        out += [
            f"- **추세 레짐 방문 {trend}분** / 오늘 {regime['today_total']}분"
            + (
                " — 추세가 0분이면 `regime_hmm` 멤버가 매분 0점인 것은 **정상이고 반증이 아니다**"
                "(v6 §7: 방향은 TREND_UP/DOWN_STRONG에만 있다). 그때 §14-3의 그 축은 «판정 불가»다."
                if trend == 0 else
                " — 이 분들에서 `regime_hmm`이 비영이어야 한다(0이면 그것이 진짜 결함이다)."
            ),
            "",
        ]
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
    out += _render_regime_model_provenance(fs.get("model"))
    return out


def _render_regime_model_provenance(model: dict | None) -> list[str]:
    """2026-08-10 — 배포된 레짐 모델이 **무엇으로** 학습됐는지.

    위 `feature_store` 줄은 **DB가 기록한 값**을 센다. 그런데 08-10부터 학습은 `iv_chg`를
    먼슬리 단독으로 **재계산해서** 쓴다(DB는 안 고친다). 그 사실을 여기 안 적으면 읽는 사람은
    학습 입력이 위 표와 같다고 가정한다 — 그 가정이 틀린 날이 오늘부터다.
    """
    if not model:
        return [
            "- 레짐 모델: **미배포** — WARMUP 폴백 동작 중(v6 §16.1)",
            "",
        ]
    if model.get("note"):
        return [f"- 레짐 모델: {model['note']}", ""]
    return [
        f"- 레짐 모델: {model.get('trained_at', '?')} 학습 · 샘플 {model.get('samples', 0):,}개 "
        f"/ 세션 {model.get('sessions', 0)}개 · **iv_chg = {model.get('iv_chg_source', '?')}**",
        "> **학습 입력이 위 `feature_store` 표와 다르다**(2026-08-10). `iv_chg`는 북 혼합으로 "
        "분 단위 구형파라(짝수분 0.5285 / 홀수분 0.7387) 학습 시점에 먼슬리 단독으로 재계산한다. "
        "**DB의 과거 행은 고치지 않는다** — 그날 실제로 계산된 값이 무엇이었는지는 기록으로 남는다.",
        "",
    ]


def _render_effective_members(db: dict) -> list[str]:
    """2026-08-06 고도화#2 — 「가용 4멤버」가 실제로 몇 개 축을 보고 있었는가.

    §14-3(멤버별 점수)이 *"어느 멤버가 뭐라고 했나"* 라면 이 줄은 그것을 **한 숫자로 접은 것**이다.
    둘 다 필요하다: 여기서 차이를 보고, §14-3에서 누구인지 본다.
    """
    mc = (db.get("decisions") or {}).get("member_count") or {}
    if not mc.get("available"):
        return [
            "> 실질 멤버 수는 2026-08-06 고도화#2 이후 판단부터 나온다"
            f"({mc.get('reason', '미기록')}).",
            "",
        ]
    dead = mc.get("dead_axis_mean") or 0
    out = [
        f"- **실질 멤버 수**: 가용 평균 {mc.get('available_mean')} vs 실질 평균 "
        f"**{mc.get('effective_mean')}** (죽은 축 평균 **{dead}**, 최소 실질 "
        f"{mc.get('effective_min')}멤버 · 죽은 축이 있던 분 {mc.get('minutes_with_dead_axis'):,})",
    ]
    if dead:
        out.append(
            "> **0은 중립이지 의견이 아니다.** 가용 멤버가 넷이어도 그중 하나가 매분 0점을 내면 "
            "앙상블은 실질 셋이고, 그때 가중치를 바꿔도 답이 안 바뀐다. 08-06에 `regime_hmm`이 "
            "399분 전량 중립이었다(§14-3) — 레짐이 23영업일 연속 한 상태였기 때문이다. "
            "**누가 죽었는지는 §14-3에서 본다.**"
        )
    else:
        out.append(
            "> 죽은 축 0 — 가용 멤버 전부가 매분 의견을 냈다. `available_member_count`를 "
            "그대로 믿어도 되는 날이다."
        )
    out.append("")
    return out


def _render_entry_cutoff(db: dict) -> list[str]:
    """2026-08-06 §2-2 / Fix#1 — v6 §4.2 「14:50 신규 진입 컷오프」의 **불변식**을 낸다.

    이 절이 다른 절과 다른 점: 여기 나오는 숫자는 시장이 아니라 **우리 코드**를 잰다.
    `enter_after_cutoff`가 0이 아닌 날은 시장이 특이한 날이 아니라 게이트가 빠진 날이다.
    """
    cutoff = (db.get("decisions") or {}).get("entry_cutoff")
    if not cutoff:
        return []
    violated = cutoff.get("enter_after_cutoff") or 0
    after_flat = cutoff.get("enter_after_forced_flat") or 0
    blocked = cutoff.get("blocked_count") or 0
    out = [
        f"- **진입 컷오프({cutoff.get('cutoff_time')})**: 차단 **{blocked}분** · "
        f"컷오프 이후 ENTER **{violated}건**(그중 강제 평탄화 {cutoff.get('forced_flat_time')} 이후 "
        f"**{after_flat}건**)",
    ]
    if violated:
        out.append(
            f"> ⛔ **불변식 위반** — v6 §4.2는 {cutoff.get('cutoff_time')} 이후 신규 진입을 금지한다. "
            "이 값이 0이 아니면 게이트가 빠진 것이다(2026-08-06 실측 21건 / 평탄화 시각 이후 "
            "19건 — 경계는 **이상**이라 15:10 정각을 포함한다. 보고서 §2-2가 적은 18건은 초과로 "
            "센 값이고, `session.is_after_entry_cutoff`의 경계 규약이 이쪽이다)."
        )
    else:
        out.append(
            "> 0이 정상이다. **진입 후보가 없어서 0인 것과 게이트가 막아서 0인 것은 다르다** — "
            "앞의 「차단 N분」이 그 구분이다(N이 0이면 그 시간대에 애초에 진입 신호가 없었다)."
        )
    out.append("")
    return out


def _render_decision_outcomes(db: dict) -> list[str]:
    """2026-08-06 고도화#5 — **진입 판단이 옳았는가.** ADVISORY 기준선이다.

    08-05 `p1`이 팔레트를 연 뒤 ENTER가 0 → 62건이 됐는데, 그 62건을 재는 축이 하나도 없었다.
    실거래 전환일에 "이전보다 나아졌는가"를 물으려면 그 전의 기준선이 있어야 한다.
    """
    outcomes = db.get("decision_outcomes") or {}
    if not outcomes.get("available"):
        return [
            f"> 진입 사후 평가는 ENTER가 있는 날부터 나온다({outcomes.get('reason', '미계산')}).",
            "",
        ]
    rows = [
        [horizon, f"{s['sample']:,}", f"{s['hits']:,}", _fmt(s["hit_pct"], "{:.1f}%"),
         _fmt(s.get("abs_move_pct"), "{:.3f}%")]
        for horizon, s in (outcomes.get("horizons") or {}).items()
    ]
    out = [f"- 진입 판단 **{outcomes['entries']:,}건**의 사후 평가", ""]
    out += _table(["지평", "표본", "적중", "적중률(방향성)", "|이동폭|(방향 무관)"], rows)
    out += [
        "> **전략 종류에 맞는 열을 읽는다**(2026-08-11 고도화 C). `적중률`은 "
        "`direction x 이동`의 부호라 **방향성 전략을 전제**한다. 08-11에 허용된 전략은 "
        "`straddle_accumulate`(방향 중립)였고, 스트래들은 어느 쪽으로든 크게 움직이면 이긴다 — "
        "그날 적중률 열은 **틀린 질문에 답했다.** 변동성 전략은 `|이동폭|`으로 읽는다.",
        "> `|이동폭|`에 **임계를 걸지 않는다** — 정상 분포를 모르고, 모르는 채 임계를 정하면 "
        "그 임계가 곧 결론이 된다(08-05 스팟 괴리율에서 한 실수). 진입 스팟 대비 비율이라 "
        "지수 수준이 달라도 비교할 수 있다.",
        "> **표본 수를 반드시 함께 읽는다** — 진입 3건인 날의 100%는 아무 뜻이 없다. 지평이 길수록 "
        "표본이 주는 것은 정상이다(장 마감을 넘긴 지평은 구조적으로 빈다).",
        "> **무변동은 적중도 실패도 아니라 분모에서 빠진다.** 0을 실패로 세면 조용한 장에서 "
        "적중률이 구조적으로 낮아지고, 성공으로 세면 반대가 된다.",
        "> 이 값으로 **가중치를 바꾸지 않는다** — 평가이지 되먹임이 아니다(v6 §11.3 Thompson "
        "Sampling은 Phase 3). 며칠 쌓고 사람이 「무엇을 성과로 볼 것인가」부터 정한다.",
        "",
    ]
    out += _render_outcome_control(outcomes.get("control") or {})
    return out


def _render_outcome_control(control: dict) -> list[str]:
    """2026-08-07 고도화#C — **거른 판단은 어땠는가.** 대조군 없이는 위 적중률을 못 읽는다.

    그날 시장이 한 방향으로 흘렀으면 아무 방향이나 찍어도 50%대가 나온다. "진입 신호가
    무작위보다 나은가"의 답은 **같은 시각에 우리가 거른 판단**과 비교해야 나온다.
    """
    if not control.get("available"):
        return [f"> REJECT 대조군 없음({control.get('reason', '사유 미상')}).", ""]
    matched = control.get("time_matched") or {}
    enter, reject = matched.get("enter") or {}, matched.get("reject") or {}
    shared = control.get("shared_hours") or []
    out = [
        f"- **REJECT 대조군** {control.get('rejects', 0):,}건 — 같은 규칙·같은 지평으로 매긴 값 "
        "(적재하지 않는다. 읽을 때 만든다)",
        "",
    ]
    out += _table(
        ["지평", "ENTER 적중률", "표본", "REJECT 적중률", "표본", "차이(ENTER−REJECT)"],
        [
            [
                horizon,
                _fmt((enter.get(horizon) or {}).get("hit_pct"), "{:.1f}%"),
                f"{(enter.get(horizon) or {}).get('sample', 0):,}",
                _fmt((reject.get(horizon) or {}).get("hit_pct"), "{:.1f}%"),
                f"{(reject.get(horizon) or {}).get('sample', 0):,}",
                _fmt(_pt_delta(enter.get(horizon), reject.get(horizon)), "{:+.1f}pt"),
            ]
            for horizon in (enter or reject)
        ],
    )
    out += [
        f"> **시간대를 맞춘 비교다** — 두 그룹이 모두 표본을 가진 시(hour)로만 제한했다"
        f"({', '.join(shared) or '없음'}시). 08-07 실측에서 두 그룹의 분포가 심하게 달랐다"
        "(ENTER는 12~13시 집중, REJECT는 08시·15시 집중) — 그대로 비교하면 **신호 품질이 아니라 "
        "시간대를 재게 된다.**",
        "> **차이가 음수면 우리가 거른 판단이 더 잘 맞혔다는 뜻이다.** 하루치로 결론 내지 않는다 — "
        "표본이 100건 안팎이고, 방향이 0인 판단은 양쪽 모두 분모에서 빠진다. 며칠 쌓아 부호가 "
        "고착되는지부터 본다.",
        "> 이 값도 **되먹임이 아니다**(위 주석과 같다).",
        "",
    ]
    return out


def _render_outcomes_by_chain_input(db: dict) -> list[str]:
    """2026-08-12 고도화 5 — **Fix#10(위상 레버)을 켤 가치 자체를 이 표가 판정한다.**

    08-12에 정규장 판단의 96%가 stale이었다는 발견이 「위상을 옮기자」로 이어졌는데,
    **늙은 체인을 본 판단이 실제로 못 맞혔는지는 아무도 재지 않았다.** 차이가 없다면 그 레버의
    가치는 우리가 생각한 것보다 작다.
    """
    split = dig(db, "decision_outcomes.by_chain_input") or {}
    if not split.get("available"):
        return [f"- 집계 없음 — {split.get('reason', 'chain_input_source 미기록')}", ""]

    sources = split.get("sources") or {}
    horizons = sorted(
        {h for s in sources.values() for h in (s.get("horizons") or {})},
        key=lambda h: int(h.rstrip("m")),
    )
    # ⚠ 헤더에 `|이동폭|` 같은 **파이프 문자를 쓰지 않는다** — 마크다운 표의 열 구분자와
    # 충돌해 헤더 셀 수가 구분선과 어긋난다(도입 당일 실제로 그렇게 깨졌다).
    headers = ["체인 입력", "진입"] + [f"{h} 적중률(표본)" for h in horizons] + [
        f"{h} 이동폭(절대)" for h in horizons
    ]
    rows = []
    for source in sorted(sources):
        info = sources[source]
        hz = info.get("horizons") or {}
        row = [f"`{source}`", str(info.get("entries", 0))]
        for h in horizons:
            cell = hz.get(h) or {}
            pct, sample = cell.get("hit_pct"), cell.get("sample", 0)
            row.append(f"{pct:.1f}% ({sample})" if pct is not None else f"— ({sample})")
        for h in horizons:
            move = (hz.get(h) or {}).get("abs_move_pct")
            row.append(f"{move:.3f}%" if move is not None else "—")
        rows.append(row)
    out = _table(headers, rows)
    out += [
        "> **읽는 법**: `current`는 그 분 체인을 본 판단, `stale`은 직전 분 이상의 스냅샷을 본 "
        "판단이다. **stale이 current보다 낮지 않다면 위상 레버(Fix#10)의 가치는 우리가 생각한 "
        "것보다 작다** — 신선도가 「보기 좋은 값」이었지 성과의 원인은 아니었다는 뜻이다.",
        "> ⚠ **하루로 결론 내지 않는다. 임계도 안 건다.** 08-12 표본은 current 11건 / stale 258건으로 "
        "**한쪽이 20배 크고**, 두 그룹의 시간대 분포도 다를 수 있다(current는 폴링이 빨랐던 분 = "
        "그날 KIS가 한가했던 분에 몰린다). `_reject_control_group`이 08-07에 겪은 것과 **같은 교란**이다.",
        "> ⚠ **표본 수를 반드시 함께 읽는다** — 괄호 안이 그것이다. 10건짜리 적중률로 레버를 켜지 말 것.",
        "",
    ]
    return out


def _pt_delta(a: dict | None, b: dict | None) -> float | None:
    """두 적중률의 퍼센트포인트 차. 한쪽이라도 표본이 없으면 None(0으로 만들지 않는다)."""
    return _pct_points((a or {}).get("hit_pct"), (b or {}).get("hit_pct"))


def _pct_points(today: float | None, previous: float | None) -> float | None:
    """퍼센트포인트 차. **한쪽이 없으면 None** — 없는 것을 0(변화 없음)으로 만들지 않는다."""
    return None if today is None or previous is None else round(today - previous, 1)


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
                # 2026-08-05 §2-5 — 산출률 위/아래에 나란히 둔다. 산출률만 보면 "올라갔으니
                # 좋아졌다"로 읽히는데, 08-05의 4.5%는 22건 중 21건이 여기 걸리는 값이었다.
                "그중 수집 행사가 범위 밖",
                (
                    "검사 불가(마이그레이션 023 미적용?)"
                    if reach.get("gamma_flip_out_of_range_count") is None
                    else f"{reach['gamma_flip_out_of_range_count']:,}건"
                ),
                "> 0 (불변식 — 0이어야 한다)",
            ],
            [
                "체인 스냅샷 레그 수",
                f"중앙 {_fmt(reach.get('chain_leg_median'), '{:.0f}')} / "
                f"최대 {_fmt(reach.get('chain_leg_max'), '{:,.0f}')}",
                f"설계 {db_metrics_module.MONTHLY_LEGS_PER_CYCLE_DESIGN}레그(먼슬리 ATM±N x C/P)",
            ],
            # 2026-08-07 고도화#2 — 위 줄의 "좀 두껍다"를 **분 단위로 세어** 불변식으로 만든다.
            # 08-07 이전에는 이 값이 매일 100분대였는데 레그 수 중앙/최대만 봐서는 안 보였다.
            [
                "└ 그중 설계 초과 분(ATM 롤 잔상)",
                f"{_fmt(reach.get('chain_leg_over_design_minutes'), '{:,.0f}분')} "
                f"(최대 +{_fmt(reach.get('chain_leg_excess_max'), '{:,.0f}')}레그)",
                "> 0 (불변식 — 08-07 Fix#1 이후 0이어야 한다)",
            ],
            [
                "체인 스냅샷 최고령 레그",
                f"중앙 {_fmt(_minutes(reach.get('chain_age_seconds_median')), '{:.1f}분')} / "
                f"최대 {_fmt(_minutes(reach.get('chain_age_seconds_max')), '{:.1f}분')}",
                f"> {db_metrics_module.SIGNAL_REACH_WARNINGS['chain_age_seconds_max'] / 60:.0f}분",
            ],
            # 2026-08-10 — 위 줄들은 전부 "얼마나 두껍고 신선한가"를 잰다. **아무것도 없었던
            # 분**은 그 축 어디에도 안 나타난다(레그 0은 중앙값에 묻히고 최고령은 NULL이다).
            # 08-10 15:15의 옵션체인 전멸이 정확히 그랬다 — §14는 그날 아무 경고도 안 냈다.
            [
                "GEX 입력이 없던 분",
                _fmt(reach.get("gex_input_missing_minutes"), "{:,.0f}분"),
                "> 0 (불변식 — 그 분의 판단은 감마 지형을 못 봤다)",
            ],
        ],
    )
    # 2026-08-11 고도화 B — 판단이 **그 분** 체인을 봤는가(마이그레이션 029).
    source = reach.get("chain_input_source") or {}
    if source.get("available"):
        counts = source["counts"]
        parts = " / ".join(f"{k} {v:,}분" for k, v in sorted(counts.items()))
        stale_pct = source.get("stale_pct")
        out += [
            "",
            f"- **체인 입력 출처**: {parts} — 그중 직전 분 이상(stale) **{_fmt(stale_pct, '{:.1f}%')}**",
            "> 08-11에 하루의 절반이 stale이었다(짝수분 20레그 폴링 19.3초 > 판단 위상 :10초). "
            "`chain_age_seconds_max` 하나로는 «느려졌다»만 알 수 있고 «몇 분이 늙은 값을 봤는가»는 "
            "못 센다 — 그것이 이 줄이 생긴 이유다. 위상 레버는 `SIGNAL_FUSION_PHASE_OFFSET_SECONDS`.",
        ]
    elif source:
        out += ["", f"- 체인 입력 출처: 집계 전 — {source.get('reason', '')}"]
    for warning in reach.get("warnings") or []:
        out.append(f"- ⚠ {warning}")
    # 2026-08-06 Fix#5 — 장전 표본만 있는 구간의 판정 유예. 경고가 아니지만 **보이기는 한다**.
    for note in reach.get("notes") or []:
        out.append(f"- ℹ {note}")
    if not reach.get("warnings") and not reach.get("notes"):
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
    slack = db.get("slack_alerts") or {}
    if slack.get("available"):
        enabled = slack.get("enabled")
        label = {True: "**켜짐**", False: "**꺼짐**", None: "미설정(env 기본값)"}[enabled]
        out += [
            f"- **Slack 경보 토글**: {label} ({slack.get('source')})",
            "",
        ]
        if enabled is False:
            out += [
                "> ⚠ **경보가 꺼져 있었다.** 그날 로그에 `slack`·`경보` 문구가 0건인 것은 "
                "「울릴 조건이 없었다」가 아니라 「울릴 수 없었다」다 — 둘을 가르는 것이 이 줄이다"
                "(08-14 §3-3이 그 구분 없이 전자로 진단했다).",
                "",
            ]
    rl = db.get("rate_limiter") or {}
    out += [
        f"- `rate_limiter_status_history` {rl.get('rows', 0):,}행 / 밀림 **{rl.get('overrun_rows', 0)}건** / "
        f"최대 배율 {_fmt(rl.get('max_multiplier'), '{:.2f}')}배",
        "",
    ]
    out += _render_spot_divergence(db)
    return out


def _render_positions(db: dict) -> list[str]:
    """2026-08-16 (Block B) — 보유 포지션(마이그레이션 030 `position_snapshots`).

    **개시일(08-18)에 가장 먼저 읽을 절이다.** 임계를 걸지 않는다 — 첫 포지션이 그날 생기므로
    정상 분포를 아직 모른다(§16 괴리율에서 배운 것: 정상 범위를 모르는 채 임계를 먼저 정하면
    그 임계가 곧 결론이 된다).
    """
    pos = db.get("positions") or {}
    if not pos.get("available"):
        return [f"> 포지션 집계 불가 — {pos.get('reason', '사유 미상')}.", ""]

    if pos.get("rows", 0) == 0:
        return [
            "- **그날 보유 포지션 기록이 없다**(행 0).",
            "",
            "> **이것은 두 가지다**(규약 C): 체결이 없어서 포지션이 없었던 것과, 적재 경로가 "
            "죽어서 못 남긴 것. 가르는 방법은 `execution_logs`를 함께 보는 것이다 — 주문이 "
            "0건이면 전자다. ADVISORY/CONFIRM 미통과 상태에서는 전자가 정상이다.",
            "",
        ]

    dist = pos.get("side_distribution") or {}
    out = [
        f"- 스냅샷 **{pos['snapshot_minutes']}분** / 종목 **{pos['symbols']}개** / "
        f"동시 보유 최대 **{pos['max_concurrent_symbols']}개**(동일방향 한도 3과 나란히 읽는다)",
        f"- 방향 분포: {', '.join(f'`{k}` {v}' for k, v in sorted(dist.items())) or '—'}",
        "",
    ]

    unknown = pos.get("unknown_side_count_max", 0)
    if unknown:
        out += [
            f"> ⚠ **방향 판정 실패 최대 {unknown}건.** 그날 `same_direction_buy_count`/"
            "`sell_count`는 실제 포지션 수를 **밑돈다**(동일방향 한도 판정은 보수적으로 "
            "부풀려져 있었다 — 미인식분을 후보 방향에 더하므로).",
            "> **`observation_loop.log`의 `잔고 방향 판정 실패` 줄에 KIS 원본 값이 있다** — "
            "그 값이 `account_tracker._BUY_SIDE_TOKENS`를 실측 기준으로 좁힐 근거이고, "
            "R8 범위표(`docs/dev_memory/KIS_RAW_FIELD_RANGES.md`)에 적을 대상이다.",
            "",
        ]
    elif "UNKNOWN" in dist:
        out += [
            "> ⚠ `side_distribution`에 `UNKNOWN`이 있는데 `unknown_side_count_max`는 0이다 — "
            "두 축이 갈렸다(잔고 스냅샷과 포지션 스냅샷이 다른 사이클을 보고 있을 수 있다).",
            "",
        ]
    else:
        out += [
            "> 방향 판정 실패 0건 — **KIS가 보낸 방향값을 전부 알아봤다.** 그 값이 무엇이었는지는 "
            "`position_snapshots.raw`에 남아 있다(R8 실측 확정의 원재료).",
            "",
        ]
    return out


def _render_spot_divergence(db: dict) -> list[str]:
    """2026-08-05 §2-3 / Fix#6 — 스팟의 두 독립 소스를 대조한 결과."""
    sd = db.get("spot_source_divergence") or {}
    if not sd.get("available"):
        return [f"> 스팟 소스 대조 불가 — {sd.get('reason', '사유 미상')}.", ""]
    out = [
        f"- **스팟 소스 괴리**(지수 REST vs 선물 WS `{sd['futures_symbol']}`, {sd['minutes']:,}분 공통): "
        f"중앙 **{_fmt(sd.get('median_pct'), '{:.3f}')}%** / 최대 **{_fmt(sd.get('max_pct'), '{:.3f}')}%**",
        f"- **지수 정지**(직전 분과 같은 값인데 선물은 움직인 분): 총 **{sd['index_frozen_minutes']}분** / "
        f"최장 연속 **{sd['index_frozen_max_run']}분**",
        "",
        "> **괴리율에는 임계를 걸지 않는다** — 선물 베이시스는 실재하는 경제량이라 정규장 중 "
        "0.2~0.9%는 정상이다(08-05 중앙 0.252%). 보고서가 처음 적었던 '0.5% 2분 연속' 규칙은 "
        "그날 09:01·09:02·09:22·10:32에 오경보를 냈을 것이다. **정상 범위를 모르는 상태에서 임계를 "
        "먼저 정하면 그 임계가 곧 결론이 된다** — 며칠 쌓아 사람이 정한다.",
        "> 판정은 **지수 정지**로 한다. 08-05에 지수가 전일 종가 1000.03에 75분간 얼어붙은 채 "
        "선물은 1048까지 가 있었고(4.8% 괴리, 15분 공통), 신호 층은 그 얼어붙은 값으로 GEX를 "
        "계산했다. **두 소스가 어긋난 채 갔는데 아무도 비교하지 않았다.**",
        "",
    ]
    return out


def _render_crosschecks(metrics: dict, db_metrics: dict | None) -> list[str]:
    """2026-08-05 고도화#3 — **이미 있는 지표끼리 서로 맞춰본다.**

    08-05 리포트는 §14(감마플립 22분)와 §15(광폭 flip 없음)를 같은 문서에 인쇄해 놓고 서로
    비교하지 않았다. 둘은 논리적으로 모순이고, 그 모순이 곧 §2-5의 결함이었다 —
    사람이 22건을 전수 대조하기 전까지 아무도 몰랐다.
    """
    findings = crosscheck.evaluate(metrics, db_metrics)
    if not findings:
        return [
            "- 모순 없음 — 오늘은 아래 규칙에서 지표끼리 어긋나지 않았다.",
            "",
            "> **「모순 없음」은 「전부 정상」이 아니다.** 이 절은 절과 절 사이만 본다 — "
            "각 절의 개별 경고는 그대로 읽어야 한다.",
            "",
        ]
    out = [f"- ⚠ **모순 {len(findings)}건**", ""]
    for f in findings:
        out += [f"### §{' ↔ §'.join(f.sections)} — {f.summary}", "", f.detail, ""]
    out += [
        "> 이 절은 **모순을 지적할 뿐 판정하지 않는다** — 어느 쪽이 틀렸는지는 사람이 정한다.",
        "",
    ]
    return out


_CAMPAIGN_ICONS = {
    "표본 미달": "⏳",
    "합격": "✅",
    "불합격": "❌",
    "관측": "👁",
    "선행 대기": "⏸",
    "경로 없음": "🚫",
    "스키마 오류": "🚫",
}


def _render_campaign(rows: list[dict]) -> list[str]:
    """
    입력: `campaign.evaluate()` 결과.
    계산: 채널별 판정·진행률·핵심 수치를 한 표로 낸다.
    해석: **`표본 미달`은 실패가 아니다.** 그 행의 값은 「아직 모른다」이고, 진행률이 그것을
         숫자로 말한다 — 이 절이 존재하는 이유가 그 구분이다(`hypotheses`는 「아직 모른다」와
         「틀렸다」를 둘 다 `inconclusive`로 뭉갠다).
         📌가 붙은 채널은 **주간회의에서 확정된 결정이 있다** — 판정과 결정은 별개이므로,
         판정이 매일 바뀌어도 그 결정은 남는다.
    실패 조건: 없음 — 빈 목록이면 안내 한 줄.
    """
    if not rows:
        return ["열린 채널이 없다."]
    out = [
        "> 판정은 **매일 재계산**된다 — 「불합격이 계속 뜬다」가 「미조치」를 뜻하지 않는다.",
        "> 📌는 확정된 결정이 있다는 뜻이고, 판정(verdict)과 결정(decision)은 별개다.",
        "",
        "| 채널 | 판정 | 진행 | 핵심 수치 |",
        "|---|---|---|---|",
    ]
    for row in rows:
        icon = _CAMPAIGN_ICONS.get(row.get("verdict", ""), "")
        detail = row.get("detail") or "-"
        if row.get("decision"):
            detail += f"  📌 **{row['decision']}** ({row.get('decision_date', '?')})"
        out.append(
            f"| {row.get('id', '?')} | {icon} {row.get('verdict', '?')} "
            f"| {row.get('progress') or '-'} | {detail} |"
        )
    unresolved = [r for r in rows if r.get("problems")]
    if unresolved:
        out += ["", "⚠ **스키마 오류가 있는 채널은 판정하지 않는다** — 고치기 전까지 표본도 안 쌓인다."]
    return out


def _render_hypotheses(results: list[dict]) -> list[str]:
    out: list[str] = []
    # 2026-08-03 §5-4 — 예정일이 지났는데 아직 `상태: pending`인 항목을 **표 위로** 띄운다.
    # 규약상 `상태`는 사람이 손으로 확정해야 하는데, 확정 안 된 것이 표에 섞여 들어가면 놓치기
    # 쉽고 그렇게 쌓이면 "예측 → 실측 검정" 규약 자체가 무력해진다.
    # 2026-08-11 Fix#8 — **경과일 내림차순**으로 낸다. 08-11에 이 목록이 23건이었고 id 알파벳
    # 순이라 4일 지난 것과 오래 묵은 것이 섞여 보였다. 목록이 길어지면 사람은 통째로 소음
    # 취급하고, 그러면 규약 F/G가 막으려는 것과 같은 실패("진짜가 소음에 묻힌다")가 난다.
    overdue = sorted(
        {(r["id"], r.get("검증예정일"), r.get("overdue_days", 0), bool(r.get("stale")))
         for r in results if r.get("overdue")},
        key=lambda t: (-t[2], t[0]),
    )
    if overdue:
        stale_count = sum(1 for *_x, is_stale in overdue if is_stale)
        header = (
            f"> ⚠ **확정 대기 {len(overdue)}건** — 검증예정일이 지났는데 `hypotheses.yaml`의 "
            "`상태`가 아직 `pending`이다. 오늘 보고서를 쓰면서 손으로 확정할 것"
        )
        if stale_count:
            header += (
                f" — 그중 **{stale_count}건은 {hypotheses_module.STALE_PENDING_DAYS}일을 넘겼다**. "
                "그 나이면 그 사이 코드가 여러 번 바뀌어 귀속이 안 갈리므로 `inconclusive`로 닫는다"
            )
        out += [header + ":", ""]
        out += [
            f"> - `{hid}` (예정일 {due or '미지정'}"
            + (f", **{days}일 경과**" if days else "")
            + (" · ⏳ 강등 대상" if is_stale else "")
            + ")"
            for hid, due, days, is_stale in overdue
        ]
        out.append("")

    # 2026-08-05 고도화#2(규약 E) — **자기 주장을 검정하지 않는 지표로 받은 합격**을 막는다.
    # 08-04 `p4`는 "왕복이 사라진다"고 주장하면서 등록 지표가 `chain_age_seconds_max`와
    # `log_volume.human_lines`였고, 왕복률이 36.1% → 47.5%로 나빠졌는데도 "확인"이 났다.
    unjudgeable = sorted({r["id"] for r in results if r.get("claim_missing")})
    if unjudgeable:
        out += [
            f"> ⚠ **주장 지표 없음 {len(unjudgeable)}건 — 판정 불가.** 등록된 지표 중 "
            "`역할: 주장`(그 가설이 실제로 주장하는 것을 재는 지표)이 하나도 없다. "
            "실측은 사실이지만 **그것으로 가설을 판정하면 08-04 `p4`의 오독이 반복된다.**",
            "",
        ]
        out += [f"> - `{hid}`" for hid in unjudgeable]
        out.append("")

    # 2026-08-06 §3-1 / Fix#3 — **경로가 존재하지 않는 지표.** 표 안에서는 "실측 없음" 한 줄과
    # 구별되지 않아, 08-05 예측 13건 중 6건이 주장 지표를 통째로 잃은 채 하루를 갔다. 그중
    # `p1`의 대가 지표는 12배 초과(ENTER 예측 ≤5에 62건)였고 아무도 자동으로 알아채지 못했다.
    # **가장 위로 올린다** — 이것은 "오늘 값이 없다"가 아니라 "yaml이 틀렸다"이고, 고치기 전까지
    # 그 가설은 영원히 검정되지 않는다.
    dead = sorted({(r["id"], r["metric"], r.get("역할", "")) for r in results if r.get("path_dead")})
    if dead:
        out += [
            f"> ⛔ **경로 없음 {len(dead)}건 — 이 지표는 오늘이 아니라 *영원히* 안 나온다.** "
            "자동 집계에 그런 경로가 없다(오타이거나 구조가 바뀐 것). **`hypotheses.yaml`을 "
            "고치기 전까지 그 가설은 검정되지 않는다** — 08-06에 이 구분이 없어 6건이 "
            "「실측 없음」에 묻혔다.",
            "",
        ]
        out += [
            f"> - `{hid}` — `{metric}`" + (f" (역할: {role})" if role else "")
            for hid, metric, role in dead
        ]
        out.append("")

    # 2026-08-12 규약 H — **레버가 꺼진 채 판정될 뻔한 항목.** 08-12에 `2026-08-11-eF`가
    # 그랬다: 레버가 안 켜졌는데 표는 「HIGH_CONVICTION 34 → 91」을 반증처럼 보여줬다.
    lever_off = sorted({
        (r["id"], ", ".join(r.get("lever_off") or [])) for r in results if r.get("lever_off")
    })
    if lever_off:
        out += [
            f"> ⚠ **미실행 {len(lever_off)}건 — 전제 레버가 꺼진 채로 하루가 갔다.** 실측은 "
            "사실이지만 **그 값은 이 가설과 무관하다**(그 코드가 안 돌았다). 오늘 판정하지 말고 "
            "레버를 켠 다음 영업일로 미룬다 — 규약 H(§0의 레버 표).",
            "",
        ]
        out += [f"> - `{hid}` — 꺼진 레버: `{keys}`" for hid, keys in lever_off]
        out.append("")

    lever_unknown = sorted({
        (r["id"], ", ".join(r.get("lever_unknown") or [])) for r in results if r.get("lever_unknown")
    })
    if lever_unknown:
        out += [
            f"> ⛔ **전제 레버를 못 읽음 {len(lever_unknown)}건.** `전제레버`에 적힌 이름이 "
            "`mahdi/ops/levers.py`의 목록에 없다(오타이거나 아직 등록 안 된 레버). "
            "**「미실행」으로 닫지 않았다** — 그것은 「꺼져 있었다」가 아니라 「모른다」이고, "
            "오타를 미실행으로 덮으면 영영 안 고쳐진다(08-06의 「경로 없음」과 같은 구분).",
            "",
        ]
        out += [f"> - `{hid}` — 모르는 레버: `{keys}`" for hid, keys in lever_unknown]
        out.append("")

    cost_missing = sorted({(r["id"], r.get("대가")) for r in results if r.get("cost_missing")})
    if cost_missing:
        out += [
            f"> ⚠ **대가 지표 없음 {len(cost_missing)}건.** 항목이 `대가:`로 트레이드오프를 "
            "선언해 놓고 그것을 재는 지표(`역할: 대가`)가 없다 — **무엇을 포기했는지 모르는 채 "
            "개선을 주장하는 상태**다(08-04 Fix#8이 그랬다).",
            "",
        ]
        out += [f"> - `{hid}` — 선언된 대가: {cost or '문구 없음'}" for hid, cost in cost_missing]
        out.append("")

    rows = [
        [
            r["id"], r["가설"], r["metric"], _fmt(r.get("actual"), "{}"), r["expect"],
            r.get("역할", hypotheses_module.ROLE_REFERENCE), r["verdict"],
        ]
        for r in results
    ]
    out += _table(["id", "가설", "지표", "실측", "예측", "역할", "판정"], rows)
    out += [
        "> 판정은 참고값이다 — **`hypotheses.yaml`의 `상태`는 자동으로 바뀌지 않는다.** "
        "사람이 보고서를 쓰면서 손으로 확정한다(자동 판정이 틀렸을 때 조용히 덮이는 것을 막는다).",
        "> **역할**: `주장`은 그 가설이 실제로 주장하는 것, `대가`는 그 fix가 포기한 것, "
        "나머지는 `참고`다(2026-08-05 고도화#2 / 규약 E).",
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
