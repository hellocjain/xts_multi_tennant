"""
Options Engine: Black-Scholes Pricing, Greeks & Option Chain Aggregator
Pure mathematical calculations with zero external dependencies (standard Python math/scipy-free).
"""
import math
import time
import datetime
from typing import Dict, Any, List, Optional

def _norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function (CDF)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def _norm_pdf(x: float) -> float:
    """Standard normal probability density function (PDF)."""
    return (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-0.5 * x * x)

def calculate_greeks(
    spot: float,
    strike: float,
    expiry_days: float,
    volatility: float,
    risk_free_rate: float = 0.07,
    option_type: str = "CE"
) -> Dict[str, float]:
    """
    Calculates Option Price and Greeks: Delta, Gamma, Theta, Vega using Black-Scholes-Merton model.
    :param spot: Current price of the underlying asset (e.g. NIFTY spot)
    :param strike: Strike price of the option
    :param expiry_days: Days remaining until expiration (can be fractional)
    :param volatility: Implied Volatility as a decimal (e.g. 0.15 for 15% IV)
    :param risk_free_rate: Annualized risk-free interest rate (default 7% = 0.07)
    :param option_type: 'CE' (Call) or 'PE' (Put)
    """
    opt_type = option_type.upper()
    if spot <= 0 or strike <= 0 or volatility <= 0:
        return {"price": 0.0, "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "iv": volatility}

    # Minimum time to expiry to prevent division by zero at expiry moment (1 minute minimum)
    t = max(expiry_days / 365.0, 0.0001)
    sigma = max(volatility, 0.001)
    r = risk_free_rate

    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t

    pdf_d1 = _norm_pdf(d1)
    cdf_d1 = _norm_cdf(d1)
    cdf_d2 = _norm_cdf(d2)

    exp_rt = math.exp(-r * t)

    # Gamma is identical for Call and Put
    gamma = pdf_d1 / (spot * sigma * sqrt_t)
    # Vega is identical for Call and Put (expressed per 1% move in volatility)
    vega = (spot * pdf_d1 * sqrt_t) / 100.0

    if opt_type in ("CE", "CALL", "C"):
        price = spot * cdf_d1 - strike * exp_rt * cdf_d2
        delta = cdf_d1
        # Theta per calendar day
        theta = (- (spot * pdf_d1 * sigma) / (2.0 * sqrt_t) - r * strike * exp_rt * cdf_d2) / 365.0
    else: # Put option
        cdf_minus_d1 = _norm_cdf(-d1)
        cdf_minus_d2 = _norm_cdf(-d2)
        price = strike * exp_rt * cdf_minus_d2 - spot * cdf_minus_d1
        delta = cdf_d1 - 1.0
        # Theta per calendar day
        theta = (- (spot * pdf_d1 * sigma) / (2.0 * sqrt_t) + r * strike * exp_rt * cdf_minus_d2) / 365.0

    return {
        "price": round(max(price, 0.0), 2),
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 2),
        "vega": round(vega, 2),
        "iv": round(sigma * 100.0, 2)
    }

def solve_implied_volatility(
    market_price: float,
    spot: float,
    strike: float,
    expiry_days: float,
    risk_free_rate: float = 0.07,
    option_type: str = "CE",
    max_iterations: int = 50,
    tolerance: float = 1e-4
) -> float:
    """
    Solves for Implied Volatility (IV) using Newton-Raphson method with Bisection fallback.
    Returns IV as a decimal (e.g. 0.15 for 15%).
    """
    if market_price <= 0 or spot <= 0 or strike <= 0 or expiry_days <= 0:
        return 0.15 # fallback default 15%

    t = max(expiry_days / 365.0, 0.0001)
    intrinsic = max(spot - strike, 0.0) if option_type.upper() in ("CE", "CALL") else max(strike - spot, 0.0)
    if market_price < intrinsic:
        return 0.05

    # Initial guess
    sigma = 0.20

    for _ in range(max_iterations):
        res = calculate_greeks(spot, strike, expiry_days, sigma, risk_free_rate, option_type)
        price_diff = res["price"] - market_price
        if abs(price_diff) < tolerance:
            return sigma
        vega_actual = res["vega"] * 100.0 # unscaled vega
        if abs(vega_actual) < 1e-6:
            break
        sigma = sigma - (price_diff / vega_actual)
        if sigma <= 0.001 or sigma > 5.0:
            break

    # Fallback to binary search if Newton-Raphson diverged
    low, high = 0.01, 3.0
    for _ in range(30):
        mid = (low + high) / 2.0
        p = calculate_greeks(spot, strike, expiry_days, mid, risk_free_rate, option_type)["price"]
        diff = p - market_price
        if abs(diff) < tolerance:
            return mid
        if diff > 0:
            high = mid
        else:
            low = mid

    return (low + high) / 2.0
