import pytest

from mahdi.features.volume import anchored_vwap, session_vwap, volume_profile, volume_spike


def test_session_vwap_known_value():
    prices = [100, 102, 104]
    volumes = [10, 20, 10]
    assert session_vwap(prices, volumes) == pytest.approx(102.0)


def test_session_vwap_zero_volume_falls_back_to_last_price():
    assert session_vwap([100, 105], [0, 0]) == 105


def test_session_vwap_empty_is_zero():
    assert session_vwap([], []) == 0.0


def test_anchored_vwap_uses_only_data_after_anchor():
    prices = [100, 102, 104]
    volumes = [10, 20, 10]
    # anchor_index=1 → 102,104 구간만: (102*20+104*10)/30
    expected = (102 * 20 + 104 * 10) / 30
    assert anchored_vwap(prices, volumes, anchor_index=1) == pytest.approx(expected)


def test_volume_profile_none_when_empty():
    assert volume_profile([], [], bin_size=1) is None


def test_volume_profile_none_when_zero_total_volume():
    assert volume_profile([100, 101], [0, 0], bin_size=1) is None


def test_volume_profile_poc_and_narrow_value_area():
    prices = [100, 100, 101, 101, 102, 103]
    volumes = [10, 10, 30, 30, 5, 5]
    profile = volume_profile(prices, volumes, bin_size=1, value_area_pct=0.5)
    # POC bin(101, 거래량 60)만으로 total(90)의 50%(45)를 이미 초과 → 확장 없음
    assert profile.poc == 101
    assert profile.vah == 101
    assert profile.val == 101


def test_volume_profile_expands_value_area_to_reach_threshold():
    prices = [100, 100, 101, 101, 102, 103]
    volumes = [10, 10, 30, 30, 5, 5]
    profile = volume_profile(prices, volumes, bin_size=1, value_area_pct=0.70)
    # target=63; POC(60)만으론 부족 → 거래량 큰 쪽(100:20 vs 102:5)인 100쪽으로 확장
    assert profile.poc == 101
    assert profile.val == 100
    assert profile.vah == 101


def test_volume_spike_ratio():
    assert volume_spike(current_volume=300, avg_volume=100) == pytest.approx(3.0)


def test_volume_spike_zero_baseline():
    assert volume_spike(current_volume=300, avg_volume=0) == 0.0
