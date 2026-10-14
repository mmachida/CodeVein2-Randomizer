"""Seed-independent installed Blood Code coverage and prepared catalog descriptors."""
from __future__ import annotations

from dataclasses import asdict
from collections import Counter

from .blood_code_catalog import BloodCodeCatalog
from .content_availability import PackageInventory

DESCRIPTOR_REVISION = 4


# Read installed blood code articles
def installed_blood_code_articles(inventory: PackageInventory) -> tuple[str, ...]:
    return tuple(sorted((path for _, path in inventory.package_path_spellings
                         if path.startswith("/Game/Blueprint/Article/BloodCode/")
                         and path.rsplit("/", 1)[-1].startswith("DA_BloodCode_")), key=str.casefold))


# Read blood code coverage
def blood_code_coverage(catalog: BloodCodeCatalog, inventory: PackageInventory) -> dict:
    rewards = {slot.original_blood_code_package.casefold() for slot in catalog.slots}
    articles = []
    for package in installed_blood_code_articles(inventory):
        if "/Debug/" in package:
            status, reason = "PROTECTED_DEBUG", "Debug Article is not an obtainable reward."
        elif package.rsplit("/", 1)[-1] == "DA_BloodCode_Lou_01":
            status, reason = (
                "PROTECTED_STARTING_UNRESOLVED",
                "The true New Game initializer is unproven. Replacing DA_InitialInventory_Player.BloodCodes adds an extra Blood Code, so Lou_01 stays outside the randomization pool.",
            )
        elif package.casefold() in rewards:
            status, reason = "ELIGIBLE", "Confirmed reward mechanism is catalogued."
        else:
            status, reason = "UNSUPPORTED", "Installed Article found; player-facing grant owner is not yet proven."
        content = ("DLC" if "/DLC/" in package else "AFTER_ENDING" if "/AfterEnd" in package else "BASE")
        articles.append({"package": package, "asset": package.rsplit("/", 1)[-1],
                         "content_class": content, "status": status, "reason": reason})
    locations = []
    for slot in catalog.slots:
        packages = slot.physical_source_packages
        missing = [package for package in (*packages, slot.original_blood_code_package) if not inventory.contains(package)]
        locations.append({**asdict(slot), "status": "MISSING_SOURCE" if missing else "ELIGIBLE",
                          "missing_packages": missing})
    return {"descriptor_revision": DESCRIPTOR_REVISION, "articles": articles, "locations": locations,
            "article_status_counts": dict(Counter(item["status"] for item in articles)),
            "location_status_counts": dict(Counter(item["status"] for item in locations)),
            "mechanism_counts": dict(Counter(slot.location_kind for slot in catalog.slots))}
