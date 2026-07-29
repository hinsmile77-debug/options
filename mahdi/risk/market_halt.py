"""거래소 서킷브레이커(CB)/거래정지 실시간 감지 (KIS WS H0UNMKO0, docs/efriend 문서 "국내주식
장운영정보 (통합)" 시트 실측).

이름이 비슷한 `mahdi/risk/circuit_breaker.py`의 `CircuitBreaker`와는 **완전히 다른 개념**이다 —
그쪽은 daily_loss/drawdown/vpin 등 마흐디 자체 판단으로 발동하는 내부 리스크 킬스위치이고, 이
모듈은 KRX가 실제로 시장 전체 매매를 정지시켰는지(서킷브레이커/시장임시정지/사이드카)를 그대로
전달만 한다. `CircuitBreaker`는 한 번 HALTED면 `reset_daily()` 전까지 그날 안엔 자동 복귀하지
않는 래치 설계인데, 거래소 CB는 KRX가 해제 이벤트를 보내는 즉시 실시간으로 풀려야 하므로 같은
래치를 적용하면 안 된다 — 그래서 별도 모듈로 분리했다(래치 없음, 최신 이벤트만 반영).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# MKOP_CLS_CODE(장운영 구분 코드) — docs/efriend 문서 "장운영구분코드" 표 실측(2026-07-29).
# 여기 없는 코드(장개시/장마감/시간외단일가 등 정상 세션 전환)는 거래정지와 무관하므로 다루지 않는다.
MKOP_CLS_LABELS: dict[str, str] = {
    "164": "시장임시정지",
    "174": "서킷브레이크 발동",
    "175": "서킷브레이크 해제",
    "182": "서킷브레이크 장중동시마감",
    "184": "서킷브레이크 발동(NXT)",
    "185": "서킷브레이크 해제(NXT)",
    "387": "사이드카 매도발동",
    "388": "사이드카 매도발동해제",
    "397": "사이드카 매수발동",
    "398": "사이드카 매수발동해제",
}

# 신규 진입을 차단해야 하는 코드 — 서킷브레이커(KRX/NXT 양쪽)와 시장임시정지. 실제 매매 자체가
# 정지되므로 이 상태에서는 주문을 내도 거부된다.
HALT_TRIGGER_CODES: frozenset[str] = frozenset({"164", "174", "182", "184"})
# 정상 재개 코드 — 수신 즉시 차단을 해제한다(래치 없음).
HALT_CLEAR_CODES: frozenset[str] = frozenset({"175", "185"})
# 사이드카는 프로그램매매 호가 효력만 5분간 정지시킬 뿐 시장 자체는 정지되지 않는다 — 신규 진입
# 차단 사유가 아니다(정보성 알림만).
SIDECAR_CODES: frozenset[str] = frozenset({"387", "388", "397", "398"})


@dataclass(frozen=True, slots=True)
class MarketOperationStatus:
    """H0UNMKO0 Response Body 그대로 — Layout 순서: TRHT_YN, TR_SUSP_REAS_CNTT, MKOP_CLS_CODE,
    ANTC_MKOP_CLS_CODE, MRKT_TRTM_CLS_CODE, DIVI_APP_CLS_CODE, ISCD_STAT_CLS_CODE, VI_CLS_CODE,
    OVTM_VI_CLS_CODE, EXCH_CLS_CODE(문서 실측, 2026-07-29)."""

    trht_yn: str
    tr_susp_reas_cntt: str
    mkop_cls_code: str
    vi_cls_code: str


@dataclass
class MarketHaltTransition:
    changed: bool
    is_halted: bool
    label: str


class MarketHaltMonitor:
    """일간 래치 없이 최신 MKOP_CLS_CODE만 반영하는 실시간 상태 머신."""

    def __init__(self) -> None:
        self._is_halted = False
        self._current_code: str | None = None
        self._label: str = "정상"
        self._halted_since: datetime | None = None

    @property
    def is_halted(self) -> bool:
        return self._is_halted

    @property
    def current_code(self) -> str | None:
        return self._current_code

    @property
    def label(self) -> str:
        return self._label

    @property
    def halted_since(self) -> datetime | None:
        return self._halted_since

    def update(self, status: MarketOperationStatus, now: datetime) -> MarketHaltTransition:
        """
        입력: 이번 WS 메시지에서 파싱한 상태, 수신 시각.
        계산: `HALT_TRIGGER_CODES`면 차단 진입(`halted_since`를 이번이 처음 진입하는 전이일 때만
             기록 — 174→182처럼 halted 상태에서 다른 halt 코드로 바뀌어도 최초 진입 시각을
             유지한다), `HALT_CLEAR_CODES`면 즉시 해제. 그 외 코드(사이드카·정상 세션 전환 등)는
             차단 여부에 영향을 주지 않고 라벨만 갱신하지 않는다(무시) — 다만 사이드카는 라벨
             자체는 알림 목적으로 호출측이 `MKOP_CLS_LABELS`에서 직접 조회해 쓸 수 있다.
        해석: 반환값 `changed`는 `is_halted`가 실제로 뒤바뀐 경우만 True — 호출측(main.py)이
             이 값으로 Slack 알림·DB 기록을 상태 전이 시점에만 트리거한다(매 메시지마다 알림
             스팸을 보내지 않기 위함, 기존 WS 재연결 알림과 동일한 절제 원칙).
        실패 조건: 미지정 코드(HALT_TRIGGER_CODES/HALT_CLEAR_CODES 어디에도 없음)는 상태를
                  그대로 유지한다 — 알 수 없는 코드로 섣불리 차단을 풀거나 걸지 않는다.
        """
        code = status.mkop_cls_code
        was_halted = self._is_halted

        if code in HALT_TRIGGER_CODES:
            self._is_halted = True
            self._current_code = code
            self._label = MKOP_CLS_LABELS.get(code, code)
            if not was_halted:
                self._halted_since = now
        elif code in HALT_CLEAR_CODES:
            self._is_halted = False
            self._current_code = code
            self._label = MKOP_CLS_LABELS.get(code, code)
            self._halted_since = None

        return MarketHaltTransition(
            changed=self._is_halted != was_halted, is_halted=self._is_halted, label=self._label
        )
