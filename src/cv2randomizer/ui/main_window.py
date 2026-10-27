"""Main desktop window for the randomizer application."""

import logging
import os
import secrets
from dataclasses import replace
from pathlib import Path

from platformdirs import user_data_dir
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QCloseEvent,
    QDesktopServices,
    QFont,
    QIcon,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from cv2randomizer import (
    APP_DESCRIPTION,
    APP_DISPLAY_NAME,
    APP_DISPLAY_VERSION,
    APP_NAME,
    GITHUB_URL,
    NEXUS_MODS_URL,
    STEAM_URL,
)
from cv2randomizer.core.config import SettingsError, SettingsStore
from cv2randomizer.core.determinism import RANDOMIZER_ALGORITHM_VERSION
from cv2randomizer.core.models import AppSettings, GenerationOptions
from cv2randomizer.core.logging_config import activity_log_path
from cv2randomizer.core.resources import (
    ApplicationResourceError,
    application_root,
    application_resource,
    resolve_app_icon,
    resolve_cv2_usmap,
)
from cv2randomizer.core.workspace import WorkspaceManager, clear_output_directory
from cv2randomizer.generation.pipeline import GenerationEngine, GenerationError, GenerationRequest, GenerationResult
from cv2randomizer.packaging.installer import InstallManager, InstallationError, RemovalResult
from cv2randomizer.core.seed import normalize_seed
from cv2randomizer.game.detection_worker import GameDetectionThread
from cv2randomizer.game.locator import check_installation
from cv2randomizer.game.game_data_cache import (
    GameDataCacheSnapshot,
    GameDataCacheState,
    GameDataCacheStore,
)
from cv2randomizer.game.versioning import VersionDetection
from cv2randomizer.ui.generation_worker import GenerationCancelled, GenerationWorker, TaskOutcome

LOGGER = logging.getLogger(__name__)


