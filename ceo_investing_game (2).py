
from __future__ import annotations

"""
CEO Investing Game - Visual Strategy Edition

A fictional investing simulator where the player is the CEO and 100% owner of a
company with $5 billion in starting cash. This version focuses on a cleaner,
clickable desktop interface with persistent save data and an employee-driven
research system.

Main additions in this edition:
- Tkinter GUI with tabs, quick actions, charts, and a real menu bar
- save/load JSON that stores company name, employee count, cash, holdings,
  trade history, monthly snapshots, and market state
- employee research depth and recommendation quality that improve as the player
  hires more staff
- monthly employee payroll of $3,000 per employee
- optional employee team trading plans, with manual execute or auto-trade
- slightly friendlier economic settings so the game is easier to grow

The market engine is synthetic but plausible. Stock prices are driven by
regimes, macro shocks, sector shocks, momentum, mean reversion, dividends,
fundamentals, and inflation pressure.

Run GUI:
    python3 ceo_investing_game.py

Run headless smoke test:
    python3 ceo_investing_game.py --nogui --autoplay-months 3 --auto-team

Save file:
    ceo_investing_save.json
"""

import argparse
import base64
import json
import math
import pickle
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
    from tkinter import scrolledtext
except Exception:  # pragma: no cover - GUI may be unavailable in some environments
    tk = None
    ttk = None
    messagebox = None
    scrolledtext = None


STARTING_CASH = 5_000_000_000.0
ANNUAL_INFLATION = 0.10
MONTHLY_INFLATION = (1.0 + ANNUAL_INFLATION) ** (1.0 / 12.0) - 1.0
TAX_RATE = 0.10
EMPLOYEE_MONTHLY_SALARY = 3_000.0
ANNUAL_CASH_YIELD = 0.045
MONTHLY_CASH_YIELD = (1.0 + ANNUAL_CASH_YIELD) ** (1.0 / 12.0) - 1.0
DEFAULT_STOCK_COUNT = 16
SAVE_VERSION = 3
DEFAULT_SAVE_FILE = "ceo_investing_save.json"
MAX_ACTIVITY_LOG = 600
MAX_SNAPSHOTS = 600
MAX_TRADE_HISTORY = 2_000


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def format_money(value: float) -> str:
    sign = "-" if value < 0 else ""
    value = abs(value)
    for suffix, size in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if value >= size:
            return f"{sign}${value / size:,.2f}{suffix}"
    return f"{sign}${value:,.2f}"


def format_pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def parse_money(text: str) -> float:
    cleaned = text.strip().lower().replace("$", "").replace(",", "")
    if not cleaned:
        raise ValueError("empty input")
    multiplier = 1.0
    if cleaned[-1] in {"k", "m", "b", "t"}:
        suffix = cleaned[-1]
        cleaned = cleaned[:-1]
        multiplier = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}[suffix]
    return float(cleaned) * multiplier


def encode_rng_state(state: object) -> str:
    return base64.b64encode(pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL)).decode("ascii")


def decode_rng_state(text: str) -> object:
    return pickle.loads(base64.b64decode(text.encode("ascii")))


@dataclass
class Position:
    shares: int = 0
    total_cost: float = 0.0

    @property
    def avg_cost(self) -> float:
        if self.shares <= 0:
            return 0.0
        return self.total_cost / self.shares

    def buy(self, shares: int, price: float) -> None:
        self.shares += shares
        self.total_cost += shares * price

    def sell(self, shares: int, price: float) -> float:
        if shares > self.shares:
            raise ValueError("cannot sell more shares than owned")
        avg_cost = self.avg_cost
        realized_gain = (price - avg_cost) * shares
        self.shares -= shares
        self.total_cost -= avg_cost * shares
        if self.shares <= 0:
            self.shares = 0
            self.total_cost = 0.0
        return realized_gain

    def to_dict(self) -> Dict[str, Any]:
        return {"shares": self.shares, "total_cost": self.total_cost}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Position":
        return cls(
            shares=safe_int(data.get("shares", 0), 0),
            total_cost=safe_float(data.get("total_cost", 0.0), 0.0),
        )


