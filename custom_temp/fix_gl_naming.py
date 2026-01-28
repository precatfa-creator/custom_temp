"""
Script to verify and fix GL Entry naming series.

Run with:
    bench --site <your-site> execute custom_temp.fix_gl_naming.verify_and_fix_gl_naming
"""

import frappe


def verify_and_fix_gl_naming():
    """
    Verify that GL Entry naming series is properly configured.
    If not, add the correct series to the database.
    """
    # Get the current year
    from frappe.utils import nowdate, getdate

    current_year = getdate(nowdate()).year

    # Expected series prefix
    series_prefix = f"ACC-GLE-.{current_year}.-"

    print(f"\n{'='*60}")
    print("GL Entry Naming Series Verification")
    print(f"{'='*60}\n")

    # Check GL Entry doctype configuration
    gl_entry_meta = frappe.get_meta("GL Entry")
    autoname = gl_entry_meta.autoname
    print(f"GL Entry autoname: {autoname}")
    print(f"Expected series prefix: {series_prefix}")

    # Check if series exists in the Series table
    existing_series = frappe.db.get_value("Series", {"name": series_prefix}, "current")

    if existing_series is not None:
        print(
            f"\n✅ Series '{series_prefix}' exists with current value: {existing_series}"
        )
    else:
        print(f"\n❌ Series '{series_prefix}' does NOT exist in the database!")
        print("   This is likely why GL entries are getting hash names.")
        print("\n   To fix this, you can run:")
        print(
            f"   frappe.db.sql(\"INSERT INTO `tabSeries` (name, current) VALUES ('{series_prefix}', 0)\")"
        )
        print("   frappe.db.commit()")

    # Check for any hash-named GL entries
    hash_entries = frappe.db.sql(
        """
        SELECT COUNT(*) as count
        FROM `tabGL Entry`
        WHERE name NOT LIKE 'ACC-GLE-%'
        AND is_cancelled = 0
    """,
        as_dict=True,
    )

    if hash_entries and hash_entries[0].count > 0:
        print(
            f"\n⚠️  Found {hash_entries[0].count} GL entries with non-standard names (hash names)"
        )
    else:
        print("\n✅ All GL entries have standard naming")

    # Check recent GL entries
    recent_entries = frappe.db.sql(
        """
        SELECT name, posting_date, voucher_type, voucher_no
        FROM `tabGL Entry`
        ORDER BY creation DESC
        LIMIT 5
    """,
        as_dict=True,
    )

    print("\n--- Recent GL Entries ---")
    for entry in recent_entries:
        name_type = "✅ Standard" if entry.name.startswith("ACC-GLE-") else "❌ Hash"
        print(
            f"  {entry.name} | {entry.posting_date} | {entry.voucher_type} | {name_type}"
        )

    print(f"\n{'='*60}\n")

    return {
        "autoname": autoname,
        "series_prefix": series_prefix,
        "series_exists": existing_series is not None,
        "series_current_value": existing_series,
        "hash_entries_count": hash_entries[0].count if hash_entries else 0,
    }


def fix_gl_naming_series():
    """
    Fix the GL Entry naming series by ensuring the Series record exists.
    """
    from frappe.utils import nowdate, getdate

    current_year = getdate(nowdate()).year
    series_prefix = f"ACC-GLE-.{current_year}.-"

    # Check if series exists
    existing = frappe.db.exists("Series", series_prefix)

    if not existing:
        # Get the highest GL Entry number for this year to set correct current value
        max_entry = frappe.db.sql(
            f"""
            SELECT MAX(CAST(SUBSTRING_INDEX(name, '-', -1) AS UNSIGNED)) as max_num
            FROM `tabGL Entry`
            WHERE name LIKE 'ACC-GLE-.{current_year}.-%'
        """,
            as_dict=True,
        )

        current_value = (
            max_entry[0].max_num if max_entry and max_entry[0].max_num else 0
        )

        # Insert the series
        frappe.db.sql(
            """INSERT INTO `tabSeries` (name, current) VALUES (%s, %s)""",
            (series_prefix, current_value),
        )
        frappe.db.commit()

        print(
            f"✅ Created Series '{series_prefix}' with current value: {current_value}"
        )
    else:
        print(f"✅ Series '{series_prefix}' already exists")

    return True
