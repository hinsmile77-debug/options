"""선물옵션 실시간체결통보 — 복호화와 파싱 (v6 §13.2 "체결통보-REST 이중 확인"의 통보 쪽).

2026-08-16 (Block C). 이 파일이 생기기 전까지 마흐디는 **자기 주문이 체결됐다는 사실을 실시간으로
알 방법이 없었다** — 시세 WS만 붙어 있었고, 체결통보는 별개 도메인·별개 TR·**암호화된 스트림**이다.

## 시세 구독과 다른 점 네 가지 (전부 공식 문서 "선물옵션 실시간체결통보" 시트에서 확인)

1. **도메인이 다르다.** 모의 `ws://ops.koreainvestment.com:31000`, 실전 `:21000`.
   시세는 계좌 무관이라 항상 실전 도메인(`MARKET_DATA_WS_DOMAIN`)을 쓰지만, 체결통보는 계좌를
   특정하므로 모의/실전이 갈린다 → **두 번째 WS 연결이 필요하다.**
2. **`tr_key`가 종목코드가 아니다.** 응답 예시가 `"tr_key": "HTS ID"`를 돌려준다
   (같은 시트의 Request Body 표는 *"예:101S12"* 로 적혀 있어 **문서 두 곳이 어긋난다** —
   8/18 실측에서 확정한다. `KISSettings.kis_hts_id` 주석 참고).
3. **페이로드가 AES-256-CBC로 암호화돼 있다.** 구독 성공 응답의 `body.output`에
   `iv`(16바이트) / `key`(32바이트)가 실려 오고, 이후 데이터 프레임은
   `1|H0IFCNI0|001|<base64 암호문>` 형태다. 키는 **그 ACK에만** 온다 —
   놓치면 그 연결에서 오는 통보를 영구히 읽을 수 없다(`ws_client.SubscriptionAck.aes_iv` 주석).
4. **복호문이 위치 기반이다.** 이름 있는 JSON이 아니라 `|`로 구분된 순서 필드다.
   순서가 곧 스키마이므로 `_NOTICE_FIELDS`가 이 파일의 계약이다.

## 실측 상태 (R8 / 계명 11)

**아래 필드 순서는 공식 문서의 「복호화 후」 예시에서 옮긴 것이고 라이브 미실측이다.**
이 계좌는 주문을 낸 적이 없어 통보를 받아본 적이 없다. 그래서:

- `parse_notice()`는 필드 수가 기대와 달라도 **예외를 던지지 않는다** — 받은 것만 채우고
  `raw`에 원문을 통째로 남긴다. 8/18에 첫 통보가 오면 그 `raw` 한 줄이 실측의 근거가 된다.
- 필드 수가 다르면 **경고를 남긴다**(계명 12: 조용한 폴백 금지).

문서 예시의 매도매수구분 `[02]`가 매수 건이므로 **01=매도 / 02=매수**가 문서로 뒷받침된다
(`mahdi/execution/account_tracker.py`의 코드값 매핑과 같은 규약).
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from mahdi.broker import tr_codes
from mahdi.broker.ws_client import KISWebSocketClient
from mahdi.config.settings import KISSettings

logger = logging.getLogger(__name__)

# 복호문 필드 순서 — 공식 문서 "복호화 후" 예시 그대로. **순서가 스키마다.**
#
# 고객ID / 계좌번호 / 주문번호 / 원주문번호 / 매도매수구분 / 정정구분 / 주문종류 / 단축종목코드 /
# 체결수량 / 체결단가 / 체결시간 / 거부여부 / 체결여부 / 접수여부 / 지점번호 / 주문수량 /
# 계좌명 / 체결종목명 / 주문조건 / 주문그룹ID / 주문그룹SEQ / 주문가격
_NOTICE_FIELDS = (
    "customer_id", "account_no", "order_no", "original_order_no",
    "sell_buy_code", "modify_code", "order_kind", "symbol",
    "filled_qty", "filled_price", "filled_time", "rejected_flag",
    "filled_flag", "accepted_flag", "branch_no", "order_qty",
    "account_name", "filled_item_name", "order_condition",
    "order_group_id", "order_group_seq", "order_price",
)

# 거부여부/체결여부/접수여부는 문서 예시가 각각 [0]/[2]/[2]다. **의미는 미실측이므로 해석하지
# 않고 원값을 그대로 들고 다닌다** — "2가 체결완료인가"를 추측해 상태를 만들면, 그 추측이
# 틀린 날 주문 상태머신이 잘못 전이한다. 상태 판정의 권위는 REST 조회(`parse_fill_status`)에 둔다.
NOTICE_REJECTED_FLAG_FALSE = "0"


@dataclass(frozen=True, slots=True)
class OrderNotice:
    """체결통보 1건. 수치도 **문자열 그대로** 들고 있는다 — 실측 전에 형식을 단정하지 않는다."""

    symbol: str
    order_no: str
    sell_buy_code: str
    filled_qty: str
    filled_price: str
    filled_time: str
    rejected_flag: str
    filled_flag: str
    accepted_flag: str
    raw: str = field(default="", compare=False, repr=False)
    fields: dict[str, str] = field(default_factory=dict, compare=False, repr=False)

    @property
    def is_rejected(self) -> bool:
        """거부여부가 "0"이 아니면 거부로 본다 — **모르면 안전한 쪽**(거부로 읽는 쪽)이다."""
        return self.rejected_flag.strip() != NOTICE_REJECTED_FLAG_FALSE


def order_notice_ws_domain(settings: KISSettings) -> str:
    """체결통보 전용 WS 도메인 — 계좌를 특정하므로 모의/실전이 갈린다(시세와 다르다)."""
    return tr_codes.VPS_WS_DOMAIN if settings.is_mock else tr_codes.REAL_WS_DOMAIN


def order_notice_tr_id(settings: KISSettings) -> str:
    """체결통보 TR ID — 모의 H0IFCNI9 / 실전 H0IFCNI0."""
    return tr_codes.WS_TR_ORDER_NOTICE["vps" if settings.is_mock else "real"]


def subscription_tr_key(settings: KISSettings) -> str | None:
    """
    반환: 구독에 쓸 `tr_key`(= HTS ID). 설정이 비어 있으면 **None**.
    해석: None이면 호출측이 **구독을 건너뛰고 경고를 남긴다** — 예외를 던지지 않는 이유는
         체결통보가 없다고 관측 루프가 안 뜨면 안 되기 때문이고, 조용히 넘기지 않는 이유는
         「주문은 나가는데 체결 알림이 없는」 상태가 가장 위험하기 때문이다.
    """
    return settings.kis_hts_id.strip() or None


def decrypt_notice(payload: str, *, aes_key: str, aes_iv: str) -> str:
    """
    입력: 데이터 프레임의 **네 번째 필드**(base64 암호문), 구독 ACK에서 받은 key/iv.
    계산: AES-256-CBC 복호 + PKCS#7 언패딩 후 UTF-8 디코드.
    해석: KIS는 key 32바이트 / iv 16바이트를 **ASCII 문자열 그대로** 준다(예시가 그렇다) —
         hex나 base64가 아니므로 `.encode()`만 한다. 이것을 hex로 오해하면 길이가 반이 되어
         `ValueError: Invalid key size`로 즉시 드러난다(조용히 틀리지는 않는다).
    실패 조건: 키 길이가 맞지 않거나 암호문이 블록 크기의 배수가 아니면 예외를 전파한다 —
              복호에 실패한 통보를 조용히 버리면 체결을 놓친다. 호출측이 잡아 경보한다.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    cipher = Cipher(algorithms.AES(aes_key.encode("utf-8")), modes.CBC(aes_iv.encode("utf-8")))
    decryptor = cipher.decryptor()
    plain = decryptor.update(base64.b64decode(payload)) + decryptor.finalize()
    # PKCS#7 언패딩 — 마지막 바이트가 패딩 길이다. `cryptography`의 unpadder를 쓰지 않는 이유는
    # 이 한 줄이 의존성 표면을 줄이고, 잘못된 패딩을 예외 대신 원문으로 남길 수 있어서다.
    pad = plain[-1] if plain else 0
    if 1 <= pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
        plain = plain[:-pad]
    return plain.decode("utf-8", errors="replace")


