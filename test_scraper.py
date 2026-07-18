import sys
import json
import csv
import os
from pathlib import Path

# Fichier principal d'interface en ligne de commande (CLI) pour tester vos scrapers.
# Permet de lancer interactivement ou via argument n'importe quel scraper enregistré.

# Provide access to the scrapers package
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scrapers.registry import SCRAPERS, get_scraper
from scrapers.base import ScraperConfig
import dotenv

dotenv.load_dotenv()

SCRAPER_EXTRAS = {
    "stackexchange": {"site": "stackoverflow"},
    "amazon": {"domain": "com"},
    "guardian": {"order_by": "relevance"},
    "vimeo": {"sort": "relevant", "direction": "desc"},
}


def build_config(scraper_name: str, keyword: str, limit: int) -> ScraperConfig:
    """
    Construit l'objet de configuration (ScraperConfig) à envoyer au scraper.
    Pointe également les paramètres spécifiques requis par certaines plateformes via SCRAPER_EXTRAS.
    """
    config = ScraperConfig(keyword=keyword, limit=limit)
    extra = SCRAPER_EXTRAS.get(scraper_name)
    if extra:
        config.extra = extra
    return config


def item_to_csv_row(item: dict) -> dict:
    """
    Formate un objet de résultat (Post, Vidéo, Article, etc.) pour qu'il soit compatible avec le format CSV.
    Les listes et dictionnaires internes sont transformés en chaînes de caractères (JSON dump) pour éviter les erreurs.
    """
    row = {}
    for key, value in item.items():
        if isinstance(value, (list, dict)):
            row[key] = json.dumps(value, ensure_ascii=False) if value is not None else ""
        else:
            row[key] = value
    return row


def main():
    if len(sys.argv) == 4:
        scraper_name = sys.argv[1]
        keyword = sys.argv[2]
        limit = int(sys.argv[3])
        print(f"\\n[+] Running {scraper_name} scraper for keyword '{keyword}' (limit: {limit})...\\n")
    else:
        print("=== Scraper Menu ===")
        
        names = list(SCRAPERS.keys())
        names.sort()
        
        if not names:
            print("No scrapers registered in registry.py!")
            return

        print("Available Scrapers:")
        for idx, name in enumerate(names, start=1):
            print(f"  {idx}. {name}")
        
        while True:
            try:
                choice = input(f"\\nSelect a scraper (1-{len(names)}): ")
                idx = int(choice)
                if 1 <= idx <= len(names):
                    scraper_name = names[idx - 1]
                    break
                print("Invalid range. Try again.")
            except ValueError:
                print("Please enter a number.")

        print(f"\\nSelected: {scraper_name}")

        keyword_input = input("Enter a search keyword (default: 'adidas'): ").strip()
        keyword = keyword_input if keyword_input else "adidas"
        if not keyword_input:
            print("> Using default keyword: 'adidas'")

        limit_str = input("Enter limit (default 2): ").strip()
        limit = int(limit_str) if limit_str.isdigit() else 2
        if limit_str == "":
            print("> Using default limit: 2")

        print(f"\\n[+] Running {scraper_name} scraper for keyword '{keyword}' (limit: {limit})...\\n")
    try:
        # 1. On récupère la classe du scraper avec son nom depuis le registre
        scraper = get_scraper(scraper_name)
        
        # 2. On prépare la configuration de base (mot-clé et limite sélectionnées)
        config = build_config(scraper_name, keyword, limit)
        
        # 3. Lancement de la procédure de scrapping
        result = scraper.scrape(config)
        
        print(f"\\n=== Results Summary ===")
        print(f"Platform: {result.platform}")
        print(f"Query:    {result.query}")
        print(f"Count:    {result.count}")
        print("-" * 25)
        
        for i, item in enumerate(result.items, 1):
            # Safe snippet printing
            item_str = json.dumps(item, ensure_ascii=False)
            # Removed truncation so you can see all fields in the console too!
            print(f"{i:02d} | {item_str}")
            
        if result.items:
            # Création du dossier 'output' s'il n'existe pas encore
            output_dir = Path("output")
            output_dir.mkdir(exist_ok=True)
            csv_filename = output_dir / f"{scraper_name}_results.csv"
            
            # Récupérer l'en-tête dynamique du CSV en utilisant les clés du premier résultat
            # On suppose que tous les objets extraits sont homogènes
            headers = list(result.items[0].keys())
            
            with open(csv_filename, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
                writer.writerows(item_to_csv_row(item) for item in result.items)
            print(f"\\n[+] Saved {len(result.items)} items to {csv_filename}")
            
        print("\\nDone!")
        
    except Exception as e:
        import traceback
        print(f"\\n[Error] {e}")
        traceback.print_exc()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\\nExiting.")
        sys.exit(0)
