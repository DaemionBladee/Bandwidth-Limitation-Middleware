"""Business grants available to User accounts; account management stays Admin-only."""

from typing import Literal

Permission = Literal[
    "read",
    "policies.create",
    "policies.update",
    "default_plan.create",
    "default_plan.update",
    "default_plan.delete",
    "subscribers.create",
    "assignments.update",
    "usage.reset",
    "enforcement.reconcile",
    "accounting.submit",
]

PERMISSION_OPTIONS = [
    {
        "code": "read",
        "label": "Read dashboard",
        "description": "View policies, subscribers, usage, status, and change history.",
    },
    {
        "code": "policies.create",
        "label": "Create policies",
        "description": "Add quota policies to the library.",
    },
    {
        "code": "policies.update",
        "label": "Edit policies",
        "description": "Change policies and their application timing for assigned subscribers.",
    },
    {
        "code": "default_plan.create",
        "label": "Create default plan",
        "description": "Choose or create the onboarding default when none is configured.",
    },
    {
        "code": "default_plan.update",
        "label": "Edit default plan",
        "description": "Edit or replace the default used for future subscribers.",
    },
    {
        "code": "default_plan.delete",
        "label": "Remove default plan",
        "description": "Stop automatic onboarding while retaining historical records.",
    },
    {
        "code": "subscribers.create",
        "label": "Add subscribers",
        "description": "Register accounting identities; an enabled default is assigned automatically.",
    },
    {
        "code": "assignments.update",
        "label": "Assign subscriber policies",
        "description": "Change individual policy assignments and their enabled status.",
    },
    {
        "code": "usage.reset",
        "label": "Reset subscriber usage",
        "description": "Grant a fresh allowance with an audited reason.",
    },
    {
        "code": "enforcement.reconcile",
        "label": "Reconcile enforcement",
        "description": "Queue another enforcement attempt.",
    },
    {
        "code": "accounting.submit",
        "label": "Submit mock accounting",
        "description": "Submit simulation records when mock accounting is enabled.",
    },
]
ALL_PERMISSIONS = [option["code"] for option in PERMISSION_OPTIONS]