def parse_notice(plaintext: str) -> OrderNotice:
    """
    입력: 복호화된 통보 문자열(`|` 구분 위치 기반).
    계산: `_NOTICE_FIELDS` 순서로 이름을 붙인다.
    해석: **필드 수가 기대와 달라도 실패하지 않는다.** 받은 만큼만 채우고 원문을 `raw`에
         보존한다 — 이 순서는 문서에서 옮긴 것이고 라이브 미실측이므로(파일 헤더 참고),
         8/18 첫 통보에서 어긋나면 그 `raw`가 정답을 알려줘야 한다. 다만 어긋난 사실 자체는
         **경고로 남긴다**(계명 12).
    실패 조건: 없음.
    """
    parts = plaintext.split("|")
    if len(parts) != len(_NOTICE_FIELDS):
        logger.warning(
            "체결통보 필드 수가 문서와 다르다 — 기대 %d개, 실제 %d개. "
            "위치 기반 파싱이므로 이름이 밀렸을 수 있다. 원문: %r",
            len(_NOTICE_FIELDS), len(parts), plaintext,
        )
    values = dict(zip(_NOTICE_FIELDS, parts))
    return OrderNotice(
        symbol=values.get("symbol", ""),
        order_no=values.get("order_no", ""),
        sell_buy_code=values.get("sell_buy_code", ""),
        filled_qty=values.get("filled_qty", ""),
        filled_price=values.get("filled_price", ""),
        filled_time=values.get("filled_time", ""),
        rejected_flag=values.get("rejected_flag", ""),
        filled_flag=values.get("filled_flag", ""),
        accepted_flag=values.get("accepted_flag", ""),
        raw=plaintext,
        fields=values,
    )


