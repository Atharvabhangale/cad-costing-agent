"""Monetary presentation policy: currency and 2-decimal rounding.

The policy
----------

- **Currency**: every rate set carries an ISO-4217-style 3-letter code. The POC
  default is **INR**, and every seeded rate set and every pre-existing stored
  row is INR-denominated. There is no currency conversion anywhere: an estimate
  is simply denominated in the currency of the rate set that produced it.
- **Rounding**: computed cost amounts are presented and stored at **2 decimal
  places**, half-up (the convention for money; Python's built-in `round` is
  half-to-even, which would turn 0.125 into 0.12).

How rounding avoids touching the arithmetic
-------------------------------------------

`Money` is a serializer annotation, not a different number type. A model field
annotated `Money` holds the **exact** float in memory and is only rounded when
the model is serialized. Python-level arithmetic therefore always reads full
precision, which makes it structurally impossible to round an intermediate and
then feed it into a later calculation — the costing formulas are untouched and
every displayed figure is the correctly-rounded form of the exact value.

One consequence is worth stating plainly: because each amount is rounded from
its own exact value, displayed components can differ from a displayed total by
a penny. Rounding the components first and summing those would remove that
discrepancy but would mean reporting a total that is not the rounded true
total, which is the worse trade.

Rates are not rounded
---------------------

Rounding applies to computed cost *amounts*. Input rates echoed back for
explainability — cost per kg, machine and labor rates per hour — are reported
as configured, since a legitimate rate such as 0.125/kg would be distorted by
2-decimal rounding.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import PlainSerializer

MONEY_DECIMAL_PLACES = 2
DEFAULT_CURRENCY = "INR"
CURRENCY_CODE_PATTERN = r"^[A-Z]{3}$"

_MONEY_QUANTUM = Decimal("0.01")
_PERCENT_QUANTUM = Decimal("0.01")


def round_money(value: float) -> float:
    """Round a cost amount to 2 decimal places, half-up."""
    return float(Decimal(str(value)).quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP))


def round_percent(value: float | None) -> float | None:
    """Round a presented percentage to 2 decimal places, half-up."""
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(_PERCENT_QUANTUM, rounding=ROUND_HALF_UP))


# A cost amount: exact in memory, rounded on the way out.
Money = Annotated[float, PlainSerializer(round_money, return_type=float)]

# A presented percentage, rounded the same way. Not money, but the same policy.
Percent = Annotated[float | None, PlainSerializer(round_percent, return_type=float | None)]
