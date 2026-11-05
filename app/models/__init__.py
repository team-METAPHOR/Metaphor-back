"""ORM models. Import new model modules here so Alembic can discover them."""

from app.models.artifact import Artifact
from app.models.audit import AuditEvent, IdempotencyRecord
from app.models.base import Base
from app.models.identity import (
    Membership,
    Organization,
    OrgInvite,
    PlatformOperator,
    Session,
    User,
)
from app.models.program import OriginVerification, Program, ProgramScopeRevision
from app.models.report import Clarification, Report, ReportDuplicate
from app.models.test_account import ProgramTestAccount
from app.models.validation_job import JobEvent, ModelUsage, QaResult, ValidationJob
from app.models.widget import ProgramWidgetSettings

__all__ = [
    "Artifact",
    "AuditEvent",
    "Base",
    "Clarification",
    "IdempotencyRecord",
    "JobEvent",
    "Membership",
    "ModelUsage",
    "OrgInvite",
    "Organization",
    "OriginVerification",
    "PlatformOperator",
    "Program",
    "ProgramScopeRevision",
    "ProgramTestAccount",
    "ProgramWidgetSettings",
    "QaResult",
    "Report",
    "ReportDuplicate",
    "Session",
    "User",
    "ValidationJob",
]
