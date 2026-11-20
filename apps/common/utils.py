import csv
from decimal import ROUND_HALF_UP, Decimal

from django.http import HttpResponse


def money(value) -> Decimal:
    return Decimal(value or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def fmt_qty(value) -> str:
    """Human quantity without trailing zeros or exponent notation: Decimal('100.000') → '100', '2.500' → '2.5'."""
    if value is None:
        return "—"
    text = f"{Decimal(value):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def csv_response(filename: str, header: list[str], rows) -> HttpResponse:
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return response
