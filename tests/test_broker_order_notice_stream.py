"""체결통보 스트림 — 실행 배선 ②.

이 테스트가 지키는 것은 **네 가지**다:

  1. HTS ID가 없으면 구독을 건너뛰되 **조용히 넘어가지 않는다**(경고 한 줄).
  2. 복호화 키는 구독 ACK에만 온다 — **그 창을 놓치면 그 연결로는 한 건도 못 읽는다.**
     그 상태를 「정상」으로 보이게 하지 않는다(`subscribed`와 `cipher_ready`를 가른다).
  3. 못 읽은 프레임을 **조용히 버리지 않는다** — 세고, 원문을 로그에 남긴다.
  4. 필드 수가 문서와 다르면 경고한다 — 위치 기반 파싱이라 이름이 통째로 밀린다.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime

import pytest

from mahdi.broker import order_notice as onx
from mahdi.broker import tr_codes
from mahdi.config.settings import KISSettings

AES_KEY = "abcdefghijklmnopabcdefghijklmnop"  # 32바이트
AES_IV = "0123456789abcdef"  # 16바이트
NOW = datetime(2026, 8, 24, 9, 30, 1)


def _settings(hts_id: str = "TESTHTS", env: str = "vps") -> KISSettings:
    return KISSettings(
        KIS_APP_KEY="k", KIS_APP_SECRET="s", KIS_ACCOUNT_NO="1", KIS_ACCOUNT_PRODUCT_CODE="03",
        KIS_ENV=env, KIS_HTS_ID=hts_id,
    )


def _encrypt(plaintext: str) -> str:
    """`decrypt_notice()`의 역 — PKCS#7 패딩 + AES-256-CBC + base64."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    raw = plaintext.encode("utf-8")
    pad = 16 - (len(raw) % 16)
    raw += bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(AES_KEY.encode()), modes.CBC(AES_IV.encode())).encryptor()
    return base64.b64encode(encryptor.update(raw) + encryptor.finalize()).decode()


def _notice_plaintext(field_count: int = 22) -> str:
    """문서 예시 순서대로 채운 복호문 — 기본은 문서가 말하는 22개."""
    values = [
        "CUST01", "12345678", "0000007047", "", "02", "0", "1", "B09FAWA37",
        "1", "22.05", "093001", "0", "2", "2", "00950", "1",
        "@3137669", "코스피200콜 C 2608W3", "", "", "", "22.05",
    ]
    return "|".join(values[:field_count])


def _frame(plaintext: str) -> str:
    return f"1|H0IFCNI9|001|{_encrypt(plaintext)}"


def _ack(*, rt_cd: str = "0", with_cipher: bool = True) -> str:
    body: dict = {"rt_cd": rt_cd, "msg_cd": "OPSP0000", "msg1": "SUBSCRIBE SUCCESS"}
    if with_cipher:
        body["output"] = {"iv": AES_IV, "key": AES_KEY}
    return json.dumps({"header": {"tr_id": "H0IFCNI9", "tr_key": "TESTHTS"}, "body": body})


def _run(coro):
    """이 저장소는 pytest-asyncio를 안 쓴다 — `test_main.py`와 같은 방식으로 감싼다."""
    return asyncio.run(coro)


class _Conn:
    """주입 연결 — 보낸 것을 모으고, 미리 정한 수신 목록을 순서대로 돌려준다."""

    def __init__(self, inbox: list[str]):
        self.sent: list[str] = []
        self._inbox = list(inbox)

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if not self._inbox:
            raise ConnectionError("연결 종료")
        return self._inbox.pop(0)

    async def close(self) -> None:
        pass


# ===== 도메인·TR — 시세와 갈린다 =====


@pytest.mark.parametrize(
    ("env", "domain", "tr_id"),
    [("vps", tr_codes.VPS_WS_DOMAIN, "H0IFCNI9"), ("prod", tr_codes.REAL_WS_DOMAIN, "H0IFCNI0")],
)
def test_notice_domain_and_tr_split_by_account_kind(env, domain, tr_id):
    """시세는 계좌 무관이라 도메인이 하나지만, 체결통보는 계좌를 특정하므로 갈린다."""
    settings = _settings(env=env)

    assert onx.order_notice_ws_domain(settings) == domain
    assert onx.order_notice_tr_id(settings) == tr_id
    assert domain != tr_codes.MARKET_DATA_WS_DOMAIN or env == "prod"


# ===== 불변식 1 — 설정이 없으면 건너뛰되 조용하지 않다 =====


def test_missing_hts_id_skips_subscription_with_a_warning(caplog):
    conn = _Conn([])
    stream = onx.OrderNoticeStream(_settings(hts_id=""), "approval", conn)

    with caplog.at_level(logging.WARNING, logger="mahdi.broker.order_notice"):
        subscribed = _run(stream.subscribe())

    assert subscribed is False
    assert conn.sent == [], "구독 메시지를 보내면 안 된다"
    assert stream.state.configured is False
    assert "KIS_HTS_ID" in caplog.text