@dataclass
class Stock:
    ticker: str
    name: str
    sector: str
    price: float
    fair_value: float
    annual_drift: float
    annual_vol: float
    beta: float
    mean_reversion: float
    inflation_pass_through: float
    quality: float
    dividend_yield: float
    base_fundamental_growth: float
    momentum: float = 0.0
    last_log_return: float = 0.0
    news: str = "No material news."
    history: List[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.history:
            self.history.append(self.price)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "name": self.name,
            "sector": self.sector,
            "price": self.price,
            "fair_value": self.fair_value,
            "annual_drift": self.annual_drift,
            "annual_vol": self.annual_vol,
            "beta": self.beta,
            "mean_reversion": self.mean_reversion,
            "inflation_pass_through": self.inflation_pass_through,
            "quality": self.quality,
            "dividend_yield": self.dividend_yield,
            "base_fundamental_growth": self.base_fundamental_growth,
            "momentum": self.momentum,
            "last_log_return": self.last_log_return,
            "news": self.news,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Stock":
        return cls(
            ticker=str(data.get("ticker", "UNK")),
            name=str(data.get("name", "Unknown Company")),
            sector=str(data.get("sector", "Unknown")),
            price=max(1.0, safe_float(data.get("price", 10.0), 10.0)),
            fair_value=max(1.0, safe_float(data.get("fair_value", 10.0), 10.0)),
            annual_drift=safe_float(data.get("annual_drift", 0.08), 0.08),
            annual_vol=clamp(safe_float(data.get("annual_vol", 0.24), 0.24), 0.08, 0.85),
            beta=clamp(safe_float(data.get("beta", 1.0), 1.0), 0.40, 1.90),
            mean_reversion=clamp(safe_float(data.get("mean_reversion", 0.8), 0.8), 0.2, 1.4),
            inflation_pass_through=clamp(safe_float(data.get("inflation_pass_through", 0.6), 0.6), 0.05, 0.98),
            quality=clamp(safe_float(data.get("quality", 1.0), 1.0), 0.50, 1.60),
            dividend_yield=clamp(safe_float(data.get("dividend_yield", 0.01), 0.01), 0.0, 0.08),
            base_fundamental_growth=safe_float(data.get("base_fundamental_growth", 0.10), 0.10),
            momentum=safe_float(data.get("momentum", 0.0), 0.0),
            last_log_return=safe_float(data.get("last_log_return", 0.0), 0.0),
            news=str(data.get("news", "No material news.")),
            history=[safe_float(x, 0.0) for x in data.get("history", [])] or [max(1.0, safe_float(data.get("price", 10.0), 10.0))],
        )


@dataclass
class Company:
    name: str
    cash: float
    monthly_revenue: float
    monthly_base_costs: float
    pricing_power: float
    efficiency: float
    employees: int
    last_pre_tax_profit: float = 0.0
    last_tax_paid: float = 0.0
    last_net_profit: float = 0.0
    last_dividends: float = 0.0
    last_cash_interest: float = 0.0
    last_payroll: float = 0.0

    @property
    def payroll_cost(self) -> float:
        return max(0, self.employees) * EMPLOYEE_MONTHLY_SALARY

    @property
    def total_monthly_costs(self) -> float:
        return self.monthly_base_costs + self.payroll_cost

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "cash": self.cash,
            "monthly_revenue": self.monthly_revenue,
            "monthly_base_costs": self.monthly_base_costs,
            "monthly_costs": self.monthly_base_costs,
            "pricing_power": self.pricing_power,
            "efficiency": self.efficiency,
            "employees": self.employees,
            "last_pre_tax_profit": self.last_pre_tax_profit,
            "last_tax_paid": self.last_tax_paid,
            "last_net_profit": self.last_net_profit,
            "last_dividends": self.last_dividends,
            "last_cash_interest": self.last_cash_interest,
            "last_payroll": self.last_payroll,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Company":
        monthly_costs = data.get("monthly_base_costs", data.get("monthly_costs", 250_000_000.0))
        return cls(
            name=str(data.get("name", "Titan Holdings")),
            cash=safe_float(data.get("cash", STARTING_CASH), STARTING_CASH),
            monthly_revenue=max(10_000_000.0, safe_float(data.get("monthly_revenue", 350_000_000.0), 350_000_000.0)),
            monthly_base_costs=max(5_000_000.0, safe_float(monthly_costs, 230_000_000.0)),
            pricing_power=clamp(safe_float(data.get("pricing_power", 0.65), 0.65), 0.2, 0.95),
            efficiency=clamp(safe_float(data.get("efficiency", 0.65), 0.65), 0.2, 0.95),
            employees=max(0, safe_int(data.get("employees", 15), 15)),
            last_pre_tax_profit=safe_float(data.get("last_pre_tax_profit", 0.0), 0.0),
            last_tax_paid=safe_float(data.get("last_tax_paid", 0.0), 0.0),
            last_net_profit=safe_float(data.get("last_net_profit", 0.0), 0.0),
            last_dividends=safe_float(data.get("last_dividends", 0.0), 0.0),
            last_cash_interest=safe_float(data.get("last_cash_interest", 0.0), 0.0),
            last_payroll=safe_float(data.get("last_payroll", 0.0), 0.0),
        )


SECTOR_PROFILES: Dict[str, Dict[str, float | Tuple[float, float]]] = {
    "Technology": {"price_range": (35.0, 240.0), "drift": 0.18, "vol": 0.28, "beta": 1.25, "inflation_pass": 0.72, "dividend": 0.004},
    "Healthcare": {"price_range": (25.0, 165.0), "drift": 0.14, "vol": 0.22, "beta": 0.95, "inflation_pass": 0.58, "dividend": 0.007},
    "Financials": {"price_range": (18.0, 95.0), "drift": 0.12, "vol": 0.21, "beta": 1.10, "inflation_pass": 0.68, "dividend": 0.019},
    "Consumer": {"price_range": (15.0, 120.0), "drift": 0.11, "vol": 0.20, "beta": 1.00, "inflation_pass": 0.66, "dividend": 0.013},
    "Industrials": {"price_range": (22.0, 140.0), "drift": 0.13, "vol": 0.22, "beta": 1.08, "inflation_pass": 0.73, "dividend": 0.016},
    "Energy": {"price_range": (12.0, 95.0), "drift": 0.12, "vol": 0.27, "beta": 1.12, "inflation_pass": 0.83, "dividend": 0.026},
    "Telecom": {"price_range": (18.0, 80.0), "drift": 0.10, "vol": 0.16, "beta": 0.82, "inflation_pass": 0.74, "dividend": 0.028},
    "Utilities": {"price_range": (20.0, 85.0), "drift": 0.08, "vol": 0.14, "beta": 0.68, "inflation_pass": 0.84, "dividend": 0.032},
    "Materials": {"price_range": (14.0, 110.0), "drift": 0.11, "vol": 0.24, "beta": 1.05, "inflation_pass": 0.78, "dividend": 0.018},
}


STOCK_TEMPLATES: List[Tuple[str, str, str]] = [
    ("CYNX", "Cynex Data Platforms", "Technology"),
    ("QUBE", "Qube Neural Systems", "Technology"),
    ("NEXA", "Nexa Cloud Dynamics", "Technology"),
    ("HLRX", "Helixor Therapeutics", "Healthcare"),
    ("VTCR", "VitaCrest Pharma", "Healthcare"),
    ("MDSN", "Medison Devices", "Healthcare"),
    ("MRBK", "Meridian Bank Group", "Financials"),
    ("QINS", "Quanta Insurance", "Financials"),
    ("LNXF", "Laneford Capital", "Financials"),
    ("FORK", "FreshFork Foods", "Consumer"),
    ("ATLS", "Atlas Retail Co", "Consumer"),
    ("HABR", "Harbor Home Goods", "Consumer"),
    ("IRON", "IronPeak Logistics", "Industrials"),
    ("SKYF", "SkyForge Aerospace", "Industrials"),
    ("TRXN", "Traxon Robotics", "Industrials"),
    ("HLEN", "Helio Energy Corp", "Energy"),
    ("RDRL", "RedRock Resources", "Energy"),
    ("ORBT", "Orbit Pipeline Partners", "Energy"),
    ("QFBR", "Quantum Fiber Networks", "Telecom"),
    ("WTEL", "Westel Communications", "Telecom"),
    ("GGRD", "GreenGrid Utilities", "Utilities"),
    ("RIVR", "RiverState Power", "Utilities"),
    ("SMTL", "Summit Materials", "Materials"),
    ("BRZN", "Borezan Chemicals", "Materials"),
    ("ALTR", "Altaris Semiconductors", "Technology"),
    ("BION", "BioNova Labs", "Healthcare"),
    ("CNVL", "CrownVale Consumer", "Consumer"),
    ("DSTR", "Deltastar Freight", "Industrials"),
    ("ELEK", "Elektra Grid Systems", "Utilities"),
    ("FERO", "Fero Metals Group", "Materials"),
]


REGIMES: Dict[str, Dict[str, float | str]] = {
    "Boom": {"market_drift": 0.24, "market_vol": 0.13, "demand": 0.12, "note": "Risk appetite is hot and buyers are paying up for growth."},
    "Expansion": {"market_drift": 0.14, "market_vol": 0.14, "demand": 0.07, "note": "The economy is expanding with healthy demand and decent funding."},
    "Normal": {"market_drift": 0.08, "market_vol": 0.16, "demand": 0.04, "note": "Conditions are balanced and markets are acting normally."},
    "Slowdown": {"market_drift": 0.01, "market_vol": 0.20, "demand": -0.01, "note": "Growth is cooling, but not collapsing."},
    "Crisis": {"market_drift": -0.10, "market_vol": 0.30, "demand": -0.07, "note": "Financing stress and fear are hurting risk assets."},
}


REGIME_TRANSITIONS: Dict[str, List[Tuple[str, float]]] = {
    "Boom": [("Boom", 0.42), ("Expansion", 0.34), ("Normal", 0.17), ("Slowdown", 0.05), ("Crisis", 0.02)],
    "Expansion": [("Boom", 0.16), ("Expansion", 0.50), ("Normal", 0.23), ("Slowdown", 0.08), ("Crisis", 0.03)],
    "Normal": [("Boom", 0.08), ("Expansion", 0.26), ("Normal", 0.44), ("Slowdown", 0.16), ("Crisis", 0.06)],
    "Slowdown": [("Boom", 0.04), ("Expansion", 0.12), ("Normal", 0.34), ("Slowdown", 0.33), ("Crisis", 0.17)],
    "Crisis": [("Boom", 0.03), ("Expansion", 0.07), ("Normal", 0.22), ("Slowdown", 0.36), ("Crisis", 0.32)],
}


MACRO_EVENTS: List[Tuple[str, float]] = [
    ("Policy easing lifts risk appetite.", 0.022),
    ("Large infrastructure orders boost industrial demand.", 0.018),
    ("Consumer spending comes in stronger than expected.", 0.015),
    ("Commodity inflation squeezes margins.", -0.017),
    ("Credit spreads widen and financing gets harder.", -0.023),
    ("A strong earnings season improves sentiment.", 0.020),
    ("Geopolitical friction raises the market risk premium.", -0.021),
    ("Business investment rebounds after upbeat guidance.", 0.016),
    ("Supply chains normalize and delivery times improve.", 0.014),
]


POSITIVE_STOCK_EVENTS: Dict[str, List[str]] = {
    "Technology": [
        "wins a major AI infrastructure contract",
        "beats cloud revenue expectations",
        "launches a successful enterprise platform",
        "reports stronger-than-expected user growth",
    ],
    "Healthcare": [
        "receives favorable regulatory news",
        "reports promising clinical data",
        "expands distribution of a key therapy",
        "wins a hospital procurement contract",
    ],
    "Financials": [
        "posts strong loan growth and stable credit quality",
        "benefits from stronger capital markets activity",
        "announces a profitable acquisition",
        "raises guidance after resilient fee income",
    ],
    "Consumer": [
        "reports strong same-store sales",
        "launches a hit consumer product line",
        "improves margins through better inventory control",
        "expands successfully into new regions",
    ],
    "Industrials": [
        "wins a multi-year supply contract",
        "benefits from stronger capital spending",
        "reports a growing order backlog",
        "improves factory utilization",
    ],
    "Energy": [
        "benefits from stronger realized energy prices",
        "announces a strong production update",
        "cuts extraction costs ahead of schedule",
        "signs a long-term supply agreement",
    ],
    "Telecom": [
        "adds more subscribers than expected",
        "stabilizes churn and raises guidance",
        "wins a large enterprise network deal",
        "improves average revenue per user",
    ],
    "Utilities": [
        "secures a favorable rate case outcome",
        "raises regulated asset guidance",
        "expands clean power capacity on budget",
        "reports stable demand and improved efficiency",
    ],
    "Materials": [
        "benefits from stronger commodity demand",
        "wins a large industrial customer contract",
        "improves production yields",
        "reports stronger pricing power",
    ],
}


NEGATIVE_STOCK_EVENTS: Dict[str, List[str]] = {
    "Technology": [
        "misses earnings after weaker enterprise spending",
        "faces a cybersecurity incident",
        "warns about slower customer growth",
        "sees product delays pressure sentiment",
    ],
    "Healthcare": [
        "reports disappointing trial data",
        "faces a regulatory setback",
        "issues weaker guidance due to pricing pressure",
        "encounters manufacturing delays",
    ],
    "Financials": [
        "raises provisions for loan losses",
        "misses estimates on weaker deal activity",
        "faces margin pressure from deposit competition",
        "reports softer trading revenue",
    ],
    "Consumer": [
        "cuts guidance after softer demand",
        "faces inventory markdowns",
        "sees input costs hurt margins",
        "reports a failed product launch",
    ],
    "Industrials": [
        "loses a major contract",
        "faces a labor disruption",
        "warns about slower order growth",
        "reports supply chain problems",
    ],
    "Energy": [
        "cuts output guidance after operational issues",
        "faces lower commodity realizations",
        "reports higher extraction costs",
        "deals with maintenance disruptions",
    ],
    "Telecom": [
        "loses subscribers to rivals",
        "cuts outlook after aggressive competition",
        "reports a network outage",
        "faces higher customer acquisition costs",
    ],
    "Utilities": [
        "faces outage repair costs",
        "receives an unfavorable regulatory ruling",
        "reports weather-related demand weakness",
        "warns about rising capital spending",
    ],
    "Materials": [
        "cuts production after weak demand",
        "faces a commodity price slump",
        "reports higher transport costs",
        "suffers an operational outage",
    ],
}


class InvestmentGame:
    def __init__(
        self,
        company_name: str = "Titan Holdings",
        employees: int = 15,
        seed: Optional[int] = None,
        stock_count: int = DEFAULT_STOCK_COUNT,
    ) -> None:
        self.seed = seed
        self.rng = random.Random(seed)
        self.company = self._create_company(company_name, employees)
        self.total_months = 0
        self.inflation_index = 1.0
        self.market_regime = "Expansion"
        self.market_note = str(REGIMES[self.market_regime]["note"])
        self.macro_event_note = "No major macro event this month."
        self.last_market_factor = 0.0
        self.last_portfolio_return = 0.0
        self.auto_team_trading = False
        self.selected_ticker = ""
        self.last_employee_plan: List[Dict[str, Any]] = []
        self.stocks = self._generate_stock_universe(stock_count)
        if self.stocks:
            self.selected_ticker = sorted(self.stocks)[0]
        self.portfolio: Dict[str, Position] = {ticker: Position() for ticker in self.stocks}
        self.net_worth_history: List[float] = [self.net_worth]
        self.real_net_worth_history: List[float] = [self.real_net_worth]
        self.trade_history: List[Dict[str, Any]] = []
        self.activity_log: List[str] = []
        self.snapshot_history: List[Dict[str, Any]] = []
        self._log(f"Started {self.company.name} with {format_money(self.company.cash)} in cash and {self.company.employees} employees.")
        self._snapshot()

    @property
    def year(self) -> int:
        return self.total_months // 12 + 1

    @property
    def month(self) -> int:
        return self.total_months % 12 + 1

    @property
    def portfolio_value(self) -> float:
        return sum(self.portfolio[ticker].shares * stock.price for ticker, stock in self.stocks.items())

    @property
    def net_worth(self) -> float:
        return self.company.cash + self.portfolio_value

    @property
    def real_net_worth(self) -> float:
        return self.net_worth / max(self.inflation_index, 1e-9)

    @property
    def team_quality(self) -> float:
        employees = max(0, self.company.employees)
        return clamp(0.18 + 0.79 * (1.0 - math.exp(-employees / 26.0)), 0.12, 0.97)

    @property
    def analysis_depth(self) -> int:
        employees = max(0, self.company.employees)
        if employees < 5:
            return 1
        if employees < 15:
            return 2
        if employees < 40:
            return 3
        return 4

    @property
    def analysis_depth_label(self) -> str:
        return {
            1: "Basic coverage",
            2: "Standard desk",
            3: "Advanced desk",
            4: "Institutional desk",
        }[self.analysis_depth]

    def _log(self, text: str) -> None:
        stamp = f"Y{self.year} M{self.month}"
        entry = f"[{stamp}] {text}"
        self.activity_log.append(entry)
        if len(self.activity_log) > MAX_ACTIVITY_LOG:
            self.activity_log = self.activity_log[-MAX_ACTIVITY_LOG:]

    def _snapshot(self) -> None:
        snap = {
            "turn": self.total_months,
            "year": self.year,
            "month": self.month,
            "cash": self.company.cash,
            "portfolio_value": self.portfolio_value,
            "net_worth": self.net_worth,
            "real_net_worth": self.real_net_worth,
            "market_regime": self.market_regime,
            "employees": self.company.employees,
            "payroll": self.company.payroll_cost,
            "pre_tax_profit": self.company.last_pre_tax_profit,
            "tax_paid": self.company.last_tax_paid,
            "net_profit": self.company.last_net_profit,
            "dividends": self.company.last_dividends,
            "cash_interest": self.company.last_cash_interest,
        }
        self.snapshot_history.append(snap)
        if len(self.snapshot_history) > MAX_SNAPSHOTS:
            self.snapshot_history = self.snapshot_history[-MAX_SNAPSHOTS:]

    def _create_company(self, name: str, employees: int) -> Company:
        revenue = self.rng.uniform(340_000_000, 460_000_000)
        cost_ratio = self.rng.uniform(0.60, 0.74)
        base_costs = revenue * cost_ratio
        pricing_power = self.rng.uniform(0.58, 0.88)
        efficiency = self.rng.uniform(0.56, 0.86)
        return Company(
            name=name,
            cash=STARTING_CASH,
            monthly_revenue=revenue,
            monthly_base_costs=base_costs,
            pricing_power=pricing_power,
            efficiency=efficiency,
            employees=max(0, employees),
        )

    def _generate_stock_universe(self, stock_count: int) -> Dict[str, Stock]:
        chosen = self.rng.sample(STOCK_TEMPLATES, k=min(stock_count, len(STOCK_TEMPLATES)))
        universe: Dict[str, Stock] = {}
        for ticker, name, sector in chosen:
            profile = SECTOR_PROFILES[sector]
            low_price, high_price = profile["price_range"]  # type: ignore[index]
            price = self.rng.uniform(low_price, high_price)
            quality = clamp(self.rng.uniform(0.85, 1.28), 0.60, 1.45)
            annual_drift = clamp(safe_float(profile["drift"], 0.10) + self.rng.gauss(0.0, 0.025), 0.02, 0.30)
            annual_vol = clamp(safe_float(profile["vol"], 0.20) + self.rng.gauss(0.0, 0.03), 0.10, 0.48)
            beta = clamp(safe_float(profile["beta"], 1.0) + self.rng.gauss(0.0, 0.10), 0.50, 1.65)
            inflation_pass = clamp(safe_float(profile["inflation_pass"], 0.65) + self.rng.gauss(0.0, 0.06), 0.15, 0.95)
            mean_reversion = self.rng.uniform(0.55, 1.10)
            dividend_yield = clamp(safe_float(profile["dividend"], 0.01) + self.rng.gauss(0.0, 0.004), 0.0, 0.06)
            base_fund_growth = clamp(annual_drift + (quality - 1.0) * 0.05 + self.rng.gauss(0.0, 0.02), -0.02, 0.28)
            fair_value = price * self.rng.uniform(0.90, 1.22)
            universe[ticker] = Stock(
                ticker=ticker,
                name=name,
                sector=sector,
                price=price,
                fair_value=fair_value,
                annual_drift=annual_drift,
                annual_vol=annual_vol,
                beta=beta,
                mean_reversion=mean_reversion,
                inflation_pass_through=inflation_pass,
                quality=quality,
                dividend_yield=dividend_yield,
                base_fundamental_growth=base_fund_growth,
            )
        return universe

    def _weighted_choice(self, pairs: List[Tuple[str, float]]) -> str:
        roll = self.rng.random()
        cumulative = 0.0
        for value, weight in pairs:
            cumulative += weight
            if roll <= cumulative:
                return value
        return pairs[-1][0]

    def _next_regime(self) -> str:
        return self._weighted_choice(REGIME_TRANSITIONS[self.market_regime])

    def _generate_macro_event(self) -> Tuple[float, str]:
        if self.rng.random() < 0.18:
            note, base_shock = self.rng.choice(MACRO_EVENTS)
            shock = clamp(base_shock + self.rng.gauss(0.0, 0.006), -0.04, 0.04)
            return shock, note
        return 0.0, "No major macro event this month."

    def _generate_stock_event(self, stock: Stock) -> Tuple[float, str, float]:
        roll = self.rng.random()
        if roll < 0.14:
            event = self.rng.choice(POSITIVE_STOCK_EVENTS[stock.sector])
            price_shock = abs(self.rng.gauss(0.045, 0.020))
            fundamental_boost = price_shock * 0.45
            return price_shock, f"Positive news: {stock.name} {event}.", fundamental_boost
        if roll < 0.22:
            event = self.rng.choice(NEGATIVE_STOCK_EVENTS[stock.sector])
            price_shock = -abs(self.rng.gauss(0.050, 0.025))
            fundamental_boost = price_shock * 0.50
            return price_shock, f"Negative news: {stock.name} {event}.", fundamental_boost
        return 0.0, "No material news.", 0.0

    def _sector_shocks(self) -> Dict[str, float]:
        shocks: Dict[str, float] = {}
        for sector in SECTOR_PROFILES:
            base = self.rng.gauss(0.0, 0.012)
            if self.rng.random() < 0.10:
                base += self.rng.gauss(0.0, 0.015)
            shocks[sector] = clamp(base, -0.04, 0.04)
        return shocks

    def _sector_theme_bonus(self, sector: str) -> float:
        regime_bonus = {
            "Boom": {"Technology": 0.030, "Industrials": 0.022, "Consumer": 0.018, "Energy": 0.015},
            "Expansion": {"Technology": 0.018, "Industrials": 0.018, "Financials": 0.015, "Consumer": 0.013},
            "Normal": {"Utilities": 0.010, "Healthcare": 0.010, "Financials": 0.008},
            "Slowdown": {"Utilities": 0.018, "Healthcare": 0.016, "Telecom": 0.012},
            "Crisis": {"Utilities": 0.026, "Healthcare": 0.024, "Telecom": 0.018, "Energy": -0.014, "Technology": -0.016},
        }
        return regime_bonus.get(self.market_regime, {}).get(sector, 0.0)

    def _simulate_company_operations(self) -> None:
        regime = REGIMES[self.market_regime]
        demand = safe_float(regime["demand"], 0.0)

        revenue_growth = (
            0.004
            + demand / 12.0
            + self.company.pricing_power * MONTHLY_INFLATION * 0.92
            + self.company.efficiency * 0.0015
            + self.rng.gauss(0.0, 0.007)
        )
        base_cost_growth = (
            MONTHLY_INFLATION * (0.94 - 0.28 * self.company.efficiency)
            + self.rng.gauss(0.0, 0.006)
        )

        revenue_growth = clamp(revenue_growth, -0.04, 0.09)
        base_cost_growth = clamp(base_cost_growth, -0.02, 0.06)

        self.company.monthly_revenue *= (1.0 + revenue_growth)
        self.company.monthly_base_costs *= (1.0 + base_cost_growth)

        payroll = self.company.payroll_cost
        pre_tax_profit = self.company.monthly_revenue - self.company.monthly_base_costs - payroll
        tax_paid = max(pre_tax_profit, 0.0) * TAX_RATE
        net_profit = pre_tax_profit - tax_paid

        cash_interest = max(self.company.cash, 0.0) * MONTHLY_CASH_YIELD

        self.company.cash += net_profit + cash_interest
        self.company.last_pre_tax_profit = pre_tax_profit
        self.company.last_tax_paid = tax_paid
        self.company.last_net_profit = net_profit
        self.company.last_cash_interest = cash_interest
        self.company.last_payroll = payroll

    def _simulate_market(self) -> None:
        dt = 1.0 / 12.0
        self.market_regime = self._next_regime()
        regime = REGIMES[self.market_regime]
        self.market_note = str(regime["note"])

        macro_shock, macro_note = self._generate_macro_event()
        self.macro_event_note = macro_note
        sector_shocks = self._sector_shocks()

        market_noise = safe_float(regime["market_vol"], 0.18) * math.sqrt(dt) * self.rng.gauss(0.0, 1.0)
        market_factor = safe_float(regime["market_drift"], 0.08) * dt + market_noise + macro_shock
        market_factor = clamp(market_factor, -0.16, 0.17)
        self.last_market_factor = market_factor

        starting_portfolio_value = self.portfolio_value
        dividends_paid = 0.0

        for stock in self.stocks.values():
            news_shock, news_text, fundamental_news = self._generate_stock_event(stock)

            fundamental_growth = (
                stock.base_fundamental_growth
                + 0.42 * safe_float(regime["demand"], 0.0)
                + 0.16 * sector_shocks[stock.sector]
                - (1.0 - stock.inflation_pass_through) * ANNUAL_INFLATION * 0.28
                + fundamental_news
                + self.rng.gauss(0.0, 0.018)
            )
            fundamental_growth = clamp(fundamental_growth, -0.22, 0.34)
            stock.fair_value *= math.exp(fundamental_growth * dt)
            stock.fair_value = max(stock.fair_value, 1.0)

            value_pull = stock.mean_reversion * math.log(stock.fair_value / stock.price) * dt
            idiosyncratic = stock.annual_vol * math.sqrt(dt) * self.rng.gauss(0.0, 1.0)
            momentum_term = 0.18 * stock.momentum
            inflation_penalty = (1.0 - stock.inflation_pass_through) * MONTHLY_INFLATION * 0.55
            carry = stock.annual_drift * dt * 0.50 + (stock.quality - 1.0) * 0.008

            total_log_return = (
                carry
                + stock.beta * market_factor
                + sector_shocks[stock.sector]
                + idiosyncratic
                + value_pull
                + momentum_term
                - inflation_penalty
                + news_shock
            )
            total_log_return = clamp(total_log_return, -0.45, 0.38)

            stock.price *= math.exp(total_log_return)
            stock.price = max(stock.price, 1.0)
            stock.last_log_return = total_log_return
            stock.momentum = 0.62 * stock.momentum + 0.38 * total_log_return
            stock.news = news_text
            stock.history.append(stock.price)
            if len(stock.history) > 240:
                stock.history = stock.history[-240:]

            position = self.portfolio[stock.ticker]
            if position.shares > 0 and stock.dividend_yield > 0:
                monthly_dividend = position.shares * stock.price * (stock.dividend_yield / 12.0)
                dividends_paid += monthly_dividend

        self.company.cash += dividends_paid
        self.company.last_dividends = dividends_paid

        ending_portfolio_value = self.portfolio_value
        if starting_portfolio_value > 0:
            self.last_portfolio_return = (ending_portfolio_value - starting_portfolio_value) / starting_portfolio_value
        else:
            self.last_portfolio_return = 0.0

    def advance_month(self) -> List[str]:
        self._simulate_company_operations()
        self._simulate_market()
        self.inflation_index *= (1.0 + MONTHLY_INFLATION)
        self.total_months += 1

        results: List[str] = []
        if self.auto_team_trading:
            plan = self.generate_employee_plan()
            if plan["entries"]:
                results.extend(self.execute_employee_plan(plan))
            else:
                results.append("Employee team did not find a high-conviction trade this month.")

        self.net_worth_history.append(self.net_worth)
        self.real_net_worth_history.append(self.real_net_worth)
        self._snapshot()
        gainers, losers = self.top_movers(3)
        self._log(
            "Month advanced. Market "
            f"{format_pct(self.last_market_factor)}. Portfolio {format_pct(self.last_portfolio_return)}. "
            f"Top gainer: {gainers[0][0]} {format_pct(gainers[0][1])}." if gainers else "Month advanced."
        )
        return results

    def analysis_snapshot(self, ticker: str) -> Dict[str, Any]:
        stock = self.stocks[ticker]
        skill = self.team_quality
        seed_text = (
            f"{self.total_months}|{self.market_regime}|{self.company.employees}|"
            f"{ticker}|{stock.price:.4f}|{stock.fair_value:.4f}|{stock.momentum:.5f}"
        )
        analyst_rng = random.Random(seed_text)

        fair_value_noise = 0.30 * (1.0 - skill) + 0.02
        estimated_fair_value = stock.fair_value * clamp(1.0 + analyst_rng.gauss(0.0, fair_value_noise), 0.45, 1.85)
        estimated_fair_value = max(1.0, estimated_fair_value)
        est_gap = estimated_fair_value / stock.price - 1.0

        estimated_vol = clamp(
            stock.annual_vol * clamp(1.0 + analyst_rng.gauss(0.0, 0.18 * (1.0 - skill) + 0.03), 0.65, 1.45),
            0.08,
            0.75,
        )
        sector_fit = self._sector_theme_bonus(stock.sector)
        trend = stock.momentum + analyst_rng.gauss(0.0, 0.035 * (1.0 - skill) + 0.004)

        estimated_12m_return = clamp(
            est_gap * (0.64 + 0.24 * skill)
            + stock.base_fundamental_growth * 0.48
            + stock.dividend_yield * 0.70
            + sector_fit
            + trend * 1.8
            - estimated_vol * (0.22 - 0.08 * skill)
            + analyst_rng.gauss(0.0, 0.08 * (1.0 - skill) + 0.01),
            -0.45,
            0.80,
        )
        conviction = clamp(
            0.12 + 0.76 * skill + 0.16 * abs(est_gap) - 0.22 * estimated_vol + 0.10 * abs(trend),
            0.05,
            0.99,
        )

        if estimated_12m_return > 0.25 and conviction > 0.62:
            recommendation = "Strong Buy"
        elif estimated_12m_return > 0.09:
            recommendation = "Buy"
        elif estimated_12m_return > -0.03:
            recommendation = "Hold"
        elif estimated_12m_return > -0.14:
            recommendation = "Trim"
        else:
            recommendation = "Sell"

        if estimated_vol < 0.18:
            risk_label = "Low"
        elif estimated_vol < 0.28:
            risk_label = "Moderate"
        else:
            risk_label = "High"

        score = round(clamp(52 + estimated_12m_return * 95 + conviction * 18 - estimated_vol * 16, 0, 100))

        reasons: List[str] = []
        if est_gap > 0.15:
            reasons.append("team sees clear undervaluation")
        elif est_gap < -0.12:
            reasons.append("team sees limited upside versus price")
        if trend > 0.04:
            reasons.append("price momentum is improving")
        elif trend < -0.04:
            reasons.append("recent momentum is weak")
        if stock.dividend_yield > 0.02:
            reasons.append("dividend support helps total return")
        if sector_fit > 0.012:
            reasons.append(f"{self.market_regime.lower()} regime favors the sector")
        elif sector_fit < -0.010:
            reasons.append(f"{self.market_regime.lower()} regime is less friendly for the sector")
        if not reasons:
            reasons.append("valuation and momentum look balanced")

        return {
            "ticker": ticker,
            "estimated_fair_value": estimated_fair_value,
            "estimated_gap": est_gap,
            "estimated_12m_return": estimated_12m_return,
            "estimated_vol": estimated_vol,
            "conviction": conviction,
            "recommendation": recommendation,
            "risk_label": risk_label,
            "score": score,
            "reasons": reasons,
            "sector_fit": sector_fit,
            "trend": trend,
        }

    def research_report(self, ticker: str) -> str:
        ticker = ticker.upper().strip()
        if ticker not in self.stocks:
            return f"{ticker} does not exist."

        stock = self.stocks[ticker]
        analysis = self.analysis_snapshot(ticker)
        depth = self.analysis_depth
        lines = []
        lines.append(f"{stock.ticker} — {stock.name}")
        lines.append(f"Sector: {stock.sector}")
        lines.append(f"Current price: {format_money(stock.price)}")
        lines.append(f"Team rating: {analysis['recommendation']} | score {analysis['score']}/100")
        lines.append(f"Estimated 12-month return: {format_pct(analysis['estimated_12m_return'])}")
        lines.append(f"Confidence: {format_pct(analysis['conviction'])}")
        lines.append(f"Risk: {analysis['risk_label']}")
        lines.append(f"Latest news: {stock.news}")

        if depth >= 2:
            lines.append(f"Team estimated fair value: {format_money(analysis['estimated_fair_value'])}")
            lines.append(f"Estimated upside/downside: {format_pct(analysis['estimated_gap'])}")
            lines.append(f"Dividend yield: {format_pct(stock.dividend_yield)}")

        if depth >= 3:
            lines.append(f"Estimated annual volatility: {format_pct(analysis['estimated_vol'])}")
            lines.append(f"Momentum signal: {format_pct(analysis['trend'])}")
            lines.append(f"Regime fit: {format_pct(analysis['sector_fit'])}")
            lines.append("Key reasons:")
            for reason in analysis["reasons"]:
                lines.append(f"  - {reason}")

        if depth >= 4:
            suggested_position = min(
                max(self.net_worth * (0.01 + 0.05 * analysis["conviction"]), 5_000_000.0),
                max(self.company.cash * 0.16, 5_000_000.0),
            )
            if analysis["recommendation"] in {"Strong Buy", "Buy"}:
                lines.append(f"Suggested entry size: about {format_money(suggested_position)} if cash allows.")
            elif analysis["recommendation"] == "Trim":
                lines.append("Suggested action: consider reducing exposure on strength.")
            elif analysis["recommendation"] == "Sell":
                lines.append("Suggested action: exit or avoid until the setup improves.")
            lines.append("Model notes: employee quality reduces estimate noise, improves conviction, and diversifies recommendations.")

        lines.append(f"Research depth: {self.analysis_depth_label} from {self.company.employees} employees.")
        lines.append(f"Monthly payroll drag: {format_money(self.company.payroll_cost)}")
        return "\n".join(lines)

    def top_movers(self, count: int = 3) -> Tuple[List[Tuple[str, float]], List[Tuple[str, float]]]:
        changes = [(stock.ticker, math.exp(stock.last_log_return) - 1.0) for stock in self.stocks.values()]
        gainers = sorted(changes, key=lambda x: x[1], reverse=True)[:count]
        losers = sorted(changes, key=lambda x: x[1])[:count]
        return gainers, losers

    def market_rows(self) -> List[Tuple[Any, ...]]:
        rows: List[Tuple[Any, ...]] = []
        for stock in self.stocks.values():
            analysis = self.analysis_snapshot(stock.ticker)
            rows.append(
                (
                    stock.ticker,
                    stock.name,
                    stock.sector,
                    stock.price,
                    math.exp(stock.last_log_return) - 1.0,
                    analysis["estimated_fair_value"],
                    analysis["estimated_gap"],
                    stock.dividend_yield,
                    analysis["score"],
                    analysis["recommendation"],
                )
            )
        call_rank = {"Strong Buy": 5, "Buy": 4, "Hold": 3, "Trim": 2, "Sell": 1}
        rows.sort(key=lambda row: (call_rank.get(str(row[9]), 0), row[8], row[6]), reverse=True)
        return rows

    def holding_rows(self) -> List[Tuple[Any, ...]]:
        rows: List[Tuple[Any, ...]] = []
        for ticker, position in self.portfolio.items():
            if position.shares <= 0:
                continue
            stock = self.stocks[ticker]
            market_value = position.shares * stock.price
            unrealized = market_value - position.total_cost
            allocation = market_value / max(self.net_worth, 1.0)
            analysis = self.analysis_snapshot(ticker)
            rows.append(
                (
                    ticker,
                    position.shares,
                    position.avg_cost,
                    market_value,
                    unrealized,
                    allocation,
                    analysis["recommendation"],
                )
            )
        rows.sort(key=lambda row: row[3], reverse=True)
        return rows

    def _record_trade(
        self,
        action: str,
        ticker: str,
        shares: int,
        price: float,
        gross_amount: float,
        source: str,
        realized_gain: Optional[float] = None,
    ) -> None:
        entry = {
            "timestamp": now_iso(),
            "turn": self.total_months,
            "year": self.year,
            "month": self.month,
            "action": action,
            "ticker": ticker,
            "shares": shares,
            "price": price,
            "gross_amount": gross_amount,
            "source": source,
            "cash_after": self.company.cash,
        }
        if realized_gain is not None:
            entry["realized_gain"] = realized_gain
        self.trade_history.append(entry)
        if len(self.trade_history) > MAX_TRADE_HISTORY:
            self.trade_history = self.trade_history[-MAX_TRADE_HISTORY:]

    def buy_stock(self, ticker: str, dollar_amount: float, source: str = "CEO") -> str:
        ticker = ticker.upper().strip()
        if ticker not in self.stocks:
            return f"Ticker {ticker} does not exist."
        if dollar_amount <= 0:
            return "Investment amount must be positive."
        stock = self.stocks[ticker]
        shares = int(dollar_amount // stock.price)
        if shares <= 0:
            return f"{format_money(dollar_amount)} is not enough to buy 1 share of {ticker} at {format_money(stock.price)}."
        cost = shares * stock.price
        if cost > self.company.cash:
            return f"Not enough cash. Available cash: {format_money(self.company.cash)}."
        self.company.cash -= cost
        self.portfolio[ticker].buy(shares, stock.price)
        self.selected_ticker = ticker
        self._record_trade("BUY", ticker, shares, stock.price, cost, source)
        self._log(f"{source} bought {shares:,} shares of {ticker} for {format_money(cost)}.")
        return f"Bought {shares:,} shares of {ticker} for {format_money(cost)} at {format_money(stock.price)} per share."

    def sell_stock(self, ticker: str, shares_to_sell: int, source: str = "CEO") -> str:
        ticker = ticker.upper().strip()
        if ticker not in self.stocks:
            return f"Ticker {ticker} does not exist."
        position = self.portfolio[ticker]
        if shares_to_sell <= 0:
            return "Shares to sell must be positive."
        if shares_to_sell > position.shares:
            return f"You only own {position.shares:,} shares of {ticker}."
        price = self.stocks[ticker].price
        proceeds = shares_to_sell * price
        realized_gain = position.sell(shares_to_sell, price)
        self.company.cash += proceeds
        self.selected_ticker = ticker
        self._record_trade("SELL", ticker, shares_to_sell, price, proceeds, source, realized_gain=realized_gain)
        self._log(f"{source} sold {shares_to_sell:,} shares of {ticker} for {format_money(proceeds)}.")
        return (
            f"Sold {shares_to_sell:,} shares of {ticker} for {format_money(proceeds)} at "
            f"{format_money(price)} per share. Realized gain/loss: {format_money(realized_gain)}."
        )

    def hire_employees(self, count: int) -> str:
        count = max(0, int(count))
        if count <= 0:
            return "Hire count must be positive."
        self.company.employees += count
        self._log(f"Hired {count} employees. Team size is now {self.company.employees}.")
        return f"Hired {count} employees. Team size is now {self.company.employees}."

    def fire_employees(self, count: int) -> str:
        count = max(0, int(count))
        if count <= 0:
            return "Fire count must be positive."
        if self.company.employees <= 0:
            return "You do not have any employees to fire."
        actual = min(count, self.company.employees)
        self.company.employees -= actual
        self._log(f"Reduced staff by {actual}. Team size is now {self.company.employees}.")
        return f"Reduced staff by {actual}. Team size is now {self.company.employees}."

    def generate_employee_plan(self) -> Dict[str, Any]:
        skill = self.team_quality
        depth = self.analysis_depth
        buy_slots = {1: 1, 2: 2, 3: 3, 4: 4}[depth]
        cash_buffer = self.net_worth * (0.05 - 0.015 * skill)
        investable_cash = max(0.0, min(self.company.cash * (0.10 + 0.20 * skill), self.company.cash - cash_buffer))
        buy_threshold = 0.05 - 0.015 * skill
        sell_threshold = -0.05 + 0.015 * skill

        coverage: List[Tuple[str, Dict[str, Any]]] = []
        for ticker in self.stocks:
            analysis = self.analysis_snapshot(ticker)
            coverage.append((ticker, analysis))

        coverage.sort(key=lambda pair: (pair[1]["estimated_12m_return"] * pair[1]["conviction"], pair[1]["score"]), reverse=True)

        entries: List[Dict[str, Any]] = []
        narrative: List[str] = []

        # Sells first
        for ticker, analysis in coverage:
            position = self.portfolio[ticker]
            if position.shares <= 0:
                continue
            if analysis["estimated_12m_return"] < sell_threshold or analysis["recommendation"] in {"Trim", "Sell"}:
                if analysis["recommendation"] == "Trim":
                    ratio = 0.30 + 0.25 * analysis["conviction"]
                elif analysis["recommendation"] == "Sell":
                    ratio = 0.65 + 0.20 * analysis["conviction"]
                else:
                    ratio = 0.45
                shares = max(1, int(position.shares * clamp(ratio, 0.20, 1.0)))
                entries.append(
                    {
                        "action": "SELL",
                        "ticker": ticker,
                        "shares": shares,
                        "confidence": analysis["conviction"],
                        "reason": "; ".join(analysis["reasons"][:2]),
                        "recommendation": analysis["recommendation"],
                    }
                )

        # Buys
        buy_candidates = [
            (ticker, analysis)
            for ticker, analysis in coverage
            if analysis["estimated_12m_return"] > buy_threshold and analysis["recommendation"] in {"Strong Buy", "Buy"}
        ]
        selected_buys = buy_candidates[:buy_slots]
        if selected_buys and investable_cash > 1_000_000:
            weights = [max(0.01, a["estimated_12m_return"] * max(0.15, a["conviction"])) for _, a in selected_buys]
            total_weight = sum(weights)
            for (ticker, analysis), weight in zip(selected_buys, weights):
                allocation = investable_cash * (weight / total_weight)
                allocation = clamp(allocation, 2_500_000.0, max(2_500_000.0, self.company.cash * 0.18))
                entries.append(
                    {
                        "action": "BUY",
                        "ticker": ticker,
                        "amount": allocation,
                        "confidence": analysis["conviction"],
                        "reason": "; ".join(analysis["reasons"][:2]),
                        "recommendation": analysis["recommendation"],
                    }
                )

        narrative.append(f"Research quality: {self.analysis_depth_label} ({round(skill * 100):d}/100).")
        narrative.append(f"Employees: {self.company.employees} | Monthly payroll: {format_money(self.company.payroll_cost)}.")
        if entries:
            narrative.append(f"The team produced {len(entries)} suggested trade(s). Better staffing reduces noise and improves sizing.")
        else:
            narrative.append("The team does not see a strong enough edge right now and prefers patience.")

        self.last_employee_plan = entries
        return {"entries": entries, "narrative": narrative, "quality": skill}

    def execute_employee_plan(self, plan: Optional[Dict[str, Any]] = None) -> List[str]:
        if plan is None:
            plan = self.generate_employee_plan()
        entries = plan.get("entries", [])
        results: List[str] = []
        if not entries:
            self._log("Employee team held position and made no trade.")
            return ["Employee team held position and made no trade."]
        # sell first
        for entry in entries:
            if entry.get("action") == "SELL":
                results.append(self.sell_stock(str(entry["ticker"]), int(entry["shares"]), source="Employee Team"))
        for entry in entries:
            if entry.get("action") == "BUY":
                results.append(self.buy_stock(str(entry["ticker"]), float(entry["amount"]), source="Employee Team"))
        self._log(f"Executed employee trading plan with {len(entries)} action(s).")
        return results

    def overview_text(self) -> str:
        gainers, losers = self.top_movers(3)
        lines = []
        lines.append(f"{self.company.name} — CEO & 100% owner")
        lines.append(f"Year {self.year}, Month {self.month}")
        lines.append("")
        lines.append(f"Market regime: {self.market_regime}")
        lines.append(str(self.market_note))
        lines.append(f"Macro note: {self.macro_event_note}")
        lines.append("")
        lines.append(f"Cash: {format_money(self.company.cash)}")
        lines.append(f"Portfolio value: {format_money(self.portfolio_value)}")
        lines.append(f"Nominal net worth: {format_money(self.net_worth)}")
        lines.append(f"Real net worth: {format_money(self.real_net_worth)}")
        lines.append(f"Inflation index: {self.inflation_index:.3f}x")
        lines.append("")
        lines.append(f"Monthly revenue: {format_money(self.company.monthly_revenue)}")
        lines.append(f"Base operating costs: {format_money(self.company.monthly_base_costs)}")
        lines.append(f"Payroll cost: {format_money(self.company.payroll_cost)}")
        lines.append(f"Pre-tax profit: {format_money(self.company.last_pre_tax_profit)}")
        lines.append(f"Tax paid: {format_money(self.company.last_tax_paid)}")
        lines.append(f"Net profit: {format_money(self.company.last_net_profit)}")
        lines.append(f"Cash yield earned: {format_money(self.company.last_cash_interest)}")
        lines.append(f"Dividends received: {format_money(self.company.last_dividends)}")
        lines.append("")
        lines.append(f"Research depth: {self.analysis_depth_label}")
        lines.append(f"Research quality score: {round(self.team_quality * 100):d}/100")
        lines.append("")
        if gainers:
            lines.append("Top gainers this month:")
            for ticker, change in gainers:
                lines.append(f"  {ticker}: {format_pct(change)}")
        if losers:
            lines.append("Top losers this month:")
            for ticker, change in losers:
                lines.append(f"  {ticker}: {format_pct(change)}")
        return "\n".join(lines)

    def recent_trade_rows(self, limit: int = 25) -> List[Tuple[Any, ...]]:
        rows = []
        for entry in self.trade_history[-limit:][::-1]:
            realized = entry.get("realized_gain")
            rows.append(
                (
                    f"Y{entry.get('year', '?')} M{entry.get('month', '?')}",
                    entry.get("source", ""),
                    entry.get("action", ""),
                    entry.get("ticker", ""),
                    entry.get("shares", 0),
                    entry.get("price", 0.0),
                    entry.get("gross_amount", 0.0),
                    realized if realized is not None else "",
                )
            )
        return rows

    def to_dict(self) -> Dict[str, Any]:
        return {
            "save_version": SAVE_VERSION,
            "saved_at": now_iso(),
            "seed": self.seed,
            "total_months": self.total_months,
            "inflation_index": self.inflation_index,
            "market_regime": self.market_regime,
            "market_note": self.market_note,
            "macro_event_note": self.macro_event_note,
            "last_market_factor": self.last_market_factor,
            "last_portfolio_return": self.last_portfolio_return,
            "auto_team_trading": self.auto_team_trading,
            "selected_ticker": self.selected_ticker,
            "company": self.company.to_dict(),
            "stocks": {ticker: stock.to_dict() for ticker, stock in self.stocks.items()},
            "portfolio": {ticker: pos.to_dict() for ticker, pos in self.portfolio.items()},
            "net_worth_history": list(self.net_worth_history),
            "real_net_worth_history": list(self.real_net_worth_history),
            "trade_history": list(self.trade_history),
            "activity_log": list(self.activity_log),
            "snapshot_history": list(self.snapshot_history),
            "last_employee_plan": list(self.last_employee_plan),
            "rng_state": encode_rng_state(self.rng.getstate()),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "InvestmentGame":
        company_data = data.get("company", {})
        stocks_data = data.get("stocks", {})
        company_name = str(company_data.get("name", "Titan Holdings"))
        employees = safe_int(company_data.get("employees", 15), 15)
        stock_count = max(4, len(stocks_data) or DEFAULT_STOCK_COUNT)
        game = cls(company_name=company_name, employees=employees, seed=data.get("seed"), stock_count=stock_count)

        game.total_months = safe_int(data.get("total_months", 0), 0)
        game.inflation_index = max(1e-9, safe_float(data.get("inflation_index", 1.0), 1.0))
        game.market_regime = str(data.get("market_regime", "Expansion"))
        if game.market_regime not in REGIMES:
            game.market_regime = "Expansion"
        game.market_note = str(data.get("market_note", REGIMES[game.market_regime]["note"]))
        game.macro_event_note = str(data.get("macro_event_note", "No major macro event this month."))
        game.last_market_factor = safe_float(data.get("last_market_factor", 0.0), 0.0)
        game.last_portfolio_return = safe_float(data.get("last_portfolio_return", 0.0), 0.0)
        game.auto_team_trading = bool(data.get("auto_team_trading", False))
        game.selected_ticker = str(data.get("selected_ticker", "")) or game.selected_ticker

        game.company = Company.from_dict(company_data)

        loaded_stocks = {}
        for ticker, stock_data in stocks_data.items():
            loaded_stocks[str(ticker)] = Stock.from_dict(stock_data)
        if loaded_stocks:
            game.stocks = loaded_stocks
        if not game.selected_ticker and game.stocks:
            game.selected_ticker = sorted(game.stocks)[0]

        loaded_portfolio: Dict[str, Position] = {ticker: Position() for ticker in game.stocks}
        for ticker, pos_data in data.get("portfolio", {}).items():
            if ticker in loaded_portfolio:
                loaded_portfolio[ticker] = Position.from_dict(pos_data)
        game.portfolio = loaded_portfolio

        game.net_worth_history = [safe_float(x, game.net_worth) for x in data.get("net_worth_history", [])] or [game.net_worth]
        game.real_net_worth_history = [safe_float(x, game.real_net_worth) for x in data.get("real_net_worth_history", [])] or [game.real_net_worth]
        game.trade_history = list(data.get("trade_history", []))
        game.activity_log = [str(x) for x in data.get("activity_log", [])][-MAX_ACTIVITY_LOG:]
        game.snapshot_history = list(data.get("snapshot_history", []))[-MAX_SNAPSHOTS:]
        game.last_employee_plan = list(data.get("last_employee_plan", []))

        rng_state = data.get("rng_state")
        if isinstance(rng_state, str) and rng_state:
            try:
                game.rng.setstate(decode_rng_state(rng_state))
            except Exception:
                pass

        if not game.snapshot_history:
            game._snapshot()
        return game

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "InvestmentGame":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    def print_summary(self) -> None:
        print("=" * 100)
        print(self.overview_text())
        print("=" * 100)
        print("Top market ideas:")
        for row in self.market_rows()[:8]:
            print(
                f"{row[0]:<5}  {row[1][:22]:<22}  Price {format_money(row[3]):>10}  "
                f"Est 12m gap {format_pct(row[6]):>9}  {row[9]}"
            )
        print("=" * 100)


class SetupDialog:
    def __init__(self, parent: "tk.Tk", title: str, defaults: Dict[str, Any]) -> None:
        self.parent = parent
        self.result: Optional[Dict[str, Any]] = None
        self.window = tk.Toplevel(parent)
        self.window.title(title)
        self.window.transient(parent)
        self.window.grab_set()
        self.window.resizable(False, False)

        outer = ttk.Frame(self.window, padding=16)
        outer.grid(row=0, column=0, sticky="nsew")

        ttk.Label(outer, text="Company name").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.company_var = tk.StringVar(value=str(defaults.get("company_name", "Titan Holdings")))
        ttk.Entry(outer, textvariable=self.company_var, width=32).grid(row=1, column=0, sticky="ew", pady=(0, 10))

        ttk.Label(outer, text="Starting employees").grid(row=2, column=0, sticky="w", pady=(0, 6))
        self.employees_var = tk.StringVar(value=str(defaults.get("employees", 15)))
        ttk.Entry(outer, textvariable=self.employees_var, width=12).grid(row=3, column=0, sticky="w", pady=(0, 10))

        ttk.Label(outer, text="Number of stocks in market").grid(row=4, column=0, sticky="w", pady=(0, 6))
        self.stocks_var = tk.StringVar(value=str(defaults.get("stocks", DEFAULT_STOCK_COUNT)))
        ttk.Entry(outer, textvariable=self.stocks_var, width=12).grid(row=5, column=0, sticky="w", pady=(0, 10))

        note = (
            "More employees give you deeper and more accurate analysis, but each one costs "
            "$3,000 per month."
        )
        ttk.Label(outer, text=note, wraplength=320, justify="left").grid(row=6, column=0, sticky="w", pady=(0, 14))

        button_row = ttk.Frame(outer)
        button_row.grid(row=7, column=0, sticky="e")
        ttk.Button(button_row, text="Cancel", command=self.cancel).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(button_row, text="Start", command=self.submit).grid(row=0, column=1)

        outer.columnconfigure(0, weight=1)
        self.window.bind("<Return>", lambda event: self.submit())
        self.window.bind("<Escape>", lambda event: self.cancel())

        self.window.update_idletasks()
        x = max(40, parent.winfo_rootx() + 60)
        y = max(40, parent.winfo_rooty() + 60)
        self.window.geometry(f"+{x}+{y}")

    def submit(self) -> None:
        company_name = self.company_var.get().strip() or "Titan Holdings"
        try:
            employees = max(0, int(self.employees_var.get().strip()))
            stocks = max(8, min(30, int(self.stocks_var.get().strip())))
        except ValueError:
            messagebox.showerror("Invalid input", "Employees and stock count must be whole numbers.")
            return
        self.result = {
            "company_name": company_name,
            "employees": employees,
            "stocks": stocks,
        }
        self.window.destroy()

    def cancel(self) -> None:
        self.result = None
        self.window.destroy()


class InvestmentGameApp:
    def __init__(self, root: "tk.Tk", game: InvestmentGame, save_path: Path, args: argparse.Namespace) -> None:
        self.root = root
        self.game = game
        self.save_path = save_path
        self.args = args
        self.status_var = tk.StringVar(value="Ready.")
        self.selected_market_ticker = tk.StringVar(value=self.game.selected_ticker or (sorted(self.game.stocks)[0] if self.game.stocks else ""))
        self.trade_ticker_var = tk.StringVar(value=self.selected_market_ticker.get())
        self.buy_amount_var = tk.StringVar(value="50m")
        self.sell_shares_var = tk.StringVar(value="10000")
        self.auto_team_var = tk.BooleanVar(value=self.game.auto_team_trading)

        self.root.title("CEO Investing Game - Visual Strategy Edition")
        self.root.geometry("1450x950")
        self.root.minsize(1180, 820)

        self._build_styles()
        self._build_menu()
        self._build_layout()
        self._bind_shortcuts()
        self.refresh_all()
        self.root.after(200, self._redraw_all_charts)

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Card.TFrame", padding=10)
        style.configure("CardHeader.TLabel", font=("TkDefaultFont", 10, "bold"))
        style.configure("BigValue.TLabel", font=("TkDefaultFont", 14, "bold"))
        style.configure("Section.TLabelframe", padding=10)
        style.configure("Section.TLabelframe.Label", font=("TkDefaultFont", 10, "bold"))
        style.configure("Primary.TButton", padding=(10, 6))
        style.configure("Treeview", rowheight=24)

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="Save now", command=self.save_now, accelerator="Ctrl+S")
        file_menu.add_command(label="Reload save", command=self.reload_save, accelerator="Ctrl+L")
        file_menu.add_separator()
        file_menu.add_command(label="New game", command=self.new_game_dialog, accelerator="Ctrl+N")
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.destroy)
        menu.add_cascade(label="File", menu=file_menu)

        time_menu = tk.Menu(menu, tearoff=False)
        time_menu.add_command(label="Advance 1 month", command=lambda: self.advance_months(1), accelerator="Space")
        time_menu.add_command(label="Advance 3 months", command=lambda: self.advance_months(3))
        time_menu.add_command(label="Advance 12 months", command=lambda: self.advance_months(12))
        menu.add_cascade(label="Time", menu=time_menu)

        team_menu = tk.Menu(menu, tearoff=False)
        team_menu.add_command(label="Generate employee plan", command=self.generate_team_plan, accelerator="Ctrl+G")
        team_menu.add_command(label="Execute employee plan", command=self.execute_team_plan, accelerator="Ctrl+E")
        team_menu.add_checkbutton(label="Auto team trading each month", variable=self.auto_team_var, command=self.toggle_auto_team)
        menu.add_cascade(label="Team", menu=team_menu)

        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="About", command=self.show_about)
        menu.add_cascade(label="Help", menu=help_menu)
        self.root.config(menu=menu)

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Control-s>", lambda event: self.save_now())
        self.root.bind("<Control-l>", lambda event: self.reload_save())
        self.root.bind("<Control-n>", lambda event: self.new_game_dialog())
        self.root.bind("<Control-g>", lambda event: self.generate_team_plan())
        self.root.bind("<Control-e>", lambda event: self.execute_team_plan())
        self.root.bind("<space>", lambda event: self.advance_months(1))

    def _build_layout(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        self.summary_frame = ttk.Frame(outer)
        self.summary_frame.pack(fill="x", pady=(0, 10))
        self.summary_labels: Dict[str, ttk.Label] = {}
        summary_specs = [
            ("cash", "Cash"),
            ("portfolio", "Portfolio"),
            ("net_worth", "Nominal worth"),
            ("real_worth", "Real worth"),
            ("employees", "Employees"),
            ("payroll", "Payroll / month"),
            ("turn", "Time"),
            ("regime", "Market regime"),
        ]
        for idx, (key, title) in enumerate(summary_specs):
            card = ttk.Frame(self.summary_frame, style="Card.TFrame", relief="ridge", borderwidth=1)
            card.grid(row=0, column=idx, padx=4, sticky="nsew")
            ttk.Label(card, text=title, style="CardHeader.TLabel").pack(anchor="w")
            label = ttk.Label(card, text="-", style="BigValue.TLabel")
            label.pack(anchor="w", pady=(6, 0))
            self.summary_labels[key] = label
            self.summary_frame.columnconfigure(idx, weight=1)

        action_bar = ttk.Frame(outer)
        action_bar.pack(fill="x", pady=(0, 10))
        ttk.Button(action_bar, text="Advance 1 month", style="Primary.TButton", command=lambda: self.advance_months(1)).pack(side="left")
        ttk.Button(action_bar, text="Advance 3 months", style="Primary.TButton", command=lambda: self.advance_months(3)).pack(side="left", padx=(6, 0))
        ttk.Button(action_bar, text="Advance 12 months", style="Primary.TButton", command=lambda: self.advance_months(12)).pack(side="left", padx=(6, 0))
        ttk.Button(action_bar, text="Save now", command=self.save_now).pack(side="left", padx=(12, 0))
        ttk.Button(action_bar, text="Generate team plan", command=self.generate_team_plan).pack(side="left", padx=(12, 0))
        ttk.Button(action_bar, text="Execute team plan", command=self.execute_team_plan).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(action_bar, text="Auto team trading", variable=self.auto_team_var, command=self.toggle_auto_team).pack(side="left", padx=(12, 0))

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)

        self.dashboard_tab = ttk.Frame(self.notebook, padding=8)
        self.market_tab = ttk.Frame(self.notebook, padding=8)
        self.portfolio_tab = ttk.Frame(self.notebook, padding=8)
        self.employees_tab = ttk.Frame(self.notebook, padding=8)
        self.history_tab = ttk.Frame(self.notebook, padding=8)

        self.notebook.add(self.dashboard_tab, text="Dashboard")
        self.notebook.add(self.market_tab, text="Market")
        self.notebook.add(self.portfolio_tab, text="Portfolio")
        self.notebook.add(self.employees_tab, text="Employees")
        self.notebook.add(self.history_tab, text="History")

        self._build_dashboard_tab()
        self._build_market_tab()
        self._build_portfolio_tab()
        self._build_employees_tab()
        self._build_history_tab()

        status_frame = ttk.Frame(outer)
        status_frame.pack(fill="x", pady=(8, 0))
        ttk.Label(status_frame, textvariable=self.status_var).pack(side="left")

    def _build_dashboard_tab(self) -> None:
        self.dashboard_tab.columnconfigure(0, weight=1)
        self.dashboard_tab.columnconfigure(1, weight=1)
        self.dashboard_tab.columnconfigure(2, weight=0)
        self.dashboard_tab.rowconfigure(0, weight=1)
        self.dashboard_tab.rowconfigure(1, weight=1)

        overview_frame = ttk.LabelFrame(self.dashboard_tab, text="Company overview", style="Section.TLabelframe")
        overview_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))
        self.overview_text_widget = scrolledtext.ScrolledText(overview_frame, wrap="word", height=18)
        self.overview_text_widget.pack(fill="both", expand=True)

        chart_frame = ttk.LabelFrame(self.dashboard_tab, text="Selected stock chart", style="Section.TLabelframe")
        chart_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        self.stock_chart_canvas = tk.Canvas(chart_frame, height=260, background="#ffffff", highlightthickness=1, highlightbackground="#cccccc")
        self.stock_chart_canvas.pack(fill="both", expand=True)

        activity_frame = ttk.LabelFrame(self.dashboard_tab, text="Activity log", style="Section.TLabelframe")
        activity_frame.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(0, 8))
        self.activity_text_widget = scrolledtext.ScrolledText(activity_frame, wrap="word", height=18)
        self.activity_text_widget.pack(fill="both", expand=True)

        trade_frame = ttk.LabelFrame(self.dashboard_tab, text="Trade desk", style="Section.TLabelframe")
        trade_frame.grid(row=0, column=2, rowspan=2, sticky="ns")
        ttk.Label(trade_frame, text="Ticker").grid(row=0, column=0, sticky="w")
        self.trade_ticker_combo = ttk.Combobox(
            trade_frame,
            textvariable=self.trade_ticker_var,
            values=sorted(self.game.stocks.keys()),
            width=12,
            state="readonly",
        )
        self.trade_ticker_combo.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.trade_ticker_combo.bind("<<ComboboxSelected>>", lambda event: self.on_trade_ticker_change())

        ttk.Label(trade_frame, text="Buy amount").grid(row=2, column=0, sticky="w")
        ttk.Entry(trade_frame, textvariable=self.buy_amount_var, width=16).grid(row=3, column=0, sticky="ew", pady=(0, 10))
        ttk.Button(trade_frame, text="Buy by amount", command=self.buy_selected_stock).grid(row=4, column=0, sticky="ew", pady=(0, 12))

        ttk.Label(trade_frame, text="Sell shares").grid(row=5, column=0, sticky="w")
        ttk.Entry(trade_frame, textvariable=self.sell_shares_var, width=16).grid(row=6, column=0, sticky="ew", pady=(0, 10))
        ttk.Button(trade_frame, text="Sell shares", command=self.sell_selected_stock).grid(row=7, column=0, sticky="ew")
        ttk.Button(trade_frame, text="Sell all", command=self.sell_all_selected_stock).grid(row=8, column=0, sticky="ew", pady=(6, 12))

        ttk.Separator(trade_frame).grid(row=9, column=0, sticky="ew", pady=10)
        ttk.Button(trade_frame, text="Show research", command=self.sync_research_from_trade).grid(row=10, column=0, sticky="ew")
        ttk.Button(trade_frame, text="Save now", command=self.save_now).grid(row=11, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(trade_frame, text="Reload save", command=self.reload_save).grid(row=12, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(trade_frame, text="New game", command=self.new_game_dialog).grid(row=13, column=0, sticky="ew", pady=(6, 0))
        trade_frame.columnconfigure(0, weight=1)

    def _build_market_tab(self) -> None:
        self.market_tab.columnconfigure(0, weight=1)
        self.market_tab.columnconfigure(1, weight=1)
        self.market_tab.rowconfigure(0, weight=1)

        tree_frame = ttk.LabelFrame(self.market_tab, text="Market board", style="Section.TLabelframe")
        tree_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        columns = ("Ticker", "Name", "Sector", "Price", "1M", "Team Fair", "Gap", "Yield", "Score", "Call")
        self.market_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=20)
        widths = {"Ticker": 70, "Name": 200, "Sector": 95, "Price": 95, "1M": 75, "Team Fair": 95, "Gap": 75, "Yield": 75, "Score": 65, "Call": 90}
        anchors = {"Ticker": "center", "Score": "center", "Call": "center"}
        for col in columns:
            self.market_tree.heading(col, text=col)
            self.market_tree.column(col, width=widths.get(col, 100), anchor=anchors.get(col, "e" if col not in {"Name", "Sector"} else "w"))
        market_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.market_tree.yview)
        self.market_tree.configure(yscrollcommand=market_scroll.set)
        self.market_tree.grid(row=0, column=0, sticky="nsew")
        market_scroll.grid(row=0, column=1, sticky="ns")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.market_tree.bind("<<TreeviewSelect>>", lambda event: self.on_market_select())
        self.market_tree.bind("<Double-1>", lambda event: self.copy_selected_market_to_trade())

        detail_frame = ttk.LabelFrame(self.market_tab, text="Research", style="Section.TLabelframe")
        detail_frame.grid(row=0, column=1, sticky="nsew")
        self.research_text_widget = scrolledtext.ScrolledText(detail_frame, wrap="word", height=22)
        self.research_text_widget.pack(fill="both", expand=True)

    def _build_portfolio_tab(self) -> None:
        self.portfolio_tab.columnconfigure(0, weight=1)
        self.portfolio_tab.columnconfigure(1, weight=1)
        self.portfolio_tab.rowconfigure(0, weight=1)

        holdings_frame = ttk.LabelFrame(self.portfolio_tab, text="Holdings", style="Section.TLabelframe")
        holdings_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        holding_cols = ("Ticker", "Shares", "Avg Cost", "Market Value", "Unrealized", "Alloc", "Call")
        self.holdings_tree = ttk.Treeview(holdings_frame, columns=holding_cols, show="headings", height=18)
        holding_widths = {"Ticker": 70, "Shares": 90, "Avg Cost": 95, "Market Value": 110, "Unrealized": 105, "Alloc": 70, "Call": 90}
        for col in holding_cols:
            self.holdings_tree.heading(col, text=col)
            self.holdings_tree.column(col, width=holding_widths.get(col, 100), anchor="e" if col not in {"Ticker", "Call"} else "center")
        holding_scroll = ttk.Scrollbar(holdings_frame, orient="vertical", command=self.holdings_tree.yview)
        self.holdings_tree.configure(yscrollcommand=holding_scroll.set)
        self.holdings_tree.grid(row=0, column=0, sticky="nsew")
        holding_scroll.grid(row=0, column=1, sticky="ns")
        holdings_frame.rowconfigure(0, weight=1)
        holdings_frame.columnconfigure(0, weight=1)
        self.holdings_tree.bind("<Double-1>", lambda event: self.on_holding_double_click())

        trades_frame = ttk.LabelFrame(self.portfolio_tab, text="Recent trades", style="Section.TLabelframe")
        trades_frame.grid(row=0, column=1, sticky="nsew")
        trade_cols = ("Time", "Source", "Action", "Ticker", "Shares", "Price", "Gross", "Realized")
        self.trades_tree = ttk.Treeview(trades_frame, columns=trade_cols, show="headings", height=18)
        trade_widths = {"Time": 90, "Source": 120, "Action": 70, "Ticker": 70, "Shares": 85, "Price": 90, "Gross": 105, "Realized": 105}
        for col in trade_cols:
            self.trades_tree.heading(col, text=col)
            self.trades_tree.column(col, width=trade_widths.get(col, 100), anchor="center" if col in {"Time", "Source", "Action", "Ticker"} else "e")
        trades_scroll = ttk.Scrollbar(trades_frame, orient="vertical", command=self.trades_tree.yview)
        self.trades_tree.configure(yscrollcommand=trades_scroll.set)
        self.trades_tree.grid(row=0, column=0, sticky="nsew")
        trades_scroll.grid(row=0, column=1, sticky="ns")
        trades_frame.rowconfigure(0, weight=1)
        trades_frame.columnconfigure(0, weight=1)

    def _build_employees_tab(self) -> None:
        self.employees_tab.columnconfigure(0, weight=0)
        self.employees_tab.columnconfigure(1, weight=1)
        self.employees_tab.rowconfigure(0, weight=1)

        left = ttk.LabelFrame(self.employees_tab, text="Workforce controls", style="Section.TLabelframe")
        left.grid(row=0, column=0, sticky="ns", padx=(0, 8))
        self.employee_stats_text = tk.StringVar(value="")
        ttk.Label(left, textvariable=self.employee_stats_text, justify="left").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        ttk.Label(left, text="Quality").grid(row=1, column=0, sticky="w")
        self.quality_bar = ttk.Progressbar(left, orient="horizontal", length=220, mode="determinate", maximum=100)
        self.quality_bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        ttk.Button(left, text="Hire 1", command=lambda: self.change_staff(1)).grid(row=3, column=0, sticky="ew")
        ttk.Button(left, text="Hire 5", command=lambda: self.change_staff(5)).grid(row=3, column=1, sticky="ew", padx=(6, 0))
        ttk.Button(left, text="Fire 1", command=lambda: self.change_staff(-1)).grid(row=4, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(left, text="Fire 5", command=lambda: self.change_staff(-5)).grid(row=4, column=1, sticky="ew", padx=(6, 0), pady=(6, 0))
        ttk.Separator(left).grid(row=5, column=0, columnspan=2, sticky="ew", pady=12)
        ttk.Checkbutton(left, text="Auto team trading", variable=self.auto_team_var, command=self.toggle_auto_team).grid(row=6, column=0, columnspan=2, sticky="w")
        ttk.Button(left, text="Generate team plan", command=self.generate_team_plan).grid(row=7, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(left, text="Execute team plan", command=self.execute_team_plan).grid(row=8, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        left.columnconfigure(0, weight=1)
        left.columnconfigure(1, weight=1)

        right = ttk.LabelFrame(self.employees_tab, text="Team plan", style="Section.TLabelframe")
        right.grid(row=0, column=1, sticky="nsew")
        self.team_plan_text_widget = scrolledtext.ScrolledText(right, wrap="word", height=24)
        self.team_plan_text_widget.pack(fill="both", expand=True)

    def _build_history_tab(self) -> None:
        self.history_tab.columnconfigure(0, weight=1)
        self.history_tab.columnconfigure(1, weight=1)
        self.history_tab.rowconfigure(0, weight=1)

        chart_frame = ttk.LabelFrame(self.history_tab, text="Net worth chart", style="Section.TLabelframe")
        chart_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.history_chart_canvas = tk.Canvas(chart_frame, height=360, background="#ffffff", highlightthickness=1, highlightbackground="#cccccc")
        self.history_chart_canvas.pack(fill="both", expand=True)

        snapshot_frame = ttk.LabelFrame(self.history_tab, text="Monthly snapshots", style="Section.TLabelframe")
        snapshot_frame.grid(row=0, column=1, sticky="nsew")
        snap_cols = ("Time", "Cash", "Portfolio", "Net", "Real", "Regime", "Employees")
        self.snapshot_tree = ttk.Treeview(snapshot_frame, columns=snap_cols, show="headings", height=18)
        snap_widths = {"Time": 90, "Cash": 95, "Portfolio": 95, "Net": 95, "Real": 95, "Regime": 95, "Employees": 80}
        for col in snap_cols:
            self.snapshot_tree.heading(col, text=col)
            self.snapshot_tree.column(col, width=snap_widths.get(col, 95), anchor="center" if col in {"Time", "Regime", "Employees"} else "e")
        snap_scroll = ttk.Scrollbar(snapshot_frame, orient="vertical", command=self.snapshot_tree.yview)
        self.snapshot_tree.configure(yscrollcommand=snap_scroll.set)
        self.snapshot_tree.grid(row=0, column=0, sticky="nsew")
        snap_scroll.grid(row=0, column=1, sticky="ns")
        snapshot_frame.rowconfigure(0, weight=1)
        snapshot_frame.columnconfigure(0, weight=1)

    def show_about(self) -> None:
        message = (
            "CEO Investing Game - Visual Strategy Edition\n\n"
            "You are the CEO and 100% owner of a company with $5 billion in starting cash.\n"
            "Inflation is 10% annually. Company operating profit is taxed at 10%.\n"
            "Employees cost $3,000 per month each and improve your research and team trading plans.\n"
            "Idle cash earns a modest treasury-style yield to make the game a bit friendlier."
        )
        messagebox.showinfo("About", message)

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def save_now(self) -> None:
        try:
            self.game.save(self.save_path)
            self._set_status(f"Saved to {self.save_path.name}.")
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))
            self._set_status("Save failed.")

    def reload_save(self) -> None:
        if not self.save_path.exists():
            messagebox.showinfo("Reload save", "No save file exists yet.")
            return
        try:
            self.game = InvestmentGame.load(self.save_path)
            self.auto_team_var.set(self.game.auto_team_trading)
            self.trade_ticker_var.set(self.game.selected_ticker or self.trade_ticker_var.get())
            self.selected_market_ticker.set(self.game.selected_ticker or self.selected_market_ticker.get())
            self.refresh_all()
            self._set_status(f"Reloaded {self.save_path.name}.")
        except Exception as exc:
            messagebox.showerror("Reload failed", str(exc))
            self._set_status("Reload failed.")

    def new_game_dialog(self) -> None:
        dialog = SetupDialog(
            self.root,
            "Start new company",
            {
                "company_name": self.args.company or "Titan Holdings",
                "employees": self.args.employees,
                "stocks": self.args.stocks,
            },
        )
        self.root.wait_window(dialog.window)
        if not dialog.result:
            return
        result = dialog.result
        self.game = InvestmentGame(
            company_name=result["company_name"],
            employees=result["employees"],
            stock_count=result["stocks"],
            seed=self.args.seed,
        )
        self.auto_team_var.set(self.game.auto_team_trading)
        self.trade_ticker_var.set(self.game.selected_ticker)
        self.selected_market_ticker.set(self.game.selected_ticker)
        self.refresh_all()
        self.save_now()
        self._set_status("Started a new company.")

    def toggle_auto_team(self) -> None:
        self.game.auto_team_trading = bool(self.auto_team_var.get())
        self.save_now()
        self.refresh_employee_tab()
        state = "enabled" if self.game.auto_team_trading else "disabled"
        self._set_status(f"Auto team trading {state}.")

    def on_trade_ticker_change(self) -> None:
        ticker = self.trade_ticker_var.get().strip().upper()
        if ticker in self.game.stocks:
            self.game.selected_ticker = ticker
            self.selected_market_ticker.set(ticker)
            self.refresh_research_panel()
            self.draw_selected_stock_chart()
            self._select_market_row(ticker)

    def copy_selected_market_to_trade(self) -> None:
        selection = self.market_tree.selection()
        if not selection:
            return
        ticker = self.market_tree.item(selection[0], "values")[0]
        self.trade_ticker_var.set(ticker)
        self.selected_market_ticker.set(ticker)
        self.game.selected_ticker = ticker
        self.refresh_research_panel()
        self.draw_selected_stock_chart()
        self.notebook.select(self.dashboard_tab)

    def on_market_select(self) -> None:
        selection = self.market_tree.selection()
        if not selection:
            return
        ticker = self.market_tree.item(selection[0], "values")[0]
        self.selected_market_ticker.set(ticker)
        self.trade_ticker_var.set(ticker)
        self.game.selected_ticker = ticker
        self.refresh_research_panel()
        self.draw_selected_stock_chart()

    def on_holding_double_click(self) -> None:
        selection = self.holdings_tree.selection()
        if not selection:
            return
        ticker = self.holdings_tree.item(selection[0], "values")[0]
        self.trade_ticker_var.set(ticker)
        self.selected_market_ticker.set(ticker)
        self.game.selected_ticker = ticker
        self.refresh_research_panel()
        self.draw_selected_stock_chart()
        self.notebook.select(self.market_tab)
        self._select_market_row(ticker)

    def sync_research_from_trade(self) -> None:
        ticker = self.trade_ticker_var.get().strip().upper()
        if ticker in self.game.stocks:
            self.game.selected_ticker = ticker
            self.selected_market_ticker.set(ticker)
            self.refresh_research_panel()
            self.draw_selected_stock_chart()
            self._select_market_row(ticker)
            self.notebook.select(self.market_tab)

    def _select_market_row(self, ticker: str) -> None:
        for item in self.market_tree.get_children():
            if self.market_tree.item(item, "values")[0] == ticker:
                self.market_tree.selection_set(item)
                self.market_tree.focus(item)
                self.market_tree.see(item)
                return

    def buy_selected_stock(self) -> None:
        ticker = self.trade_ticker_var.get().strip().upper()
        try:
            amount = parse_money(self.buy_amount_var.get())
        except ValueError:
            messagebox.showerror("Invalid amount", "Enter a valid dollar amount like 25m or 5000000.")
            return
        message = self.game.buy_stock(ticker, amount)
        self.save_now()
        self.refresh_all()
        self._set_status(message)

    def sell_selected_stock(self) -> None:
        ticker = self.trade_ticker_var.get().strip().upper()
        try:
            shares = int(self.sell_shares_var.get().replace(",", "").strip())
        except ValueError:
            messagebox.showerror("Invalid shares", "Enter a whole number of shares to sell.")
            return
        message = self.game.sell_stock(ticker, shares)
        self.save_now()
        self.refresh_all()
        self._set_status(message)

    def sell_all_selected_stock(self) -> None:
        ticker = self.trade_ticker_var.get().strip().upper()
        if ticker not in self.game.portfolio or self.game.portfolio[ticker].shares <= 0:
            self._set_status(f"You do not own any shares of {ticker}.")
            return
        shares = self.game.portfolio[ticker].shares
        message = self.game.sell_stock(ticker, shares)
        self.save_now()
        self.refresh_all()
        self._set_status(message)

    def change_staff(self, delta: int) -> None:
        message = self.game.hire_employees(delta) if delta > 0 else self.game.fire_employees(-delta)
        self.save_now()
        self.refresh_all()
        self._set_status(message)

    def generate_team_plan(self) -> None:
        plan = self.game.generate_employee_plan()
        self.refresh_employee_tab(plan)
        self._set_status("Generated a fresh employee trading plan.")

    def execute_team_plan(self) -> None:
        plan = self.game.generate_employee_plan()
        results = self.game.execute_employee_plan(plan)
        self.save_now()
        self.refresh_all(plan)
        self._set_status(" | ".join(results[:2]) if results else "Team plan executed.")

    def advance_months(self, months: int) -> None:
        months = max(1, int(months))
        result_messages: List[str] = []
        for _ in range(months):
            result_messages.extend(self.game.advance_month())
        self.save_now()
        self.refresh_all()
        if result_messages:
            self._set_status(" | ".join(result_messages[:2]))
        else:
            self._set_status(f"Advanced {months} month(s).")

    def refresh_all(self, current_plan: Optional[Dict[str, Any]] = None) -> None:
        self.trade_ticker_combo.configure(values=sorted(self.game.stocks.keys()))
        self.refresh_summary_cards()
        self.refresh_overview()
        self.refresh_market_tree()
        self.refresh_research_panel()
        self.refresh_portfolio_tab()
        self.refresh_employee_tab(current_plan)
        self.refresh_history_tab()
        self.draw_selected_stock_chart()
        self.draw_history_chart()

    def refresh_summary_cards(self) -> None:
        self.summary_labels["cash"].configure(text=format_money(self.game.company.cash))
        self.summary_labels["portfolio"].configure(text=format_money(self.game.portfolio_value))
        self.summary_labels["net_worth"].configure(text=format_money(self.game.net_worth))
        self.summary_labels["real_worth"].configure(text=format_money(self.game.real_net_worth))
        self.summary_labels["employees"].configure(text=f"{self.game.company.employees:,}")
        self.summary_labels["payroll"].configure(text=format_money(self.game.company.payroll_cost))
        self.summary_labels["turn"].configure(text=f"Y{self.game.year} M{self.game.month}")
        self.summary_labels["regime"].configure(text=self.game.market_regime)

    def refresh_overview(self) -> None:
        self.overview_text_widget.config(state="normal")
        self.overview_text_widget.delete("1.0", "end")
        self.overview_text_widget.insert("1.0", self.game.overview_text())
        self.overview_text_widget.config(state="disabled")

        self.activity_text_widget.config(state="normal")
        self.activity_text_widget.delete("1.0", "end")
        activity = "\n".join(self.game.activity_log[-80:][::-1])
        self.activity_text_widget.insert("1.0", activity)
        self.activity_text_widget.config(state="disabled")

    def refresh_market_tree(self) -> None:
        existing = set(self.market_tree.get_children())
        for item in existing:
            self.market_tree.delete(item)
        for row in self.game.market_rows():
            values = (
                row[0],
                row[1],
                row[2],
                format_money(row[3]),
                format_pct(row[4]),
                format_money(row[5]),
                format_pct(row[6]),
                format_pct(row[7]),
                row[8],
                row[9],
            )
            self.market_tree.insert("", "end", values=values)
        self._select_market_row(self.game.selected_ticker or self.selected_market_ticker.get())

    def refresh_research_panel(self) -> None:
        ticker = self.selected_market_ticker.get().strip().upper() or self.trade_ticker_var.get().strip().upper()
        if ticker not in self.game.stocks and self.game.stocks:
            ticker = sorted(self.game.stocks)[0]
        if ticker:
            self.game.selected_ticker = ticker
            self.selected_market_ticker.set(ticker)
            self.trade_ticker_var.set(ticker)
        report = self.game.research_report(ticker) if ticker else "No stock selected."
        self.research_text_widget.config(state="normal")
        self.research_text_widget.delete("1.0", "end")
        self.research_text_widget.insert("1.0", report)
        self.research_text_widget.config(state="disabled")

    def refresh_portfolio_tab(self) -> None:
        for item in self.holdings_tree.get_children():
            self.holdings_tree.delete(item)
        for row in self.game.holding_rows():
            values = (
                row[0],
                f"{row[1]:,}",
                format_money(row[2]),
                format_money(row[3]),
                format_money(row[4]),
                format_pct(row[5]),
                row[6],
            )
            self.holdings_tree.insert("", "end", values=values)

        for item in self.trades_tree.get_children():
            self.trades_tree.delete(item)
        for row in self.game.recent_trade_rows(35):
            realized = row[7] if row[7] == "" else format_money(float(row[7]))
            values = (
                row[0],
                row[1],
                row[2],
                row[3],
                f"{int(row[4]):,}",
                format_money(float(row[5])),
                format_money(float(row[6])),
                realized,
            )
            self.trades_tree.insert("", "end", values=values)

    def refresh_employee_tab(self, plan: Optional[Dict[str, Any]] = None) -> None:
        if plan is None:
            plan = {"entries": self.game.last_employee_plan, "narrative": [], "quality": self.game.team_quality}
        self.employee_stats_text.set(
            f"Employees: {self.game.company.employees:,}\n"
            f"Monthly payroll: {format_money(self.game.company.payroll_cost)}\n"
            f"Research depth: {self.game.analysis_depth_label}\n"
            f"Auto team trading: {'On' if self.game.auto_team_trading else 'Off'}"
        )
        self.quality_bar["value"] = round(self.game.team_quality * 100)

        lines = []
        lines.append(f"Team quality score: {round(self.game.team_quality * 100):d}/100")
        lines.append(f"Employees cost {format_money(self.game.company.payroll_cost)} every month.")
        lines.append("")
        for text in plan.get("narrative", []):
            lines.append(text)
        entries = plan.get("entries", [])
        if entries:
            lines.append("")
            lines.append("Suggested actions:")
            for entry in entries:
                if entry["action"] == "BUY":
                    lines.append(
                        f"  BUY {entry['ticker']} for about {format_money(entry['amount'])} "
                        f"| {entry['recommendation']} | confidence {format_pct(entry['confidence'])}"
                    )
                else:
                    lines.append(
                        f"  SELL {entry['ticker']} {entry['shares']:,} shares "
                        f"| {entry['recommendation']} | confidence {format_pct(entry['confidence'])}"
                    )
                lines.append(f"    Reason: {entry['reason']}")
        else:
            lines.append("")
            lines.append("No active recommendation right now.")
        lines.append("")
        lines.append("More employees make the analysis more detailed, reduce estimate noise, and improve plan sizing.")
        self.team_plan_text_widget.config(state="normal")
        self.team_plan_text_widget.delete("1.0", "end")
        self.team_plan_text_widget.insert("1.0", "\n".join(lines))
        self.team_plan_text_widget.config(state="disabled")

    def refresh_history_tab(self) -> None:
        for item in self.snapshot_tree.get_children():
            self.snapshot_tree.delete(item)
        for snap in self.game.snapshot_history[-120:][::-1]:
            values = (
                f"Y{snap['year']} M{snap['month']}",
                format_money(float(snap["cash"])),
                format_money(float(snap["portfolio_value"])),
                format_money(float(snap["net_worth"])),
                format_money(float(snap["real_net_worth"])),
                snap["market_regime"],
                f"{int(snap['employees']):,}",
            )
            self.snapshot_tree.insert("", "end", values=values)

    def _redraw_all_charts(self) -> None:
        self.draw_selected_stock_chart()
        self.draw_history_chart()

    def draw_selected_stock_chart(self) -> None:
        ticker = self.game.selected_ticker or self.selected_market_ticker.get()
        if ticker not in self.game.stocks:
            return
        stock = self.game.stocks[ticker]
        self._draw_series_chart(
            self.stock_chart_canvas,
            [stock.history[-60:]],
            [f"{ticker} price"],
            ["#2b6cb0"],
            f"{ticker} — {stock.name}",
            currency=True,
        )

    def draw_history_chart(self) -> None:
        self._draw_series_chart(
            self.history_chart_canvas,
            [self.game.net_worth_history[-120:], self.game.real_net_worth_history[-120:]],
            ["Nominal worth", "Real worth"],
            ["#1f7a1f", "#7a1f7a"],
            "Company net worth",
            currency=True,
        )

    def _draw_series_chart(
        self,
        canvas: "tk.Canvas",
        series_list: List[List[float]],
        labels: List[str],
        colors: List[str],
        title: str,
        currency: bool = False,
    ) -> None:
        canvas.delete("all")
        canvas.update_idletasks()
        width = max(320, canvas.winfo_width())
        height = max(220, canvas.winfo_height())
        margin_left = 60
        margin_right = 20
        margin_top = 32
        margin_bottom = 32
        plot_w = width - margin_left - margin_right
        plot_h = height - margin_top - margin_bottom

        clean_series = [list(s) for s in series_list if len(s) >= 2]
        if not clean_series:
            canvas.create_text(width / 2, height / 2, text="Not enough data yet.", font=("TkDefaultFont", 11))
            return

        values = [value for series in clean_series for value in series]
        min_value = min(values)
        max_value = max(values)
        if math.isclose(min_value, max_value):
            min_value *= 0.95
            max_value *= 1.05
        if math.isclose(min_value, max_value):
            min_value = 0.0
            max_value = max(1.0, max_value)

        canvas.create_rectangle(0, 0, width, height, fill="#ffffff", outline="")
        canvas.create_text(12, 12, anchor="nw", text=title, font=("TkDefaultFont", 11, "bold"))

        # Axes
        x0 = margin_left
        y0 = height - margin_bottom
        x1 = width - margin_right
        y1 = margin_top
        canvas.create_line(x0, y0, x1, y0, fill="#777777")
        canvas.create_line(x0, y0, x0, y1, fill="#777777")

        def y_from_value(value: float) -> float:
            pct = (value - min_value) / (max_value - min_value)
            return y0 - pct * plot_h

        # Grid / labels
        for idx in range(5):
            frac = idx / 4
            value = min_value + (max_value - min_value) * frac
            y = y0 - frac * plot_h
            canvas.create_line(x0, y, x1, y, fill="#efefef")
            label = format_money(value) if currency else f"{value:,.2f}"
            canvas.create_text(x0 - 8, y, anchor="e", text=label, font=("TkDefaultFont", 8))

        max_len = max(len(series) for series in clean_series)
        for idx, series in enumerate(clean_series):
            color = colors[idx % len(colors)]
            points: List[float] = []
            if len(series) == 1:
                continue
            for i, value in enumerate(series):
                x = x0 + (i / max(1, len(series) - 1)) * plot_w
                y = y_from_value(value)
                points.extend([x, y])
            canvas.create_line(*points, fill=color, width=2, smooth=True)

        legend_x = x1 - 150
        for idx, label in enumerate(labels[:len(clean_series)]):
            color = colors[idx % len(colors)]
            y = margin_top + idx * 16
            canvas.create_line(legend_x, y, legend_x + 18, y, fill=color, width=3)
            canvas.create_text(legend_x + 24, y, anchor="w", text=label, font=("TkDefaultFont", 8))

    def on_close(self) -> None:
        try:
            self.game.save(self.save_path)
        except Exception:
            pass
        self.root.destroy()


def create_or_load_game(save_path: Path, args: argparse.Namespace) -> InvestmentGame:
    if save_path.exists() and not args.new_game:
        try:
            return InvestmentGame.load(save_path)
        except Exception:
            pass
    company_name = args.company or "Titan Holdings"
    return InvestmentGame(
        company_name=company_name,
        employees=args.employees,
        seed=args.seed,
        stock_count=args.stocks,
    )


def run_headless(game: InvestmentGame, save_path: Path, autoplay_months: int, auto_team: bool) -> None:
    game.auto_team_trading = auto_team
    for _ in range(max(0, autoplay_months)):
        game.advance_month()
    game.save(save_path)
    game.print_summary()


def main() -> None:
    parser = argparse.ArgumentParser(description="CEO investing game")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducible market generation")
    parser.add_argument("--autoplay-months", type=int, default=0, help="Advance N months automatically")
    parser.add_argument("--company", type=str, default=None, help="Company name")
    parser.add_argument("--employees", type=int, default=15, help="Starting employee count for a new game")
    parser.add_argument("--stocks", type=int, default=DEFAULT_STOCK_COUNT, help="Number of generated stocks")
    parser.add_argument("--save-file", type=str, default=DEFAULT_SAVE_FILE, help="JSON save path")
    parser.add_argument("--new-game", action="store_true", help="Ignore any existing save and start over")
    parser.add_argument("--nogui", action="store_true", help="Run in headless mode")
    parser.add_argument("--auto-team", action="store_true", help="Enable automatic employee trading in headless mode")
    args = parser.parse_args()

    save_path = Path(args.save_file)

    if args.nogui or tk is None or ttk is None or scrolledtext is None:
        game = create_or_load_game(save_path, args)
        run_headless(game, save_path, args.autoplay_months, args.auto_team)
        return

    root = tk.Tk()
    root.withdraw()

    if save_path.exists() and not args.new_game:
        try:
            game = InvestmentGame.load(save_path)
        except Exception:
            game = None
    else:
        game = None

    if game is None:
        defaults = {
            "company_name": args.company or "Titan Holdings",
            "employees": args.employees,
            "stocks": args.stocks,
        }
        dialog = SetupDialog(root, "Create your company", defaults)
        root.wait_window(dialog.window)
        if not dialog.result:
            root.destroy()
            return
        result = dialog.result
        game = InvestmentGame(
            company_name=result["company_name"],
            employees=result["employees"],
            stock_count=result["stocks"],
            seed=args.seed,
        )
        try:
            game.save(save_path)
        except Exception:
            pass

    root.deiconify()
    app = InvestmentGameApp(root, game, save_path, args)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
