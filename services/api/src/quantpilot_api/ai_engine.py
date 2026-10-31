"""AI analysis engine (PRD 8): statistical Market Regime + Strategy Evaluator.

First-release provider is a deterministic statistical classifier
(statistical-regime/regime-rules-v1) - no LLM call, no external dependency:
PRD 8.2 puts Market Regime on "statistical features + models", and doc 8.6
only requires LLM-style governance for LLM-style costs (recorded as 0 here).

Every analysis is one row per (account, symbol, timeframe, source bar) in
ai_analyses with the strict doc 8.4 structure inside output_json:

- Market Regime (8.2): ATR/close volatility ratio, Wilder ADX and EMA20/50
  trend features classify TREND_UP / TREND_DOWN / RANGE / HIGH_VOL / UNKNOWN.
- Strategy Evaluator (8.2): regime-conditional suitability score + reason
  code for every ENABLED strategy instance bound to that (symbol, timeframe).
- Signal Filter (8.2): a first-pass ALLOW / REVIEW suggestion. In ADVISORY
  mode it is a display artifact only and NEVER changes trade permission
  (8.5); the FILTER-mode wiring is a later slice.

Idempotency: the input feature snapshot is hashed (doc 8.4
input_snapshot_id); UNIQUE(account, symbol, timeframe, input_hash) makes
re-delivered bar batches a no-op - same policy as signal dedup (7.5).

Runtime failures are isolated by the caller (writers.handle_bars): a failed
analysis must never stall the bars projection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from . import db
from .account_config import AccountConfig
from .strategies import Bar
from .strategies.indicators import adx_series, atr_series, ema_series

logger = logging.getLogger("quantpilot.ai")

MODEL_PROVIDER = "statistical-regime"
MODEL_ID = "regime-rules-v1"
PROMPT_VERSION = "market-eval-v1"

# feature window: covers EMA50 (50) + Wilder ADX14 (29) + realized vol (21)
# with the same extra-history margin philosophy as the strategy engine
FEATURE_WINDOW = 120
MIN_WINDOW = 60

_ADX_PERIOD = 14
_EMA_FAST = 20
_EMA_SLOW = 50
_ADX_TREND_THRESHOLD = Decimal("25")
# ATR / close above this => HIGH_VOL (EURUSD M15 ATR ~0.0002..0.0020)
_VOL_HIGH_THRESHOLD = Decimal("0.0015")
_REALIZED_VOL_PERIOD = 20

REGIMES = ("TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOL", "UNKNOWN")

_TF_MINUTES = {"M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240}

# regime -> strategy code -> (score, reason_code); unknown codes fall back
# to a neutral score so future strategies still appear in every analysis
_SCORE_TABLE: dict[str, dict[str, tuple[str, str]]] = {
    "TREND_UP": {
        "trend_following": ("0.90", "TREND_MARKET"),
        "ma_cross": ("0.80", "TREND_MARKET"),
        "rsi_momentum": ("0.40", "COUNTER_TREND_RISK"),
    },
    "TREND_DOWN": {
        "trend_following": ("0.85", "TREND_MARKET"),
        "ma_cross": ("0.75", "TREND_MARKET"),
        "rsi_momentum": ("0.40", "COUNTER_TREND_RISK"),
    },
    "RANGE": {
        "trend_following": ("0.20", "RANGE_MARKET"),
        "ma_cross": ("0.30", "RANGE_MARKET"),
        "rsi_momentum": ("0.55", "RANGE_STABILITY"),
    },
    "HIGH_VOL": {
        "trend_following": ("0.35", "VOLATILITY_ELEVATED"),
        "ma_cross": ("0.30", "VOLATILITY_ELEVATED"),
        "rsi_momentum": ("0.35", "VOLATILITY_ELEVATED"),
    },
    "UNKNOWN": {},
}
_DEFAULT_SCORE = ("0.50", "REGIME_UNKNOWN")

D0 = Decimal("0")


def ai_mode(account_id: str) -> str:
    """Latest configured AI mode for the account (doc 8.5; default ADVISORY).

    Absence of a risk_profiles row means "never configured" - the doc 8.5
    first-release default applies.
    """
    row = db.query_one(
        "SELECT config_json FROM risk_profiles WHERE account_id = %s ORDER BY version DESC LIMIT 1",
        (account_id,),
    )
    if row is None:
        return "ADVISORY"
    try:
        return AccountConfig.model_validate(row["config_json"]).ai.mode
    except Exception:
        # a poisoned config must not take analysis down with it; doc 8.5
        # fallback direction is conservative-but-default
        logger.warning("invalid account config json; ai mode defaults to ADVISORY")
        return "ADVISORY"


def _bar_window(
    account_id: str, broker_symbol: str, timeframe: str, source_bar_time: datetime, size: int
) -> list[Bar]:
    rows = db.query(
        """
        SELECT bar_time, open, high, low, close, tick_volume
          FROM symbol_bars
         WHERE account_id = %s AND broker_symbol = %s AND timeframe = %s
           AND bar_time <= %s
         ORDER BY bar_time DESC
         LIMIT %s
        """,
        (account_id, broker_symbol, timeframe, source_bar_time, size),
    )
    rows.reverse()  # oldest-first; trigger bar is the last element
    return [
        Bar(
            bar_time=row["bar_time"],
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            tick_volume=row["tick_volume"],
        )
        for row in rows
    ]


def _realized_vol(closes: list[Decimal], period: int) -> Decimal | None:
    """Std of the last `period` close-to-close returns (doc 8.3 realized vol)."""
    if len(closes) < period + 1:
        return None
    returns: list[Decimal] = []
    for i in range(len(closes) - period, len(closes)):
        prev = closes[i - 1]
        if prev == D0:
            return None
        returns.append((closes[i] - prev) / prev)
    mean = sum(returns, D0) / Decimal(len(returns))
    variance = sum(((r - mean) * (r - mean) for r in returns), D0) / Decimal(len(returns))
    return variance.sqrt()


def _features(window: list[Bar], source_bar_time: datetime) -> dict[str, Any] | None:
    closes = [bar.close for bar in window]
    ema_fast = ema_series(closes, _EMA_FAST)[-1]
    ema_slow = ema_series(closes, _EMA_SLOW)[-1]
    adx = adx_series(window, _ADX_PERIOD)[-1]
    atr = atr_series(window, _ADX_PERIOD)[-1]
    rvol = _realized_vol(closes, _REALIZED_VOL_PERIOD)
    if ema_fast is None or ema_slow is None or adx is None or atr is None or rvol is None:
        return None
    close = closes[-1]
    if close == D0:
        return None
    return {
        "ema20": ema_fast,
        "ema50": ema_slow,
        "adx": adx,
        "atr": atr,
        "vol_ratio": atr / close,
        "realized_vol": rvol,
        "source_bar_time": source_bar_time,
    }


def _quantize2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return min(max(value, low), high)


def _classify(features: dict[str, Any]) -> tuple[str, Decimal, str]:
    """(regime, confidence in [0,1], short explanation) - doc 8.2 layer 1."""
    vol_ratio: Decimal = features["vol_ratio"]
    adx: Decimal = features["adx"]
    ema20: Decimal = features["ema20"]
    ema50: Decimal = features["ema50"]

    if vol_ratio > _VOL_HIGH_THRESHOLD:
        conf = _clamp(vol_ratio / (Decimal("2") * _VOL_HIGH_THRESHOLD), Decimal("0.5"), Decimal("1"))
        return (
            "HIGH_VOL",
            _quantize2(conf),
            f"ATR/close ratio {vol_ratio:.5f} exceeds the {_VOL_HIGH_THRESHOLD} high-volatility threshold",
        )
    if adx >= _ADX_TREND_THRESHOLD:
        direction = "up" if ema20 > ema50 else "down"
        regime = "TREND_UP" if ema20 > ema50 else "TREND_DOWN"
        conf = _clamp(adx / Decimal("50"), Decimal("0.5"), Decimal("1"))
        return (
            regime,
            _quantize2(conf),
            f"ADX {adx:.1f} with EMA{_EMA_FAST} {direction} of EMA{_EMA_SLOW}: trending {direction}",
        )
    conf = _clamp(((_ADX_TREND_THRESHOLD - adx) / _ADX_TREND_THRESHOLD), Decimal("0.2"), Decimal("1"))
    return (
        "RANGE",
        _quantize2(conf),
        f"ADX {adx:.1f} below {_ADX_TREND_THRESHOLD} with contained volatility: ranging market",
    )


def _enabled_instances(account_id: str, broker_symbol: str, timeframe: str) -> list[dict[str, Any]]:
    return db.query(
        """
        SELECT si.id::text AS instance_id, s.code AS strategy_code
          FROM strategy_instances si
          JOIN strategy_versions sv ON sv.id = si.strategy_version_id
          JOIN strategies s ON s.id = sv.strategy_id
         WHERE si.account_id = %s AND si.broker_symbol = %s AND si.timeframe = %s
           AND si.status = 'ENABLED' AND s.status = 'ACTIVE'
         ORDER BY s.code
        """,
        (account_id, broker_symbol, timeframe),
    )


def _strategy_scores(regime: str, instances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    table = _SCORE_TABLE.get(regime, {})
    scores: list[dict[str, Any]] = []
    for instance in instances:
        score, reason = table.get(instance["strategy_code"], _DEFAULT_SCORE)
        scores.append(
            {
                "strategy_id": instance["strategy_code"],
                "score": float(Decimal(score)),
                "reason_codes": [reason],
            }
        )
    return scores


def _decision(regime: str) -> tuple[str, list[str]]:
    """First-pass Signal Filter suggestion (doc 8.2 layer 3).

    ADVISORY-only in this release: the value is stored/displayed but never
    consulted by the risk gate (doc 8.5).
    """
    if regime == "HIGH_VOL":
        return "REVIEW", ["VOLATILITY_ELEVATED"]
    if regime == "UNKNOWN":
        return "REVIEW", ["REGIME_UNKNOWN"]
    return "ALLOW", []


def _input_hash(
    account_id: str,
    broker_symbol: str,
    timeframe: str,
    window_size: int,
    features: dict[str, Any] | None,
    source_bar_time: datetime,
) -> str:
    feature_payload: dict[str, Any]
    if features is None:
        # UNKNOWN regime: hash the provenance only (indicator history missing)
        feature_payload = {"indicators": "UNAVAILABLE"}
    else:
        feature_payload = {
            "ema20": str(features["ema20"]),
            "ema50": str(features["ema50"]),
            "adx": str(features["adx"]),
            "atr": str(features["atr"]),
            "vol_ratio": str(features["vol_ratio"]),
            "realized_vol": str(features["realized_vol"]),
        }
    canonical = json.dumps(
        {
            "account_id": account_id,
            "broker_symbol": broker_symbol,
            "timeframe": timeframe,
            "model": MODEL_ID,
            "prompt_version": PROMPT_VERSION,
            "window_size": window_size,
            "source_bar_time": source_bar_time.astimezone(timezone.utc).isoformat(),
            "features": feature_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def analyze_newest_bar(
    account_id: str, broker_symbol: str, timeframe: str, source_bar_time: datetime
) -> dict[str, Any] | None:
    """Analyze at one closed bar. Returns the doc-8.4 analysis, or None when
    the account mode is OFF / bars are missing / the window is too short.

    Called from writers.handle_bars after the bars commit (bar-close trigger,
    doc 8.6: never per-tick) and from the on-demand endpoint.
    """
    if ai_mode(account_id) == "OFF":
        return None
    window = _bar_window(account_id, broker_symbol, timeframe, source_bar_time, FEATURE_WINDOW)
    if not window or window[-1].bar_time != source_bar_time:
        return None
    if len(window) < MIN_WINDOW:
        return None

    started = time.monotonic()
    features = _features(window, source_bar_time)
    if features is None:
        regime, confidence, explanation = "UNKNOWN", Decimal("0"), "insufficient indicator history"
    else:
        regime, confidence, explanation = _classify(features)

    instances = _enabled_instances(account_id, broker_symbol, timeframe)
    scores = _strategy_scores(regime, instances)
    decision, risk_flags = _decision(regime)

    created_at = datetime.now(timezone.utc)
    expires_at = created_at + timedelta(minutes=2 * _TF_MINUTES.get(timeframe, 15))
    input_hash = _input_hash(
        account_id, broker_symbol, timeframe, len(window), features, source_bar_time
    )
    analysis_id = str(uuid.uuid4())
    duration_ms = int((time.monotonic() - started) * 1000)

    # strict doc 8.4 structure (+ provenance extras)
    doc = {
        "analysis_id": analysis_id,
        "model_provider": MODEL_PROVIDER,
        "model_id": MODEL_ID,
        "prompt_version": PROMPT_VERSION,
        "created_at": created_at.isoformat(),
        "input_snapshot_id": input_hash,
        "market_regime": regime,
        "regime_confidence": float(confidence),
        "strategy_scores": scores,
        "signal_decision": decision,
        "risk_flags": risk_flags,
        "explanation": explanation,
        "expires_at": expires_at.isoformat(),
        "source_bar_time": source_bar_time.astimezone(timezone.utc).isoformat(),
        "duration_ms": duration_ms,
        "cost": "0",  # statistical provider: no LLM call (doc 8.6 cost ledger)
    }

    with db.conn() as connection:
        with connection.cursor() as cursor:
            inserted = cursor.execute(
                """
                INSERT INTO ai_analyses (analysis_id, account_id, broker_symbol, timeframe,
                                        model_provider, model_id, prompt_version, input_hash,
                                        output_json, source_bar_time, created_at, expires_at,
                                        duration_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CAST(%s AS jsonb), %s, %s, %s, %s)
                ON CONFLICT (account_id, broker_symbol, timeframe, input_hash) DO NOTHING
                RETURNING id::text
                """,
                (
                    analysis_id,
                    account_id,
                    broker_symbol,
                    timeframe,
                    MODEL_PROVIDER,
                    MODEL_ID,
                    PROMPT_VERSION,
                    input_hash,
                    json.dumps(doc, ensure_ascii=False),
                    source_bar_time,
                    created_at,
                    expires_at,
                    duration_ms,
                ),
            ).fetchone()
    if inserted is None:
        # same feature snapshot already analyzed (redelivered batch / manual
        # replay) - return the stored analysis instead of a duplicate
        row = db.query_one(
            """
            SELECT output_json FROM ai_analyses
             WHERE account_id = %s AND broker_symbol = %s AND timeframe = %s AND input_hash = %s
            """,
            (account_id, broker_symbol, timeframe, input_hash),
        )
        return row["output_json"] if row is not None else None
    return doc


def analyze_on_demand(
    account_id: str, broker_symbol: str, timeframe: str
) -> tuple[str, dict[str, Any] | None, str | None]:
    """Manual trigger (UI/testing). Returns (ai_mode, analysis, error)."""
    mode = ai_mode(account_id)
    if mode == "OFF":
        return mode, None, None
    row = db.query_one(
        """
        SELECT max(bar_time) AS newest FROM symbol_bars
         WHERE account_id = %s AND broker_symbol = %s AND timeframe = %s
        """,
        (account_id, broker_symbol, timeframe),
    )
    if row is None or row["newest"] is None:
        return mode, None, "no closed bars for this account+symbol+timeframe"
    analysis = analyze_newest_bar(account_id, broker_symbol, timeframe, row["newest"])
    return mode, analysis, None