def parse_frame(raw: str, *, aes_key: str, aes_iv: str) -> OrderNotice | None:
    """
    입력: WS에서 받은 원문 한 줄, 복호화 키.
    계산: `1|H0IFCNI0|001|<암호문>` 형태에서 네 번째 필드를 떼어 복호·파싱한다.
    해석: 앞 세 필드는 암호화여부(1=암호화) / TR ID / 데이터 건수다. **건수가 2 이상일 수
         있다** — 그때 복호문에 여러 건이 이어 붙어 오는지는 미실측이라, 지금은 한 건으로
         파싱하고 필드 수 경고가 그것을 드러내게 둔다(첫 실측에서 확인할 항목).
    실패 조건: 형태가 다르면 **None**(제어 메시지나 PINGPONG일 수 있다). 복호 실패는
              예외를 전파한다 — 형태는 맞는데 못 읽은 것은 조용히 버릴 일이 아니다.
    """
    parts = raw.split("|", 3)
    if len(parts) < 4:
        return None
    return parse_notice(decrypt_notice(parts[3], aes_key=aes_key, aes_iv=aes_iv))


# ===== 2026-08-23 (실행 배선 ②) — 구독·수신 루프 =====
#
# 여기까지가 「무엇이 오는가」였고, 아래는 「그것을 어떻게 받는가」다. `KISWebSocketClient`가
# 연결 객체를 주입받는 것과 같은 이유로 이 클래스도 **연결을 주입받는다** — 실제 네트워크
# 없이 「ACK에서 키를 받고 → 프레임을 복호하고 → 못 읽으면 그 사실을 센다」 전 과정을
# 테스트가 재현할 수 있어야 한다.
#
# ## 이 스트림이 시세 WS와 다른 네 가지 (전부 파일 헤더에서 확인한 것)
#
#   1. **도메인이 다르다** — 계좌를 특정하므로 모의/실전이 갈린다.
#   2. **연결이 하나 더 필요하다** — 시세 소켓에 얹을 수 없다.
#   3. **키를 놓치면 그 연결은 영구히 못 읽는다** — 키는 구독 ACK에만 온다.
#   4. **`tr_key`가 무엇인지 문서 두 곳이 어긋난다**(HTS ID vs 종목코드). 실측으로 정한다.
#
# ## 왜 이 루프가 관측 루프를 죽이면 안 되는가
#
# 체결통보가 없어도 REST 조회(`get_order_fill_status`)로 체결을 확인할 수 있다 — v6 §13.2의
# 「이중 확인」에서 이쪽은 **빠른 축**이지 유일한 축이 아니다. 반대로 이 루프의 예외가 위로
# 전파되면 시세 수집·판단·원장까지 같이 죽는다. 그래서 끊김은 재연결로 흡수한다.
#
# 다만 조용히 죽는 것은 막는다: 「주문은 나가는데 체결 알림이 없는」 상태가 이 시스템에서 가장
# 위험한 상태이고, `subscription_tr_key()` 주석이 같은 말을 이미 하고 있다.

