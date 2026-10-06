"""Forex research routing must not silently use company analysis or broker prices."""

from types import SimpleNamespace

import pytest

from cli.models import AnalystType, AssetType
from cli.prompts import detect_asset_type, filter_analysts_for_asset_type, parse_analysts
from tradingagents.agents.context import build_instrument_context
from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
from tradingagents.agents.researchers.bull_researcher import create_bull_researcher
from tradingagents.dataflows.symbols import forex_pair
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.trading_graph import TradingAgentsGraph


@pytest.mark.parametrize("symbol", ["EURUSD", " eurusd=x ", "USDJPY+", "GBPNZD"])
def test_forex_detection(symbol):
    assert forex_pair(symbol) is not None
    assert detect_asset_type(symbol) == AssetType.FOREX


@pytest.mark.parametrize("symbol", ["XAUUSD", "GC=F", "EURUSD.a", "USDUSD", "AAPL", "BTCUSD"])
def test_non_pairs_are_not_mislabeled_forex(symbol):
    assert forex_pair(symbol) is None
    assert detect_asset_type(symbol) != AssetType.FOREX


def test_forex_cli_analysts_and_explicit_rejection():
    assert filter_analysts_for_asset_type(list(AnalystType), AssetType.FOREX) == [
        AnalystType.MARKET, AnalystType.NEWS
    ]
    assert parse_analysts("market,news", AssetType.FOREX) == [AnalystType.MARKET, AnalystType.NEWS]
    for analyst in ("social", "fundamentals"):
        with pytest.raises(ValueError, match="not available for forex"):
            parse_analysts(analyst, AssetType.FOREX)


def test_forex_context_ignores_company_profile_and_defines_quote_direction():
    context = build_instrument_context("EURUSD", "forex", {"company_name": "Wrong Company"})
    assert "Wrong Company" not in context
    assert "Base currency: EUR; quote currency: USD" in context
    assert "USD per one EUR" in context
    assert "EUR strengthens against USD" in context
    assert "not executable broker bid/ask quotes" in context
    with pytest.raises(ValueError, match="Unsupported forex pair"):
        build_instrument_context("XAUUSD", "forex")


def _state_graph(analysts):
    graph = object.__new__(TradingAgentsGraph)
    graph.selected_analysts = analysts
    graph.propagator = Propagator()
    return graph


def test_forex_run_state_does_not_fetch_company_identity(monkeypatch):
    def refuse(_ticker):
        pytest.fail("Forex must not fetch a company profile")

    monkeypatch.setattr("tradingagents.graph.trading_graph.resolve_instrument_identity", refuse)
    state = _state_graph(("market", "news")).create_run_state("EURUSD=X", "2026-10-06", "forex")
    assert state["asset_type"] == "forex"
    assert state["fundamentals_report"] == ""
    assert "Base currency: EUR" in state["instrument_context"]


@pytest.mark.parametrize("analyst", ["fundamentals", "social"])
def test_programmatic_forex_rejects_company_analysts(analyst):
    with pytest.raises(ValueError, match="only market/news"):
        _state_graph(("market", analyst)).create_run_state("EURUSD", "2026-10-06", "forex")


@pytest.mark.parametrize("factory", [create_bull_researcher, create_bear_researcher])
def test_forex_debate_uses_currency_drivers(factory):
    prompts = []

    def invoke(prompt):
        prompts.append(prompt)
        return SimpleNamespace(content="Research only")

    state = Propagator().create_initial_state("EURUSD", "2026-10-06", "forex")
    factory(SimpleNamespace(invoke=invoke))(state)
    assert "rate differentials" in prompts[0]
    assert "revenue projections" not in prompts[0]
    assert "declining innovation" not in prompts[0]
    assert "No fundamentals report in this run" in prompts[0]
