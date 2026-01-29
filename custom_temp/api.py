import frappe
from frappe import _


@frappe.whitelist()
def make_delivery_note(source_name, target_doc=None):
    """
    Override standard make_delivery_note to map deferred revenue details
    of Sales Invoice to deferred expense details on Delivery Note.
    """
    from erpnext.accounts.doctype.sales_invoice.sales_invoice import (
        make_delivery_note as standard_make_delivery_note,
    )

    doclist = standard_make_delivery_note(source_name, target_doc)

    # Convert doclist back to a document object if it's a dict
    if isinstance(doclist, dict):
        doclist = frappe.get_doc(doclist)

    # Get source items to fetch deferred revenue details
    source_items = frappe.get_all(
        "Sales Invoice Item",
        filters={"parent": source_name},
        fields=[
            "name",
            "enable_deferred_revenue",
            "deferred_revenue_account",
            "service_start_date",
            "service_end_date",
            "service_stop_date",
        ],
    )

    source_item_map = {item.name: item for item in source_items}

    for item in doclist.get("items"):
        # standard mapping sets si_detail to source Sales Invoice Item name
        if item.si_detail and item.si_detail in source_item_map:
            si_item = source_item_map[item.si_detail]

            if get_item_deferred_details(item.item_code, doclist.company).get(
                "enable_deferred_expense"
            ):
                item.enable_deferred_expense = 1

            # Map deferred date details if enabled in Sales Invoice
            if si_item.enable_deferred_revenue:
                item.service_start_date = si_item.service_start_date
                item.service_end_date = si_item.service_end_date
                item.service_stop_date = si_item.service_stop_date

                # For the account, let's fetch the item's default deferred expense account
                # as the deferred_revenue_account is a liability account.
                if not item.deferred_expense_account:
                    details = get_item_deferred_details(item.item_code, doclist.company)
                    item.deferred_expense_account = details.get(
                        "deferred_expense_account"
                    )

    return doclist


@frappe.whitelist()
def get_item_deferred_details(item_code=None, company=None):
    """
    Get deferred expense details for an item.
    Called from Delivery Note Item when an item is selected.

    Args:
        item_code: The item code to look up
        company: The company context

    Returns:
        dict with enable_deferred_expense, no_of_months_exp, deferred_expense_account
    """
    result = {
        "enable_deferred_expense": 0,
        "no_of_months_exp": 0,
        "deferred_expense_account": "",
    }

    if not item_code:
        return result

    # Get item deferred expense settings
    item = frappe.db.get_value(
        "Item",
        item_code,
        ["enable_deferred_expense", "no_of_months_exp"],
        as_dict=1,
    )

    if item and item.enable_deferred_expense:
        result["enable_deferred_expense"] = item.enable_deferred_expense
        result["no_of_months_exp"] = item.no_of_months_exp or 0

        # Get deferred expense account from Item Default
        if company:
            account = frappe.db.get_value(
                "Item Default",
                {"parent": item_code, "company": company},
                "deferred_expense_account",
            )
            result["deferred_expense_account"] = account or ""

    return result
