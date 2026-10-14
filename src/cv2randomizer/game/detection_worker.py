"""Run potentially large executable hashes outside the Qt UI thread."""

from pathlib import Path
from PySide6.QtCore import QThread, Signal

from cv2randomizer.game.versioning import GameVersionDetector, VersionDetection
from cv2randomizer.game.game_data_cache import GameDataCacheStore


class GameDetectionThread(QThread):
    """Validate a selected game folder without freezing the main window."""

    result_ready = Signal(str, object)
    cache_result_ready = Signal(str, object)

    # Initialize this object
    def __init__(
        self,
        game_root: Path,
        parent=None,
        *,
        cache_store: GameDataCacheStore | None = None,
        allow_local_unverified: bool = False,
    ) -> None:
        super().__init__(parent)
        self._game_root = game_root
        self._cache_store = cache_store
        self._allow_local_unverified = allow_local_unverified

    # Run the requested background task
    def run(self) -> None:
        try:
            result = GameVersionDetector(
                allow_local_unverified=self._allow_local_unverified,
            ).detect(self._game_root)
        except OSError as error:
            result = VersionDetection(
                False,
                None,
                None,
                None,
                f"Could not read the selected game files: {error}",
            )
        self.result_ready.emit(str(self._game_root), result)
        if self._cache_store is not None:
            try:
                cache = self._cache_store.inspect(self._game_root, result)
            except Exception as error:
                cache = error
            self.cache_result_ready.emit(str(self._game_root), cache)
