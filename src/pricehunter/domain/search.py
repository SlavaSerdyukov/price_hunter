from typing import Literal

from pydantic import BaseModel, Field


class ProviderSearchOutcome(BaseModel):
    provider: str
    status: Literal["NETWORK_FAILED", "PARTIAL", "SUCCESS", "EMPTY"] = "EMPTY"
    result_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    duplicate_count: int = 0
    errors: list[str] = Field(default_factory=list)
