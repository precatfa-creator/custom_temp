// Copyright (c) 2024, ARD and contributors
// For license information, please see license.txt

frappe.query_reports["Delivery Note Deferred Ledger"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 1,
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -1),
			reqd: 1,
			width: "80px",
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
			width: "80px",
		},
		{
			fieldname: "delivery_note",
			label: __("Delivery Note"),
			fieldtype: "Link",
			options: "Delivery Note",
			mandatory: 0,
			reqd: 0,
			get_query: function () {
				var company = frappe.query_report.get_filter_value("company");
				return {
					filters: {
						docstatus: 1,
						company: company,
					},
				};
			},
		},
	],
};
