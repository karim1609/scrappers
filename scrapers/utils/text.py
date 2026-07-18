import re

def clean_html(raw_html: str) -> str:
    """
    Supprime toutes les balises HTML d'un texte brut.
    Très utile car de nombreuses APIs renvoient des descriptions polluées par des <a> <b> etc.
    """
    if not raw_html:
        return ""
    cleaner = re.compile('<.*?>')
    return re.sub(cleaner, '', raw_html).strip()


def normalize_whitespace(text: str) -> str:
    """
    Supprime les espaces en double ou les retours à la ligne intempestifs.
    Très utile pour formater proprement le texte d'un commentaire.
    """
    if not text:
        return ""
    return " ".join(text.split())