LOG_NOTICE_SUBSCRIBED = "체결통보 구독 성립: tr_id=%s tr_key=%s · 복호화 키 %s"
LOG_NOTICE_RECEIVED = (
    "체결통보 수신: 종목=%s 주문번호=%s 체결수량=%s 체결단가=%s 체결시각=%s · 필드 %d개%s"
)
LOG_NOTICE_NOT_CONFIGURED = (
    "체결통보를 구독하지 않는다 — KIS_HTS_ID가 비어 있다. 주문이 나가도 실시간 체결 알림이 "
    "없다(REST 조회로만 확인된다). .env에 KIS_HTS_ID를 넣고 재기동하면 켜진다"
)


@dataclass
class OrderNoticeStreamState:
    """스트림이 지금 어떤 상태인지 — 리포트·워치독이 읽는 값.

    **`subscribed`와 `cipher_ready`를 가른다.** 구독은 성립했는데 키를 못 받은 상태가 실제로
    가능하고(ACK에 `output`이 없는 경우), 그때는 프레임이 와도 한 건도 못 읽는다. 둘을 한
    불리언으로 합치면 그 상태가 화면에서 「정상」으로 보인다 — 08-21 §1-13이 겪은 형태다.
    """

    configured: bool = False
    subscribed: bool = False
    cipher_ready: bool = False
    notices: int = 0
    unreadable: int = 0
    last_notice_at: datetime | None = None
    last_error: str | None = None


class OrderNoticeStream:
    """체결통보 전용 WS 한 세션 — 구독 → 키 수신 → 프레임 복호 → 콜백.

    재연결 루프는 이 클래스 **밖**에 있다. 한 세션의 수명과 재연결 정책을 갈라 두면, 테스트가
    「한 세션 안에서 무슨 일이 일어나는가」를 재연결 없이 검사할 수 있다.
    """

    def __init__(
        self,
        settings: KISSettings,
        approval_key: str,
        connection,
        on_notice=None,
        state: OrderNoticeStreamState | None = None,
    ) -> None:
        self._settings = settings
        self._approval_key = approval_key
        self._conn = connection
        self._on_notice = on_notice
        self.state = state if state is not None else OrderNoticeStreamState()
        self._aes_key: str | None = None
        self._aes_iv: str | None = None

    def _subscribe_envelope(self, tr_key: str) -> str:
        return json.dumps(
            {
                "header": {
                    "approval_key": self._approval_key,
                    "custtype": "P",
                    "tr_type": "1",
                    "content-type": "utf-8",
                },
                "body": {"input": {"tr_id": order_notice_tr_id(self._settings), "tr_key": tr_key}},
            }
        )

    async def subscribe(self) -> bool:
        """
        계산: HTS ID로 체결통보를 구독 요청한다.
        반환: 요청을 보냈으면 True. 설정이 비어 있으면 **False + 경고 한 줄**(예외 아님).
        해석: 여기서 예외를 던지면 체결통보 설정 하나가 관측 루프 전체를 못 뜨게 만든다 —
             그것이 `subscription_tr_key()`가 None을 돌려주도록 설계된 이유다.
        실패 조건: 없다(전송 실패는 호출측 재연결이 받는다).
        """
        tr_key = subscription_tr_key(self._settings)
        if tr_key is None:
            self.state.configured = False
            logger.warning(LOG_NOTICE_NOT_CONFIGURED)
            return False
        self.state.configured = True
        await self._conn.send(self._subscribe_envelope(tr_key))
        return True

    def absorb_ack(self, message: dict) -> None:
        """구독 ACK에서 복호화 키를 챙긴다 — **이 창을 놓치면 그 연결은 영구히 못 읽는다.**"""
        ack = KISWebSocketClient.parse_subscription_ack(message)
        if ack is None:
            return
        if not ack.succeeded:
            self.state.last_error = f"구독 거부: {ack.msg_code} {ack.message}"
            logger.warning("체결통보 구독이 거부됐다 — %s %s", ack.msg_code, ack.message)
            return
        self.state.subscribed = True
        if ack.carries_cipher_material:
            self._aes_key, self._aes_iv = ack.aes_key, ack.aes_iv
            self.state.cipher_ready = True
        logger.info(
            LOG_NOTICE_SUBSCRIBED, ack.tr_id, ack.tr_key,
            "수신" if ack.carries_cipher_material else "**없음 — 이 연결로는 통보를 못 읽는다**",
        )

    async def handle_frame(self, raw: str, received_at: datetime | None = None) -> OrderNotice | None:
        """
        입력: 데이터 프레임 원문, (선택) 수신 시각.
        계산: 복호·파싱해 콜백에 넘긴다.
        해석: 키가 없거나 복호가 실패하면 **`unreadable`을 올리고 원문을 로그에 남긴다** —
             조용히 버리면 체결을 놓치고, 그 사실조차 남지 않는다(계명 12).
        실패 조건: 없다 — 이 함수는 예외를 밖으로 내보내지 않는다. 한 건의 실패가 스트림을
                  끊으면 그 뒤의 체결을 전부 놓친다.
        """
        if self._aes_key is None or self._aes_iv is None:
            self.state.unreadable += 1
            self.state.last_error = "복호화 키 없음"
            logger.warning(
                "체결통보 프레임이 왔는데 복호화 키가 없다 — 이 건은 영구히 못 읽는다. 원문: %r", raw
            )
            return None
        try:
            notice = parse_frame(raw, aes_key=self._aes_key, aes_iv=self._aes_iv)
        except Exception as exc:
            self.state.unreadable += 1
            self.state.last_error = f"복호 실패: {exc}"
            logger.warning("체결통보 복호 실패 — 원문: %r", raw, exc_info=True)
            return None
        if notice is None:
            return None
        self.state.notices += 1
        self.state.last_notice_at = received_at
        logger.info(
            LOG_NOTICE_RECEIVED,
            notice.symbol, notice.order_no, notice.filled_qty, notice.filled_price,
            notice.filled_time, len(notice.fields),
            "" if len(notice.fields) == len(_NOTICE_FIELDS)
            else f" ⚠ 문서와 다르다(기대 {len(_NOTICE_FIELDS)}개) — 이름이 밀렸을 수 있다",
        )
        if self._on_notice is not None:
            await self._on_notice(notice, received_at)
        return notice

    async def run_once(self) -> None:
        """
        계산: 구독하고, 끊길 때까지 받는다. 제어 메시지(JSON)는 ACK 흡수로, 데이터 프레임은
             복호·파싱으로 보낸다.
        해석: **한 세션이다** — 끊기면 예외가 그대로 나가고 재연결은 호출측이 한다.
        실패 조건: 구독이 설정 미비로 건너뛰어지면 즉시 반환한다(무한 대기하지 않는다).
        """
        if not await self.subscribe():
            return
        while True:
            raw = await self._conn.recv()
            if not raw:
                continue
            if raw.startswith("{"):
                try:
                    self.absorb_ack(json.loads(raw))
                except json.JSONDecodeError:
                    logger.debug("체결통보 소켓의 비-JSON 제어 메시지 무시: %r", raw)
                continue
            await self.handle_frame(raw, db_local_now())


