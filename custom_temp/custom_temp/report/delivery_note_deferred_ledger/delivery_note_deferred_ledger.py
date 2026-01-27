# Copyright (c) 2024, ARD and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt, getdate, add_months, today


def execute(filters=None):
    if not filters:
        filters = {}

    # Set default dates if missing (fallback mechanism)
    if not filters.get("from_date"):
        filters["from_date"] = add_months(today(), -1)
    if not filters.get("to_date"):
        filters["to_date"] = today()

    columns = get_columns(filters)
    data = get_data(filters)

    return columns, data


def get_columns(filters):
    return [
        {
            "label": _("Posting Date"),
            "fieldname": "posting_date",
            "fieldtype": "Date",
            "width": 100,
        },
        {
            "label": _("Account"),
            "fieldname": "account",
            "fieldtype": "Link",
            "options": "Account",
            "width": 180,
        },
        {
            "label": _("Debit"),
            "fieldname": "debit",
            "fieldtype": "Currency",
            "width": 120,
        },
        {
            "label": _("Credit"),
            "fieldname": "credit",
            "fieldtype": "Currency",
            "width": 120,
        },
        {
            "label": _("Balance"),
            "fieldname": "balance",
            "fieldtype": "Currency",
            "width": 120,
        },
        {
            "label": _("Voucher Type"),
            "fieldname": "voucher_type",
            "fieldtype": "Data",
            "width": 120,
        },
        {
            "label": _("Voucher No"),
            "fieldname": "voucher_no",
            "fieldtype": "Dynamic Link",
            "options": "voucher_type",
            "width": 160,
        },
        {
            "label": _("Against Account"),
            "fieldname": "against",
            "fieldtype": "Data",
            "width": 120,
        },
        {
            "label": _("Against Voucher"),
            "fieldname": "against_voucher",
            "fieldtype": "Data",
            "width": 100,
        },
        {
            "label": _("Remarks"),
            "fieldname": "remarks",
            "fieldtype": "Data",
            "width": 300,
        },
    ]


def get_data(filters):
    # 1. Determine which Vouchers to fetch
    vouchers = []

    if filters.get("delivery_note"):
        # Specific Delivery Note
        vouchers = get_related_vouchers([filters.get("delivery_note")])
    else:
        # Fetch ALL Delivery Notes in Date Range
        conditions = {"docstatus": 1}
        if filters.get("company"):
            conditions["company"] = filters.get("company")

        # We need to filter by date range.
        # Since standard get_all doesn't support convenient date range in simple dict, use filters list
        date_filters = [
            ["Delivery Note", "docstatus", "=", 1],
            ["Delivery Note", "posting_date", ">=", filters.get("from_date")],
            ["Delivery Note", "posting_date", "<=", filters.get("to_date")],
        ]

        if filters.get("company"):
            date_filters.append(
                ["Delivery Note", "company", "=", filters.get("company")]
            )

        delivery_notes = frappe.get_all(
            "Delivery Note", filters=date_filters, pluck="name"
        )

        if delivery_notes:
            vouchers = get_related_vouchers(delivery_notes)

    if not vouchers:
        return []

    # 2. Fetch GL Entries for these vouchers
    gl_entries = frappe.db.sql(
        """
        SELECT
            posting_date,
            account,
            debit,
            credit,
            voucher_type,
            voucher_no,
            against,
            against_voucher,
            remarks,
            creation
        FROM `tabGL Entry`
        WHERE voucher_no IN %s
        AND is_cancelled = 0
        ORDER BY posting_date, voucher_no, creation
        """,
        (vouchers,),
        as_dict=True,
    )

    # 3. Process data (calculate running balance)
    data = []

    running_balance = 0.0
    for gle in gl_entries:
        running_balance += flt(gle.debit) - flt(gle.credit)
        gle["balance"] = running_balance
        data.append(gle)

    return data


def get_related_vouchers(delivery_notes):
    """
    Finds the Delivery Notes themselves and any Journal Entries
    that were created to handle their deferred expenses.
    Input: list of Delivery Note names
    """
    if not delivery_notes:
        return []

    vouchers = list(delivery_notes)

    # Find linked Journal Entries for ALL these DNs

    # 1. Check by Reference Name (Efficient bulk check)
    linked_jes_by_ref = frappe.get_all(
        "Journal Entry Account",
        filters={
            "reference_type": "Delivery Note",
            "reference_name": ["in", delivery_notes],
            "docstatus": 1,
        },
        pluck="parent",
        distinct=True,
    )
    vouchers.extend(linked_jes_by_ref)

    # 2. Check by Cheque No
    linked_jes_by_cheque = frappe.get_all(
        "Journal Entry",
        filters={"cheque_no": ["in", delivery_notes], "docstatus": 1},
        pluck="name",
    )
    vouchers.extend(linked_jes_by_cheque)

    # 3. Check by Remarks (Harder to do in bulk efficiently with LIKE, skipping for bulk view performance
    # unless specific DN is selected, but here we cover bulk mostly)
    # If list is small enough, we could try, but regex/like on many items is slow.
    # We will rely on Ref Name and Cheque No for bulk Mode.

    return list(set(vouchers))
