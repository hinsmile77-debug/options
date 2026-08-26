"""선물옵션 실시간체결통보 — 복호화·파싱 (2026-08-16, Block C).

이 절이 지키는 것: **암호화된 통보를 실제로 읽을 수 있는가**, 그리고 **문서와 어긋났을 때
조용히 넘어가지 않는가**. 이 계좌는 아직 통보를 받아본 적이 없으므로 필드 순서는 문서에서
옮긴 것이고, 어긋나면 경고가 그것을 드러내야 한다(계명 12).
"""

import base64

import pytest

from mahdi.broker.order_notice import (
    OrderNotice,
    decrypt_notice,
    order_notice_tr_id,
    order_notice_ws_domain,
    parse_frame,
    parse_notice,
    subscription_tr_key,
)
from mahdi.broker.ws_client import KISWebSocketClient
from mahdi.config.settings import KISSettings

# 공식 문서 "선물옵션 실시간체결통보" 응답 예시의 키/iv — 길이(32/16)가 계약이다.
_KEY = "abcdefghijklmnopabcdefghijklmnop"
_IV = "0123456789abcdef"

# 같은 시트의 「복호화 후」 예시를 필드 순서대로 재구성한 것(22개).
_SAMPLE_FIELDS = [
    "abcd1234", "1234567803", "0000001666", "", "02", "0", "0", "111V06",
    "0000000002", "007840000", "095835", "0", "2", "2", "00950", "000000000",
    "김한국", "삼성전자   F 2", "", "", "", "000000000",
]


def _encrypt(plaintext: str, key: str = _KEY, iv: str = _IV) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    data = plaintext.encode("utf-8")
    pad = 16 - (len(data) % 16)
    enc = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode())).encryptor()
    return base64.b64encode(enc.update(data + bytes([pad]) * pad) + enc.finalize()).decode()


def _settings(**overrides) -> KISSettings:
    defaults = dict(KIS_APP_KEY="k", KIS_APP_SECRET="s", KIS_ACCOUNT_NO="12345678", KIS_ENV="vps")
    defaults.update(overrides)
    # 2026-08-26 — **`.env`를 끊고 만든다.** `KISSettings`는 `env_file=PROJECT_ROOT/.env`를
    # 읽으므로, 여기서 안 넘긴 키가 **그 PC의 `.env`에서 조용히 채워진다.**
    #
    # 08-26 장전에 사람이 `.env`에 `KIS_HTS_ID`를 넣자 아래
    # `test_missing_hts_id_yields_none_so_the_caller_can_warn_instead_of_crashing`이
    # **그 PC에서만** 붉어졌다(다른 PC에서는 여전히 통과한다). 테스트가 재는 것은 「HTS ID가
    # 없을 때의 동작」인데, 실제로 재고 있던 것은 「이 PC의 `.env`에 그 키가 있는가」였다.
    # 멀티 PC에서 같은 커밋이 다른 결과를 내는 형태이고, 그것은 계측이 아니라 우연이다.
    return KISSettings(_env_file=None, **defaults)


def test_subscription_ack_keeps_the_cipher_material_it_used_to_throw_away():
    """**복호화 키는 구독 ACK에만 실려 온다** — 놓치면 그 연결의 통보를 영구히 못 읽는다.

    종전 `parse_subscription_ack()`은 `rt_cd`/`msg_cd`/`msg1`만 뽑고 `body.output`을 버렸다.
    """
    ack = KISWebSocketClient.parse_subscription_ack({
        "header": {"tr_id": "H0IFCNI9", "tr_key": "hts123"},
        "body": {"rt_cd": "0", "msg_cd": "OPSP0000", "msg1": "SUBSCRIBE SUCCESS",
                 "output": {"iv": _IV, "key": _KEY}},
    })

    assert ack.succeeded is True
    assert ack.aes_iv == _IV and ack.aes_key == _KEY
    assert ack.carries_cipher_material is True


def test_quote_subscription_ack_has_no_cipher_material_and_that_is_normal():
    """시세 구독 ACK에는 `output`이 없다 — 그때 둘은 None이고 종전과 같은 값이다."""
    ack = KISWebSocketClient.parse_subscription_ack({
        "header": {"tr_id": "H0IFCNT0", "tr_key": "201W09"},
        "body": {"rt_cd": "0", "msg_cd": "OPSP0000", "msg1": "SUBSCRIBE SUCCESS"},
    })

    assert ack.aes_iv is None and ack.aes_key is None
    assert ack.carries_cipher_material is False


