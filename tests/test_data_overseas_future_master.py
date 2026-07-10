from pathlib import Path

from mahdi.data.overseas_future_master import OverseasFutureMaster, parse_master_file

# ffcode.mst는 고정폭(fixed-width) 포맷 — 오프셋 출처는 overseas_future_master.py 상단 주석과
# stocks_info/overseas_future_code.py. parse_master_file은 앞부분(종목코드·한글명)은 양수
# 오프셋으로, 뒷부분(거래소코드 이후)은 음수 오프셋(끝에서부터)으로 읽는다 — 파일을 줄 단위로
# 읽으면 각 줄 끝에 "\n"이 그대로 남으므로(파이썬 표준 동작), 음수 오프셋은 "\n" 포함 길이를
# 기준으로 계산해야 한다. 총 content 길이를 198자로 맞추면 "\n" 포함 199자 기준으로 -92가
# 정확히 content index 107(한글명 끝)과 맞아떨어진다(실제 마스터파일로 검증 완료, 갭 없음).


def _row(
    symbol: str,
    label: str,
    exchange_code: str,
    product_code: str,
    is_front_month: bool,
    is_most_active: bool = False,
) -> str:
    field = lambda s, width: s.ljust(width)[:width]
    parts = [
        field(symbol, 32),
        " ",  # 서버자동주문 가능 종목 여부
        " ",  # 서버자동주문 TWAP 가능 종목 여부
        " ",  # 서버자동 경제지표 주문 가능 종목 여부
        field("", 47),  # 필러
        field(label, 25),  # 종목한글명
        field(exchange_code, 10),
        field(product_code, 10),
        field("003", 3),  # 품목종류
        field("5", 5),  # 출력 소수점
        field("5", 5),  # 계산 소수점
        field("0.05", 14),  # 틱사이즈
        field("50", 14),  # 틱가치
        field("1000", 10),  # 계약크기
        field("10", 4),  # 가격표시진법
        field("1", 10),  # 환산승수
        "1" if is_most_active else "0",
        "1" if is_front_month else "0",
        "0",  # 스프레드여부
        "0",  # 스프레드기준종목 LEG1 여부
        field("", 2),  # 서브 거래소 코드(원래 3자리 — 총 content 길이를 198로 맞추기 위해 여기서 1자 줄임,
        # 파서는 이 필드를 읽지 않아 값 손실이 결과에 영향 없음)
    ]
    row = "".join(parts)
    assert len(row) == 198, len(row)
    return row


_SAMPLE_ROWS = [
    _row("VXN26", "VIX Index-202607", "CBOE", "VX", is_front_month=True, is_most_active=True),
    _row("VXQ26", "VIX Index-202608", "CBOE", "VX", is_front_month=False),
    _row("VXU26", "VIX Index-202609", "CBOE", "VX", is_front_month=False),
    _row("CNHN26", "USDCNH(HKEX)-202607", "HKEx", "CNH", is_front_month=True),
    _row("CNHQ26", "USDCNH(HKEX)-202608", "HKEx", "CNH", is_front_month=False),
    _row("CNHU26", "USDCNH(HKEX)-202609", "HKEx", "CNH", is_front_month=False, is_most_active=True),
    _row("ZNU26", "10year U.S T-Notes-202609", "CME", "ZN", is_front_month=True, is_most_active=True),
    # 스프레드 콤보 — 종목코드에 "-" 포함, 근월물 계산에서 제외돼야 함
    _row("VXMN26-Q26", "Mini Vix-2607-2608", "CBOE", "VXMVXM", is_front_month=False),
]


def _write_sample_mst(tmp_path: Path) -> Path:
    mst_path = tmp_path / "ffcode.mst"
    mst_path.write_text("\n".join(_SAMPLE_ROWS) + "\n", encoding="cp949", newline="")
    return mst_path


def _master(tmp_path: Path) -> OverseasFutureMaster:
    return OverseasFutureMaster.from_file(_write_sample_mst(tmp_path))


def test_parse_master_file_extracts_expected_fields(tmp_path):
    contracts = parse_master_file(_write_sample_mst(tmp_path))
    symbols = {c.symbol for c in contracts}
    assert "VXN26" in symbols
    assert "VXMN26-Q26" not in symbols  # 스프레드 콤보는 제외

    vxn26 = next(c for c in contracts if c.symbol == "VXN26")
    assert vxn26.exchange_code == "CBOE"
    assert vxn26.product_code == "VX"
    assert vxn26.expiry_yyyymm == "202607"
    assert vxn26.is_front_month is True


def test_front_month_code_picks_flagged_row(tmp_path):
    master = _master(tmp_path)
    assert master.front_month_code("VX") == "VXN26"
    assert master.front_month_code("CNH") == "CNHN26"
    assert master.front_month_code("ZN") == "ZNU26"


def test_front_month_code_unknown_product_returns_none(tmp_path):
    master = _master(tmp_path)
    assert master.front_month_code("NOPE") is None


def test_front_two_codes_returns_nearest_two_by_expiry(tmp_path):
    master = _master(tmp_path)
    assert master.front_two_codes("VX") == ("VXN26", "VXQ26")
    assert master.front_two_codes("CNH") == ("CNHN26", "CNHQ26")


def test_front_two_codes_second_is_none_when_only_one_contract(tmp_path):
    master = _master(tmp_path)
    assert master.front_two_codes("ZN") == ("ZNU26", None)


def test_contracts_by_expiry_sorted_ascending(tmp_path):
    master = _master(tmp_path)
    codes = [c.symbol for c in master.contracts_by_expiry("VX")]
    assert codes == ["VXN26", "VXQ26", "VXU26"]
