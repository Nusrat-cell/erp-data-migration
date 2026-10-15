"""Generate a small legacy ERP export with the problems real migrations have.

The data is synthetic. Every planted problem is listed in PLANTED so the tests
can check that each one is caught, and nothing else is flagged by accident.
"""
import csv
import random
from pathlib import Path

PLANTED = {
    "exact_duplicate_customer": "C1007 is exported twice with identical values",
    "vat_duplicate_customer": "C1012 and C1031 are the same company (same VAT number, 'A/S' vs 'AS')",
    "missing_vat": "C1019 is a Danish company with no VAT number",
    "bad_email": "C1023 has an email without a domain",
    "country_free_text": "countries are exported as 'DK', 'Danmark', 'Denmark', 'Sverige', ...",
    "mixed_date_formats": "created dates use both dd-mm-yyyy and yyyy-mm-dd",
    "danish_decimal_comma": "amounts use '1.234,50' as well as '1234.50'",
    "orphan_invoice": "INV-5040 points to customer C1999, which does not exist",
    "duplicate_sku": "SKU ITM-207 appears twice with different prices",
    "negative_amount_not_credit": "INV-5033 has a negative amount but is typed as an invoice, not a credit note",
    "missing_currency": "INV-5027 has no currency",
}

COMPANIES = [
    ("Nordhavn Byg", "DK"), ("Lyngby Trading", "DK"), ("Aros Maskiner", "DK"), ("Fyns Fragt", "DK"),
    ("Kattegat Foods", "DK"), ("Vestjysk Stål", "DK"), ("Malmö Logistik", "SE"), ("Göteborg Verktyg", "SE"),
    ("Oslo Bygg", "NO"), ("Hamburg Hafen Handel", "DE"), ("Berlin Werkzeug", "DE"), ("Øresund Data", "DK"),
    ("Roskilde Energi", "DK"), ("Aalborg Plast", "DK"), ("Herning Tekstil", "DK"), ("Kolding Kemi", "DK"),
    ("Odense Optik", "DK"), ("Esbjerg Offshore", "DK"), ("Silkeborg Papir", "DK"), ("Viborg Vinduer", "DK"),
    ("Holstebro Hydraulik", "DK"), ("Uppsala Elektro", "SE"), ("Bergen Marine", "NO"), ("Kiel Komponenten", "DE"),
    ("Randers Reb", "DK"), ("Horsens Huse", "DK"), ("Vejle Ventilation", "DK"), ("Næstved Nødstrøm", "DK"),
    ("Hillerød Hjul", "DK"), ("Lund Labs", "SE"),
]
COUNTRY_TEXT = {"DK": ["DK", "Danmark", "Denmark", "dk"], "SE": ["SE", "Sverige", "Sweden"],
                "NO": ["NO", "Norge", "Norway"], "DE": ["DE", "Tyskland", "Germany"]}
CURRENCY = {"DK": "DKK", "SE": "SEK", "NO": "NOK", "DE": "EUR"}


def _amount_text(rng, cents):
    whole, frac = divmod(abs(cents), 100)
    sign = "-" if cents < 0 else ""
    if rng.random() < 0.5:  # Danish style: thousands dot, decimal comma
        return f"{sign}{whole:,}".replace(",", ".") + f",{frac:02d}"
    return f"{sign}{whole}.{frac:02d}"


def _date_text(rng, y, m, d):
    return f"{d:02d}-{m:02d}-{y}" if rng.random() < 0.5 else f"{y}-{m:02d}-{d:02d}"


def generate(out_dir, seed=7):
    rng = random.Random(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    customers = []
    for i, (name, cc) in enumerate(COMPANIES):
        no = f"C{1001 + i}"
        suffix = "A/S" if cc == "DK" else ("AB" if cc == "SE" else ("AS" if cc == "NO" else "GmbH"))
        vat = f"{cc}{rng.randint(10_000_000, 99_999_999)}"
        slug = name.lower().replace(" ", "").replace("ø", "o").replace("æ", "ae").replace("å", "aa").replace("ö", "o").replace("ä", "a")
        customers.append({
            "CustNo": no, "Name": f"{name} {suffix}", "VATNo": vat,
            "Country": rng.choice(COUNTRY_TEXT[cc]), "City": name.split()[0],
            "Email": f"ap@{slug}.example", "Currency": CURRENCY[cc],
            "Created": _date_text(rng, rng.randint(2015, 2024), rng.randint(1, 12), rng.randint(1, 28)),
        })
    by_no = {c["CustNo"]: c for c in customers}

    # Planted customer problems.
    customers.append(dict(by_no["C1007"]))                       # exact duplicate
    twin = dict(by_no["C1012"])                                    # same company, re-created later
    twin.update(CustNo="C1031", Name=twin["Name"].replace("A/S", "AS"), Created="2025-03-14")
    customers.append(twin)
    by_no["C1019"]["VATNo"] = ""                                   # missing VAT on a DK company
    by_no["C1023"]["Email"] = "ap@"                                # broken email

    items = []
    for i in range(12):
        items.append({"ItemNo": f"ITM-{201 + i}", "Description": f"Spare part {201 + i}",
                      "UnitPrice": _amount_text(rng, rng.randint(5_000, 250_000)), "Unit": "PCS"})
    dup = dict(items[6])
    dup["UnitPrice"] = _amount_text(rng, rng.randint(5_000, 250_000))
    items.append(dup)                                              # duplicate SKU, different price

    invoices = []
    real = [c["CustNo"] for c in customers[:30]]
    for i in range(39):
        cust = by_no[real[i % 30]]
        cents = rng.randint(10_000, 4_000_000)
        doc_type = "Invoice"
        if i % 9 == 4:
            doc_type, cents = "CreditNote", -rng.randint(10_000, 200_000)
        invoices.append({"DocNo": f"INV-{5001 + i}", "DocType": doc_type, "CustNo": cust["CustNo"],
                         "DocDate": _date_text(rng, 2026, rng.randint(6, 9), rng.randint(1, 28)),
                         "DueDate": _date_text(rng, 2026, rng.randint(10, 12), rng.randint(1, 28)),
                         "Currency": cust["Currency"], "OpenAmount": _amount_text(rng, cents)})
    # Twin customer has its own open invoice: it must move to the surviving record.
    invoices[11]["CustNo"] = "C1031"
    invoices[26]["Currency"] = ""                                  # missing currency
    invoices[32]["OpenAmount"] = "-" + invoices[32]["OpenAmount"].lstrip("-")  # negative, still "Invoice"
    invoices[32]["DocType"] = "Invoice"
    invoices.append({"DocNo": "INV-5040", "DocType": "Invoice", "CustNo": "C1999", "DocDate": "2026-09-30",
                     "DueDate": "2026-10-30", "Currency": "DKK", "OpenAmount": "12.500,00"})  # orphan

    for name, rows in (("legacy_customers.csv", customers), ("legacy_items.csv", items),
                       ("legacy_open_invoices.csv", invoices)):
        with open(out / name, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter=";")
            w.writeheader()
            w.writerows(rows)
    return out
