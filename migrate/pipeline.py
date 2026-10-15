"""Cleanse, validate, de-duplicate and reconcile a legacy ERP export.

Every change to the data is written down as an issue with a rule id, so the
business can see what was fixed automatically, what was merged and what was
held back for a decision. Nothing is dropped without a line in the issue log.
"""
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
import yaml

HERE = Path(__file__).parent

COUNTRIES = {
    "dk": "DK", "danmark": "DK", "denmark": "DK",
    "se": "SE", "sverige": "SE", "sweden": "SE",
    "no": "NO", "norge": "NO", "norway": "NO",
    "de": "DE", "tyskland": "DE", "germany": "DE", "deutschland": "DE",
}
SUFFIXES = re.compile(r"\b(a/s|as|aps|ab|gmbh|ltd|i/s)\b\.?", re.I)
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)

BLOCKING, WARNING, INFO = "blocking", "warning", "info"


# --------------------------------------------------------------------- parsing
def parse_amount(text):
    """'1.234,50' and '1234.50' both become 123450 (whole cents). None if unreadable."""
    t = (text or "").strip().replace(" ", "")
    if not t:
        return None
    if "," in t:  # Danish format: dot for thousands, comma for decimals
        t = t.replace(".", "").replace(",", ".")
    try:
        return int((Decimal(t) * 100).quantize(Decimal("1")))
    except InvalidOperation:
        return None


