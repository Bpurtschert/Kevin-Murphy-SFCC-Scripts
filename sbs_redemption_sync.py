import csv
import json
import xml.etree.ElementTree as ET
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path


# =========================
# CONFIG
# =========================

LEDGER_XML_PATH = Path("data/ledger_export.xml")
OLD_REDEMPTIONS_CSV_PATH = Path("data/old_redemptions.csv")

OUTPUT_AUDIT_CSV_PATH = Path("output/old_redemption_sync_audit.csv")
LEDGER_UPDATE_XML_PATH = Path("output/ledger_update_old_redemptions.xml")

# Keep this False until you review the audit CSV.
GENERATE_UPDATE_XML = True

# If True, only salons with old redemption rows will be included in update XML.
ONLY_WRITE_CHANGED_LEDGERS = True

# CSV field names expected in old_redemptions.csv.
# Change these if your export headers are different.
OLD_REDEMPTION_CUSTOMER_FIELD = "customerNo"
OLD_REDEMPTION_MONTH_FIELD = "ledgerMonth"  # accepted examples: 2026-02, 2026-02-01, 2/1/2026
OLD_REDEMPTION_AMOUNT_FIELD = "redeemed"


# =========================
# HELPERS
# =========================

def to_decimal(value):
    if value is None:
        return Decimal("0")

    if isinstance(value, list):
        if len(value) == 0:
            return Decimal("0")
        return sum(to_decimal(v) for v in value)

    value = str(value).strip()

    if value == "" or value.lower() in ("null", "none", "nan", "undefined"):
        return Decimal("0")

    value = value.replace(",", "")

    try:
        return Decimal(value)
    except InvalidOperation:
        print(f"WARNING: Invalid decimal value found: {value}. Defaulting to 0.")
        return Decimal("0")


def decimal_to_number(value):
    value = to_decimal(value)
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def local_name(tag):
    return tag.split("}")[-1]


def normalize_month(value):
    """
    Converts common date formats to YYYY-MM.
    Accepted examples:
      2026-02
      2026-02-01
      2/1/2026
      02/2026
    """
    raw = str(value or "").strip()
    if not raw:
        return ""

    # YYYY-MM or YYYY-MM-DD
    if len(raw) >= 7 and raw[4] == "-":
        return raw[:7]

    # M/D/YYYY or MM/DD/YYYY
    if "/" in raw:
        parts = raw.split("/")
        if len(parts) == 3:
            month = int(parts[0])
            year = int(parts[2])
            return f"{year:04d}-{month:02d}"
        if len(parts) == 2:
            month = int(parts[0])
            year = int(parts[1])
            return f"{year:04d}-{month:02d}"

    raise ValueError(f"Could not normalize month value: {raw}")


def normalize_ledger_value_r(value):
    """
    The existing ledger can have r as a number or list.
    We normalize to Decimal sum so updated r becomes one clean number.
    """
    return to_decimal(value)


# =========================
# LOAD INPUTS
# =========================

def parse_ledger_xml(path):
    tree = ET.parse(path)
    root = tree.getroot()

    records = {}

    for obj in root.iter():
        if local_name(obj.tag) != "custom-object":
            continue

        if obj.attrib.get("type-id") != "SalonRewardsLedger":
            continue

        salon_id = obj.attrib.get("object-id", "").strip()
        history_ledger_text = None

        for attr in obj:
            if local_name(attr.tag) != "object-attribute":
                continue

            if attr.attrib.get("attribute-id") == "historyLedger":
                history_ledger_text = attr.text
                break

        if not salon_id:
            continue

        if not history_ledger_text:
            records[salon_id] = {"ledger": {}}
            continue

        try:
            raw_ledger = json.loads(history_ledger_text)
        except json.JSONDecodeError as e:
            print(f"WARNING: Invalid historyLedger JSON for {salon_id}: {e}")
            raw_ledger = {}

        records[salon_id] = {"ledger": normalize_ledger(raw_ledger, salon_id)}

    return records


def normalize_ledger(raw_ledger, salon_id):
    normalized = {}

    if not isinstance(raw_ledger, dict):
        print(f"WARNING: Bad ledger object for {salon_id}: {raw_ledger}")
        return normalized

    for month, values in raw_ledger.items():
        month = normalize_month(month)

        if not isinstance(values, dict):
            print(f"WARNING: Bad ledger month for {salon_id} {month}: {values}")
            continue

        normalized[month] = {
            "b": to_decimal(values.get("b")),
            "a": to_decimal(values.get("a")),
            "r": normalize_ledger_value_r(values.get("r")),
        }

    return normalized


