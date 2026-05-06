import csv
import json
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from pathlib import Path


LEDGER_XML_PATH = Path("data/ledger_export.xml")
BEGINNING_BALANCES_CSV_PATH = Path("data/beginning_balances.csv")
CUSTOMER_XML_PATH = Path("data/customer_export.xml")

OUTPUT_CSV_PATH = Path("output/beginning_balance_audit.csv")
LEDGER_UPDATE_XML_PATH = Path("output/ledger_update.xml")
CUSTOMER_UPDATE_XML_PATH = Path("output/customer_update.xml")

GENERATE_UPDATE_XML = True # Set to True/False to generate XML files for eligible records


def to_decimal(value):
    if value is None:
        return Decimal("0")

    if isinstance(value, list):
        if len(value) == 0:
            return Decimal("0")

        value = value[0]

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
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def local_name(tag):
    return tag.split("}")[-1]


def load_beginning_balances(path):
    balances = {}

    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        for row in reader:
            store_id = row["store_id"].strip()
            starting_balance = to_decimal(row["reward_points"])
            balances[store_id] = starting_balance

    return balances


def load_customer_balances(path):
    tree = ET.parse(path)
    root = tree.getroot()

    customer_balances = {}

    for customer in root.iter():
        if local_name(customer.tag) != "customer":
            continue

        salon_id = customer.attrib.get("customer-no", "").strip()
        reward_points_text = None

        for attr in customer.iter():
            if local_name(attr.tag) != "custom-attribute":
                continue

            if attr.attrib.get("attribute-id") == "rewardPoints":
                reward_points_text = attr.text
                break

        if not salon_id:
            continue

        customer_balances[salon_id] = to_decimal(reward_points_text)

    return customer_balances


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

        records[salon_id] = {
            "ledger": normalize_ledger(raw_ledger, salon_id),
        }

    return records


def normalize_ledger(raw_ledger, salon_id):
    normalized = {}

    if not isinstance(raw_ledger, dict):
        print(f"WARNING: Bad ledger object for {salon_id}: {raw_ledger}")
        return normalized

    for month, values in raw_ledger.items():
        if not isinstance(values, dict):
            print(f"WARNING: Bad ledger month for {salon_id} {month}: {values}")
            continue

        normalized[month] = {
            "b": to_decimal(values.get("b")),
            "a": to_decimal(values.get("a")),
            "r": to_decimal(values.get("r")),
        }

    return normalized


def get_first_ledger_beginning(ledger):
    if not ledger:
        return Decimal("0"), ""

    sorted_months = sorted(ledger.keys())
    first_month = sorted_months[0]
    first_beginning = ledger[first_month]["b"]

    return first_beginning, first_month


def build_audit_rows(beginning_balances, customer_balances, ledger_records):
    rows = []

    for salon_id, starting_balance in beginning_balances.items():
        current_balance = customer_balances.get(salon_id)
        ledger_record = ledger_records.get(salon_id)

        if current_balance is None:
            rows.append({
                "salonId": salon_id,
                "startingBalance": starting_balance,
                "currentBalance": "",
                "ledgerExists": "YES" if ledger_record else "NO",
                "firstLedgerMonth": "",
                "firstLedgerBeginning": "",
                "missingStartingBalance": "",
                "deltaApplied": Decimal("0"),
                "newBalance": "",
                "eligibleForUpdate": "NO",
                "reasonSkipped": "Missing SR customer record",
            })
            continue

        ledger = ledger_record["ledger"] if ledger_record else {}
        first_ledger_beginning, first_ledger_month = get_first_ledger_beginning(ledger)
        missing_starting_balance = starting_balance - first_ledger_beginning

        eligible = (
            starting_balance > 0
            and first_ledger_beginning == 0
            and missing_starting_balance > 0
            and current_balance >= 0
        )

        delta_applied = missing_starting_balance if eligible else Decimal("0")
        new_balance = current_balance + delta_applied

        reason_skipped = ""
        if not eligible:
            if starting_balance <= 0:
                reason_skipped = "Starting balance is 0 or negative"
            elif first_ledger_beginning != 0:
                reason_skipped = "First ledger beginning is not 0"
            elif missing_starting_balance <= 0:
                reason_skipped = "Missing starting balance is not positive"
            elif current_balance < 0:
                reason_skipped = "Current balance is negative"
            else:
                reason_skipped = "Not eligible based on guardrails"

        rows.append({
            "salonId": salon_id,
            "startingBalance": starting_balance,
            "currentBalance": current_balance,
            "ledgerExists": "YES" if ledger_record else "NO",
            "firstLedgerMonth": first_ledger_month,
            "firstLedgerBeginning": first_ledger_beginning,
            "missingStartingBalance": missing_starting_balance,
            "deltaApplied": delta_applied,
            "newBalance": new_balance,
            "eligibleForUpdate": "YES" if eligible else "NO",
            "reasonSkipped": reason_skipped,
        })

    return rows