def test_subscribe_sends_hts_id_as_tr_key():
    conn = _Conn([])
    stream = onx.OrderNoticeStream(_settings("MYHTS"), "approval-key", conn)

    assert _run(stream.subscribe()) is True
    envelope = json.loads(conn.sent[0])
    assert envelope["header"]["approval_key"] == "approval-key"
    assert envelope["header"]["tr_type"] == "1"
    assert envelope["body"]["input"] == {"tr_id": "H0IFCNI9", "tr_key": "MYHTS"}


# ===== 불변식 2 — 키를 못 받은 상태를 「정상」으로 보이게 하지 않는다 =====


def test_ack_without_cipher_material_is_subscribed_but_not_ready(caplog):
    """구독은 성립했는데 키가 없다 — 프레임이 와도 한 건도 못 읽는다.

    두 상태를 한 불리언으로 합치면 이 상황이 화면에서 초록으로 보인다.
    """
    stream = onx.OrderNoticeStream(_settings(), "approval", _Conn([]))

    with caplog.at_level(logging.INFO, logger="mahdi.broker.order_notice"):
        stream.absorb_ack(json.loads(_ack(with_cipher=False)))

    assert stream.state.subscribed is True
    assert stream.state.cipher_ready is False
    assert "못 읽는다" in caplog.text


def test_rejected_ack_does_not_mark_subscribed(caplog):
    stream = onx.OrderNoticeStream(_settings(), "approval", _Conn([]))

    with caplog.at_level(logging.WARNING, logger="mahdi.broker.order_notice"):
        stream.absorb_ack(json.loads(_ack(rt_cd="1", with_cipher=False)))

    assert stream.state.subscribed is False
    assert stream.state.last_error is not None


# ===== 정상 경로 — ACK에서 키를 받고 프레임을 읽는다 =====


def test_full_session_subscribes_absorbs_key_and_parses_a_notice():
    received: list = []

    async def _on_notice(notice, at):
        received.append((notice, at))

    conn = _Conn([_ack(), _frame(_notice_plaintext())])
    stream = onx.OrderNoticeStream(_settings(), "approval", conn, on_notice=_on_notice)

    with pytest.raises(ConnectionError):
        _run(stream.run_once())  # 받을 게 떨어지면 연결이 끊긴 것으로 본다

    assert stream.state.cipher_ready is True
    assert stream.state.notices == 1
    assert stream.state.unreadable == 0
    notice = received[0][0]
    assert notice.symbol == "B09FAWA37"
    assert notice.order_no == "0000007047"
    assert notice.filled_qty == "1"
    assert notice.filled_price == "22.05"


# ===== 불변식 3 — 못 읽은 프레임을 조용히 버리지 않는다 =====


def test_frame_before_the_key_arrives_is_counted_and_logged(caplog):
    stream = onx.OrderNoticeStream(_settings(), "approval", _Conn([]))

    with caplog.at_level(logging.WARNING, logger="mahdi.broker.order_notice"):
        result = _run(stream.handle_frame(_frame(_notice_plaintext()), NOW))

    assert result is None
    assert stream.state.unreadable == 1
    assert stream.state.notices == 0
    assert "영구히 못 읽는다" in caplog.text


def test_undecryptable_frame_is_counted_not_raised(caplog):
    stream = onx.OrderNoticeStream(_settings(), "approval", _Conn([]))
    stream.absorb_ack(json.loads(_ack()))

    with caplog.at_level(logging.WARNING, logger="mahdi.broker.order_notice"):
        result = _run(stream.handle_frame("1|H0IFCNI9|001|!!!not-base64!!!", NOW))

    assert result is None, "예외가 밖으로 나가면 그 뒤의 체결을 전부 놓친다"
    assert stream.state.unreadable == 1


# ===== 불변식 4 — 필드 수가 다르면 경고한다 =====


def test_field_count_mismatch_warns_because_positions_shift(caplog):
    """위치 기반 파싱이라 하나가 밀리면 그 뒤가 전부 밀린다 — 값은 그럴듯해서 조용히 통과한다."""
    stream = onx.OrderNoticeStream(_settings(), "approval", _Conn([]))
    stream.absorb_ack(json.loads(_ack()))

    with caplog.at_level(logging.INFO, logger="mahdi.broker.order_notice"):
        notice = _run(stream.handle_frame(_frame(_notice_plaintext(field_count=18)), NOW))

    assert notice is not None
    assert len(notice.fields) == 18
    assert "문서와 다르다" in caplog.text


# ===== DB 행 — 원문이 본체다 =====


def test_notice_row_preserves_plaintext_and_field_count():
    notice = onx.parse_notice(_notice_plaintext(field_count=18))

    row = onx.notice_row(notice, NOW, seq=0, tr_id="H0IFCNI9")

    assert row["plaintext"] == _notice_plaintext(field_count=18)
    assert row["field_count"] == 18, "이 값이 이 표의 자기검증이다"
    assert row["received_at"] == NOW and row["seq"] == 0
    assert row["symbol"] == "B09FAWA37"


def test_notice_row_maps_empty_strings_to_null():
    """빈 문자열과 NULL을 가른다 — 빈칸은 「그 필드가 안 왔다」이지 「빈 값이 왔다」가 아니다."""
    notice = onx.parse_notice("|".join([""] * 22))

    row = onx.notice_row(notice, NOW, seq=0, tr_id="H0IFCNI9")

    assert row["symbol"] is None
    assert row["order_no"] is None
    assert row["field_count"] == 22