def load_old_redemptions(path):
    """
    Returns:
      old_redemptions[customerNo][YYYY-MM] = total old redeemed for that month
    """
    old_redemptions = {}

    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        required_fields = {
            OLD_REDEMPTION_CUSTOMER_FIELD,
            OLD_REDEMPTION_MONTH_FIELD,
            OLD_REDEMPTION_AMOUNT_FIELD,
        }
        missing = required_fields - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Old redemptions CSV is missing required columns: {sorted(missing)}")

        for row in reader:
            salon_id = row[OLD_REDEMPTION_CUSTOMER_FIELD].strip()
            month = normalize_month(row[OLD_REDEMPTION_MONTH_FIELD])
            amount = to_decimal(row[OLD_REDEMPTION_AMOUNT_FIELD])

            if not salon_id or not month or amount == 0:
                continue

            old_redemptions.setdefault(salon_id, {})
            old_redemptions[salon_id][month] = old_redemptions[salon_id].get(month, Decimal("0")) + amount

    return old_redemptions


# =========================
# SYNC LOGIC
# =========================

def apply_old_redemptions_to_ledger(original_ledger, salon_old_redemptions):
    """
    Rules:
      1. For the same month, add old redemption to that month's r.
      2. For future months only, subtract cumulative prior old redemptions from b.
      3. Do not change the same month's b.
      4. Do not change accrued.
    """
    ledger = deepcopy(original_ledger)
    audit_rows = []

    all_months = sorted(set(ledger.keys()) | set(salon_old_redemptions.keys()))

    cumulative_prior_old_redeemed = Decimal("0")

    for month in all_months:
        if month not in ledger:
            ledger[month] = {
                "b": Decimal("0"),
                "a": Decimal("0"),
                "r": Decimal("0"),
            }
            ledger_month_exists = "NO"
        else:
            ledger_month_exists = "YES"

        original_b = ledger[month]["b"]
        original_a = ledger[month]["a"]
        original_r = ledger[month]["r"]

        old_redeemed_this_month = salon_old_redemptions.get(month, Decimal("0"))

        # Future-balance effect from old redemptions in prior months.
        new_b = original_b - cumulative_prior_old_redeemed

        # Same-month redeemed effect.
        new_r = original_r + old_redeemed_this_month

        ledger[month]["b"] = new_b
        ledger[month]["r"] = new_r

        audit_rows.append({
            "month": month,
            "ledgerMonthExists": ledger_month_exists,
            "originalBeginningBalance": original_b,
            "newBeginningBalance": new_b,
            "beginningBalanceDelta": new_b - original_b,
            "accrued": original_a,
            "originalRedeemed": original_r,
            "oldRedeemedAdded": old_redeemed_this_month,
            "newRedeemed": new_r,
            "redeemedDelta": old_redeemed_this_month,
            "cumulativePriorOldRedeemedAppliedToBalance": cumulative_prior_old_redeemed,
        })

        # This month's old redemption affects future months, not this month.
        cumulative_prior_old_redeemed += old_redeemed_this_month

    return ledger, audit_rows


def build_sync_results(ledger_records, old_redemptions):
    all_audit_rows = []
    updated_ledgers = {}

    for salon_id, salon_old_redemptions in sorted(old_redemptions.items()):
        ledger_record = ledger_records.get(salon_id)

        if not ledger_record:
            for month, amount in sorted(salon_old_redemptions.items()):
                all_audit_rows.append({
                    "salonId": salon_id,
                    "month": month,
                    "salonExistsInLedger": "NO",
                    "ledgerMonthExists": "NO",
                    "originalBeginningBalance": "",
                    "newBeginningBalance": "",
                    "beginningBalanceDelta": "",
                    "accrued": "",
                    "originalRedeemed": "",
                    "oldRedeemedAdded": amount,
                    "newRedeemed": "",
                    "redeemedDelta": "",
                    "cumulativePriorOldRedeemedAppliedToBalance": "",
                    "eligibleForUpdate": "NO",
                    "reasonSkipped": "Salon not found in SFCC ledger export",
                })
            continue

        original_ledger = ledger_record["ledger"]
        updated_ledger, salon_audit_rows = apply_old_redemptions_to_ledger(
            original_ledger,
            salon_old_redemptions,
        )

        updated_ledgers[salon_id] = updated_ledger

        for row in salon_audit_rows:
            old_added = to_decimal(row["oldRedeemedAdded"])
            balance_delta = to_decimal(row["beginningBalanceDelta"])
            changed = old_added != 0 or balance_delta != 0

            all_audit_rows.append({
                "salonId": salon_id,
                "month": row["month"],
                "salonExistsInLedger": "YES",
                "ledgerMonthExists": row["ledgerMonthExists"],
                "originalBeginningBalance": row["originalBeginningBalance"],
                "newBeginningBalance": row["newBeginningBalance"],
                "beginningBalanceDelta": row["beginningBalanceDelta"],
                "accrued": row["accrued"],
                "originalRedeemed": row["originalRedeemed"],
                "oldRedeemedAdded": row["oldRedeemedAdded"],
                "newRedeemed": row["newRedeemed"],
                "redeemedDelta": row["redeemedDelta"],
                "cumulativePriorOldRedeemedAppliedToBalance": row["cumulativePriorOldRedeemedAppliedToBalance"],
                "eligibleForUpdate": "YES" if changed else "NO",
                "reasonSkipped": "" if changed else "No old redemption or balance impact for this month",
            })

    return updated_ledgers, all_audit_rows


