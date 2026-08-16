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
import logging
from dataclasses import dataclass, field

from mahdi.broker import tr_codes
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
