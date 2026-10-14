"""Reusable canonical Article payload candidates for compatible reward pools."""

from __future__ import annotations

from cv2randomizer.game.article_reward_catalog import CanonicalArticleRewardCatalog
from cv2randomizer.game.catalogs import RewardPayload
from cv2randomizer.game.fname_identity import SoftObjectPathIdentity
from cv2randomizer.randomizer.world_loot import WorldLootRewardCandidate


# List canonical article world candidates
def canonical_article_world_candidates(
    catalog: CanonicalArticleRewardCatalog,
    *,
    include_boosters: bool = True,
) -> tuple[WorldLootRewardCandidate, ...]:
    """Expose metadata-defined Articles as candidates without fabricating locations."""
    return tuple(
        WorldLootRewardCandidate(
            candidate_id=f"canonical-article:{item.reward_category.casefold()}:{item.reward_id.casefold()}",
            rewards=(RewardPayload(
                item.asset_path,
                item.count,
                item.notification,
                item.content_class,
                SoftObjectPathIdentity.from_literal_path(item.asset_path),
            ),),
            source_location=None,
            candidate_category=item.reward_category.casefold(),
            canonical_identity=item.asset_path,
            identity_aliases=(item.asset_path,),
        )
        for item in catalog.world_reward_items
        if include_boosters or item.reward_category != "BOOSTER"
    )