def write_audit_csv(rows, path):
    path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "salonId",
        "startingBalance",
        "currentBalance",
        "ledgerExists",
        "firstLedgerMonth",
        "firstLedgerBeginning",
        "missingStartingBalance",
        "deltaApplied",
        "newBalance",
        "eligibleForUpdate",
        "reasonSkipped",
    ]

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_corrected_ledger(row, ledger_records):
    salon_id = row["salonId"]
    starting_balance = to_decimal(row["startingBalance"])
    ledger_record = ledger_records.get(salon_id)

    if ledger_record and ledger_record["ledger"]:
        ledger = ledger_record["ledger"].copy()
        first_ledger_beginning, first_ledger_month = get_first_ledger_beginning(ledger)

        ledger[first_ledger_month]["b"] = starting_balance
        return ledger

    return {
        "2026-01": {
            "b": starting_balance,
            "a": Decimal("0"),
            "r": Decimal("0"),
        }
    }


def ledger_to_json(ledger):
    json_ready = {}

    for month in sorted(ledger.keys()):
        json_ready[month] = {
            "b": decimal_to_number(ledger[month]["b"]),
            "a": decimal_to_number(ledger[month]["a"]),
            "r": decimal_to_number(ledger[month]["r"]),
        }

    return json.dumps(json_ready, separators=(",", ":"))


def write_ledger_update_xml(rows, ledger_records, path):
    path.parent.mkdir(parents=True, exist_ok=True)

    eligible_rows = [row for row in rows if row["eligibleForUpdate"] == "YES"]

    root = ET.Element(
        "custom-objects",
        {
            "xmlns": "http://www.demandware.com/xml/impex/customobject/2006-10-31"
        },
    )

    for row in eligible_rows:
        salon_id = row["salonId"]
        corrected_ledger = build_corrected_ledger(row, ledger_records)

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
        history_attr.text = ledger_to_json(corrected_ledger)

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

    print(f"Wrote ledger update XML: {path} ({len(eligible_rows)} records)")


def write_customer_update_xml(rows, path):
    path.parent.mkdir(parents=True, exist_ok=True)

    eligible_rows = [row for row in rows if row["eligibleForUpdate"] == "YES"]

    root = ET.Element(
        "customer-list",
        {
            "xmlns": "http://www.demandware.com/xml/impex/customer/2006-10-31",
            "list-id": "km_sr",
        },
    )

    for row in eligible_rows:
        salon_id = row["salonId"]
        new_balance = to_decimal(row["newBalance"])

        customer = ET.SubElement(
            root,
            "customer",
            {
                "customer-no": salon_id,
            },
        )

        profile = ET.SubElement(customer, "profile")
        custom_attributes = ET.SubElement(profile, "custom-attributes")

        reward_points_attr = ET.SubElement(
            custom_attributes,
            "custom-attribute",
            {
                "attribute-id": "rewardPoints",
            },
        )
        reward_points_attr.text = str(decimal_to_number(new_balance))

    tree = ET.ElementTree(root)
    ET.indent(tree, space="    ", level=0)
    tree.write(path, encoding="UTF-8", xml_declaration=True)

    print(f"Wrote customer update XML: {path} ({len(eligible_rows)} records)")


def main():
    beginning_balances = load_beginning_balances(BEGINNING_BALANCES_CSV_PATH)
    customer_balances = load_customer_balances(CUSTOMER_XML_PATH)
    ledger_records = parse_ledger_xml(LEDGER_XML_PATH)

    print(f"Beginning balances loaded: {len(beginning_balances)}")
    print(f"Customer balances loaded: {len(customer_balances)}")
    print(f"Ledger records loaded: {len(ledger_records)}")

    beginning_ids = set(beginning_balances.keys())
    customer_ids = set(customer_balances.keys())
    ledger_ids = set(ledger_records.keys())

    print(f"Beginning ∩ Customer matches: {len(beginning_ids & customer_ids)}")
    print(f"Beginning ∩ Ledger matches: {len(beginning_ids & ledger_ids)}")
    print(f"Customer ∩ Ledger matches: {len(customer_ids & ledger_ids)}")

    rows = build_audit_rows(beginning_balances, customer_balances, ledger_records)
    write_audit_csv(rows, OUTPUT_CSV_PATH)

    eligible_count = sum(1 for row in rows if row["eligibleForUpdate"] == "YES")
    missing_customer_count = sum(
        1 for row in rows if row["reasonSkipped"] == "Missing SR customer record"
    )
    no_ledger_count = sum(1 for row in rows if row["ledgerExists"] == "NO")

    print(f"Eligible rows: {eligible_count}")
    print(f"Missing SR customer records: {missing_customer_count}")
    print(f"Rows with no ledger record: {no_ledger_count}")
    print(f"Audit complete. Wrote {len(rows)} rows to {OUTPUT_CSV_PATH}")

    if GENERATE_UPDATE_XML:
        write_ledger_update_xml(rows, ledger_records, LEDGER_UPDATE_XML_PATH)
        write_customer_update_xml(rows, CUSTOMER_UPDATE_XML_PATH)
    else:
        print("GENERATE_UPDATE_XML is False. No update XML files were generated.")


if __name__ == "__main__":
    main()