"""Internationalization (i18n) module for Atlas Medical Chat."""
import json
from pathlib import Path
from typing import Any, Dict, Optional

# Supported languages with their codes and display names
SUPPORTED_LANGUAGES = {
    "en": "English",
    "es": "Español",
    "fr": "Français",
    "de": "Deutsch",
    "zh": "中文",
    "ar": "العربية",
    "bn": "বাংলা",
    "hi": "हिन्दी",
    "ur": "اردو",
}

DEFAULT_LANGUAGE = "en"
I18N_DIR = Path(__file__).resolve().parent


class I18nManager:
    """Manages translations for the application."""

    def __init__(self):
        """Initialize the translation manager."""
        self._translations: Dict[str, Dict[str, Any]] = {}
        self._load_all_translations()

    def _load_all_translations(self) -> None:
        """Load all language translation files."""
        for lang_code in SUPPORTED_LANGUAGES:
            self._load_language(lang_code)

    def _load_language(self, lang_code: str) -> None:
        """Load a specific language translation file."""
        lang_file = I18N_DIR / f"{lang_code}.json"
        if lang_file.exists():
            with open(lang_file, encoding="utf-8") as f:
                self._translations[lang_code] = json.load(f)

    def get_text(
        self, key: str, lang_code: str = DEFAULT_LANGUAGE, default: str = ""
    ) -> str:
        """
        Get translated text for a given key and language.

        Args:
            key: Dot-notation key path (e.g., 'login.title', 'common.logout')
            lang_code: Language code (e.g., 'en', 'es', 'fr')
            default: Default value if key not found

        Returns:
            Translated text or default value
        """
        if lang_code not in self._translations:
            lang_code = DEFAULT_LANGUAGE

        translations = self._translations.get(lang_code, {})
        parts = key.split(".")

        try:
            result = translations
            for part in parts:
                result = result[part]
            return result
        except (KeyError, TypeError):
            return default

    def get_translations(self, lang_code: str = DEFAULT_LANGUAGE) -> Dict[str, Any]:
        """
        Get all translations for a specific language.

        Args:
            lang_code: Language code (e.g., 'en', 'es', 'fr')

        Returns:
            Complete translation dictionary
        """
        if lang_code not in self._translations:
            lang_code = DEFAULT_LANGUAGE
        return self._translations.get(lang_code, {})

    def get_supported_languages(self) -> Dict[str, str]:
        """Get all supported languages."""
        return SUPPORTED_LANGUAGES.copy()

    def is_supported_language(self, lang_code: str) -> bool:
        """Check if a language is supported."""
        return lang_code in SUPPORTED_LANGUAGES

    def get_default_language(self) -> str:
        """Get the default language code."""
        return DEFAULT_LANGUAGE


# Global instance
i18n = I18nManager()
