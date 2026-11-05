"""ORM models. Import new model modules here so Alembic can discover them."""

from app.models.artifact import Artifact
from app.models.audit import AuditEvent, IdempotencyRecord
from app.models.base import Base
from app.models.evaluation import ReportEvalTag
from app.models.identity import (
    Membership,
    Organization,
    OrgInvite,
    PlatformOperator,
    Session,
    User,
)
from app.models.live_preview import LivePreview, LiveTicket
from app.models.program import OriginVerification, Program, ProgramScopeRevision
from app.models.report import Clarification, Report, ReportDuplicate
from app.models.test_account import ProgramTestAccount
from app.models.test_condition import ProgramTestCondition
from app.models.validation_job import (
    JobEvent,
    ModelUsage,
    ProviderBackoff,
    QaResult,
    ValidationJob,
    ValidationJobCondition,
)
from app.models.widget import ProgramWidgetSettings

__all__ = [
    "Artifact",
    "AuditEvent",
    "Base",
    "Clarification",
    "IdempotencyRecord",
    "JobEvent",
    "LivePreview",
    "LiveTicket",
    "Membership",
    "ModelUsage",
    "ProviderBackoff",
    "OrgInvite",
    "Organization",
    "OriginVerification",
    "PlatformOperator",
    "Program",
    "ProgramScopeRevision",
    "ProgramTestAccount",
    "ProgramTestCondition",
    "ProgramWidgetSettings",
    "QaResult",
    "Report",
    "ReportDuplicate",
    "ReportEvalTag",
    "Session",
    "User",
    "ValidationJob",
    "ValidationJobCondition",
]