def test_decrypt_notice_round_trips_the_documented_key_lengths():
    """KIS는 key 32바이트 / iv 16바이트를 **ASCII 문자열 그대로** 준다 — hex도 base64도 아니다."""
    assert len(_KEY.encode()) == 32 and len(_IV.encode()) == 16
    plaintext = "|".join(_SAMPLE_FIELDS)

    assert decrypt_notice(_encrypt(plaintext), aes_key=_KEY, aes_iv=_IV) == plaintext


def test_decrypt_notice_fails_loudly_on_a_wrong_key_length():
    """키를 hex로 오해하면 길이가 반이 된다 — 그때 조용히 틀리지 않고 즉시 터져야 한다."""
    with pytest.raises(ValueError):
        decrypt_notice(_encrypt("x"), aes_key="tooshort", aes_iv=_IV)


def test_parse_frame_reads_the_documented_example_end_to_end():
    """`1|H0IFCNI0|001|<암호문>` — 앞 세 필드는 암호화여부/TR ID/건수, 네 번째가 페이로드다."""
    ciphertext = _encrypt("|".join(_SAMPLE_FIELDS))
    notice = parse_frame(f"1|H0IFCNI0|001|{ciphertext}", aes_key=_KEY, aes_iv=_IV)

    assert notice.symbol == "111V06"
    assert notice.order_no == "0000001666"
    assert notice.sell_buy_code == "02"  # 문서 예시가 매수 건 → 02=매수 (계좌 추적기와 같은 규약)
    assert notice.filled_qty == "0000000002"
    assert notice.filled_price == "007840000"
    assert notice.filled_time == "095835"
    assert notice.is_rejected is False
    assert notice.raw == "|".join(_SAMPLE_FIELDS)  # 원문 보존(R8)


def test_parse_frame_returns_none_for_control_messages():
    """PINGPONG 등은 이 형태가 아니다 — 예외가 아니라 None으로 넘긴다."""
    assert parse_frame("PINGPONG", aes_key=_KEY, aes_iv=_IV) is None
    assert parse_frame("1|H0IFCNI0", aes_key=_KEY, aes_iv=_IV) is None


def test_parse_notice_warns_when_the_field_count_disagrees_with_the_document(caplog):
    """**필드 순서는 문서에서 옮긴 것이고 라이브 미실측이다.**

    어긋나면 이름이 밀렸을 수 있으므로 경고하고 원문을 남긴다 — 8/18 첫 통보가 정답을 알려준다.
    """
    with caplog.at_level("WARNING"):
        notice = parse_notice("abcd1234|1234567803|0000001666")

    assert "필드 수가 문서와 다르다" in caplog.text
    assert notice.order_no == "0000001666"  # 받은 만큼은 채운다
    assert notice.raw == "abcd1234|1234567803|0000001666"


def test_unknown_rejection_flag_is_treated_as_rejected():
    """거부여부가 "0"이 아니면 거부로 읽는다 — 모르면 안전한 쪽이다."""
    assert OrderNotice("s", "o", "02", "0", "0", "0", "1", "0", "2").is_rejected is True
    assert OrderNotice("s", "o", "02", "0", "0", "0", "처음보는값", "0", "2").is_rejected is True
    assert OrderNotice("s", "o", "02", "0", "0", "0", "0", "0", "2").is_rejected is False


def test_order_notice_uses_the_account_specific_domain_not_the_market_data_one():
    """체결통보는 계좌를 특정하므로 모의/실전 도메인이 갈린다 — 시세는 항상 실전 도메인이다."""
    assert order_notice_ws_domain(_settings(KIS_ENV="vps")).endswith(":31000")
    assert order_notice_ws_domain(_settings(KIS_ENV="prod")).endswith(":21000")
    assert order_notice_tr_id(_settings(KIS_ENV="vps")) == "H0IFCNI9"
    assert order_notice_tr_id(_settings(KIS_ENV="prod")) == "H0IFCNI0"


def test_missing_hts_id_yields_none_so_the_caller_can_warn_instead_of_crashing():
    """HTS ID가 없으면 구독을 건너뛴다 — 예외를 던지면 관측 루프가 안 뜨고,
    조용히 넘기면 「주문은 나가는데 체결 알림이 없는」 가장 위험한 상태가 된다."""
    assert subscription_tr_key(_settings()) is None
    assert subscription_tr_key(_settings(KIS_HTS_ID="  ")) is None
    assert subscription_tr_key(_settings(KIS_HTS_ID="hts123")) == "hts123"
