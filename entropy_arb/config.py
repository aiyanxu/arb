"""Configuration: strategy AND market selection (symbol + both venues) from
a YAML file, credentials from .env, with optional per-venue symbol renames
from symbol_map.yaml.

The split is deliberate: config.yaml holds the strategy (thresholds, sizing,
risk) and the markets you trade (symbol, base_venue, hedge_venue) in one
shareable example file; .env holds only secrets; --symbol / --base / --hedge
are optional per-run overrides that win over the YAML. Every YAML key is
validated against the schema below, so a typo is an error rather than a
setting that silently does nothing.

Threshold model (fixed numbers the user derives from recorded minute data;
premium is measured for ONE (base, hedge) pair — swap either leg and the
numbers must be re-measured, they do not transfer):

    premium_bps = (base_price / hedge_price - 1) * 10_000

    SELL base / BUY hedge  fires when the executable premium
        (base bid over hedge ask) >= midline_bps + upper_bps
    BUY base / SELL hedge  fires when the executable premium
        (base ask under hedge bid) <= midline_bps - lower_bps

    Both hurdles are net of both venues' taker fees, so a full round trip
    nets >= (upper_bps + lower_bps) after fees by construction.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import yaml
from dotenv import load_dotenv

HL_API_URL = "https://api.hyperliquid.xyz"
HL_WS_URL = "wss://api.hyperliquid.xyz/ws"   # official ws — the only HL feed used
ASTER_API_URL = "https://fapi.asterdex.com"  # V3 futures API
ASTER_WS_URL = "wss://fstream.asterdex.com"
POLYMARKET_API_URL = "https://api.perpetuals.polymarket.com"  # perps gateway
POLYMARKET_WS_URL = "wss://ws.perpetuals.polymarket.com/v1/ws"

# Any venue can be either leg (base or hedge); the two legs must differ.
# 任意 venue 均可作为 base 或 hedge 腿，但两条腿不能相同。
VENUES = ("entropy", "lighter", "lighter-rh", "tradexyz", "aster", "polymarket")
DEFAULT_BASE_VENUE = "entropy"


@dataclass(frozen=True)
class LighterProfile:
    name: str
    api_url: str
    ws_url: str
    chain_id: int


# Endpoint profiles for the two supported zkLighter deployments (these match
# lighter-python's lighter.endpoint_profiles, duplicated here so --record-only
# data collection works without the SDK installed).
LIGHTER_PROFILES: Dict[str, LighterProfile] = {
    "lighter": LighterProfile(
        "mainnet", "https://mainnet.zklighter.elliot.ai",
        "wss://mainnet.zklighter.elliot.ai/stream", 304),
    "lighter-rh": LighterProfile(
        "robinhood", "https://api.rh.lighter.xyz",
        "wss://api.rh.lighter.xyz/stream", 466324),
}


@dataclass(frozen=True)
class VenueSpec:
    """Static per-venue facts (either leg is built from these + the yaml)."""
    kind: str                     # "hl" | "lighter" | "aster" | "polymarket"
    label: str                    # display name: "ENTROPY" | "XYZ" | ...
    hl_dex: str                   # default dex for hl venues ("" for lighter)
    fee_bps: float                # venue taker fee default
    orders_per_min: int           # default order-send budget
    lighter_profile: Optional[LighterProfile] = None


# One registry for both legs: kind/label/defaults per venue name. The base:
# and hedge: yaml sections override fee/cap/orders on top of these defaults.
VENUE_REGISTRY: Dict[str, VenueSpec] = {
    "entropy": VenueSpec("hl", "ENTROPY", "io", 0.0, 120),
    "tradexyz": VenueSpec("hl", "XYZ", "xyz", 1.0, 120),
    "lighter": VenueSpec("lighter", "LIGHTER", "", 0.0, 30,
                         LIGHTER_PROFILES["lighter"]),
    "lighter-rh": VenueSpec("lighter", "RH", "", 0.0, 30,
                            LIGHTER_PROFILES["lighter-rh"]),
    "aster": VenueSpec("aster", "ASTER", "", 4.5, 120),
    "polymarket": VenueSpec("polymarket", "POLY", "", 4.0, 120),
}


@dataclass
class LighterCreds:
    account_index: Optional[int]
    api_key_index: Optional[int]
    api_private_key: Optional[str]

    @property
    def complete(self) -> bool:
        return (self.account_index is not None and self.api_key_index is not None
                and bool(self.api_private_key))


@dataclass
class HLCreds:
    private_key: Optional[str]
    account_address: Optional[str]

    @property
    def complete(self) -> bool:
        return bool(self.private_key)


@dataclass
class AsterCreds:
    private_key: Optional[str]        # API (agent) wallet key — does the signing
    account_address: Optional[str]    # master wallet — the "user" param, NOT
                                      # derivable from the signer key

    @property
    def complete(self) -> bool:
        return bool(self.private_key) and bool(self.account_address)


@dataclass
class PolymarketCreds:
    proxy_address: Optional[str]      # proxy wallet address (the polymarket-proxy
                                      # header; separate keypair from the owner)
    proxy_secret: Optional[str]       # from the one-time createProxy ceremony
    private_key: Optional[str]        # proxy wallet key — signs every op

    @property
    def complete(self) -> bool:
        return (bool(self.proxy_address) and bool(self.proxy_secret)
                and bool(self.private_key))


@dataclass
class VenueConf:
    key: str                  # "base" | "hedge"
    kind: str                 # "hl" | "lighter" | "aster"
    label: str                # human name for logs, e.g. "ENTROPY", "RH"
    symbol: str
    fee_bps: float
    cap_usd: float
    orders_per_min: int
    # hl
    hl_dex: str = ""
    hl_creds: Optional[HLCreds] = None
    # lighter
    lighter_profile: Optional[LighterProfile] = None
    lighter_creds: Optional[LighterCreds] = None
    # aster
    aster_creds: Optional[AsterCreds] = None
    # polymarket
    polymarket_creds: Optional[PolymarketCreds] = None


@dataclass
class Config:
    symbol: str
    base_venue: str
    hedge_venue: str
    base: VenueConf
    hedge: VenueConf
    # thresholds (the whole signal)
    midline_bps: float
    upper_bps: float
    lower_bps: float
    # sizing
    take_fraction: float
    max_order_notional: float
    min_order_notional: float
    # inventory ladder
    inventory_scale_bps: float
    inventory_floor_frac: float
    # execution
    premium_persist_sec: float
    cooldown_sec: float
    settle_timeout_sec: float
    leg_slippage_bps: float
    hedge_slippage_bps: float
    net_tolerance_base: float
    max_consecutive_errors: int
    rate_limit_pause_sec: float
    staleness_sec: float
    reconcile_sec: float
    venue_probe_sec: float
    http_keepalive_sec: float
    # recorder
    recorder_enabled: bool
    recorder_db: str
    # logging
    log_level: str
    status_interval_sec: float
    trades_csv: str
    dashboard: bool
    log_file: str
    # runtime
    hl_api_url: str = HL_API_URL
    hl_ws_url: str = HL_WS_URL

    @property
    def creds_complete(self) -> bool:
        for v in (self.base, self.hedge):
            if v.kind == "hl" and not (v.hl_creds and v.hl_creds.complete):
                return False
            if v.kind == "lighter" and not (v.lighter_creds
                                            and v.lighter_creds.complete):
                return False
            if v.kind == "aster" and not (v.aster_creds
                                          and v.aster_creds.complete):
                return False
            if v.kind == "polymarket" and not (v.polymarket_creds
                                               and v.polymarket_creds.complete):
                return False
        return True


# ----------------------------------------------------------------- YAML layer

# Schema: nested dict of key -> type (or nested dict). Unknown keys are errors.
_SCHEMA: Dict[str, Any] = {
    # market selection — optional here; the CLI --symbol / --base / --hedge
    # flags override these for a single run
    "symbol": str,
    "base_venue": str,
    "hedge_venue": str,
    "thresholds": {
        "midline_bps": float,
        "upper_bps": float,
        "lower_bps": float,
    },
    "base": {
        "dex": str,
        "taker_fee_bps": float,
        "max_position_usd": float,
        "max_orders_per_min": int,
    },
    "hedge": {
        "dex": str,
        "taker_fee_bps": float,
        "max_position_usd": float,
        "max_orders_per_min": int,
    },
    "sizing": {
        "take_fraction": float,
        "max_order_notional_usd": float,
        "min_order_notional_usd": float,
    },
    "inventory": {
        "scale_bps": float,
        "floor_frac": float,
    },
    "execution": {
        "premium_persist_sec": float,
        "cooldown_sec": float,
        "settle_timeout_sec": float,
        "leg_slippage_bps": float,
        "hedge_slippage_bps": float,
        "net_tolerance_base": float,
        "max_consecutive_errors": int,
        "rate_limit_pause_sec": float,
        "staleness_sec": float,
        "reconcile_sec": float,
        "venue_probe_sec": float,
        "http_keepalive_sec": float,
    },
    "recorder": {
        "enabled": bool,
        "db": str,
    },
    "logging": {
        "level": str,
        "status_interval_sec": float,
        "trades_csv": str,
        "dashboard": bool,
        "file": str,
    },
}


class ConfigError(ValueError):
    pass


def _validate(node: Any, schema: Dict[str, Any], path: str = "") -> None:
    if not isinstance(node, dict):
        raise ConfigError(f"'{path or '<root>'}' must be a mapping")
    for key, val in node.items():
        here = f"{path}.{key}" if path else str(key)
        if key not in schema:
            raise ConfigError(f"unknown config key '{here}' "
                              f"(valid: {', '.join(sorted(schema))})")
        want = schema[key]
        if isinstance(want, dict):
            _validate(val, want, here)
        elif want is float:
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise ConfigError(f"'{here}' must be a number, got {val!r}")
        elif want is int:
            if not isinstance(val, int) or isinstance(val, bool):
                raise ConfigError(f"'{here}' must be an integer, got {val!r}")
        elif want is bool:
            if not isinstance(val, bool):
                raise ConfigError(f"'{here}' must be true/false, got {val!r}")
        elif want is str:
            if not isinstance(val, str):
                raise ConfigError(f"'{here}' must be a string, got {val!r}")


def _get(d: dict, section: str, key: str, default):
    return (d.get(section) or {}).get(key, default)


# -------------------------------------------------------------- symbol map

def _load_symbol_map(path: str) -> Dict[str, Dict[str, str]]:
    """Load CLI symbol -> {venue key -> venue-native symbol}. Missing = {}.

    Venue-native names can differ from the CLI symbol (e.g. trade.xyz lists
    SNDK as TTSLA). Strict like the main schema: a typo is an error, not a
    setting that silently does nothing.
    """
    try:
        with open(path) as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as e:
        raise ConfigError(f"symbol map '{path}' is not valid YAML: {e}")
    if not isinstance(raw, dict):
        raise ConfigError(f"symbol map '{path}' must be a mapping of "
                          f"symbol -> venue -> symbol")
    venues = VENUES
    out: Dict[str, Dict[str, str]] = {}
    for sym, entry in raw.items():
        # a bare NO/ON/OFF key is parsed as a YAML 1.1 bool — reject it here
        if not isinstance(sym, str) or not sym.strip():
            raise ConfigError(f"symbol map '{path}': symbol keys must be "
                              f"non-empty strings, got {sym!r}")
        if not isinstance(entry, dict):
            raise ConfigError(f"symbol map '{path}': entry '{sym}' must be a "
                              f"mapping of venue -> symbol")
        for venue, mapped in entry.items():
            if venue not in venues:
                raise ConfigError(f"symbol map '{path}': '{sym}.{venue}' is "
                                  f"not a venue (valid: {', '.join(venues)})")
            if not isinstance(mapped, str) or not mapped.strip():
                raise ConfigError(f"symbol map '{path}': '{sym}.{venue}' must "
                                  f"be a non-empty string, got {mapped!r}")
            out.setdefault(sym.strip(), {})[venue] = mapped.strip()
    return out


# ------------------------------------------------------------------ env layer

def _env_s(name: str) -> Optional[str]:
    v = os.getenv(name)
    return v.strip() if v not in (None, "") else None


def _env_i(name: str) -> Optional[int]:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else None


def _make_leg(role: str, venue: str, raw: dict, symbol: str,
              both_lighter: bool) -> VenueConf:
    """Build one leg (base or hedge) from the venue registry + yaml section.

    Credentials are keyed by VENUE, not by role: entropy reads HL_*, tradexyz
    reads HL_*_XYZ (falling back to HL_* — one Hyperliquid account may trade
    both dexes), a Lighter deployment reads LIGHTER_*. When BOTH legs are
    Lighter deployments the hedge leg reads LIGHTER_HEDGE_* with NO fallback
    (falling back would silently point both legs at one account index).
    """
    spec = VENUE_REGISTRY[venue]
    sec = raw.get(role) or {}
    if "dex" in sec and spec.kind != "hl":
        raise ConfigError(
            f"'{role}.dex' only applies to Hyperliquid venues — {venue!r} is "
            f"a Lighter deployment, Aster or Polymarket / dex 仅适用于 "
            f"Hyperliquid 交易所")
    fee = float(sec.get("taker_fee_bps", spec.fee_bps))
    if spec.kind in ("hl", "aster", "polymarket") and fee < spec.fee_bps:
        # underestimating a venue's fee makes every threshold systematically
        # too loose — never let it pass silently
        raise ConfigError(
            f"{venue!r} charges ~{spec.fee_bps:.1f} bps taker but "
            f"{role}.taker_fee_bps is {fee} — the fee must not be configured "
            f"below the venue default / 手续费配置低于该交易所默认费率，"
            f"阈值会系统性偏松")
    cap = float(sec.get("max_position_usd", 1000.0))
    orders = int(sec.get("max_orders_per_min", spec.orders_per_min))

    if spec.kind == "lighter":
        if both_lighter and role == "hedge":
            creds = LighterCreds(_env_i("LIGHTER_HEDGE_ACCOUNT_INDEX"),
                                 _env_i("LIGHTER_HEDGE_API_KEY_INDEX"),
                                 _env_s("LIGHTER_HEDGE_API_PRIVATE_KEY"))
        else:
            creds = LighterCreds(_env_i("LIGHTER_ACCOUNT_INDEX"),
                                 _env_i("LIGHTER_API_KEY_INDEX"),
                                 _env_s("LIGHTER_API_PRIVATE_KEY"))
        return VenueConf(key=role, kind="lighter", label=spec.label,
                         symbol=symbol, fee_bps=fee, cap_usd=cap,
                         orders_per_min=orders,
                         lighter_profile=spec.lighter_profile,
                         lighter_creds=creds)

    if spec.kind == "aster":
        # aster can occupy at most one leg (the two legs must differ), so
        # there is no hedge-variant block like LIGHTER_HEDGE_* — no fallback
        # either: the master wallet address cannot be derived from the key.
        creds = AsterCreds(_env_s("ASTER_PRIVATE_KEY"),
                           _env_s("ASTER_ACCOUNT_ADDRESS"))
        return VenueConf(key=role, kind="aster", label=spec.label,
                         symbol=symbol, fee_bps=fee, cap_usd=cap,
                         orders_per_min=orders, aster_creds=creds)

    if spec.kind == "polymarket":
        # proxy credential from the one-time createProxy ceremony (see
        # tools/polymarket_make_proxy.py); expires after ~1 week — re-run the
        # ceremony and update all three values together. Occupies at most one
        # leg, so no hedge-variant env block.
        creds = PolymarketCreds(_env_s("POLYMARKET_PROXY_ADDRESS"),
                                _env_s("POLYMARKET_PROXY_SECRET"),
                                _env_s("POLYMARKET_PROXY_PRIVATE_KEY"))
        return VenueConf(key=role, kind="polymarket", label=spec.label,
                         symbol=symbol, fee_bps=fee, cap_usd=cap,
                         orders_per_min=orders, polymarket_creds=creds)

    if venue == "tradexyz":
        hc = HLCreds(_env_s("HL_PRIVATE_KEY_XYZ") or _env_s("HL_PRIVATE_KEY"),
                     _env_s("HL_ACCOUNT_ADDRESS_XYZ")
                     or _env_s("HL_ACCOUNT_ADDRESS"))
    else:                          # entropy (or any future hl venue)
        hc = HLCreds(_env_s("HL_PRIVATE_KEY"), _env_s("HL_ACCOUNT_ADDRESS"))
    return VenueConf(key=role, kind="hl", label=spec.label, symbol=symbol,
                     fee_bps=fee, cap_usd=cap, orders_per_min=orders,
                     hl_dex=str(sec.get("dex", spec.hl_dex)), hl_creds=hc)


# -------------------------------------------------------------------- loading

def load_config(config_file: str = "config.yaml", env_file: str = ".env", *,
                symbol: Optional[str] = None,
                base_venue: Optional[str] = None,
                hedge_venue: Optional[str] = None,
                symbol_map_file: str = "symbol_map.yaml") -> Config:
    load_dotenv(env_file)
    try:
        with open(config_file) as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        raise ConfigError(
            f"config file '{config_file}' not found — copy config.example.yaml "
            f"to config.yaml and edit it / 未找到配置文件，请先复制 "
            f"config.example.yaml 为 config.yaml 并修改")
    if "entropy" in raw:
        raise ConfigError(
            "config key 'entropy' was renamed to 'base' (the base leg's venue "
            "is now 'base_venue') / 配置键 'entropy' 已改名为 'base'，"
            "base 腿的交易所用 'base_venue' 指定")
    _validate(raw, _SCHEMA)

    # market selection: CLI flag wins over config.yaml; at least one source
    # required. An empty CLI string counts as "not provided" and falls back
    # to the config value. base_venue defaults to entropy (the historical
    # base leg), so pre-existing configs keep working unchanged.
    symbol = (symbol or raw.get("symbol") or "").strip()
    if not symbol:
        raise ConfigError(
            "symbol is required — set 'symbol:' in config.yaml or pass "
            "--symbol SNDK / 必须指定交易品种：在 config.yaml 填 symbol，"
            "或启动时用 --symbol 指定")
    base_venue = (base_venue or raw.get("base_venue")
                  or DEFAULT_BASE_VENUE).strip()
    if base_venue not in VENUES:
        raise ConfigError(
            f"base_venue must be one of {list(VENUES)}, got {base_venue!r} — "
            f"set 'base_venue:' in config.yaml or pass --base / base 腿必须是 "
            f"{list(VENUES)} 之一：在 config.yaml 填 base_venue，或启动时用 "
            f"--base 指定")
    hedge_venue = hedge_venue or (raw.get("hedge_venue") or "").strip()
    if hedge_venue not in VENUES:
        raise ConfigError(
            f"hedge_venue must be one of {list(VENUES)}, got "
            f"{hedge_venue!r} — set 'hedge_venue:' in config.yaml or pass "
            f"--hedge / 对冲腿必须是 {list(VENUES)} 之一：在 config.yaml "
            f"填 hedge_venue，或启动时用 --hedge 指定")
    if base_venue == hedge_venue:
        raise ConfigError(
            f"base_venue and hedge_venue must differ — both are "
            f"{base_venue!r} / 两条腿不能是同一个交易所")

    # per-venue symbol overrides: DEX naming can differ from the CLI symbol
    # (each leg resolves by ITS OWN venue name)
    symbol_map = _load_symbol_map(symbol_map_file)
    mapped = symbol_map.get(symbol, {})
    base_symbol = mapped.get(base_venue, symbol)
    hedge_symbol = mapped.get(hedge_venue, symbol)

    thr = raw.get("thresholds") or {}
    for k in ("midline_bps", "upper_bps", "lower_bps"):
        if k not in thr:
            raise ConfigError(f"'thresholds.{k}' is required — derive it from "
                              f"recorded minute data / 必须填写，请用采集的分钟"
                              f"数据计算后填入")
    upper, lower = float(thr["upper_bps"]), float(thr["lower_bps"])
    if upper <= 0 or lower <= 0:
        raise ConfigError("thresholds.upper_bps and lower_bps must be > 0 "
                          "(the round trip nets upper+lower bps after fees)")

    take_fraction = float(_get(raw, "sizing", "take_fraction", 0.5))
    if not 0.0 < take_fraction <= 1.0:
        raise ConfigError("sizing.take_fraction must be in (0, 1] — taking "
                          "more than the profitable depth loses money on the "
                          "tail / 必须在 (0, 1] 之间")

    both_lighter = (VENUE_REGISTRY[base_venue].kind == "lighter"
                    and VENUE_REGISTRY[hedge_venue].kind == "lighter")
    base = _make_leg("base", base_venue, raw, base_symbol, both_lighter)
    hedge = _make_leg("hedge", hedge_venue, raw, hedge_symbol, both_lighter)
    if (base.kind == "hl" and hedge.kind == "hl"
            and base.hl_dex == hedge.hl_dex):
        raise ConfigError(
            f"both legs trade the same Hyperliquid dex {base.hl_dex!r} — that "
            f"is one market, not an arb / 两条腿是同一个市场，不构成套利")

    return Config(
        symbol=symbol,
        base_venue=base_venue,
        hedge_venue=hedge_venue,
        base=base,
        hedge=hedge,
        midline_bps=float(thr["midline_bps"]),
        upper_bps=upper,
        lower_bps=lower,
        take_fraction=take_fraction,
        max_order_notional=float(_get(raw, "sizing", "max_order_notional_usd", 500.0)),
        min_order_notional=float(_get(raw, "sizing", "min_order_notional_usd", 10.0)),
        inventory_scale_bps=float(_get(raw, "inventory", "scale_bps", 10.0)),
        inventory_floor_frac=float(_get(raw, "inventory", "floor_frac", 0.5)),
        premium_persist_sec=float(_get(raw, "execution", "premium_persist_sec", 0.3)),
        cooldown_sec=float(_get(raw, "execution", "cooldown_sec", 0.0)),
        settle_timeout_sec=float(_get(raw, "execution", "settle_timeout_sec", 5.0)),
        leg_slippage_bps=float(_get(raw, "execution", "leg_slippage_bps", 50.0)),
        hedge_slippage_bps=float(_get(raw, "execution", "hedge_slippage_bps", 20.0)),
        net_tolerance_base=float(_get(raw, "execution", "net_tolerance_base", 0.001)),
        max_consecutive_errors=int(_get(raw, "execution", "max_consecutive_errors", 3)),
        rate_limit_pause_sec=float(_get(raw, "execution", "rate_limit_pause_sec", 10.0)),
        staleness_sec=float(_get(raw, "execution", "staleness_sec", 10.0)),
        reconcile_sec=float(_get(raw, "execution", "reconcile_sec", 15.0)),
        venue_probe_sec=float(_get(raw, "execution", "venue_probe_sec", 30.0)),
        http_keepalive_sec=float(_get(raw, "execution", "http_keepalive_sec", 10.0)),
        recorder_enabled=bool(_get(raw, "recorder", "enabled", True)),
        recorder_db=_get(raw, "recorder", "db", "logs/minutes.duckdb"),
        log_level=str(_get(raw, "logging", "level", "INFO")).upper(),
        status_interval_sec=float(_get(raw, "logging", "status_interval_sec", 30.0)),
        trades_csv=_get(raw, "logging", "trades_csv", "logs/trades.csv"),
        dashboard=bool(_get(raw, "logging", "dashboard", True)),
        log_file=_get(raw, "logging", "file", "logs/engine.log"),
    )
