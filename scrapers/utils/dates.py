from datetime import datetime, timezone
from typing import Optional

def to_iso_format(date_obj: Optional[datetime]) -> str:
    """
    Convertit un objet date Python en une chaîne de texte standardisée (ISO 8601).
    """
    if not date_obj:
        return ""
    return date_obj.isoformat()


def get_current_utc_string() -> str:
    """
    Récupère la date et l'heure actuelle précise pour marquer "quand" un scrap a eu lieu.
    """
    return datetime.now(timezone.utc).isoformat()
