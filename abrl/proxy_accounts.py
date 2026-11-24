"""Deployment-wide session caps for inventory backed by a shared provider account."""

import os
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy import select

from abrl.platform_models import ProxyLease


class AccountGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["decodo", "oxylabs"]
    username_refs: list[Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,47}$")]] = Field(min_length=1)
    max_sessions: int = Field(strict=True, ge=1, le=500)


@dataclass
class AccountCapacity:
    groups: dict[tuple[str, str], str]
    remaining: dict[str, int]

    @classmethod
    def load(cls, db):
        raw = os.getenv("ABRL_PROXY_ACCOUNT_GROUPS", "")
        capacity = cls({}, {})
        if not raw:
            return capacity
        try:
            settings = TypeAdapter(dict[str, AccountGroup]).validate_json(raw)
        except ValidationError:
            raise ValueError("Invalid ABRL_PROXY_ACCOUNT_GROUPS configuration") from None
        for name, group in settings.items():
            capacity.remaining[name] = group.max_sessions
            for reference in group.username_refs:
                key = (group.provider, reference.upper())
                if key in capacity.groups:
                    raise ValueError("Proxy credential reference belongs to multiple account groups")
                capacity.groups[key] = name
        if capacity.groups:
            # Quarantined sessions retain their active lease until explicit recovery.
            for config in db.scalars(select(ProxyLease.config).where(ProxyLease.status == "active")):
                capacity.reserve(config)
        return capacity

    def group(self, inventory):
        return self.groups.get((inventory.get("provider", "oxylabs"), inventory["username_ref"].upper()))

    def reserve(self, inventory):
        group = self.group(inventory)
        if group is not None:
            self.remaining[group] -= 1
