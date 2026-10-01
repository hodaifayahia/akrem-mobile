"""Localized wording for installment plans ("every 2 months", "24,750 دج every month × 6")."""

from __future__ import annotations

from app.i18n import ar


def every_text(interval: int) -> str:
    """How often the client pays: every month, every 2 months, every 6 months…"""
    if interval <= 1:
        return ar.PLAN_EVERY_1
    if interval == 2:
        return ar.PLAN_EVERY_2
    if interval <= 10:
        return ar.PLAN_EVERY_FEW.format(n=interval)
    return ar.PLAN_EVERY_MANY.format(n=interval)


def amount_label(interval: int) -> str:
    """Caption for the per-payment amount: "monthly installment" or "installment amount"."""
    return ar.MONTHLY_AMOUNT if interval <= 1 else ar.INSTALLMENT_AMOUNT


def plan_summary(amount: int, interval: int, count: int) -> str:
    """``24,750 دج كل شهر × 6``."""
    return ar.PLAN_SUMMARY.format(amount=money(amount), every=every_text(interval), count=count)


def plan_description(months: int, interval: int) -> str:
    """``6 أشهر · كل شهر``."""
    return ar.PLAN_DESCRIPTION.format(months=months, every=every_text(interval))


def money(amount: int) -> str:
    """Whole dinars with separators and the currency suffix."""
    return f"{amount:,} {ar.CURRENCY_SUFFIX}"
