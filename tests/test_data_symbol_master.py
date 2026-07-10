from pathlib import Path

from mahdi.data.symbol_master import IndexDerivativesMaster, parse_master_file

# 필드 순서: 상품종류|단축코드|표준코드|한글종목명|월물구분코드|행사가|ATM구분|기초자산단축코드|기초자산명
# (실제 마스터파일 실측 순서 — symbol_master.py의 _COLUMNS 주석 참고)
_SAMPLE_ROWS = [
    "1|A01609|STD001|F 202609|1|0.0| |2001|KOSPI200",
    "1|A01612|STD002|F 202612|2|0.0| |2001|KOSPI200",
    "5|B0160350|STD010|C 202607|1|350.0| |2001|KOSPI200",
    "5|B0160352|STD011|C 202607|1|352.5| |2001|KOSPI200",
    "5|B0160355|STD012|C 202608|2|355.0| |2001|KOSPI200",
    "6|C0160350|STD020|P 202607|1|350.0| |2001|KOSPI200",
    "6|C0160352|STD021|P 202607|1|352.5| |2001|KOSPI200",
    "1|Z09609|STD030|F 202609|1|0.0| |3003|KSQ150",
    # 위클리(상품종류 N/O) — 한글종목명이 "위클리M C 2607W1 1,300.0" 형식(2026-07-06 실측)
    "N|BAFBLWA41|STDW01|위클리M C 2607W1 1,100.0|2|1100.0| |2001|KOSPI200",
    "N|BAFBLWA73|STDW02|위클리M C 2607W1 1,180.0|2|1180.0| |2001|KOSPI200",
    "N|BAFBMWA73|STDW03|위클리M C 2607W2 1,180.0|1|1180.0| |2001|KOSPI200",
    "O|CAFBLWA41|STDW04|위클리M P 2607W1 1,100.0|2|1100.0| |2001|KOSPI200",
    # 위클리 코드풀 B(상품종류 L/M) — 2026-07-10 실측: N/O 풀과 격주로 교대 배정되며 한글종목명에
    # "M"이 끼지 않는다("위클리C 2607W0" 형식). 두 풀 모두 위클리로 집계돼야 한다.
    "L|BAFCLWA10|STDW05|위클리C 2607W0 1,100.0|2|1100.0| |2001|KOSPI200",
    "M|CAFCLWA10|STDW06|위클리P 2607W0 1,100.0|2|1100.0| |2001|KOSPI200",
]


def _write_sample_mst(tmp_path: Path) -> Path:
    # newline="" 필수: 기본 텍스트모드는 Windows에서 \n -> \r\n으로 바꿔, pandas가 마지막
    # 컬럼(기초자산명) 값 끝에 \r을 남겨 "KOSPI200" 문자열 비교가 깨진다.
    mst_path = tmp_path / "fo_idx_code_mts.mst"
    mst_path.write_text("\n".join(_SAMPLE_ROWS) + "\n", encoding="cp949", newline="")
    return mst_path


def _master(tmp_path: Path) -> IndexDerivativesMaster:
    return IndexDerivativesMaster.from_file(_write_sample_mst(tmp_path))


def test_parse_master_file_has_expected_columns_and_numeric_rank(tmp_path):
    df = parse_master_file(_write_sample_mst(tmp_path))
    assert list(df.columns) == [
        "상품종류",
        "단축코드",
        "표준코드",
        "한글종목명",
        "월물구분코드",
        "행사가",
        "ATM구분",
        "기초자산단축코드",
        "기초자산명",
    ]
    assert df["월물구분코드"].dtype.kind in "if"  # 숫자형으로 강제 변환됨


def test_front_month_future_code_picks_lowest_rank(tmp_path):
    master = _master(tmp_path)
    assert master.front_month_future_code("KOSPI200") == "A01609"


def test_front_month_future_code_filters_by_underlying(tmp_path):
    master = _master(tmp_path)
    assert master.front_month_future_code("KSQ150") == "Z09609"
    assert master.front_month_future_code("NONEXISTENT") is None


def test_options_filters_call_put_and_underlying(tmp_path):
    master = _master(tmp_path)
    calls = master.options("C")
    puts = master.options("P")
    assert set(calls["단축코드"]) == {"B0160350", "B0160352", "B0160355"}
    assert set(puts["단축코드"]) == {"C0160350", "C0160352"}


def test_options_invalid_type_raises(tmp_path):
    master = _master(tmp_path)
    try:
        master.options("X")
        assert False, "ValueError를 기대했으나 발생하지 않음"
    except ValueError:
        pass


def test_nearest_expiry_chain_only_includes_nearest_rank(tmp_path):
    master = _master(tmp_path)
    chain = master.nearest_expiry_chain("KOSPI200")

    # 202608(월물구분코드=2) 콜(B0160355)은 최근월이 아니므로 제외되어야 함
    symbols = {entry["symbol"] for entry in chain}
    assert "B0160355" not in symbols
    assert symbols == {"B0160350", "B0160352", "C0160350", "C0160352"}
    assert all(entry["month_label"] == "C 202607" or entry["month_label"] == "P 202607" for entry in chain)


def test_option_symbol_matches_nearest_expiry_and_strike(tmp_path):
    master = _master(tmp_path)
    assert master.option_symbol("C", 350.0) == "B0160350"
    assert master.option_symbol("P", 352.5) == "C0160352"


def test_option_symbol_returns_none_for_unlisted_strike(tmp_path):
    master = _master(tmp_path)
    assert master.option_symbol("C", 999.0) is None


def test_options_weekly_series_filters_by_product_type_N_O_and_L_M(tmp_path):
    master = _master(tmp_path)
    calls = master.options("C", series="weekly")
    puts = master.options("P", series="weekly")
    # N/O 풀(BAFB*)과 L/M 풀(BAFCLWA10/CAFCLWA10)이 모두 위클리로 잡혀야 함
    assert set(calls["단축코드"]) == {"BAFBLWA41", "BAFBLWA73", "BAFBMWA73", "BAFCLWA10"}
    assert set(puts["단축코드"]) == {"CAFBLWA41", "CAFCLWA10"}
    # regular(기본값)에는 위클리 행이 섞여 들면 안 됨
    assert "BAFBLWA41" not in set(master.options("C")["단축코드"])
    assert "BAFCLWA10" not in set(master.options("C")["단축코드"])


def test_nearest_expiry_chain_weekly_picks_nearest_week_across_pools(tmp_path):
    master = _master(tmp_path)
    chain = master.nearest_expiry_chain("KOSPI200", series="weekly")
    symbols = {entry["symbol"] for entry in chain}
    # W0(BAFCLWA10/CAFCLWA10, L/M 풀)가 W1(N/O 풀)보다 가까우므로 W0만 남아야 함 —
    # 즉 두 코드풀을 섞어 봤을 때도 진짜 최근접 위클리를 놓치지 않는다는 검증.
    assert symbols == {"BAFCLWA10", "CAFCLWA10"}


def test_option_symbol_weekly_series_matches_nearest_week_and_strike(tmp_path):
    master = _master(tmp_path)
    assert master.option_symbol("C", 1100.0, series="weekly") == "BAFCLWA10"
    assert master.option_symbol("P", 1100.0, series="weekly") == "CAFCLWA10"
    assert master.option_symbol("C", 9999.0, series="weekly") is None


def test_options_invalid_series_raises(tmp_path):
    master = _master(tmp_path)
    try:
        master.options("C", series="quarterly")
        assert False, "ValueError를 기대했으나 발생하지 않음"
    except ValueError:
        pass