# =========================
# OUTPUTS
# =========================

def write_audit_csv(rows, path):
    path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "salonId",
        "month",
        "salonExistsInLedger",
        "ledgerMonthExists",
        "originalBeginningBalance",
        "newBeginningBalance",
        "beginningBalanceDelta",
        "accrued",
        "originalRedeemed",
        "oldRedeemedAdded",
        "newRedeemed",
        "redeemedDelta",
        "cumulativePriorOldRedeemedAppliedToBalance",
        "eligibleForUpdate",
        "reasonSkipped",
    ]

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote audit CSV: {path} ({len(rows)} rows)")


def ledger_to_json(ledger):
    json_ready = {}

    for month in sorted(ledger.keys()):
        json_ready[month] = {
            "b": decimal_to_number(ledger[month]["b"]),
            "a": decimal_to_number(ledger[month]["a"]),
            "r": decimal_to_number(ledger[month]["r"]),
        }

    return json.dumps(json_ready, separators=(",", ":"))


def write_ledger_update_xml(updated_ledgers, path):
    path.parent.mkdir(parents=True, exist_ok=True)

    root = ET.Element(
        "custom-objects",
        {
            "xmlns": "http://www.demandware.com/xml/impex/customobject/2006-10-31"
        },
    )

    for salon_id, ledger in sorted(updated_ledgers.items()):
        obj = ET.SubElement(
            root,
            "custom-object",
            {
                "type-id": "SalonRewardsLedger",
                "object-id": salon_id,
            },
        )

        history_attr = ET.SubElement(
            obj,
            "object-attribute",
            {
                "attribute-id": "historyLedger",
            },
        )
        history_attr.text = ledger_to_json(ledger)

        processed_attr = ET.SubElement(
            obj,
            "object-attribute",
            {
                "attribute-id": "processed",
            },
        )
        processed_attr.text = "true"

    tree = ET.ElementTree(root)
    ET.indent(tree, space="    ", level=0)
    tree.write(path, encoding="UTF-8", xml_declaration=True)

    print(f"Wrote ledger update XML: {path} ({len(updated_ledgers)} salons)")


# =========================
# MAIN
# =========================

def main():
    ledger_records = parse_ledger_xml(LEDGER_XML_PATH)
    old_redemptions = load_old_redemptions(OLD_REDEMPTIONS_CSV_PATH)

    print(f"Ledger records loaded: {len(ledger_records)}")
    print(f"Salons with old redemptions loaded: {len(old_redemptions)}")

    ledger_ids = set(ledger_records.keys())
    old_ids = set(old_redemptions.keys())

    print(f"Old redemption salons found in ledger: {len(old_ids & ledger_ids)}")
    print(f"Old redemption salons missing from ledger: {len(old_ids - ledger_ids)}")

    updated_ledgers, audit_rows = build_sync_results(ledger_records, old_redemptions)
    write_audit_csv(audit_rows, OUTPUT_AUDIT_CSV_PATH)

    changed_rows = [row for row in audit_rows if row["eligibleForUpdate"] == "YES"]
    missing_ledger_rows = [row for row in audit_rows if row["salonExistsInLedger"] == "NO"]

    print(f"Audit rows eligible for update: {len(changed_rows)}")
    print(f"Audit rows skipped because salon missing from ledger: {len(missing_ledger_rows)}")

    if ONLY_WRITE_CHANGED_LEDGERS:
        changed_salon_ids = {row["salonId"] for row in changed_rows}
        updated_ledgers = {
            salon_id: ledger
            for salon_id, ledger in updated_ledgers.items()
            if salon_id in changed_salon_ids
        }

    if GENERATE_UPDATE_XML:
        write_ledger_update_xml(updated_ledgers, LEDGER_UPDATE_XML_PATH)
    else:
        print("GENERATE_UPDATE_XML is False. No update XML file was generated.")
        print("Review the audit CSV first, then set GENERATE_UPDATE_XML = True.")


if __name__ == "__main__":
    main()
