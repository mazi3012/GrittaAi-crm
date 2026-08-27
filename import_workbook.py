"""Import the CRM Excel workbook into the shared Gretta leads database.

Usage:
    python import_workbook.py "/absolute/path/to/CRM - Instagram Data(1).xlsx"
    python import_workbook.py "/absolute/path/to/file.xlsx" --dry-run

Only setter tabs containing the 27-column lead header are imported. Tracker
and Closer are derived/reporting tabs and are intentionally skipped.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET

from dotenv import load_dotenv

load_dotenv()

import db  # noqa: E402
from sheets import HEADER_TO_FIELD  # noqa: E402


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"m": MAIN_NS, "r": REL_NS}

EXPECTED_HEADERS = tuple(HEADER_TO_FIELD)
SKIP_TABS = {"Tracker", "Closer", "Sync Log"}
DATE_FIELDS = {
    "first_touchpoint", "last_touchpoint", "next_touchpoint",
    "follow_up_1_date", "follow_up_2_date", "follow_up_3_date",
    "follow_up_4_date", "discovery_date",
}
YESNO_FIELDS = {
    "replied", "number_received", "follow_up_1", "follow_up_2",
    "follow_up_3", "follow_up_4", "discovery_call",
}

# These are the canonical names already used by the CRM/dashboard.
CANONICAL_SETTERS = {
    "guthal basumatary": "Guthal Basumatary",
    "guthal basumaatry": "Guthal Basumatary",
    "guthal": "Guthal Basumatary",
    "archit": "Archit",
    "archit setter": "Archit",
    "mazidur": "Mazidur Rahman",
    "mazidur rahman": "Mazidur Rahman",
}


def _text(element):
    return "".join(node.text or "" for node in element.iter(f"{{{MAIN_NS}}}t"))


def _column_ref(cell_ref):
    return re.match(r"[A-Z]+", cell_ref or "").group(0)


def _column_name(index):
    """Convert zero-based index 0.. to Excel letters (A..Z, AA..)."""
    result = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _shared_strings(zf):
    name = "xl/sharedStrings.xml"
    if name not in zf.namelist():
        return []
    root = ET.fromstring(zf.read(name))
    return [_text(item) for item in root.findall("m:si", NS)]


def _sheet_targets(zf):
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    relmap = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in rels.findall(f"{{{PKG_REL_NS}}}Relationship")
    }
    result = []
    for sheet in workbook.find("m:sheets", NS):
        target = relmap[sheet.attrib[f"{{{REL_NS}}}id"]]
        if not target.startswith("xl/"):
            target = "xl/" + target.lstrip("/")
        result.append((sheet.attrib["name"], target))
    return result


def _read_rows(zf, target, shared):
    root = ET.fromstring(zf.read(target))
    rows = []
    for row in root.findall(".//m:sheetData/m:row", NS):
        values = {}
        for cell in row.findall("m:c", NS):
            value_node = cell.find("m:v", NS)
            cell_type = cell.attrib.get("t")
            if cell_type == "inlineStr":
                value = _text(cell)
            elif value_node is None:
                value = ""
            else:
                value = value_node.text or ""
                if cell_type == "s" and value.isdigit():
                    value = shared[int(value)] if int(value) < len(shared) else ""
            values[_column_ref(cell.attrib.get("r"))] = value
        rows.append(values)
    return rows


def _excel_date(value):
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        serial = float(value)
        return (date(1899, 12, 30) + timedelta(days=serial)).isoformat()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %b", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(value, fmt)
            if fmt == "%d %b":
                parsed = parsed.replace(year=date.today().year)
            return parsed.date().isoformat()
        except ValueError:
            continue
    return value


def _yes_no(value):
    value = str(value or "").strip().lower()
    if value in {"yes", "y", "true", "1", "sent", "received"}:
        return "Yes"
    if value in {"no", "n", "false", "0", "not sent", "not received"}:
        return "No"
    return ""


def _setter_name(value, tab_name):
    raw = str(value or "").strip()
    fallback = tab_name[:-len(" Setter")].strip() if tab_name.endswith(" Setter") else tab_name.strip()
    raw = raw or fallback
    return CANONICAL_SETTERS.get(raw.casefold(), raw)


def load_workbook(path):
    """Return (lead dictionaries, import metadata) from an .xlsx workbook."""
    leads = []
    metadata = {"tabs": [], "skipped": {}, "blank_rows": 0, "duplicate_rows": 0}
    seen = set()
    with ZipFile(path) as zf:
        shared = _shared_strings(zf)
        for tab_name, target in _sheet_targets(zf):
            rows = _read_rows(zf, target, shared)
            headers = [_column_name(i) for i in range(len(EXPECTED_HEADERS))]
            headers = [rows[0].get(column, "").strip() for column in headers] if rows else []
            if tab_name in SKIP_TABS or tuple(headers) != EXPECTED_HEADERS:
                metadata["skipped"][tab_name] = "derived/non-lead tab"
                continue
            metadata["tabs"].append(tab_name)
            fields = [HEADER_TO_FIELD.get(header) for header in headers]
            for raw in rows[1:]:
                lead = {}
                for index, field in enumerate(fields):
                    if field:
                        value = raw.get(_column_name(index), "")
                        if field in DATE_FIELDS:
                            value = _excel_date(value)
                        elif field in YESNO_FIELDS:
                            value = _yes_no(value)
                        elif field == "lead_number":
                            try:
                                value = int(float(str(value).strip() or 0))
                            except (ValueError, TypeError):
                                value = 0
                        else:
                            value = str(value or "").strip()
                        lead[field] = value
                username = db.normalize_username(lead.get("user_name"))
                if not username:
                    metadata["blank_rows"] += 1
                    continue
                lead["user_name"] = username
                lead["sender_name"] = _setter_name(lead.get("sender_name"), tab_name)
                lead["profile_link"] = lead.get("profile_link") or db.profile_link_for(username)
                if lead.get("status") not in db.STATUSES:
                    lead["status"] = "Message Sent"
                if username in seen:
                    metadata["duplicate_rows"] += 1
                seen.add(username)
                leads.append(lead)
    metadata["total"] = len(leads)
    return leads, metadata


def import_leads(leads, reset=False):
    """Write all leads in one transaction without triggering mirror syncs."""
    # A duplicate handle can occur across tabs. The last workbook row wins,
    # matching the normal full-snapshot import behavior.
    unique = {}
    for lead in leads:
        unique[lead["user_name"].casefold()] = lead

    db.init_db()
    conn = db._connect()
    try:
        with db._write_lock:
            if reset:
                conn.execute("DELETE FROM leads")
            columns = list(db.LEAD_FIELDS)
            placeholders = ", ".join(db._PH for _ in columns)
            assignments = ", ".join(
                f"{column} = EXCLUDED.{column}"
                for column in columns if column != "user_name"
            )
            statement = (
                f"INSERT INTO leads ({', '.join(columns)}) "
                f"VALUES ({placeholders}) "
                f"ON CONFLICT (user_name) DO UPDATE SET {assignments}"
            )
            values = []
            for lead in unique.values():
                data = dict(lead)
                db._apply_rules(data)
                data.setdefault("profile_link", db.profile_link_for(data["user_name"]))
                data.setdefault("status", "Message Sent")
                data.setdefault("first_touchpoint", db.today_str())
                data.setdefault("last_touchpoint", db.today_str())
                values.append(tuple(data.get(column) or "" for column in columns))
            with conn.cursor() as cursor:
                cursor.executemany(statement, values)
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return len(unique)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--offset", type=int, default=0,
                        help="Number of valid workbook rows to skip")
    parser.add_argument("--batch-size", type=int, default=0,
                        help="Maximum valid workbook rows to import (0 = all)")
    parser.add_argument("--reset", action="store_true",
                        help="Delete existing leads before importing")
    args = parser.parse_args()
    if not args.workbook.is_file():
        parser.error(f"Workbook not found: {args.workbook}")

    leads, metadata = load_workbook(args.workbook)
    if args.offset < 0 or args.batch_size < 0:
        parser.error("--offset and --batch-size must not be negative")
    total_ready = len(leads)
    end = total_ready if not args.batch_size else min(
        args.offset + args.batch_size, total_ready)
    leads = leads[args.offset:end]
    print(f"Tabs imported: {', '.join(metadata['tabs'])}")
    print(f"Rows ready: {metadata['total']}; blank rows skipped: {metadata['blank_rows']}; duplicate handles: {metadata['duplicate_rows']}")
    if args.batch_size or args.offset:
        print(f"Batch rows: {args.offset + 1 if leads else args.offset}..{end} of {total_ready}")
    if metadata["skipped"]:
        print("Skipped tabs: " + ", ".join(sorted(metadata["skipped"])))
    by_setter = {}
    for lead in leads:
        by_setter[lead["sender_name"]] = by_setter.get(lead["sender_name"], 0) + 1
    print("Setter totals: " + ", ".join(f"{name}={count}" for name, count in sorted(by_setter.items())))
    if args.dry_run:
        print("Dry run: database unchanged")
        return 0

    imported = import_leads(leads, reset=args.reset)
    print(f"Imported successfully: {imported} unique leads")
    if args.reset:
        print("Existing leads were replaced; schema and non-lead tables were preserved")
    return 0


if __name__ == "__main__":
    sys.exit(main())