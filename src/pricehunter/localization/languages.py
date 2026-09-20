from typing import Literal, get_args

LanguageCode = Literal["en", "fr", "de", "es", "it", "pl", "ru"]
DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES: tuple[str, ...] = get_args(LanguageCode)
LANGUAGE_NAMES = {
    "en": "English",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
    "it": "Italiano",
    "pl": "Polski",
    "ru": "Русский",
}
NUMBER_LOCALES = {
    "en": "en_US",
    "fr": "fr_FR",
    "de": "de_DE",
    "es": "es_ES",
    "it": "it_IT",
    "pl": "pl_PL",
    "ru": "ru_RU",
}


def normalize_language(language: str) -> str:
    code = language.lower().replace("_", "-").split("-", 1)[0]
    return code if code in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE
