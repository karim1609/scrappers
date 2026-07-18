from __future__ import annotations

from scrapers.base import BaseScraper

# Registre global contenant tous les scrapers actuellement disponibles (dictionnaire clé-valeur).
SCRAPERS: dict[str, type[BaseScraper]] = {}


def _register(name: str, scraper_cls: type[BaseScraper]) -> type[BaseScraper]:
    """Méthode interne permettant de lier une chaîne de caractères à la classe Python correspondante."""
    SCRAPERS[name] = scraper_cls
    return scraper_cls


def get_scraper(name: str, **kwargs) -> BaseScraper:
    """Récupère une instance du scraper demandé s'il est enregistré."""
    if name not in SCRAPERS:
        available = ", ".join(sorted(SCRAPERS))
        raise KeyError(f"Scraper inconnu {name!r}. Disponibles: {available}")
    return SCRAPERS[name](**kwargs)


def _load_scrapers() -> None:
    """
    Charge manuellement (via des imports) tous les scrapers créés et structurés.
    C'est ici qu'il faut en déclarer un nouveau à l'avenir si on agrandi le projet.
    """
    from scrapers.blogs.blogger_fetch import BloggerScraper
    from scrapers.blogs.substack_fetch import SubstackScraper
    from scrapers.news.bbc_fetch import BbcScraper
    from scrapers.news.guardian_fetch import GuardianScraper
    from scrapers.news.newsapi_fetch import NewsApiScraper
    from scrapers.news.wordpress_fetch import WordPressScraper
    from scrapers.review_sites.appstore_fetch import AppStoreScraper
    from scrapers.review_sites.booking_fetch import BookingScraper
    from scrapers.review_sites.gmaps_fetch import GMapsScraper
    from scrapers.review_sites.googleplaystore_fetch import GooglePlayStoreScraper
    from scrapers.review_sites.trustpilot_fetch import TrustpilotScraper
    from scrapers.social_media.bluesky_fetch import BlueskyScraper
    from scrapers.social_media.mastodon_fetch import MastodonScraper
    from scrapers.social_media.reddit_fetch import RedditScraper
    from scrapers.social_media.stackexchange_fetch import StackExchangeScraper
    from scrapers.social_media.vimeo_fetch import VimeoScraper
    from scrapers.social_media.youtube_fetch import YouTubeScraper
    from scrapers.social_media.dailymotion_fetch import DailymotionScraper

    _register("blogger", BloggerScraper)
    _register("substack", SubstackScraper)
    _register("bbc", BbcScraper)
    _register("guardian", GuardianScraper)
    _register("newsapi", NewsApiScraper)
    _register("wordpress", WordPressScraper)
    _register("appstore", AppStoreScraper)
    _register("booking", BookingScraper)
    _register("gmaps", GMapsScraper)
    _register("google_play", GooglePlayStoreScraper)
    _register("trustpilot", TrustpilotScraper)
    _register("bluesky", BlueskyScraper)
    _register("mastodon", MastodonScraper)
    _register("reddit", RedditScraper)
    _register("stackexchange", StackExchangeScraper)
    _register("vimeo", VimeoScraper)
    _register("youtube", YouTubeScraper)
    _register("dailymotion", DailymotionScraper)


_load_scrapers()
