from pydantic import BaseModel, ConfigDict, Field, field_validator


class MerchantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")
    display_name: str = Field(min_length=1, max_length=100)
    primary_domain: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9.-]{1,199}$")

    @field_validator("display_name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A nonempty display name is required")
        return value.strip()
