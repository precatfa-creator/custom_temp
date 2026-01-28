import frappe
from frappe import _


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
