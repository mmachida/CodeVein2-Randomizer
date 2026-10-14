"""Canonical, provenance-preserving package requests for Extract.

Catalogs are authored by independent randomizer systems. This registry joins
their package requests before inventory resolution or extraction while keeping
the systems and reasons that asked for each source visible for diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable, Iterable, Mapping


# Build canonical package key
def canonical_package_key(package_path: str) -> str:
    """Return the comparison key used for Unreal package identity."""
    if not isinstance(package_path, str):
        raise TypeError("Dependency package path must be text.")
    normalized = package_path.strip().replace("\\", "/")
    if normalized[:6].casefold() == "/game/":
        normalized = "/Game/" + normalized[6:]
    if not normalized.startswith("/Game/") or any(part in {"", ".", ".."} for part in normalized.split("/")[1:]):
        raise ValueError(f"Invalid dependency package path: {package_path!r}.")
    return normalized.casefold()


# Find missing required packages
def find_missing_required_packages(
    required_paths: Iterable[str],
    resolved_paths: Mapping[str, str],
    resolve_installed_path: Callable[[str], str | None],
    extracted_paths: Iterable[str],
) -> tuple[str, ...]:
    """Find required requests absent from an extracted package set.

    ``resolved_paths`` preserves the game's exact package spelling for Retoc,
    while extracted inventory keys are compared case-insensitively. Keep the
    two representations distinct so path spelling cannot look like a missing
    package during final cache validation.
    """
    resolved_by_key = {
        canonical_package_key(package): resolved
        for package, resolved in resolved_paths.items()
    }
    extracted_keys = {canonical_package_key(package) for package in extracted_paths}
    missing: list[str] = []
    for package in required_paths:
        key = canonical_package_key(package)
        resolved = resolved_by_key.get(key)
        if resolved is None:
            resolved = resolve_installed_path(package)
        if resolved is None or canonical_package_key(resolved) not in extracted_keys:
            missing.append(package)
    return tuple(missing)


@dataclass(frozen=True, slots=True)
class DependencyPackageRequest:
    """One canonical package and the complete provenance of its requests."""

    canonical_package_path: str
    package_path: str
    requested_by_systems: tuple[str, ...]
    reasons: tuple[str, ...]
    required_components: tuple[str, ...]
    request_count: int
    spellings: tuple[str, ...]


@dataclass(slots=True)
class _RequestBuilder:
    package_spellings: set[str] = field(default_factory=set)
    systems: set[str] = field(default_factory=set)
    reasons: set[str] = field(default_factory=set)
    components: set[str] = field(default_factory=set)
    requests_by_system: dict[str, int] = field(default_factory=dict)
    request_count: int = 0


class DependencyRequestRegistry:
    """Globally deduplicate Extract package work by case-insensitive identity."""

    # Initialize this object
    def __init__(self) -> None:
        self._requests: dict[str, _RequestBuilder] = {}

    # Add the requested reward item
    def add(
        self,
        package_path: str,
        *,
        system: str,
        reason: str,
        required_components: Iterable[str] = (),
    ) -> None:
        key = canonical_package_key(package_path)
        if not system or not system.strip() or not reason or not reason.strip():
            raise ValueError("Dependency requests require a system and a reason.")
        builder = self._requests.setdefault(key, _RequestBuilder())
        builder.package_spellings.add(package_path.strip().replace("\\", "/"))
        builder.systems.add(system.strip())
        builder.reasons.add(reason.strip())
        builder.components.update(value for value in required_components if value)
        builder.requests_by_system[system.strip()] = builder.requests_by_system.get(system.strip(), 0) + 1
        builder.request_count += 1

    # Add many
    def add_many(self, package_paths: Iterable[str], *, system: str, reason: str) -> None:
        for package_path in package_paths:
            self.add(package_path, system=system, reason=reason)

    # Set components
    def set_components(self, package_path: str, components: Iterable[str]) -> None:
        builder = self._requests.get(canonical_package_key(package_path))
        if builder is not None:
            builder.components.update(value for value in components if value)

    # Collect generation requests
    @property
    def requests(self) -> tuple[DependencyPackageRequest, ...]:
        result: list[DependencyPackageRequest] = []
        for key, builder in sorted(self._requests.items()):
            spellings = tuple(sorted(builder.package_spellings, key=lambda value: (value.casefold(), value)))
            result.append(DependencyPackageRequest(
                canonical_package_path=key,
                package_path=spellings[0],
                requested_by_systems=tuple(sorted(builder.systems, key=str.casefold)),
                reasons=tuple(sorted(builder.reasons, key=str.casefold)),
                required_components=tuple(sorted(builder.components, key=str.casefold)),
                request_count=builder.request_count,
                spellings=spellings,
            ))
        return tuple(result)

    # Resolve package paths
    @property
    def package_paths(self) -> tuple[str, ...]:
        return tuple(request.package_path for request in self.requests)

    # Count requested packages
    @property
    def request_count(self) -> int:
        return sum(request.request_count for request in self.requests)

    # Count unique package count
    @property
    def unique_package_count(self) -> int:
        return len(self._requests)

    # Count duplicate request count
    @property
    def duplicate_request_count(self) -> int:
        return self.request_count - self.unique_package_count

    # Count records by system
    def system_counts(self) -> dict[str, dict[str, int]]:
        requested: dict[str, int] = {}
        unique: dict[str, int] = {}
        for builder in self._requests.values():
            for system, count in builder.requests_by_system.items():
                unique[system] = unique.get(system, 0) + 1
                requested[system] = requested.get(system, 0) + count
        return {
            name: {"requests": requested[name], "unique_packages": unique[name]}
            for name in sorted(requested, key=str.casefold)
        }

    # Find duplicate table rows
    def duplicate_rows(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "canonical_package_path": request.canonical_package_path,
                "package_path": request.package_path,
                "request_count": request.request_count,
                "requested_by_systems": ";".join(request.requested_by_systems),
                "reasons": ";".join(request.reasons),
                "required_components": ";".join(request.required_components),
                "spellings": ";".join(request.spellings),
            }
            for request in self.requests if request.request_count > 1 or len(request.requested_by_systems) > 1
        )

    # Summarize recorded diagnostics
    def summary(self) -> dict[str, object]:
        return {
            "total_requests": self.request_count,
            "unique_packages": self.unique_package_count,
            "duplicate_requests": self.duplicate_request_count,
            "duplicate_ratio": (
                self.duplicate_request_count / self.request_count if self.request_count else 0.0
            ),
            "packages_requested_by_multiple_systems": sum(
                len(request.requested_by_systems) > 1 for request in self.requests
            ),
            "systems": self.system_counts(),
        }
