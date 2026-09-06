"""Tests for multiple statistics.

Percentiles use linear interpolation (Excel PERCENTILE.INC), the method a
banker's spreadsheet produces, so app output can be tied out against
Excel directly. NM multiples are excluded and n reports how many
companies actually inform each statistic.
"""

import pytest

from app.valuation.models import MultipleValue
from app.valuation.stats import percentile_inc, summarize_multiples


def mv(value):
    return MultipleValue(value=value, is_nm=False)


NM = MultipleValue(value=None, is_nm=True, nm_reason="test")


class TestPercentileInc:
    def test_matches_excel_on_even_count(self):
        values = [1.0, 2.0, 3.0, 4.0]
        assert percentile_inc(values, 0.25) == pytest.approx(1.75)
        assert percentile_inc(values, 0.50) == pytest.approx(2.5)
        assert percentile_inc(values, 0.75) == pytest.approx(3.25)

    def test_matches_excel_on_odd_count(self):
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        assert percentile_inc(values, 0.25) == pytest.approx(20.0)
        assert percentile_inc(values, 0.50) == pytest.approx(30.0)
        assert percentile_inc(values, 0.75) == pytest.approx(40.0)

    def test_endpoints(self):
        values = [3.0, 1.0, 2.0]  # unsorted input must be fine
        assert percentile_inc(values, 0.0) == pytest.approx(1.0)
        assert percentile_inc(values, 1.0) == pytest.approx(3.0)

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            percentile_inc([], 0.5)


class TestSummarizeMultiples:
    def test_full_summary(self):
        stats = summarize_multiples([mv(8.0), mv(10.0), mv(12.0), mv(14.0)])
        assert stats.n == 4
        assert stats.min == pytest.approx(8.0)
        assert stats.p25 == pytest.approx(9.5)
        assert stats.median == pytest.approx(11.0)
        assert stats.mean == pytest.approx(11.0)
        assert stats.p75 == pytest.approx(12.5)
        assert stats.max == pytest.approx(14.0)

    def test_nm_values_are_excluded_and_n_reflects_it(self):
        stats = summarize_multiples([mv(10.0), NM, mv(20.0), NM])
        assert stats.n == 2
        assert stats.median == pytest.approx(15.0)
        assert stats.mean == pytest.approx(15.0)

    def test_all_nm_yields_empty_stats(self):
        stats = summarize_multiples([NM, NM])
        assert stats.n == 0
        assert stats.min is None
        assert stats.p25 is None
        assert stats.median is None
        assert stats.mean is None
        assert stats.p75 is None
        assert stats.max is None

    def test_single_value(self):
        stats = summarize_multiples([mv(7.5)])
        assert stats.n == 1
        assert stats.min == stats.p25 == stats.median == stats.mean == stats.p75 == stats.max
        assert stats.median == pytest.approx(7.5)
