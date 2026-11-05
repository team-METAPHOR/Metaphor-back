from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

# Opaque labels only: no spaces or free text, so an answer key or reference plan can't be
# smuggled in. Validation errors never echo the value (app/core/errors.py).
EvalId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")]


class EvalTagUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eval_run_id: EvalId
    case_id: EvalId
    repeat_id: EvalId


class EvalTag(BaseModel):
    report_id: str
    eval_run_id: str
    case_id: str
    repeat_id: str
    tagged_at: datetime
