"""Exact serialized FName identities used by Unreal soft object paths."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FNameIdentity:
    """An FName's literal NameMap value and its independent numeric suffix."""

    value: str
    number: int = 0

    # Validate initialized settings
    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value:
            raise ValueError("FName literal value must be a non-empty string.")
        if isinstance(self.number, bool) or not isinstance(self.number, int) or self.number < 0:
            raise ValueError("FName number must be a non-negative integer.")

    # Read resolved
    @property
    def resolved(self) -> str:
        """Return Unreal's displayed name without losing the source identity."""
        return self.value if self.number == 0 else f"{self.value}_{self.number - 1}"


@dataclass(frozen=True, slots=True)
class SoftObjectPathIdentity:
    """The two exact FNames that make up an FTopLevelAssetPath."""

    package_name: FNameIdentity
    asset_name: FNameIdentity

    # Build literal path
    @classmethod
    def from_literal_path(cls, path: str) -> SoftObjectPathIdentity:
        """Represent a catalog path literally with FName Number zero."""
        asset_name = path.rsplit("/", 1)[-1]
        return cls(FNameIdentity(path), FNameIdentity(asset_name))

    # Resolve resolved path
    @property
    def resolved_path(self) -> str:
        return self.package_name.resolved

    # Read resolved asset name
    @property
    def resolved_asset_name(self) -> str:
        return self.asset_name.resolved
