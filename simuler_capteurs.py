"""
Envoie à l'API SICoSE les quatre scénarios des tests (normal, humidité, gaz, flamme)
et affiche le niveau d'alerte renvoyé. Aucun matériel n'est nécessaire.

Usage :
    python simuler_capteurs.py https://votre-api.onrender.com
"""
import sys
import requests

SCENARIOS = {
    "1. Normal": {
        "temperature": 23, "humidity": 50, "point_rosee": 12,
        "gas": 420, "flame": 1, "temp_ext": 12, "hum_ext": 60,
    },
    "2. Humidité élevée (vigilance)": {
        "temperature": 24, "humidity": 85, "point_rosee": 21,
        "gas": 430, "flame": 1, "temp_ext": 12, "hum_ext": 50,
    },
    "3. Gaz élevé (critique)": {
        "temperature": 23, "humidity": 50, "point_rosee": 12,
        "gas": 1500, "flame": 1, "temp_ext": 12, "hum_ext": 60,
    },
    "4. Flamme (urgence)": {
        "temperature": 23, "humidity": 50, "point_rosee": 12,
        "gas": 420, "flame": 0, "temp_ext": 12, "hum_ext": 60,
    },
}


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    base = sys.argv[1].rstrip("/")
    for nom, mesure in SCENARIOS.items():
        r = requests.post(f"{base}/api/ai/risk-score", json=mesure, timeout=60)
        r.raise_for_status()
        data = r.json()
        print(f"{nom:32s} niveau {data['niveau_alerte']} | {data['message']}")
        for raison in data["raisons"]:
            print(f"    - {raison}")


if __name__ == "__main__":
    main()
