"""SEC Form 8-K item code mappings, priority classification, and non-8-K form descriptions."""

from typing import Dict, List, NamedTuple, Set, Tuple

# Official SEC Form 8-K item catalog
ITEM_DESCRIPTIONS: Dict[str, str] = {
    # Section 1: Registrant's Business and Operations
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.04": "Mine Safety - Reporting of Shutdowns and Patterns of Violations",
    "1.05": "Material Cybersecurity Incidents",
    
    # Section 2: Financial Information
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation or an Off-Balance Sheet Arrangement",
    "2.04": "Triggering Events Accelerating or Increasing a Direct Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    
    # Section 3: Securities and Trading Markets
    "3.01": "Notice of Delisting or Failure to Satisfy Continued Listing Rule; Transfer of Listing",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    
    # Section 4: Matters Related to Accountants and Financial Statements
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements or Related Audit Report",
    
    # Section 5: Corporate Governance and Management
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure of Directors or Principal Officers; Election/Appointment; Compensatory Arrangements",
    "5.03": "Amendments to Articles of Incorporation or Bylaws; Change in Fiscal Year",
    "5.04": "Temporary Suspension of Trading Under Employee Benefit Plans",
    "5.05": "Amendments to Code of Ethics, or Waiver of Provision",
    "5.06": "Change in Shell Company Status",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "5.08": "Shareholder Director Nominations",
    
    # Section 6: Asset-Backed Securities
    "6.01": "ABS Informational and Computational Material",
    "6.02": "Change of Servicer or Trustee",
    "6.03": "Change in Credit Enhancement or Other External Support",
    "6.04": "Failure to Make a Required Distribution",
    "6.05": "Securities Act Updating Disclosure",
    
    # Section 7: Regulation FD
    "7.01": "Regulation FD Disclosure",
    
    # Section 8: Other Events
    "8.01": "Other Events",
    
    # Section 9: Financial Statements and Exhibits
    "9.01": "Financial Statements and Exhibits",
}

# Items designated as HIGH PRIORITY per PRD specifications
HIGH_PRIORITY_ITEMS: Set[str] = {
    "1.01",  # Material Definitive Agreement
    "1.03",  # Bankruptcy or Receivership
    "2.02",  # Results of Operations and Financial Condition (Earnings)
    "3.01",  # Notice of Delisting
    "4.02",  # Non-Reliance on Previously Issued Financial Statements
    "5.02",  # Director/Officer Departure or Appointment
}

# Descriptions for non-8-K tracked forms
FORM_DESCRIPTIONS: Dict[str, str] = {
    "10-K": "Annual Report (comprehensive financial performance & audited statements)",
    "10-K/A": "Annual Report Amendment",
    "10-Q": "Quarterly Report (unaudited interim financial statements)",
    "10-Q/A": "Quarterly Report Amendment",
    "4": "Form 4: Statement of Changes in Beneficial Ownership (Insider Transaction)",
    "4/A": "Form 4 Amendment: Statement of Changes in Beneficial Ownership",
    "SC 13D": "Schedule 13D: Beneficial Ownership (>5% activist/active intent)",
    "SC 13D/A": "Schedule 13D Amendment: Beneficial Ownership",
    "SC 13G": "Schedule 13G: Beneficial Ownership (>5% passive institutional)",
    "SC 13G/A": "Schedule 13G Amendment: Beneficial Ownership",
}


class ParsedItem(NamedTuple):
    code: str
    description: str
    is_high_priority: bool


def normalize_item_code(code: str) -> str:
    """Normalize item code strings like 'Item 2.02' or '2.2' or '2.02'."""
    cleaned = code.strip()
    if cleaned.lower().startswith("item "):
        cleaned = cleaned[5:].strip()
    return cleaned


def parse_items(items_raw: str | None) -> List[ParsedItem]:
    """
    Parse a comma-separated or space-separated items string (e.g. '2.02,9.01')
    into a list of ParsedItem namedtuples.
    Unknown codes render as 'Item X.XX (unrecognized)'.
    """
    if not items_raw:
        return []

    # Items can be comma-separated or space-separated in some EDGAR filings
    raw_tokens = [t.strip() for t in str(items_raw).replace(";", ",").split(",") if t.strip()]
    results: List[ParsedItem] = []

    for token in raw_tokens:
        code = normalize_item_code(token)
        if not code:
            continue
        
        if code in ITEM_DESCRIPTIONS:
            desc = ITEM_DESCRIPTIONS[code]
            is_high = code in HIGH_PRIORITY_ITEMS
        else:
            desc = f"Item {code} (unrecognized)"
            is_high = False

        results.append(ParsedItem(code=code, description=desc, is_high_priority=is_high))

    return results


def is_high_priority_filing(form: str, parsed_items: List[ParsedItem]) -> bool:
    """
    Determine if an alert is HIGH PRIORITY.
    - For 8-K filings: True if ANY parsed item is in HIGH_PRIORITY_ITEMS.
      Note: 9.01 alone NEVER makes an alert high priority.
    - For amendments (8-K/A): Same priority rule based on items.
    - Other forms can be configured or defaulted.
    """
    # Check if any item in parsed items is high priority
    return any(item.is_high_priority for item in parsed_items)


def get_form_description(form: str) -> str:
    """Return descriptive text for non-8-K form types or default representation."""
    cleaned_form = form.strip()
    if cleaned_form in FORM_DESCRIPTIONS:
        return FORM_DESCRIPTIONS[cleaned_form]
    if cleaned_form.startswith("8-K"):
        return "Current Report"
    return f"Filing Form {cleaned_form}"