def parse_date(text):
    """dd-mm-yyyy or yyyy-mm-dd to ISO yyyy-mm-dd. None if unreadable."""
    t = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            return pd.to_datetime(t, format=fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def country_code(text):
    return COUNTRIES.get((text or "").strip().lower())


def match_name(name):
    """Name used only for matching: no legal suffix, no punctuation, lower case."""
    n = SUFFIXES.sub("", (name or "").lower())
    return re.sub(r"[^a-z0-9æøåäö]+", "", n)


# ---------------------------------------------------------------------- result
@dataclass
class Result:
    customers: pd.DataFrame
    items: pd.DataFrame
    invoices: pd.DataFrame
    issues: pd.DataFrame
    dedupe_map: pd.DataFrame
    reconciliation: pd.DataFrame
    totals: pd.DataFrame
    mapping: dict = field(default_factory=dict)

    @property
    def blocking(self):
        return int((self.issues["severity"] == BLOCKING).sum()) if len(self.issues) else 0

    @property
    def balanced(self):
        return bool(self.reconciliation["balanced"].all() and self.totals["balanced"].all())

    @property
    def go(self):
        return self.balanced and self.blocking == 0


class IssueLog:
    def __init__(self):
        self.rows = []

    def add(self, rule, severity, entity, record, fld, value, message, action):
        self.rows.append(dict(rule=rule, severity=severity, entity=entity, record=record, field=fld,
                              value=value, message=message, action=action))

    def frame(self):
        cols = ["rule", "severity", "entity", "record", "field", "value", "message", "action"]
        return pd.DataFrame(self.rows, columns=cols)


def load_mapping(path=None):
    with open(path or HERE / "mapping.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _read(source, name):
    return pd.read_csv(Path(source) / name, sep=";", dtype=str, keep_default_na=False)


# ------------------------------------------------------------------- customers
def clean_customers(df, log, company):
    src = len(df)
    dup_mask = df.duplicated(keep="first")
    for _, r in df[dup_mask].iterrows():
        log.add("CUST-DUP-EXACT", INFO, "customer", r.CustNo, "*", "", "Identical row exported twice", "removed copy")
    df = df[~dup_mask].copy()
    removed = int(dup_mask.sum())

    rejected = set()
    for i, r in df.iterrows():
        cc = country_code(r.Country)
        if cc is None:
            log.add("CUST-COUNTRY", BLOCKING, "customer", r.CustNo, "Country", r.Country, "Unknown country", "rejected")
            rejected.add(r.CustNo)
        else:
            if cc != r.Country:
                log.add("CUST-COUNTRY", INFO, "customer", r.CustNo, "Country", r.Country, f"Free text mapped to {cc}", "fixed")
            df.at[i, "Country"] = cc
        d = parse_date(r.Created)
        if d is None:
            log.add("CUST-DATE", BLOCKING, "customer", r.CustNo, "Created", r.Created, "Unreadable date", "rejected")
            rejected.add(r.CustNo)
        else:
            df.at[i, "Created"] = d
        if r.Email and not EMAIL.match(r.Email):
            log.add("CUST-EMAIL", WARNING, "customer", r.CustNo, "Email", r.Email,
                    "Invalid email, loaded blank so invoices are not sent to a dead address", "fixed: blanked")
            df.at[i, "Email"] = ""
        if cc in company["vat_required_countries"] and not r.VATNo.strip():
            log.add("CUST-VAT", BLOCKING, "customer", r.CustNo, "VATNo", "",
                    "VAT number required for a Danish business customer", "held: ask AR team")
            rejected.add(r.CustNo)

    # Same company created twice: same VAT number, or same name once suffixes are removed.
    df["_match"] = df.Name.map(match_name)
    df = df.sort_values("CustNo")
    survivor_of = {}
    for key in ("VATNo", "_match"):
        for value, grp in df[df[key].str.len() > 0].groupby(key):
            if len(grp) < 2:
                continue
            keep = grp.CustNo.iloc[0]  # oldest number survives: history and documents point to it
            for no in grp.CustNo.iloc[1:]:
                if no in survivor_of:
                    continue
                survivor_of[no] = keep
                log.add("CUST-DUP-MATCH", WARNING, "customer", no, key.strip("_"), value,
                        f"Same company as {keep}", f"merged into {keep}")
    merged_mask = df.CustNo.isin(survivor_of)
    merged = int(merged_mask.sum())
    df = df[~merged_mask].drop(columns="_match")

    rej_mask = df.CustNo.isin(rejected)
    held = df[rej_mask]
    df = df[~rej_mask]
    dedupe = pd.DataFrame(sorted(survivor_of.items()), columns=["legacy_no", "surviving_no"])
    rec = dict(entity="customers", source=src, removed_duplicates=removed, merged=merged,
               rejected=len(held), loaded=len(df))
    return df.reset_index(drop=True), dedupe, rec, survivor_of, set(held.CustNo)


# ----------------------------------------------------------------------- items
def clean_items(df, log):
    src = len(df)
    rejected = set()
    for no, grp in df.groupby("ItemNo"):
        if len(grp) > 1:
            prices = ", ".join(grp.UnitPrice)
            log.add("ITEM-DUP-SKU", BLOCKING, "item", no, "UnitPrice", prices,
                    "Same item number with different prices", "held: ask pricing owner")
            rejected.add(no)
    df = df.copy()
    for i, r in df.iterrows():
        cents = parse_amount(r.UnitPrice)
        if cents is None or cents < 0:
            log.add("ITEM-PRICE", BLOCKING, "item", r.ItemNo, "UnitPrice", r.UnitPrice, "Unreadable price", "rejected")
            rejected.add(r.ItemNo)
        else:
            df.at[i, "UnitPrice"] = f"{cents / 100:.2f}"
    held = df[df.ItemNo.isin(rejected)]
    df = df[~df.ItemNo.isin(rejected)]
    rec = dict(entity="items", source=src, removed_duplicates=0, merged=0, rejected=len(held), loaded=len(df))
    return df.reset_index(drop=True), rec


# -------------------------------------------------------------------- invoices
def clean_invoices(df, log, customers_loaded, survivor_of, customers_held, legacy_customers):
    src = len(df)
    df = df.copy()
    df["_cents"] = df.OpenAmount.map(parse_amount)
    cust_currency = dict(zip(legacy_customers.CustNo, legacy_customers.Currency))
    loaded_nos = set(customers_loaded.CustNo)
    rejected = set()
    for i, r in df.iterrows():
        if r.CustNo in survivor_of:
            new = survivor_of[r.CustNo]
            log.add("INV-REMAP", INFO, "open_invoice", r.DocNo, "CustNo", r.CustNo,
                    f"Customer was merged, entry moved to {new}", "fixed")
            df.at[i, "CustNo"] = new
        cust = df.at[i, "CustNo"]
        if cust in customers_held:
            log.add("INV-CUST-HELD", BLOCKING, "open_invoice", r.DocNo, "CustNo", cust,
                    "Customer is held back, entry waits with it", "held")
            rejected.add(r.DocNo)
        elif cust not in loaded_nos:
            log.add("INV-ORPHAN", BLOCKING, "open_invoice", r.DocNo, "CustNo", cust,
                    "Customer does not exist in the legacy export", "rejected: ask AR team")
            rejected.add(r.DocNo)
        if not r.Currency:
            guess = cust_currency.get(cust, "")
            if guess:
                log.add("INV-CURRENCY", WARNING, "open_invoice", r.DocNo, "Currency", "",
                        f"Missing currency, taken from customer {cust}", f"fixed: {guess}")
                df.at[i, "Currency"] = guess
            else:
                log.add("INV-CURRENCY", BLOCKING, "open_invoice", r.DocNo, "Currency", "", "Missing currency", "rejected")
                rejected.add(r.DocNo)
        if r._cents is None:
            log.add("INV-AMOUNT", BLOCKING, "open_invoice", r.DocNo, "OpenAmount", r.OpenAmount, "Unreadable amount", "rejected")
            rejected.add(r.DocNo)
        elif r._cents < 0 and r.DocType != "CreditNote":
            log.add("INV-SIGN", BLOCKING, "open_invoice", r.DocNo, "OpenAmount", r.OpenAmount,
                    "Negative amount on an invoice: credit note or sign error?", "held: ask finance")
            rejected.add(r.DocNo)
        for col in ("DocDate", "DueDate"):
            d = parse_date(r[col])
            if d is None:
                log.add("INV-DATE", BLOCKING, "open_invoice", r.DocNo, col, r[col], "Unreadable date", "rejected")
                rejected.add(r.DocNo)
            else:
                df.at[i, col] = d

    held = df[df.DocNo.isin(rejected)]
    loaded = df[~df.DocNo.isin(rejected)].copy()
    totals = []
    # Currency of each source row as finally decided, so totals compare like with like.
    for cur in sorted(set(df.Currency)):
        s = int(df.loc[df.Currency == cur, "_cents"].fillna(0).sum())
        l = int(loaded.loc[loaded.Currency == cur, "_cents"].sum())
        h = int(held.loc[held.Currency == cur, "_cents"].fillna(0).sum())
        totals.append(dict(currency=cur or "(missing)", source=s / 100, loaded=l / 100, held=h / 100, balanced=(s == l + h)))
    loaded["OpenAmount"] = loaded._cents.map(lambda c: f"{c / 100:.2f}")
    rec = dict(entity="open_invoices", source=src, removed_duplicates=0, merged=0, rejected=len(held), loaded=len(loaded))
    return loaded.drop(columns="_cents").reset_index(drop=True), rec, pd.DataFrame(totals)


# ------------------------------------------------------------------------- run
def to_target(df, spec):
    return df[list(spec["fields"].values())].rename(columns={v: k for k, v in spec["fields"].items()})


def load_decisions(path):
    if not path:
        return []
    with open(path, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("decisions", [])


def apply_decisions(frames, decisions, mapping, log):
    """Business decisions on blocking issues, applied to the source before cleansing.

    set:  correct one field on one record (e.g. the AR team supplies a VAT number)
    drop: remove source rows that match (e.g. the wrong one of two prices for a SKU)
    Each decision is logged with who made it and why, so the audit trail stays complete.
    """
    dropped = {k: 0 for k in frames}
    dropped_amounts = {}  # currency -> cents removed from open entries by decision
    for d in decisions:
        ent = d["entity"]
        df, key = frames[ent], mapping[ent]["key"]
        mask = df[key] == d["record"]
        for fld, val in (d.get("where") or {}).items():
            mask &= df[fld] == str(val)
        if not mask.any():
            raise ValueError(f"decision matches nothing: {d}")
        who = f"{d.get('by', 'unknown')}: {d.get('reason', '')}".strip()
        if d["action"] == "set":
            df.loc[mask, d["field"]] = str(d["value"])
            log.add("DECISION", INFO, ent, d["record"], d["field"], d["value"], who, "set by decision")
        elif d["action"] == "drop":
            if ent == "open_invoices":
                for _, r in df[mask].iterrows():
                    cur = r.Currency or "(missing)"
                    dropped_amounts[cur] = dropped_amounts.get(cur, 0) + (parse_amount(r.OpenAmount) or 0)
            frames[ent] = df[~mask]
            dropped[ent] += int(mask.sum())
            log.add("DECISION", INFO, ent, d["record"], "*", "", who, f"dropped {int(mask.sum())} row(s) by decision")
        else:
            raise ValueError(f"unknown action {d['action']}")
    return dropped, dropped_amounts


def _add_dropped(totals, dropped_amounts):
    """Put amounts removed by decision back into the source column, so source = loaded + held + dropped."""
    t = totals.set_index("currency") if len(totals) else pd.DataFrame(columns=["source", "loaded", "held"])
    t["dropped_by_decision"] = 0.0
    for cur, cents in dropped_amounts.items():
        if cur not in t.index:
            t.loc[cur] = [0.0, 0.0, 0.0, 0.0] + ([True] if "balanced" in t.columns else [])
        t.loc[cur, "dropped_by_decision"] = cents / 100
        t.loc[cur, "source"] = round(t.loc[cur, "source"] + cents / 100, 2)
    t["balanced"] = (t.source * 100).round() == ((t.loaded + t.held + t.dropped_by_decision) * 100).round()
    return t.reset_index()[["currency", "source", "loaded", "held", "dropped_by_decision", "balanced"]]


def run(source, mapping=None, decisions=None):
    m = mapping or load_mapping()
    log = IssueLog()
    frames = {ent: _read(source, m[ent]["source"]) for ent in ("customers", "items", "open_invoices")}
    src_counts = {ent: len(df) for ent, df in frames.items()}
    dropped, dropped_amounts = apply_decisions(frames, decisions or [], m, log)
    legacy_c = frames["customers"]
    customers, dedupe, rec_c, survivor_of, held_c = clean_customers(legacy_c.copy(), log, m["company"])
    items, rec_i = clean_items(frames["items"], log)
    invoices, rec_v, totals = clean_invoices(frames["open_invoices"], log, customers, survivor_of, held_c, legacy_c)
    totals = _add_dropped(totals, dropped_amounts)
    recon = pd.DataFrame([rec_c, rec_i, rec_v])
    recon["dropped_by_decision"] = recon.entity.map(dropped)
    recon["source"] = recon.entity.map(src_counts)
    recon = recon[["entity", "source", "dropped_by_decision", "removed_duplicates", "merged", "rejected", "loaded"]]
    recon["balanced"] = recon.source == (recon.dropped_by_decision + recon.removed_duplicates + recon.merged
                                         + recon.rejected + recon.loaded)
    return Result(
        customers=to_target(customers, m["customers"]),
        items=to_target(items, m["items"]),
        invoices=to_target(invoices, m["open_invoices"]),
        issues=log.frame(), dedupe_map=dedupe, reconciliation=recon, totals=totals, mapping=m,
    )
