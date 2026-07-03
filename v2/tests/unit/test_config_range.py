"""Config read-range regression."""


class TestPlantsReadRangeCoversPrBaseline:
    """Regression for 2026-07-03: pr_baseline lives at column AJ (36), but
    load_portfolio read Plants only to AB (28) — the value silently loaded
    as None and soiling never computed. Dict-keyed mocks can't catch a
    range bug, so this pins the requested range itself."""

    def test_plants_range_reaches_column_aj(self):
        from unittest.mock import MagicMock
        from argia.core.sheets import SheetsClient
        from argia.core.config import load_portfolio
        sheets = MagicMock(spec=SheetsClient)
        sheets.read_table.return_value = []
        try:
            load_portfolio(sheets)
        except Exception:
            pass  # empty tables may raise later; the range call happened
        plants_calls = [c for c in sheets.read_table.call_args_list
                        if c.args and c.args[0] == "Plants"]
        assert plants_calls, "load_portfolio never read Plants"
        rng = plants_calls[0].args[1]
        end = rng.split(":")[1].rstrip("0123456789")
        def col_num(a1):
            n = 0
            for ch in a1:
                n = n * 26 + (ord(ch) - 64)
            return n
        assert col_num(end) >= col_num("AJ"), (
            f"Plants range {rng} stops before AJ — pr_baseline unreadable")