def db_local_now() -> datetime:
    """`db.local_now()`를 늦게 부른다 — 이 모듈이 `mahdi.data.db`를 임포트하면 순환이 된다.

    (`db`는 설정만 임포트하므로 지금은 순환이 아니지만, 브로커 층이 데이터 층을 아는 것 자체가
    이 패키지의 경계를 흐린다. 시각 하나를 위해 그 경계를 무너뜨리지 않는다.)
    """
    from mahdi.data import db

    return db.local_now()


def notice_row(notice: OrderNotice, received_at: datetime, seq: int, tr_id: str) -> dict:
    """
    입력: 파싱된 통보, 수신 시각, 같은 시각 안의 일련번호, 구독 TR ID.
    계산: `db.insert_order_notice()`에 넘길 dict.
    해석: **`plaintext`가 이 행의 본체다.** 나머지 컬럼은 위치 기반 파싱 결과라 미실측
         상태에서 밀렸을 수 있고, `field_count`가 그 자기검증이다(마이그레이션 035 주석).
    실패 조건: 없다.
    """
    return {
        "received_at": received_at,
        "seq": seq,
        "tr_id": tr_id,
        "symbol": notice.symbol or None,
        "order_no": notice.order_no or None,
        "sell_buy_code": notice.sell_buy_code or None,
        "filled_qty": notice.filled_qty or None,
        "filled_price": notice.filled_price or None,
        "filled_time": notice.filled_time or None,
        "rejected_flag": notice.rejected_flag or None,
        "filled_flag": notice.filled_flag or None,
        "accepted_flag": notice.accepted_flag or None,
        "field_count": len(notice.fields),
        "plaintext": notice.raw,
    }