class OutlinedCheckBox(QCheckBox):
    """Draw a clear check mark on the custom outlined indicator."""

    # Paint Event
    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self.isChecked():
            return

        option = QStyleOptionButton()
        self.initStyleOption(option)
        indicator = self.style().subElementRect(
            QStyle.SubElement.SE_CheckBoxIndicator, option, self
        )
        if not indicator.isValid():
            return

        color = QColor("#ffffff" if self.isEnabled() else "#929292")
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(
            QPen(
                color,
                2.0,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.drawLine(
            indicator.left() + 3,
            indicator.center().y(),
            indicator.left() + 6,
            indicator.bottom() - 4,
        )
        painter.drawLine(
            indicator.left() + 6,
            indicator.bottom() - 4,
            indicator.right() - 2,
            indicator.top() + 3,
        )


class _ClickableOptionLabel(QLabel):
    # Initialize this object
    def __init__(self, text: str, checkbox: OutlinedCheckBox, parent: QWidget) -> None:
        super().__init__(text, parent)
        self._checkbox = checkbox
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # Read mouse Press Event
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._checkbox.toggle()
            event.accept()
            return
        super().mousePressEvent(event)


class DescribedCheckBox(QWidget):
    """Checkbox with inline, wrapping description text and normal checkbox behavior."""

    toggled = Signal(bool)

    # Initialize this object
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._checkbox = OutlinedCheckBox(self)
        self._checkbox.setAccessibleName(text)
        self._label = _ClickableOptionLabel(text, self._checkbox, self)
        self._label.setAccessibleName(text)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        layout.addWidget(self._checkbox, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._label, 1, Qt.AlignmentFlag.AlignVCenter)
        self._checkbox.toggled.connect(self.toggled.emit)
        self.setAccessibleName(text)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    # Read text
    def text(self) -> str:
        return self._label.text()

    # Set Text
    def setText(self, text: str) -> None:
        self._label.setText(text)
        self._label.setAccessibleName(text)
        self._checkbox.setAccessibleName(text)
        self.setAccessibleName(text)

    # Check Checked
    def isChecked(self) -> bool:
        return self._checkbox.isChecked()

    # Set Checked
    def setChecked(self, checked: bool) -> None:
        self._checkbox.setChecked(checked)

    # Toggle the selected option
    def toggle(self) -> None:
        self._checkbox.toggle()

    # Set Tool Tip
    def setToolTip(self, text: str) -> None:
        super().setToolTip(text)
        self._checkbox.setToolTip(text)
        self._label.setToolTip(text)


class MainWindow(QMainWindow):
    """Application controls and diagnostics."""

    # Initialize this object
    def __init__(
        self,
        *,
        generation_engine: GenerationEngine | None = None,
        installer: InstallManager | None = None,
        settings_store: SettingsStore | None = None,
    ) -> None:
        super().__init__()
        self._worker: GenerationWorker | None = None
        self._task_kind: str | None = None
        self._last_progress_stage: str | None = None
        self._scan_workers: set[GameDetectionThread] = set()
        self._settings_store = settings_store or SettingsStore()
        try:
            self._settings = self._settings_store.load()
        except SettingsError as error:
            LOGGER.error("Could not load settings: %s", error)
            self._settings = AppSettings()
        self._game_data_cache_store = (
            getattr(generation_engine, "game_data_cache_store", None)
            if generation_engine is not None else None
        ) or GameDataCacheStore()
        self._cache_path = self._game_data_cache_store.root
        self._game_data_cache_snapshot: GameDataCacheSnapshot | None = None
        self._game_version_detection: VersionDetection | None = None
        self._cache_ready = False
        try:
            self._mappings_path = resolve_cv2_usmap()
            self._mappings_error: str | None = None
        except ApplicationResourceError as error:
            self._mappings_path = None
            self._mappings_error = str(error)
        self._data_path = Path(user_data_dir(APP_NAME))
        self._log_file_path = activity_log_path()
        self._log_path = self._log_file_path.parent
        self._output_path = self._data_path / "output"
        if self._settings.output_dir:
            try:
                configured_output = Path(self._settings.output_dir).expanduser().resolve()
                configured_output.relative_to(self._data_path.resolve())
                self._output_path = configured_output
            except (OSError, ValueError):
                LOGGER.warning(
                    "Ignoring a saved output directory outside app-owned data: %s",
                    self._settings.output_dir,
                )
        self._installer = installer or InstallManager()
        self._generation_engine = generation_engine or GenerationEngine(
            workspace_manager=WorkspaceManager(self._output_path),
            game_data_cache_store=self._game_data_cache_store,
            installer=self._installer,
            allow_local_unverified=True,
        )

        self.setWindowTitle(f"{APP_DISPLAY_NAME} {APP_DISPLAY_VERSION}")
        self.setWindowIcon(QIcon(str(resolve_app_icon())))
        self.resize(1000, 500)
        self.setMinimumSize(900, 500)
        self._apply_neutral_desktop_theme()
        self._build_window()
        self._make_random_seed()
        self._restore_settings()

    # Build window
    def _build_window(self) -> None:
        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 1, 8, 1)
        layout.setSpacing(2)
        layout.addLayout(self._build_game_and_seed_row())
        self.mapping_warning_label = QLabel()
        self.mapping_warning_label.setStyleSheet("color: #8a4b08;")
        self.mapping_warning_label.setVisible(False)
        layout.addWidget(self.mapping_warning_label)

        self.tabs = QTabWidget()
        self.tabs.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.randomizer_page = self._build_randomizer_tab()
        self.tabs.addTab(self.randomizer_page, "Item Randomizer")
        self.rules_page = self._build_rules_tab()
        self.tabs.addTab(self.rules_page, "Rules")
        self.tabs.addTab(self._build_diagnostics_tab(), "Diagnostic")
        self.about_page = self._build_about_tab()
        self.tabs.addTab(self.about_page, "About")
        # Let the options pane occupy the flexible space down to the pinned
        # action row. The Item Randomizer page scrolls if its grid cannot fit.
        layout.addWidget(self.tabs, 1)
        self.actions_row = self._build_actions_row()
        self.actions_row.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        layout.addWidget(self.actions_row)
        self.status_group = self._build_status_panel()
        self.status_group.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        layout.addWidget(self.status_group)
        self.setCentralWidget(root)
        self._standardize_control_heights()
        for control in (
            self.randomize_healing_check,
            self.materials_check,
            self.shops_check,
            self.unlock_npc_trader_shops_check,
            self.enemy_drops_check,
            self.blood_codes_check,
            self.boosters_check,
            self.extractor_recipes_check,
            self.cheaper_booster_extractor_check,
            self.initial_inventory_check,
            self.unlock_all_booster_slots_check,
            self.disable_tutorials_check,
        ):
            control.toggled.connect(self._update_mapping_warning)
        self.game_path.textEdited.connect(self._on_game_path_edited)
        self.game_path.textChanged.connect(
            lambda value: self.open_install_folder_button.setEnabled(bool(value.strip()))
        )
        self.game_path.editingFinished.connect(self._check_edited_game_path)
        self._update_mapping_warning()
        self._apply_cache_state()

    # Apply neutral desktop theme
    @staticmethod
    def _apply_neutral_desktop_theme() -> None:
        """Use a restrained light desktop palette with standard-looking controls."""
        application = QApplication.instance()
        if application is None:
            return
        application.setStyle("Fusion")
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, Qt.GlobalColor.white)
        palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.black)
        palette.setColor(QPalette.ColorRole.Base, Qt.GlobalColor.white)
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#f6f6f6"))
        palette.setColor(QPalette.ColorRole.ToolTipBase, Qt.GlobalColor.white)
        palette.setColor(QPalette.ColorRole.ToolTipText, Qt.GlobalColor.black)
        palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.black)
        palette.setColor(QPalette.ColorRole.Button, QColor("#e7e7e7"))
        palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.black)
        palette.setColor(QPalette.ColorRole.BrightText, Qt.GlobalColor.red)
        palette.setColor(QPalette.ColorRole.Highlight, QColor("#0078d4"))
        palette.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.white)
        palette.setColor(QPalette.ColorRole.Mid, Qt.GlobalColor.gray)
        application.setPalette(palette)
        application.setFont(QFont("Segoe UI", 9))
        application.setStyleSheet(
            "QWidget { background: #f2f2f2; color: #202020; }"
            "QLineEdit { background: #ffffff; border: 1px solid #999999; padding: 3px 5px; }"
            "QPushButton { background: #e7e7e7; border: 1px solid #a7a7a7;"
            " border-radius: 2px; padding: 4px 9px; min-height: 20px; }"
            "QPushButton:hover { background: #ededed; border-color: #777777; }"
            "QPushButton:pressed { background: #d7d7d7; }"
            "QPushButton:disabled { color: #858585; background: #eeeeee; }"
            "QPushButton[needsExtraction=\"true\"] { background: #fff0c2; border-color: #b07a00;"
            " color: #4b3a10; font-weight: 600; }"
            "QPushButton[needsExtraction=\"true\"]:hover { background: #ffe7a1; }"
            "QPushButton[needsExtraction=\"true\"]:disabled { background: #f3ecd9;"
            " border-color: #c7b989; color: #938b71; }"
            "QPushButton[randomizeReady=\"true\"] { background: #dff2e1;"
            " border-color: #36834b; color: #194b25; font-weight: 600; }"
            "QPushButton[randomizeReady=\"true\"]:hover { background: #cce9d1;"
            " border-color: #286d3a; }"
            "QPushButton[randomizeReady=\"true\"]:pressed { background: #b8dbbf; }"
            "QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #202020;"
            " border-radius: 2px; background: #ffffff; }"
            "QCheckBox::indicator:checked { background: #0078d4; border-color: #005a9e; }"
            "QCheckBox:disabled { color: #929292; }"
            "QCheckBox::indicator:disabled { background: #ededed; border-color: #c6c6c6; }"
            "QCheckBox::indicator:checked:disabled { background: #dddddd; border-color: #c3c3c3; }"
            "QGroupBox { border: 1px solid #c7c7c7; border-radius: 2px;"
            " margin-top: 8px; padding-top: 4px; font-weight: 600; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 7px;"
            " padding: 0 3px; background: #f2f2f2; }"
            "QTabWidget::pane { border: 1px solid #c7c7c7; top: -1px; }"
            "QTabBar::tab { background: #e8e8e8; border: 1px solid #c7c7c7;"
            " padding: 5px 11px; margin-right: 2px; }"
            "QTabBar::tab:selected { background: #f2f2f2; border-bottom-color: #f2f2f2; }"
            "QProgressBar { background: #ffffff; border: 1px solid #8f8f8f;"
            " border-radius: 2px; min-height: 18px; max-height: 18px;"
            " text-align: center; color: #202020; }"
            "QProgressBar::chunk { background: #1683c4; }"
        )

    # Build game and seed row
    def _build_game_and_seed_row(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        layout.setSpacing(5)
        layout.addWidget(QLabel("Game Path"))
        self.game_path = QLineEdit()
        self.game_path.setPlaceholderText(
            r"C:\Program Files (x86)\Steam\steamapps\common\CodeVein2"
        )
        layout.addWidget(self.game_path, 1)
        self.game_browse_button = QPushButton("Browse…")
        self.game_browse_button.clicked.connect(self._browse_game_path)
        layout.addWidget(self.game_browse_button)

        layout.addWidget(QLabel("Seed"))
        self.seed_input = QLineEdit()
        self.seed_input.setPlaceholderText("Enter a number or text seed")
        layout.addWidget(self.seed_input, 1)
        self.copy_seed_button = QPushButton("Copy")
        self.copy_seed_button.setToolTip("Copy the current seed to the clipboard.")
        self.copy_seed_button.clicked.connect(self._copy_seed)
        layout.addWidget(self.copy_seed_button)
        self.random_seed_button = QPushButton("Random")
        self.random_seed_button.clicked.connect(self._make_random_seed)
        layout.addWidget(self.random_seed_button)
        return layout

    # Build actions row
    def _build_actions_row(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.game_data_status_label = QLabel("Game Data: select a folder to verify")
        self.game_data_status_label.setWordWrap(False)
        self.extract_game_data_button = QPushButton("Extract Game Data")
        self.extract_game_data_button.clicked.connect(self._start_game_data_extraction)
        layout.addWidget(self.extract_game_data_button)
        layout.addWidget(self.game_data_status_label)
        layout.addStretch(1)
        self.generate_install_button = QPushButton("Randomize")
        self.generate_install_button.clicked.connect(self._start_generation)
        layout.addWidget(self.generate_install_button)

        self.remove_button = QPushButton("Uninstall Randomizer")
        self.remove_button.clicked.connect(self._start_remove)
        self.remove_button.setToolTip("Uninstall the Randomizer files recorded as owned by this app.")
        layout.addWidget(self.remove_button)
        self.open_install_folder_button = QPushButton("Open Install Folder")
        self.open_install_folder_button.setToolTip("Open the selected game's Paks/~mods folder.")
        self.open_install_folder_button.clicked.connect(self._open_install_folder)
        self.open_install_folder_button.setEnabled(bool(self.game_path.text().strip()))
        layout.addWidget(self.open_install_folder_button)
        return row

    # Read standardize control heights
    def _standardize_control_heights(self) -> None:
        """Keep input fields and their adjacent buttons aligned across tabs."""
        for control in self.findChildren(QLineEdit):
            control.setFixedHeight(30)
        for button in self.findChildren(QPushButton):
            button.setFixedHeight(30)

    # Build randomizer tab
    def _build_randomizer_tab(self) -> QWidget:
        scroll_area = QScrollArea()
        scroll_area.setObjectName("randomizerScrollArea")
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll_area.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        page = QWidget()
        # Keep the paired rows compact instead of stretching their group boxes
        # to consume spare height; this avoids empty space under short option lists.
        page.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        layout = QVBoxLayout(page)
        layout.setContentsMargins(5, 10, 5, 0)
        layout.setSpacing(0)

        # Build feature group
        def feature_group(title: str, checkbox_text: str, description: str):
            group = QGroupBox(title)
            group_layout = QVBoxLayout(group)
            group_layout.setContentsMargins(7, 0, 7, 0)
            group_layout.setSpacing(6)
            checkbox = DescribedCheckBox(f"{checkbox_text} ({description.rstrip('.')})")
            checkbox.setToolTip(description)
            group_layout.addSpacing(8)
            group_layout.addWidget(checkbox)
            group_layout.addSpacing(6)
            return group, group_layout, checkbox

        healing_ingredients_group = QGroupBox("Healing Items and Ingredients")
        healing_ingredients_layout = QVBoxLayout(healing_ingredients_group)
        healing_ingredients_layout.setContentsMargins(7, 0, 7, 0)
        healing_ingredients_layout.setSpacing(6)
        healing_ingredients_layout.addSpacing(8)
        self.randomize_healing_check = DescribedCheckBox(
            "Randomize Healing Items (Nectars and Blood Beads join World Loot)"
        )
        healing_ingredients_layout.addWidget(self.randomize_healing_check)
        self.materials_check = DescribedCheckBox(
            "Randomize Renewable Ingredients (shuffle only among ingredient materials)"
        )
        self.materials_group = healing_ingredients_group
        self.materials_check.setToolTip(
            "Only material pickups that respawn after resting are randomized. "
            "Every pickup of the same material gets the same replacement across the world."
        )
        healing_ingredients_layout.addWidget(self.materials_check)
        healing_ingredients_layout.addSpacing(6)
        enemy_drop_group, _enemy_layout, self.enemy_drops_check = feature_group(
            "Enemy Drops",
            "Randomize Enemy Drops",
            "Shuffles enemy drops, including drops shared with bosses; one-time rewards join World Loot.",
        )
        booster_group = QGroupBox("Boosters")
        booster_layout = QVBoxLayout(booster_group)
        booster_layout.setContentsMargins(7, 0, 7, 0)
        booster_layout.setSpacing(6)
        booster_layout.addSpacing(8)
        self.boosters_check = DescribedCheckBox(
            "Mix Boosters with World Loot"
        )
        self.boosters_check.setToolTip("Mix Booster rewards into World Loot.")
        booster_layout.addWidget(self.boosters_check)
        self.extractor_recipes_check = DescribedCheckBox(
            "Randomize Booster Extractor"
        )
        self.extractor_recipes_check.setToolTip("Shuffle Booster Extractor rewards.")
        booster_layout.addWidget(self.extractor_recipes_check)
        booster_layout.addSpacing(6)
        initial_inventory_blood_codes_group = QGroupBox(
            "Initial Inventory and Blood Codes"
        )
        initial_inventory_blood_codes_layout = QVBoxLayout(
            initial_inventory_blood_codes_group
        )
        initial_inventory_blood_codes_layout.setContentsMargins(7, 0, 7, 0)
        initial_inventory_blood_codes_layout.setSpacing(6)
        initial_inventory_blood_codes_layout.addSpacing(8)
        self.initial_inventory_check = DescribedCheckBox(
            "Randomize Initial Inventory (Weapon, Defensive Formae and Jail)"
        )
        initial_inventory_blood_codes_layout.addWidget(self.initial_inventory_check)
        self.initial_inventory_check.setToolTip(
            "The first Blood Code stays vanilla for the opening quest."
        )
        self.blood_codes_check = DescribedCheckBox(
            "Randomize Blood Codes (Shuffle eligible rewards)"
        )
        self.blood_codes_check.setToolTip("Shuffle eligible rewards.")
        initial_inventory_blood_codes_layout.addWidget(self.blood_codes_check)
        initial_inventory_blood_codes_layout.addSpacing(6)
        shops_group = QGroupBox("Shops")
        shops_layout = QVBoxLayout(shops_group)
        shops_layout.setContentsMargins(7, 0, 7, 0)
        shops_layout.setSpacing(6)
        shops_layout.addSpacing(8)
        self.shops_check = DescribedCheckBox(
            "Randomize Shops and NPC Traders (Finite-stock items)"
        )
        self.shops_check.setToolTip(
            "Shuffle finite-stock Shop and NPC Trader rewards."
        )
        shops_layout.addWidget(self.shops_check)
        self.mix_shops_with_world_check = DescribedCheckBox(
            "Mix Shop rewards with World Loot"
        )
        self.mix_shops_with_world_check.setToolTip(
            "Eligible Shop rewards can swap with World Loot. Prices and stock stay fixed."
        )
        shops_layout.addWidget(self.mix_shops_with_world_check)
        shops_layout.addSpacing(6)
        self.shops_check.toggled.connect(self._update_shop_mix_enabled)
        key_quest_group = QGroupBox("Key Items and Quests")
        key_quest_layout = QVBoxLayout(key_quest_group)
        key_quest_layout.setContentsMargins(7, 0, 7, 0)
        key_quest_layout.setSpacing(6)
        key_quest_layout.addSpacing(8)
        self.randomize_keys_check = OutlinedCheckBox("Randomize Keys (In Progress)")
        self.randomize_keys_check.setEnabled(False)
        key_quest_layout.addWidget(self.randomize_keys_check)
        self.randomize_quest_items_check = OutlinedCheckBox(
            "Randomize Quest Items (In Progress)"
        )
        self.randomize_quest_items_check.setEnabled(False)
        key_quest_layout.addWidget(self.randomize_quest_items_check)
        key_quest_layout.addSpacing(6)

        miscellaneous_group = QGroupBox("Miscellaneous")
        miscellaneous_outer_layout = QVBoxLayout(miscellaneous_group)
        miscellaneous_outer_layout.setContentsMargins(7, 0, 7, 0)
        miscellaneous_outer_layout.setSpacing(0)
        miscellaneous_outer_layout.addSpacing(8)
        miscellaneous_layout = QGridLayout()
        miscellaneous_layout.setContentsMargins(0, 0, 0, 0)
        miscellaneous_layout.setHorizontalSpacing(10)
        miscellaneous_layout.setVerticalSpacing(6)
        miscellaneous_layout.setColumnStretch(0, 1)
        miscellaneous_layout.setColumnStretch(1, 1)
        self.unlock_all_booster_slots_check = DescribedCheckBox(
            "Unlock all 6 Booster Slots (New Game only)"
        )
        self.unlock_all_booster_slots_check.setToolTip(
            "New games start with all six slots. Existing saves are unchanged."
        )
        miscellaneous_layout.addWidget(self.unlock_all_booster_slots_check, 0, 0)
        self.unlock_npc_trader_shops_check = DescribedCheckBox(
            "Unlock all NPC Trader Shop Tiers"
        )
        self.unlock_npc_trader_shops_check.setToolTip(
            "Unlock every affinity-gated NPC Trader tier."
        )
        miscellaneous_layout.addWidget(self.unlock_npc_trader_shops_check, 1, 0)
        self.motorcycle_unlocked_check = DescribedCheckBox(
            "Unlock Motorcycle at Start (New Game only)"
        )
        self.motorcycle_unlocked_check.setToolTip(
            "Start a new game with motorcycle access unlocked."
        )
        miscellaneous_layout.addWidget(self.motorcycle_unlocked_check, 2, 0)
        self.second_weapon_unlocked_check = DescribedCheckBox(
            "Unlock Second Weapon Slot (New Game only)"
        )
        self.second_weapon_unlocked_check.setToolTip(
            "New games start with the second weapon slot unlocked."
        )
        miscellaneous_layout.addWidget(self.second_weapon_unlocked_check, 3, 0)

        self.disable_tutorials_check = DescribedCheckBox(
            "Disable Tutorial Pop-ups"
        )
        self.disable_tutorials_check.setToolTip(
            "Skip first-time tutorial pop-ups. The Tutorial menu stays available."
        )
        miscellaneous_layout.addWidget(self.disable_tutorials_check, 0, 1)
        self.low_blood_code_mastery_exp_check = DescribedCheckBox(
            "Unrestricted Blood Code Mastery"
        )
        self.low_blood_code_mastery_exp_check.setToolTip(
            "Use Lou 1's mastery EXP and remove enemy level restrictions."
        )
        miscellaneous_layout.addWidget(self.low_blood_code_mastery_exp_check, 1, 1)
        self.cheaper_booster_extractor_check = DescribedCheckBox(
            "Cheaper Booster Extractor (only 1x of each material)"
        )
        self.cheaper_booster_extractor_check.setToolTip(
            "Each required material costs 1. Haze cost is unchanged."
        )
        miscellaneous_layout.addWidget(self.cheaper_booster_extractor_check, 2, 1)
        self.tap_to_skip_dialogue_check = DescribedCheckBox(
            "Tap Instead of Holding to Skip Dialogue (Experimental)"
        )
        self.tap_to_skip_dialogue_check.setToolTip(
            "Tap the game's configured Interact button to skip eligible dialogue. "
            "Keyboard and controller remaps are supported; the game's skip-prohibited check remains active."
        )
        miscellaneous_layout.addWidget(self.tap_to_skip_dialogue_check, 3, 1)
        miscellaneous_outer_layout.addLayout(miscellaneous_layout)
        miscellaneous_outer_layout.addSpacing(6)

        for left_group, right_group in (
            (initial_inventory_blood_codes_group, booster_group),
            (shops_group, enemy_drop_group),
            (healing_ingredients_group, key_quest_group),
        ):
            row_height = max(
                left_group.minimumSizeHint().height(),
                right_group.minimumSizeHint().height(),
            )
            left_group.setMaximumHeight(row_height)
            right_group.setMaximumHeight(row_height)
        miscellaneous_group.setMaximumHeight(
            miscellaneous_group.minimumSizeHint().height()
        )

        # A shared grid keeps opposing groups aligned by row; Miscellaneous
        # spans both columns so its longer option list has room to breathe.
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(5)
        grid.setVerticalSpacing(1)
        for column in (0, 1):
            grid.setColumnStretch(column, 1)
        for row, (left_group, right_group) in enumerate(
            (
                (initial_inventory_blood_codes_group, booster_group),
                (shops_group, enemy_drop_group),
            )
        ):
            grid.addWidget(left_group, row, 0)
            grid.addWidget(right_group, row, 1)
        grid.addWidget(healing_ingredients_group, 2, 0)
        grid.addWidget(key_quest_group, 2, 1)
        grid.addWidget(miscellaneous_group, 3, 0, 1, 2)
        layout.addLayout(grid)
        scroll_area.setWidget(page)
        self.randomizer_options_page = page
        return scroll_area

    # Build rules tab
    def _build_rules_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 8, 10, 8)

        rules = QTextBrowser(page)
        rules.setObjectName("rulesTextBrowser")
        rules.setFrameShape(QFrame.Shape.NoFrame)
        rules.setOpenLinks(False)
        rules.setOpenExternalLinks(False)
        rules.setReadOnly(True)
        rules.setHtml(
            """<!doctype html>
<html><head><style>
body { font-family: 'Segoe UI'; font-size: 10pt; color: #202020; }
h2 { font-size: 15pt; margin: 2px 0 6px; }
h3 { font-size: 11pt; margin: 12px 0 4px; }
p { margin: 3px 0 6px; }
ul { margin-top: 2px; margin-bottom: 5px; }
li { margin: 3px 0; }
</style></head><body>
<h2>How Randomize works</h2>
<p>Randomize creates a separate mod and leaves the original game files untouched. To remove it, click <b>Uninstall Randomizer</b>, or delete the Randomizer files from the game's <code>~mods</code> folder.</p>
<p>Choose a seed and the options you want, then click <b>Randomize</b>. The same seed, options, and game version give you the same result.</p>

<h3>Compatibility</h3>
<p>The randomizer is fully compatible with Code Vein 2 <b>v2.0.3</b>. Other versions may have missing or different files. Items that are not compatible are left unchanged and counted by category in <code>Spoiler.txt</code>.</p>

<h3>Items that can change</h3>
<ul>
  <li><b>World Loot</b> is always randomized. Items found on the ground and some one-time rewards can swap. <i>Example: ground item A → ground item B.</i></li>
  <li><b>Healing Items</b> adds Nectars and Blood Beads to World Loot. <i>Example: Nectar → another ground item.</i></li>
  <li><b>Renewable Ingredients</b> changes only materials that come back after resting at a Mistle. The same material always becomes the same new item, and its pickup looks the same. <i>Example: Sap of Life → Onion.</i></li>
  <li><b>Enemy Drops</b> shuffles supported enemy rewards. Some one-time rewards can also move into World Loot. <i>Example: enemy drop A → enemy drop B.</i></li>
  <li><b>Blood Codes</b> shuffles supported Blood Code rewards. <i>Example: Blood Code A → Blood Code B.</i></li>
  <li><b>Mix Boosters with World Loot</b> lets Booster rewards swap with ground items. <i>Example: Booster A → ground item B.</i></li>
  <li><b>Booster Extractor</b> shuffles the items given by its recipes. <i>Example: recipe reward A → recipe reward B.</i></li>
  <li><b>Shops and NPC Traders</b> shuffle items in limited-stock offers. The price and stock stay with each offer. <i>Example: shop item A → shop item B.</i></li>
  <li><b>Mix Shop rewards with World Loot</b> lets some shop items swap with ground items. <i>Example: shop item A → ground item B.</i></li>
  <li><b>Initial Inventory</b> shuffles starting weapons, Defensive Formae, and Jail separately. <i>Example: starting weapon A → starting weapon B.</i></li>
</ul>

<h3>Never randomized for now</h3>
<ul>
  <li>Pre-order items are left alone.</li>
  <li>The Tools Shop in MagMell is left alone.</li>
  <li>Items with infinite shop stock are left alone.</li>
</ul>
<h3>Keys and quests</h3>
<p>Protected keys and story items stay where they are. Quest rewards are still being reviewed: some can change, and others are not included yet. <i>Example: protected key A stays key A; quest reward A → item B.</i></p>

<h3>Other options</h3>
<p>These options unlock features or change small settings. They do not shuffle item rewards. <i>Example: Disable Tutorial Pop-ups turns those messages off.</i></p>
<p><b>Tap Instead of Holding to Skip Dialogue (Experimental)</b> lets a quick press of your configured Interact button skip eligible dialogue, whether you use a keyboard or controller. The game's skip-prohibited check still applies, and this option is skipped if the installed version has an unsupported layout.</p>

<h3>Seed and game files</h3>
<p>The spoiler shows each changed item and what it became. Extract Game Data saves files that Randomize can reuse next time.</p>
</body></html>"""
        )
        layout.addWidget(rules)
        return page

    # Build diagnostics tab
    def _build_diagnostics_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        form.addRow("App version", QLabel(APP_DISPLAY_VERSION))
        self._game_path_diagnostic = self._readonly_path_label("Not selected")
        form.addRow("Game path", self._game_path_diagnostic)
        self._game_version_label = QLabel("Not detected")
        form.addRow("Game version", self._game_version_label)
        self._game_data_cache_diagnostic = QLabel("Not extracted")
        self._game_data_cache_diagnostic.setWordWrap(True)
        form.addRow("Game data", self._game_data_cache_diagnostic)
        form.addRow("Cache", self._readonly_path_label(self._cache_path))
        form.addRow("Output", self._readonly_path_label(self._output_path))
        form.addRow("Settings", self._readonly_path_label(self._settings_store.path))
        layout.addLayout(form)

        buttons = QHBoxLayout()
        open_output_button = QPushButton("Open Output Folder")
        open_output_button.clicked.connect(lambda: self._open_folder(self._output_path))
        buttons.addWidget(open_output_button)
        open_settings_button = QPushButton("Open Settings Folder")
        open_settings_button.clicked.connect(
            lambda: self._open_folder(self._settings_store.path.parent)
        )
        buttons.addWidget(open_settings_button)
        open_cache_button = QPushButton("Open Cache Folder")
        open_cache_button.clicked.connect(lambda: self._open_folder(self._cache_path))
        buttons.addWidget(open_cache_button)
        self.clear_output_button = QPushButton("Clear Old Output")
        self.clear_output_button.clicked.connect(self._clear_output)
        buttons.addWidget(self.clear_output_button)
        self.clear_cache_button = QPushButton("Clear Extracted Game Data")
        self.clear_cache_button.clicked.connect(self._clear_cache)
        buttons.addWidget(self.clear_cache_button)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return page

    # Build about tab
    def _build_about_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(0)

        content = QHBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(14)
        self.about_icon_label = QLabel()
        self.about_icon_label.setFixedSize(72, 72)
        self.about_icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.about_icon_label.setAccessibleName("Application icon")
        icon_path: Path | None = None
        pixmap = QPixmap()
        try:
            icon_path = application_resource("ico", "icon.png")
            if icon_path.is_file():
                pixmap = QPixmap(str(icon_path))
        except (ApplicationResourceError, OSError, RuntimeError) as error:
            LOGGER.warning("Could not load the About tab icon: %s", error)
        png_loaded = not pixmap.isNull()
        if pixmap.isNull():
            pixmap = self.windowIcon().pixmap(72, 72)
        if not pixmap.isNull():
            self.about_icon_label.setPixmap(
                pixmap.scaled(
                    72,
                    72,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        self.about_icon_label.setProperty("iconLoaded", not pixmap.isNull())
        self.about_icon_label.setProperty("iconPngLoaded", png_loaded)
        self.about_icon_label.setProperty("iconPath", str(icon_path) if icon_path else "")
        content.addWidget(self.about_icon_label, 0, Qt.AlignmentFlag.AlignTop)

        details = QVBoxLayout()
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(3)
        title = QLabel(APP_DISPLAY_NAME)
        title.setObjectName("aboutApplicationName")
        title_font = QFont(title.font())
        title_font.setPointSize(title_font.pointSize() + 2)
        title_font.setBold(True)
        title.setFont(title_font)
        details.addWidget(title)

        version = QLabel(f"Version {APP_DISPLAY_VERSION}")
        version.setObjectName("aboutApplicationVersion")
        details.addWidget(version)
        description = QLabel(APP_DESCRIPTION)
        description.setObjectName("aboutApplicationDescription")
        details.addWidget(description)

        links = QVBoxLayout()
        links.setContentsMargins(0, 4, 0, 0)
        links.setSpacing(2)
        self.about_github_link = self._about_link("GitHub", GITHUB_URL)
        self.about_steam_link = self._about_link("Code Vein 2 on Steam", STEAM_URL)
        self.about_nexus_link = self._about_link("Nexus Mods", NEXUS_MODS_URL)
        for link in (
            self.about_github_link,
            self.about_steam_link,
            self.about_nexus_link,
        ):
            links.addWidget(link)
        details.addLayout(links)
        content.addLayout(details)
        content.addStretch(1)
        layout.addLayout(content)
        layout.addStretch(1)
        return page

    # Read about link
    def _about_link(self, title: str, url: str | None) -> QLabel:
        label = QLabel()
        label.setObjectName(
            "about" + "".join(part.title() for part in title.split()) + "Link"
        )
        label.setAccessibleName(title)
        if not url:
            label.setText(f"{title} — Coming soon")
            label.setEnabled(False)
            return label

        link_palette = label.palette()
        link_palette.setColor(QPalette.ColorRole.Link, QColor("#005A9E"))
        link_palette.setColor(QPalette.ColorRole.LinkVisited, QColor("#5B2C83"))
        label.setPalette(link_palette)
        label.setText(
            f'<a href="{url}" style="color:#005A9E; text-decoration:underline;">'
            f"{title}</a>"
        )
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        label.setOpenExternalLinks(False)
        label.linkActivated.connect(
            lambda _href, link_url=url, link_title=title: self._open_about_link(
                link_url, link_title
            )
        )
        return label

    # Open about link
    def _open_about_link(self, url: str, title: str) -> None:
        try:
            opened = QDesktopServices.openUrl(QUrl(url))
        except Exception:
            LOGGER.exception("Could not open About link %s (%s)", title, url)
            opened = False
        if not opened:
            LOGGER.warning("Could not open About link %s (%s)", title, url)
            QMessageBox.warning(
                self,
                "Could not open link",
                f"The {title} link could not be opened in your default browser.",
            )

    # Read readonly path label
    @staticmethod
    def _readonly_path_label(value: str | Path) -> QLabel:
        label = QLabel(str(value))
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    # Build status panel
    def _build_status_panel(self) -> QGroupBox:
        group = QGroupBox("Status")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(8, 5, 8, 6)
        layout.setSpacing(4)
        status_row = QHBoxLayout()
        self.status_label = QLabel("Select a game folder to verify extracted data.")
        self.status_label.setWordWrap(False)
        status_row.addWidget(self.status_label, 1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setMaximumWidth(88)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_task)
        status_row.addWidget(self.cancel_button)
        layout.addLayout(status_row)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        layout.addWidget(self.progress_bar)
        return group

    # Restore settings
    def _restore_settings(self) -> None:
        if self._settings.game_path:
            self.game_path.setText(self._settings.game_path)
            self.open_install_folder_button.setEnabled(True)
            self._game_path_diagnostic.setText(self._settings.game_path)
            self._start_game_check(self._settings.game_path)
        self._apply_generation_options(self._settings.last_options)
        self._update_mapping_warning()

    # Apply generation options
    def _apply_generation_options(self, options: GenerationOptions) -> None:
        self.randomize_healing_check.setChecked(options.randomize_healing)
        self.materials_check.setChecked(options.randomize_materials)
        self.shops_check.setChecked(options.randomize_shops)
        self.mix_shops_with_world_check.setChecked(options.mix_shops_with_world)
        self.unlock_npc_trader_shops_check.setChecked(options.unlock_npc_trader_shops)
        self.enemy_drops_check.setChecked(options.randomize_enemy_drops)
        self.blood_codes_check.setChecked(options.randomize_blood_codes)
        self.boosters_check.setChecked(options.randomize_boosters)
        self.extractor_recipes_check.setChecked(options.randomize_extractor_recipes)
        self.cheaper_booster_extractor_check.setChecked(options.cheaper_booster_extractor)
        self.low_blood_code_mastery_exp_check.setChecked(options.low_blood_code_mastery_exp)
        self.initial_inventory_check.setChecked(options.randomize_initial_inventory)
        self.unlock_all_booster_slots_check.setChecked(options.unlock_all_booster_slots)
        self.motorcycle_unlocked_check.setChecked(self._settings.motorcycle_unlocked)
        self.second_weapon_unlocked_check.setChecked(options.second_weapon_unlocked)
        self.disable_tutorials_check.setChecked(options.disable_tutorials)
        self.tap_to_skip_dialogue_check.setChecked(options.tap_to_skip_dialogue)
        self._update_shop_mix_enabled()

    # Read current generation options
    def _current_generation_options(self) -> GenerationOptions:
        return GenerationOptions(
            randomize_world_loot=True,
            randomize_healing=self.randomize_healing_check.isChecked(),
            randomize_materials=self.materials_check.isChecked(),
            randomize_shops=self.shops_check.isChecked(),
            randomize_enemy_drops=self.enemy_drops_check.isChecked(),
            randomize_blood_codes=self.blood_codes_check.isChecked(),
            randomize_boosters=self.boosters_check.isChecked(),
            randomize_extractor_recipes=self.extractor_recipes_check.isChecked(),
            cheaper_booster_extractor=self.cheaper_booster_extractor_check.isChecked(),
            low_blood_code_mastery_exp=self.low_blood_code_mastery_exp_check.isChecked(),
            randomize_initial_inventory=self.initial_inventory_check.isChecked(),
            randomize_bequeathed_formae_quest_rewards=True,
            mix_shops_with_world=self.mix_shops_with_world_check.isChecked(),
            unlock_npc_trader_shops=self.unlock_npc_trader_shops_check.isChecked(),
            unlock_all_booster_slots=self.unlock_all_booster_slots_check.isChecked(),
            motorcycle_unlocked=self.motorcycle_unlocked_check.isChecked(),
            second_weapon_unlocked=self.second_weapon_unlocked_check.isChecked(),
            disable_tutorials=self.disable_tutorials_check.isChecked(),
            tap_to_skip_dialogue=self.tap_to_skip_dialogue_check.isChecked(),
            install_after_generation=True,
            generate_spoiler_log=False,
        )

    # Update shop mix enabled
    def _update_shop_mix_enabled(self) -> None:
        """Keep Shop/World mixing available while Shops are enabled."""
        self.mix_shops_with_world_check.setEnabled(
            self.shops_check.isChecked()
        )

    # Save settings
    def _save_settings(self) -> None:
        self._settings = replace(
            self._settings,
            game_path=self.game_path.text() or None,
            last_seed=self.seed_input.text(),
            output_dir=str(self._output_path),
            last_options=self._current_generation_options(),
            motorcycle_unlocked=self.motorcycle_unlocked_check.isChecked(),
            second_weapon_unlocked=self.second_weapon_unlocked_check.isChecked(),
        )
        try:
            self._settings_store.save(self._settings)
        except SettingsError as error:
            LOGGER.error("Could not save settings: %s", error)
            self.status_label.setText("Could not save application settings; see the log.")
            self._record_activity(str(error))

    # Close Event
    def closeEvent(self, event: QCloseEvent) -> None:
        for scan_worker in tuple(self._scan_workers):
            scan_worker.wait()
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_cancel()
            if not self._worker.wait(5000):
                event.ignore()
                self.status_label.setText("Waiting for the current task to stop safely…")
                return
        self._save_settings()
        super().closeEvent(event)

    # Start game check
    def _start_game_check(self, game_path: str) -> None:
        self._game_version_label.setText("Checking…")
        self._game_data_cache_snapshot = None
        self._cache_ready = False
        self.game_data_status_label.setText("Checking extracted game data…")
        self.status_label.setText("Verifying extracted game data…")
        self.progress_bar.setRange(0, 0)
        self._apply_cache_state()
        worker = GameDetectionThread(
            Path(game_path), self, cache_store=self._game_data_cache_store,
            allow_local_unverified=True,
        )
        self._scan_workers.add(worker)
        worker.result_ready.connect(self._on_game_check_result)
        worker.cache_result_ready.connect(self._on_game_cache_result)
        worker.finished.connect(self._finish_game_check)
        worker.start()

    # Read on game check result
    def _on_game_check_result(self, game_path: str, result: object) -> None:
        if not self._same_game_path(game_path, self.game_path.text()):
            return
        detection = result
        if not isinstance(detection, VersionDetection):
            self.status_label.setText("Could not interpret game detection result.")
            return
        self._game_version_detection = detection
        if detection.local_unverified:
            if detection.display_version:
                self._game_version_label.setText(detection.display_version)
                self._game_version_label.setToolTip(
                    "Version label matched by executable MD5 for display only. "
                    "Extraction and generation still use local-unverified validation."
                )
            else:
                self._game_version_label.setText("Local Validation (unverified)")
                self._game_version_label.setToolTip(detection.reason)
        else:
            self._game_version_label.setText(
                detection.display_version or detection.version_id or "Unknown / unsupported"
            )
            self._game_version_label.setToolTip("")
        self.status_label.setText("Validating cached game data…")
        self._record_activity(
            "Game build detected for local validation; missing optional assets will be skipped."
            if detection.local_unverified else detection.reason
        )

    # Read on game cache result
    def _on_game_cache_result(self, game_path: str, result: object) -> None:
        if not self._same_game_path(game_path, self.game_path.text()):
            return
        if not isinstance(result, GameDataCacheSnapshot):
            self._game_data_cache_snapshot = None
            self._cache_ready = False
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
            self.game_data_status_label.setText("Game Data: Check failed")
            self._game_data_cache_diagnostic.setText(str(result))
            self.status_label.setText("Could not verify extracted game data.")
            LOGGER.warning("Extracted game-data cache check failed: %s", result)
            self._apply_cache_state()
            return
        self._game_data_cache_snapshot = result
        self._cache_ready = result.ready
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100 if result.ready else 0)
        cache_label = self._cache_state_label(result)
        if result.ready:
            cache_label += self._game_data_version_suffix()
        self.game_data_status_label.setText(f"Game Data: {cache_label}")
        self._game_data_cache_diagnostic.setText(result.reason)
        self.status_label.setText(
            "Extracted game data ready."
            if result.ready else "Extracted game data needs validation or extraction."
        )
        if result.reason:
            self._record_activity(result.reason)
        self._apply_cache_state()

    # Resolve same game path
    @staticmethod
    def _same_game_path(first: str, second: str) -> bool:
        """Compare paths independent of slash style, case, and trailing separators."""
        if not first.strip() or not second.strip():
            return False
        first_path = os.path.normcase(os.path.abspath(os.path.expanduser(first.strip())))
        second_path = os.path.normcase(os.path.abspath(os.path.expanduser(second.strip())))
        return first_path == second_path

    # Cache state label
    @staticmethod
    def _cache_state_label(snapshot: GameDataCacheSnapshot) -> str:
        return {
            GameDataCacheState.NOT_EXTRACTED: "Not Extracted",
            GameDataCacheState.READY: "Ready",
            GameDataCacheState.OUTDATED: "Outdated",
            GameDataCacheState.PREPARED_OUTDATED: "Prepared Data Needs Update",
            GameDataCacheState.INVALID: "Invalid",
            GameDataCacheState.EXTRACTING: "Extracting…",
        }[snapshot.state]

    # Read game data version suffix
    def _game_data_version_suffix(self) -> str:
        """Show local-validation state and a recognized game version in the action row."""
        detection = self._game_version_detection
        if detection is None:
            return ""
        details: list[str] = []
        if detection.local_unverified:
            details.append("local validation")
        if detection.display_version:
            version = detection.display_version
            details.append(version if version.casefold().startswith("v") else f"v{version}")
        return f" ({', '.join(details)})" if details else ""

    # Read on game path edited
    def _on_game_path_edited(self, _value: str) -> None:
        self.open_install_folder_button.setEnabled(bool(self.game_path.text().strip()))
        self._game_data_cache_snapshot = None
        self._game_version_detection = None
        self._cache_ready = False
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.game_data_status_label.setText("Game Data: Select/check the game installation")
        self._game_data_cache_diagnostic.setText("Game data must be checked for the selected installation.")
        self.status_label.setText("Select a game folder to verify extracted data.")
        self._apply_cache_state()

    # Check edited game path
    def _check_edited_game_path(self) -> None:
        value = self.game_path.text().strip()
        if value and (self._game_version_detection is None or str(Path(value)) != str(Path(self._settings.game_path or ""))):
            self._game_path_diagnostic.setText(value)
            self._start_game_check(value)

    # Start game data extraction
    def _start_game_data_extraction(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        game_value = self.game_path.text().strip()
        detection = self._game_version_detection
        if not game_value or detection is None or not detection.usable_for_local_validation:
            QMessageBox.warning(self, "Valid game installation required", "Select a valid Code Vein 2 installation first.")
            return
        if self._mappings_path is None or not self._mappings_path.is_file():
            QMessageBox.warning(
                self,
                "Application resource missing",
                self._mappings_error or "The bundled CV2.usmap mapping is missing or unreadable.",
            )
            return
        try:
            prepared_only_rebuild = (
                self._game_data_cache_snapshot is not None
                and self._game_data_cache_snapshot.state is GameDataCacheState.PREPARED_OUTDATED
            )
            replace_existing_cache = self._cache_ready or self._game_data_cache_store.has_contents()
        except (OSError, ValueError) as error:
            self._show_task_error(error)
            return
        if (
            replace_existing_cache and not prepared_only_rebuild
            and not self._confirm_extracted_cache_action("Re-extract Game Data")
        ):
            return

        self._game_data_cache_snapshot = None
        self._cache_ready = False
        self.game_data_status_label.setText("Game Data: Extracting…")
        self._game_data_cache_diagnostic.setText(
            "Rebuilding prepared files from the existing extracted data."
            if prepared_only_rebuild else "Building and validating a replacement cache."
        )
        self.status_label.setText(
            "Rebuilding prepared data from the raw cache…"
            if prepared_only_rebuild else (
                "Building a replacement for the current extraction…"
                if replace_existing_cache else "Extracting game data…"
            )
        )
        self.progress_bar.setRange(0, 0)
        self._apply_cache_state()

        # Extract game data
        def extract_game_data(report):
            return self._generation_engine.extract_game_data(
                Path(game_value).expanduser(), self._mappings_path, progress=report,
                reuse_cached_raw=prepared_only_rebuild,
            )

        self._launch_task(
            extract_game_data,
            kind="extract-game-data",
            cancellable=True,
        )
        self._record_activity("Extract Game Data started.")

    # Read finish game check
    def _finish_game_check(self) -> None:
        worker = self.sender()
        if isinstance(worker, GameDetectionThread):
            self._scan_workers.discard(worker)
            worker.deleteLater()
            self._apply_cache_state()

    # Resolve browse game path
    def _browse_game_path(self) -> None:
        initial_directory = self._game_path_browse_start_directory()
        selected = QFileDialog.getExistingDirectory(
            self, "Select Code Vein 2 folder", initial_directory
        )
        if selected:
            self.game_path.setText(selected)
            self._game_path_diagnostic.setText(selected)
            self._save_settings()
            self._start_game_check(selected)

    # Choose game path browse start
    def _game_path_browse_start_directory(self) -> str:
        candidates: list[Path] = []
        current_value = self.game_path.text().strip()
        if current_value:
            candidates.append(Path(current_value).expanduser())
        candidates.append(application_root())

        for candidate in candidates:
            if self._directory_can_be_browsed(candidate):
                return str(candidate)
        return str(Path.home())

    # Check directory can be browsed
    @staticmethod
    def _directory_can_be_browsed(directory: Path) -> bool:
        try:
            return directory.is_dir() and os.access(directory, os.R_OK)
        except OSError:
            return False

    # Update mapping warning
    def _update_mapping_warning(self, *_args) -> None:
        has_selected = self._has_selected_randomizer()
        show_warning = has_selected and (self._mappings_path is None or not self._mappings_path.is_file())
        self.mapping_warning_label.setText(
            self._mappings_error or "The bundled CV2.usmap mapping is missing or unreadable."
        )
        self.mapping_warning_label.setVisible(show_warning)

    # Make random seed
    def _make_random_seed(self) -> None:
        self.seed_input.setText(str(secrets.randbelow(2**63)))

    # Copy seed
    def _copy_seed(self) -> None:
        QApplication.clipboard().setText(self.seed_input.text())

    # Start generation
    def _start_generation(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        if not self._has_selected_randomizer():
            QMessageBox.information(self, "No options selected", "Select at least one randomizer option.")
            return
        if not self._cache_ready or self._game_data_cache_snapshot is None or self._game_data_cache_snapshot.entry_path is None:
            QMessageBox.warning(
                self,
                "Game data required",
                "Extract and validate game data before generating a randomizer.",
            )
            return
        game_value = self.game_path.text().strip()
        if not game_value:
            QMessageBox.warning(self, "Game path required", "Select the Code Vein 2 installation folder first.")
            return
        if self._mappings_path is None or not self._mappings_path.is_file():
            QMessageBox.warning(
                self,
                "Application resource missing",
                self._mappings_error or "The bundled CV2.usmap mapping is missing or unreadable.",
            )
            return

        seed_text = self.seed_input.text()
        if not seed_text.strip():
            QMessageBox.warning(self, "Seed required", "Enter a seed or use Random before generating.")
            return
        normalized_seed = normalize_seed(seed_text)
        answer = QMessageBox.question(
            self,
            "Replace installed Randomizer?",
            "Randomizing will overwrite the currently installed mod with a new Randomizer for this seed.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.seed_input.setText(normalized_seed.display_value)
        options = replace(self._current_generation_options(), install_after_generation=True)
        self._apply_generation_options(options)
        request = GenerationRequest(
            game_root=Path(game_value).expanduser(),
            mappings_path=self._mappings_path,
            seed=normalized_seed.display_value,
            options=options,
            game_data_cache_entry=self._game_data_cache_snapshot.entry_path,
            replace_existing_randomizer=True,
        )
        self._save_settings()
        self._record_activity(f"Randomize started for seed {normalized_seed.display_value}.")
        self.status_label.setText("Preparing randomizer…")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setRange(0, 0)
        self._launch_task(
            lambda report: self._generation_engine.generate(request, progress=report),
            kind="generation",
            cancellable=True,
        )

    # Check selected randomizer
    def _has_selected_randomizer(self) -> bool:
        # World Loot is an unconditional baseline, so Generate always has a pool.
        return True

    # Start remove
    def _start_remove(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        selected = Path(self.game_path.text()).expanduser() if self.game_path.text().strip() else None
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setValue(0)
        self.status_label.setText("Removing Randomizer files…")
        self._launch_task(
            lambda _report: self._installer.remove(game_root=selected),
            kind="remove",
            cancellable=False,
        )

    # Launch task
    def _launch_task(self, operation, *, kind: str, cancellable: bool) -> None:
        self._task_kind = kind
        self._last_progress_stage = None
        self._set_generation_controls_enabled(False)
        self.cancel_button.setEnabled(cancellable)
        self._worker = GenerationWorker(operation, self)
        self._worker.progress.connect(self._on_task_progress)
        self._worker.completed.connect(self._on_task_completed)
        self._worker.finished.connect(self._on_task_thread_finished)
        self._worker.start()

    # Read on task progress
    def _on_task_progress(self, stage: str, current: int, total: int) -> None:
        ranges = {
            "creating-workspace": ("Preparing generation workspace", 0, 2),
            "preparing-shop-tables": ("Preparing shop tables", 2, 6),
            "checking-asset-cache": ("Checking extracted asset cache", 5, 14),
            "extracting-assets": ("Loading required game assets from cache", 8, 25),
            "parsing-shop-tables": ("Reading shop data", 22, 30),
            "validating-material-pool": ("Validating renewable material classes", 30, 36),
            "preparing-enemy-drops": ("Preparing Enemy Drop plan", 28, 38),
            "extracting-blood-code-quests": ("Loading Blood Code grants from cache", 28, 38),
            "inspecting-blood-code-quests": ("Validating Blood Code grants", 36, 48),
            "extracting-booster-table": ("Loading Booster table from cache", 28, 38),
            "inspecting-booster-table": ("Validating Booster mastery table", 36, 48),
            "extracting-defensive-gadget-grants": ("Loading Defensive Gadget NPC grants from cache", 28, 38),
            "inspecting-defensive-gadget-grants": ("Validating Defensive Gadget NPC grants", 36, 48),
            "extracting-initial-inventory": ("Loading starting inventory from cache", 28, 38),
            "extracting-extractor-recipe-tables": ("Loading Booster recipe tables from cache", 28, 38),
            "inspecting-initial-inventory": ("Validating starting inventory", 36, 48),
            "resolving-material-class-defaults": ("Checking material reset behavior", 33, 40),
            "materializing-material-maps": ("Loading material maps", 40, 46),
            "materializing-maps": ("Loading world maps", 44, 55),
            "inspecting-map-classes": ("Inspecting map classes", 50, 62),
            "resolving-blueprint-parents": ("Resolving Blueprint parents", 57, 65),
            "staging-blueprint-dependencies": ("Loading Blueprint dependencies from cache", 57, 65),
            "patching-shops": ("Patching shop tables", 65, 72),
            "patching-world-loot": ("Patching World Loot", 72, 88),
            "patching-world-loot-package": ("Patching World Loot package", 72, 88),
            "patching-material-cdos": ("Patching ingredient rewards", 82, 91),
            "extracting-enemy-drop-tables": ("Loading Enemy Drop tables from cache", 28, 38),
            "inspecting-enemy-drop-tables": ("Validating Enemy Drop tables", 36, 48),
            "patching-enemy-drops": ("Patching Enemy Drops", 84, 91),
            "patching-blood-codes": ("Patching Blood Code grants", 84, 91),
            "patching-booster-table": ("Patching Booster rewards", 84, 91),
            "patching-defensive-gadget-grants": ("Patching Defensive Gadget NPC rewards", 84, 91),
            "patching-initial-inventory": ("Patching starting inventory", 84, 91),
            "extracting-direct-article-grant": ("Loading direct World Loot rewards from cache", 38, 46),
            "patching-direct-article-grant": ("Patching direct World Loot rewards", 84, 91),
            "extracting-bequeathed-formae": ("Loading Bequeathed Forma quest packages from cache", 28, 38),
            "inspecting-bequeathed-formae": ("Validating Bequeathed Forma quest rewards", 36, 48),
            "patching-bequeathed-formae": ("Patching Bequeathed Forma quest rewards", 84, 91),
            "packaging-mod": ("Packaging the mod", 92, 97),
            "installing-mod": ("Installing the mod", 98, 99),
            "cleaning-generation-files": ("Cleaning temporary generation files", 99, 100),
            "reading-game-archives": ("Reading game archives", 0, 14),
            "preparing-source-registry": ("Preparing game data list", 12, 20),
            "extracting-game-data": ("Extracting game data", 20, 74),
            "validating-game-data": ("Validating extracted game data", 74, 92),
            "saving-game-data-cache": ("Saving persistent vanilla cache", 92, 100),
            "preparing-staging-files": ("Preparing staging files from cache", 5, 20),
        }
        if stage in ranges:
            description, start, end = ranges[stage]
        else:
            description = stage.replace("-", " ").capitalize()
            start = end = self.progress_bar.value()
        if total > 0:
            self.progress_bar.setRange(0, 100)
            fraction = min(max(current / total, 0.0), 1.0)
            value = round(start + (end - start) * fraction)
            self.status_label.setText(f"{description} · {current:,} / {total:,}")
        else:
            self.progress_bar.setRange(0, 0)
            value = 0
            self.status_label.setText(f"{description}…")
        if total > 0:
            self.progress_bar.setValue(max(self.progress_bar.value(), value))
        if stage != self._last_progress_stage:
            self._record_activity(description)
            self._last_progress_stage = stage

    # Read on task completed
    def _on_task_completed(self, outcome: object) -> None:
        self.progress_bar.setRange(0, 100)
        if not isinstance(outcome, TaskOutcome):
            self._show_task_error(RuntimeError("The background task returned an invalid result."))
            return
        if outcome.error is not None:
            if isinstance(outcome.error, GenerationCancelled):
                message = "Generation cancelled."
                self.status_label.setText(message)
                self.progress_bar.setValue(0)
                self._record_activity(message)
            else:
                self._show_task_error(outcome.error)
                if self._task_kind in {"extract-game-data", "clear-cache"} and self.game_path.text().strip():
                    self._start_game_check(self.game_path.text().strip())
            return
        if self._task_kind == "extract-game-data":
            if not isinstance(outcome.result, GameDataCacheSnapshot) or not outcome.result.ready:
                self._show_task_error(RuntimeError("Extracted game data did not pass cache validation."))
                if self.game_path.text().strip():
                    self._start_game_check(self.game_path.text().strip())
                return
            self._game_data_cache_snapshot = outcome.result
            self._cache_ready = True
            ready_suffix = self._game_data_version_suffix()
            self.game_data_status_label.setText(f"Game Data: Ready{ready_suffix}")
            self._game_data_cache_diagnostic.setText(outcome.result.reason)
            self.status_label.setText("Game data extracted and validated.")
            self.progress_bar.setValue(100)
            self._record_activity(
                f"Game data cache ready: {len(outcome.result.available_sources)} known sources; "
                f"{len(outcome.result.unavailable_optional_sources)} optional sources unavailable."
            )
            self._apply_cache_state()
            self._save_settings()
            return
        if self._task_kind == "remove":
            if not isinstance(outcome.result, RemovalResult):
                self._show_task_error(RuntimeError("The removal task returned an invalid result."))
                return
            if outcome.result.removed_files:
                message = "Installed Randomizer files removed."
            else:
                message = "No installed Randomizer was found."
            self.status_label.setText(message)
            self.progress_bar.setValue(100)
            self._record_activity(message)
            return
        if self._task_kind == "clear-output":
            removed_entries = int(outcome.result or 0)
            message = "Old output cleared." if removed_entries else "Output folder was already empty."
            self.status_label.setText(message)
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)
            self._record_activity(f"Cleared old output ({removed_entries} entries removed).")
            return
        if self._task_kind == "clear-cache":
            self._game_data_cache_snapshot = None
            self._cache_ready = False
            self.game_data_status_label.setText("Game Data: Not Extracted")
            self._game_data_cache_diagnostic.setText(
                "Game data must be extracted before generating a randomizer."
            )
            message = "Extracted game data cleared."
            self.status_label.setText(message)
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)
            self._record_activity("Persistent vanilla game-data cache cleared.")
            self._apply_cache_state()
            return
        if not isinstance(outcome.result, GenerationResult):
            self._show_task_error(RuntimeError("The generation task returned an invalid result."))
            return
        result = outcome.result
        world_count = len(result.world_plan.assignments)
        material_count = len(result.material_preparation.plan.assignments) if result.material_preparation else 0
        shop_count = len(result.shop_plan.assignments)
        enemy_drop_count = result.enemy_drop_plan.changed_slots if result.enemy_drop_plan else 0
        enemy_drop_total = result.enemy_drop_plan.eligible_slots if result.enemy_drop_plan else 0
        blood_code_count = result.blood_code_plan.changed_slots if result.blood_code_plan else 0
        blood_code_total = len(result.blood_code_plan.assignments) if result.blood_code_plan else 0
        booster_count = result.booster_plan.changed_slots if result.booster_plan else 0
        booster_total = len(result.booster_plan.assignments) if result.booster_plan else 0
        defensive_gadget_count = result.defensive_gadget_plan.changed_slots if result.defensive_gadget_plan else 0
        defensive_gadget_total = len(result.defensive_gadget_plan.assignments) if result.defensive_gadget_plan else 0
        pending_gadget_count = len(result.defensive_gadget_plan.pending_grants) if result.defensive_gadget_plan else 0
        formae_changed = result.bequeathed_formae_plan.changed_rewards if result.bequeathed_formae_plan else 0
        formae_total = len(result.bequeathed_formae_plan.assignments) if result.bequeathed_formae_plan else 0
        initial_inventory_count = result.initial_inventory_plan.changed_slots if result.initial_inventory_plan else 0
        world_candidate_note = ""
        if result.world_plan.gadget_injected_count or result.world_plan.jail_injected_count:
            world_candidate_note = (
                f" (+{result.world_plan.gadget_injected_count} Defensive Gadgets, "
                f"+{result.world_plan.jail_injected_count} Jails)"
            )
        if any(grant.changed for grant in result.world_direct_grant_results):
            world_candidate_note += " (direct quest/NPC rewards included)"
        skipped = (
            len(result.world_plan.locked_locations)
            + len(result.world_plan.excluded_locations)
            + len(result.world_plan.disabled_locations)
            + (len(result.material_preparation.plan.locked_locations)
               + len(result.material_preparation.plan.disabled_locations)
               if result.material_preparation else 0)
            + len(result.shop_plan.locked_slots)
            + len(result.shop_plan.unvalidated_slots)
            + len(result.shop_plan.disabled_slots)
            + (
                sum(table.source_rows for table in result.enemy_drop_plan.table_stats)
                - result.enemy_drop_plan.eligible_rows
                if result.enemy_drop_plan else 0
            )
        )
        target = (
            result.installation_result.mods_directory
            if result.installation_result is not None
            else self._output_path
        )
        mode = (
            "Installed" if result.installation_result is not None
            else "No changes" if result.package_result is None and not result.output_files
            else "Installed"
        )
        summary = (
            f"{mode}: World Loot {world_count}{world_candidate_note} (Defensive Gadget NPC {defensive_gadget_count}/{defensive_gadget_total}, "
            f"{pending_gadget_count} pending), Materials {material_count}, Shops {shop_count}, "
            f"Enemy Drops {enemy_drop_count}/{enemy_drop_total} changed, "
            f"Blood Codes {blood_code_count}/{blood_code_total} changed, "
            f"Boosters {booster_count}/{booster_total} changed, "
            f"Initial Inventory {initial_inventory_count}/3 changed, "
            f"Bequeathed Formae quests {formae_changed}/{formae_total} changed; "
            f"locked/skipped {skipped}. Seed {result.seed.display_value}. Output: {target}"
        )
        if result.installation_result and result.installation_result.recovery_backup:
            self._record_activity(
                "Previous modified Randomizer containers were preserved at: "
                f"{result.installation_result.recovery_backup}"
            )
        self._record_activity(summary)
        if all((result.config_fingerprint, result.content_fingerprint, result.assignment_manifest_sha256)):
            self._record_activity(
                f"Reproducibility: algorithm=v{RANDOMIZER_ALGORITHM_VERSION}; "
                f"config={result.config_fingerprint}; content={result.content_fingerprint}; "
                f"assignment={result.assignment_manifest_sha256}"
            )
        if result.assignment_manifest_path is not None:
            self._record_activity(f"Assignment manifest: {result.assignment_manifest_path}")
        if result.spoiler_text_path is not None:
            self._record_activity(f"Spoiler: {result.spoiler_text_path}")
        if result.cleanup_report is not None:
            report = result.cleanup_report
            reclaimed_mib = report.reclaimed_bytes / (1024 * 1024)
            before_mib = report.temporary_bytes_before / (1024 * 1024)
            after_mib = report.temporary_bytes_after / (1024 * 1024)
            self._record_activity(
                f"Temporary generation data: {before_mib:.2f} MiB before, "
                f"{after_mib:.2f} MiB after; reclaimed {reclaimed_mib:.2f} MiB."
            )
            for warning in report.warnings:
                self._record_activity(f"Cleanup warning: {warning}")
        self.status_label.setText("Randomizer completed")
        self.progress_bar.setValue(100)
        self._record_activity("Randomizer completed")

    # Show task error
    def _show_task_error(self, error: Exception) -> None:
        LOGGER.error("Randomizer task failed: %s", error)
        message = f"The task could not be completed. {error} Activity log: {self._log_file_path}."
        self.status_label.setText("Task failed. See the activity log for details.")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self._record_activity(message)
        QMessageBox.critical(self, "Randomizer task failed", message)

    # Read on task thread finished
    def _on_task_thread_finished(self) -> None:
        worker = self.sender()
        if isinstance(worker, GenerationWorker):
            worker.deleteLater()
        self._worker = None
        self._task_kind = None
        self._set_generation_controls_enabled(True)

    # Cancel task
    def _cancel_task(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_cancel()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("Cancellation requested; waiting for the next safe checkpoint…")

    # Set generation controls enabled
    def _set_generation_controls_enabled(self, enabled: bool) -> None:
        self.generate_install_button.setEnabled(enabled and self._cache_ready)
        self.remove_button.setEnabled(enabled and self._cache_ready)
        self.cancel_button.setEnabled(False)
        self._apply_cache_state(task_controls_enabled=enabled)

    # Apply cache state
    def _apply_cache_state(self, *, task_controls_enabled: bool | None = None) -> None:
        if task_controls_enabled is None:
            task_controls_enabled = self._worker is None or not self._worker.isRunning()
        ready = self._cache_ready and self._game_data_cache_snapshot is not None
        selected_game = bool(self.game_path.text().strip())
        usable = bool(
            self._game_version_detection
            and self._game_version_detection.usable_for_local_validation
        )
        self.game_path.setEnabled(task_controls_enabled)
        self.game_browse_button.setEnabled(task_controls_enabled)
        mapping_ready = self._mappings_path is not None and self._mappings_path.is_file()
        self.extract_game_data_button.setEnabled(task_controls_enabled and selected_game and usable and mapping_ready)
        cache_state = self._game_data_cache_snapshot.state if self._game_data_cache_snapshot is not None else None
        self.extract_game_data_button.setText(
            "Re-extract Game Data" if ready else (
                "Update Prepared Data" if cache_state is GameDataCacheState.PREPARED_OUTDATED
                else "Extract Game Data"
            )
        )
        self._set_extraction_highlight(not ready)
        can_generate = task_controls_enabled and ready and usable and mapping_ready
        self.seed_input.setEnabled(can_generate)
        self.copy_seed_button.setEnabled(can_generate)
        self.random_seed_button.setEnabled(can_generate)
        # Keep options editable while game data is extracting or being checked.
        # Only actions that consume or modify the installation require a READY cache.
        self.randomizer_page.setEnabled(True)
        self.clear_cache_button.setEnabled(task_controls_enabled and not self._scan_workers)
        self.clear_output_button.setEnabled(task_controls_enabled and not self._scan_workers)
        self.generate_install_button.setEnabled(can_generate)
        self._set_randomize_highlight(can_generate)
        self.remove_button.setEnabled(task_controls_enabled and ready and usable)
        if ready:
            self._apply_feature_cache_availability(self._game_data_cache_snapshot)
        self._update_mapping_warning()

    # Set extraction highlight
    def _set_extraction_highlight(self, needs_extraction: bool) -> None:
        button = self.extract_game_data_button
        if button.property("needsExtraction") == needs_extraction:
            return
        button.setProperty("needsExtraction", needs_extraction)
        button.style().unpolish(button)
        button.style().polish(button)
        button.update()

    # Set randomize highlight
    def _set_randomize_highlight(self, ready: bool) -> None:
        button = self.generate_install_button
        if button.property("randomizeReady") == ready:
            return
        button.setProperty("randomizeReady", ready)
        button.style().unpolish(button)
        button.style().polish(button)
        button.update()

    # Apply feature cache availability
    def _apply_feature_cache_availability(self, snapshot: GameDataCacheSnapshot) -> None:
        metadata = snapshot.metadata or {}
        registry = metadata.get("source_registry", {})
        available = {value.casefold() for value in metadata.get("extracted_package_inventory", ())}
        if not isinstance(registry, dict):
            return

        # Group present
        def group_present(*groups: str) -> bool:
            return any(
                isinstance(registry.get(group), list)
                and any(str(package).casefold() in available for package in registry[group])
                for group in groups
            )

        table_packages = {
            "normal": "/game/blueprint/gamesystem/shop/dt_shoplineup",
            "dlc": "/game/blueprint/gamesystem/shop/dt_shoplineup_dlc",
            "luxury": "/game/blueprint/gamesystem/shop/dt_shoplineup_luxury",
            "trade": "/game/blueprint/gamesystem/shop/dt_shoplineup_trade",
            "trade_dlc": "/game/blueprint/gamesystem/shop/dt_shoplineup_trade_dlc",
        }
        shop_tables = {key: path in available for key, path in table_packages.items()}
        world_loot = group_present("world_loot")
        shops = any(shop_tables.values())
        trader_tables = shop_tables["trade"] or shop_tables["trade_dlc"]
        feature_controls = (
            (self.randomize_healing_check, world_loot, "No supported World Loot source is present in the cache."),
            (self.materials_check, group_present("materials"), "No renewable ingredient sources are present in the extracted data."),
            (self.shops_check, shops, "No ordinary Shop source table is present in the cache."),
            (self.enemy_drops_check, group_present("enemy_drops"), "No Enemy Drop table is present in the cache."),
            (self.blood_codes_check, group_present("blood_codes"), "No Blood Code grant source is present in the cache."),
            (self.boosters_check, group_present("boosters"), "No Booster mastery table is present in the cache."),
            (self.extractor_recipes_check, group_present("extractor"), "No Extractor recipe table is present in the cache."),
            (self.initial_inventory_check, group_present("initial_inventory"), "The Initial Inventory asset is absent from the cache."),
        )
        for control, enabled, reason in feature_controls:
            control.setEnabled(enabled)
            if not enabled:
                control.setToolTip(reason)
        self.cheaper_booster_extractor_check.setEnabled(group_present("extractor"))
        if not group_present("extractor"):
            self.cheaper_booster_extractor_check.setToolTip(
                "No Booster recipe tables are present in the extracted data."
            )
        self.shops_check.setEnabled(shops)
        self.unlock_npc_trader_shops_check.setEnabled(trader_tables)
        self.unlock_npc_trader_shops_check.setToolTip(
            "Make affinity-gated NPC Trader tiers available from the start; story gates stay intact. Highest current affinity requirement: 72."
            if trader_tables else "No NPC Trader source table is present in the extracted cache."
        )
        self.unlock_all_booster_slots_check.setToolTip(
            "New games start with Booster slots 5 and 6 unlocked. Existing saves keep their current flag values."
        )
        self._update_shop_mix_enabled()

    # Record activity
    def _record_activity(self, message: str) -> None:
        LOGGER.info(message)

    # Open folder
    def _open_folder(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    # Open install folder
    def _open_install_folder(self) -> None:
        game_path = self.game_path.text().strip()
        if not game_path:
            return
        installation = check_installation(Path(game_path))
        if not installation.valid or installation.pak_directory is None:
            QMessageBox.warning(self, "Invalid game folder", installation.reason)
            return
        mods_directory = installation.pak_directory / "~mods"
        try:
            mods_directory.mkdir(exist_ok=True)
        except OSError as error:
            self._show_task_error(error)
            return
        self._open_folder(mods_directory)

    # Clear cache
    def _clear_cache(self) -> None:
        if (self._worker is not None and self._worker.isRunning()) or self._scan_workers:
            return
        if not self._confirm_extracted_cache_action("Clear Extracted Game Data"):
            return
        self._game_data_cache_snapshot = None
        self._cache_ready = False
        self.game_data_status_label.setText("Game Data: Clearing…")
        self._game_data_cache_diagnostic.setText("Clearing extracted game data.")
        self.status_label.setText("Clearing extracted game data…")
        self.progress_bar.setRange(0, 0)
        self._record_activity("Clearing persistent vanilla game-data cache.")
        self._launch_task(
            lambda _report: self._game_data_cache_store.clear(),
            kind="clear-cache",
            cancellable=False,
        )

    # Clear output
    def _clear_output(self) -> None:
        if (self._worker is not None and self._worker.isRunning()) or self._scan_workers:
            return
        answer = QMessageBox.question(
            self,
            "Clear Old Output",
            "Delete everything in the output folder, including old generation files and logs?\n\n"
            "The installed mod will not be removed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.status_label.setText("Clearing old output…")
        self.progress_bar.setRange(0, 0)
        self._record_activity("Clearing old generation output.")
        self._launch_task(
            lambda _report: clear_output_directory(
                self._output_path,
                app_data_root=self._data_path,
                protected_paths=(
                    self._settings_store.path,
                    self._cache_path,
                    self._log_path,
                ),
            ),
            kind="clear-output",
            cancellable=False,
        )

    # Read confirm extracted cache action
    def _confirm_extracted_cache_action(self, action: str) -> bool:
        if action == "Re-extract Game Data":
            message = (
                "Extract the selected game again and replace the current extracted data after the new cache passes validation?\n\n"
                "The current cache remains available if extraction fails."
            )
        else:
            message = (
                "Delete extracted game data?\n\n"
                "This permanently deletes the extracted game data cache. Do you want to continue?"
            )
        answer = QMessageBox.question(
            self,
            action,
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes
