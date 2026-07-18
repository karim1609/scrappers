from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ScraperConfig:
    """
    Configuration de base envoyée à chaque scraper.
    Définit le mot-clé principal de la recherche (keyword), le nombre maximum d'éléments (limit),
    ainsi que les paramètres supplémentaires globaux stockés dans le dictionnaire `extra`.
    """
    keyword: str
    limit: int = 10
    strict: bool = False
    output_path: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)
    # extra examples:
    # {"site": "stackoverflow"}
    # {"subreddit": "all", "sort": "relevance", "time_filter": "all", "comment_limit": 20}
    # {"instance": "https://mastodon.social", "mode": "auto"}


@dataclass
class ScraperResult:
    """
    La structure de réponse unifiée renvoyée par tous les scrapers.
    Garantit que peu importe la plateforme, nous recevons le même package standard
    contenant la plateforme, le compte des items, et la liste des résultats.
    """
    query: str
    platform: str
    count: int
    items: list[dict[str, Any]]


class BaseScraper(ABC):
    """
    Classe abstraite parente dont tous les scrapers doivent hériter.
    Ceci nous permet d'imposer un modèle objet standard : chaque scraper *doit*
    définir une méthode 'scrape' et 'validate_config'.
    """

    platform: str = ""
    items_key: str = "items"

    def __init__(self, session=None):
        self.session = session

    @abstractmethod
    def scrape(self, config: ScraperConfig) -> ScraperResult:
        """Lance l'extraction et normalise les données du site."""
        ...

    @abstractmethod
    def validate_config(self, config: ScraperConfig) -> None:
        """Soulève une erreur si la configuration/clés requises manquent pour ce site."""
        ...

    def normalize_item(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Option : Utilisé pour formater un seul objet/post avant de l'ajouter dans la liste."""
        return raw

    def filter_strict(self, keyword: str, item: dict[str, Any]) -> bool:
        """Optional shared strict-mode logic."""
        return True

    def to_json(self, result: ScraperResult) -> dict[str, Any]:
        """Standard output envelope."""
        return {
            "query": result.query,
            "platform": result.platform,
            "count": result.count,
            self.items_key: result.items,
        }
