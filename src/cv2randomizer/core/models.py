"""Immutable generation and application settings models."""

from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any


@dataclass(frozen=True, slots=True)
class GenerationOptions:
    """User-selected randomizer systems and output preferences."""

    randomize_world_loot: bool = True
    randomize_healing: bool = True
    randomize_materials: bool = False
    randomize_shops: bool = True
    randomize_npc_traders: bool = False
    npc_traders_mode: str = "only"
    randomize_enemy_drops: bool = False
    randomize_blood_codes: bool = False
    randomize_boosters: bool = False
    randomize_extractor_recipes: bool = False
    cheaper_booster_extractor: bool = False
    low_blood_code_mastery_exp: bool = False
    randomize_initial_inventory: bool = False
    randomize_defensive_gadget_npc_rewards: bool = False
    randomize_bequeathed_formae_quest_rewards: bool = True
    unlock_npc_trader_shops: bool = False
    unlock_all_booster_slots: bool = False
    disable_tutorials: bool = False
    tap_to_skip_dialogue: bool = False
    motorcycle_unlocked: bool = False
    second_weapon_unlocked: bool = False
    mix_postgame_dlc_with_base: bool = False
    shops_normal: bool = True
    shops_luxury: bool = True
    shops_trade: bool = True
    mix_shops_with_world: bool = False
    install_after_generation: bool = True
    generate_spoiler_log: bool = False

    # Validate initialized settings
    def __post_init__(self) -> None:
        # Deprecated controls are retained as effective-value compatibility
        # attributes for existing planners. They are no longer user choices.
        if type(self.mix_shops_with_world) is not bool:
            raise ValueError("Option 'mix_shops_with_world' must be a boolean.")
        object.__setattr__(self, "mix_postgame_dlc_with_base", True)
        object.__setattr__(self, "randomize_defensive_gadget_npc_rewards", self.randomize_world_loot)
        object.__setattr__(self, "randomize_npc_traders", self.randomize_shops)
        object.__setattr__(self, "npc_traders_mode", "mixed" if (
            self.randomize_shops and self.randomize_world_loot and self.mix_shops_with_world
        ) else "only")
        object.__setattr__(self, "mix_shops_with_world", (
            self.randomize_shops and self.randomize_world_loot and self.mix_shops_with_world
        ))
        object.__setattr__(self, "shops_normal", True)
        object.__setattr__(self, "shops_luxury", True)
        object.__setattr__(self, "shops_trade", True)
        for definition in fields(self):
            if definition.name == "npc_traders_mode":
                if getattr(self, definition.name) not in ("only", "mixed"):
                    raise ValueError("NPC Trader mode must be 'only' or 'mixed'.")
                continue
            if type(getattr(self, definition.name)) is not bool:
                raise ValueError(f"Option {definition.name!r} must be a boolean.")
    # Convert settings to a dictionary
    def to_dict(self) -> dict[str, bool | str]:
        """Return current user settings, omitting retired controls."""
        values = asdict(self)
        for retired in (
            "randomize_npc_traders", "npc_traders_mode",
            "randomize_healing_upgrades",
            "randomize_defensive_gadget_npc_rewards", "mix_postgame_dlc_with_base",
            "shops_normal", "shops_luxury", "shops_trade",
            # Retired diagnostic: do not restore the temporary Pipe shop test
            # from older settings files.
            "test_unidentified_shop_materials",
        ):
            values.pop(retired, None)
        return values

    # Load settings from a dictionary
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GenerationOptions":
        """Load known options and ignore obsolete settings from older versions."""
        retired = {
            "randomize_npc_traders", "npc_traders_mode",
            "randomize_healing_upgrades",
            "randomize_defensive_gadget_npc_rewards", "mix_postgame_dlc_with_base",
            "shops_normal", "shops_luxury", "shops_trade",
            # Legacy Extractor options map to the current one-material cost option.
            "booster_material_cost_test",
            # These systems are mandatory in the current app UI. Ignore an old
            # saved opt-out so updating the app cannot silently disable them.
            "randomize_world_loot", "randomize_bequeathed_formae_quest_rewards",
        }
        allowed = {definition.name for definition in fields(cls)} - retired
        values: dict[str, bool | str] = {}
        # Older settings may contain the failed menu-refund option or the
        # earlier diagnostic toggle. Both now select the supported 1x recipe
        # cost behavior unless the user has saved the new setting explicitly.
        migrated_cost_option = data.get("cheaper_booster_extractor")
        if migrated_cost_option is None:
            migrated_cost_option = data.get("free_booster_extraction", data.get("booster_material_cost_test", False))
        if type(migrated_cost_option) is not bool:
            raise ValueError("Option 'cheaper_booster_extractor' must be a boolean.")
        values["cheaper_booster_extractor"] = migrated_cost_option
        for name, value in data.items():
            if name not in allowed:
                continue
            if name == "npc_traders_mode":
                if value not in ("only", "mixed"):
                    raise ValueError("Option 'npc_traders_mode' must be 'only' or 'mixed'.")
                values[name] = value
                continue
            if not isinstance(value, bool):
                raise ValueError(f"Option {name!r} must be a boolean.")
            values[name] = value
        # The app always enables the mandatory baseline pools. Keeping these
        # fields on the model still lets isolated internal tests exercise a
        # smaller pipeline, while old saved opt-outs cannot disable them.
        values["randomize_world_loot"] = True
        values["randomize_bequeathed_formae_quest_rewards"] = True
        return cls(**values)


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Preferences retained between application launches."""

    game_path: str | None = None
    # Kept only so older callers can load the model; it is no longer saved or
    # used by the UI. CV2.usmap is resolved from the application resources.
    mappings_path: str | None = None
    last_seed: str = ""
    output_dir: str | None = None
    last_options: GenerationOptions = field(default_factory=GenerationOptions)
    motorcycle_unlocked: bool = False
    second_weapon_unlocked: bool = False

    # Validate initialized settings
    def __post_init__(self) -> None:
        if type(self.motorcycle_unlocked) is not bool:
            raise ValueError("motorcycle_unlocked must be a boolean.")
        if type(self.second_weapon_unlocked) is not bool:
            raise ValueError("second_weapon_unlocked must be a boolean.")
        # Keep the legacy top-level preference synchronized with the canonical
        # generation option so old settings files migrate without losing it.
        second_weapon_unlocked = self.second_weapon_unlocked or self.last_options.second_weapon_unlocked
        if second_weapon_unlocked != self.second_weapon_unlocked:
            object.__setattr__(self, "second_weapon_unlocked", second_weapon_unlocked)
        if second_weapon_unlocked != self.last_options.second_weapon_unlocked:
            object.__setattr__(
                self,
                "last_options",
                replace(self.last_options, second_weapon_unlocked=second_weapon_unlocked),
            )

    # Return a JSON-ready representation
    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready representation."""
        saved_options = self.last_options.to_dict()
        # Randomize is the only user action. Keep the engine's
        # install flag for internal callers and manifests, but do not persist a
        # retired UI choice from older settings files.
        saved_options.pop("install_after_generation", None)
        return {
            "game_path": self.game_path,
            "last_seed": self.last_seed,
            "output_dir": self.output_dir,
            "last_options": saved_options,
            "motorcycle_unlocked": self.motorcycle_unlocked,
            "second_weapon_unlocked": self.second_weapon_unlocked,
        }

    # Load settings from a dictionary
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppSettings":
        """Build settings from a decoded JSON object."""
        game_path = data.get("game_path")
        # Ignore the former manually selected mapping path.
        last_seed = data.get("last_seed", "")
        output_dir = data.get("output_dir")
        options = data.get("last_options", {})
        if not isinstance(options, dict):
            raise ValueError("last_options must be an object.")
        motorcycle_unlocked = data.get("motorcycle_unlocked", options.get("motorcycle_unlocked", False))
        second_weapon_unlocked = options.get("second_weapon_unlocked", data.get("second_weapon_unlocked", False))
        if game_path is not None and not isinstance(game_path, str):
            raise ValueError("game_path must be a string or null.")
        if not isinstance(last_seed, str):
            raise ValueError("last_seed must be a string.")
        if output_dir is not None and not isinstance(output_dir, str):
            raise ValueError("output_dir must be a string or null.")
        if type(motorcycle_unlocked) is not bool:
            raise ValueError("motorcycle_unlocked must be a boolean.")
        if type(second_weapon_unlocked) is not bool:
            raise ValueError("second_weapon_unlocked must be a boolean.")
        restored_options = dict(options)
        restored_options.pop("install_after_generation", None)
        return cls(
            game_path=game_path,
            mappings_path=None,
            last_seed=last_seed,
            output_dir=output_dir,
            last_options=GenerationOptions.from_dict(restored_options),
            motorcycle_unlocked=motorcycle_unlocked,
            second_weapon_unlocked=second_weapon_unlocked,
        )
