# SICoSE : système intelligent de surveillance environnementale

Projet de fin d'année (PFA), FST Fès, 2025-2026, réalisé en équipe de cinq.

Un système IoT qui surveille la température, l'humidité, le gaz et la présence de flamme dans un local. Il estime le risque de moisissures, détecte les situations anormales, pilote un ventilateur et une pompe, et envoie des notifications sur une application mobile.

**Ma contribution : la partie intelligence artificielle : l'API FastAPI, les modèles Random Forest et Isolation Forest, la logique des niveaux d'alerte et les explications. Le firmware ESP32 et l'application mobile ont été réalisés par l'équipe.

> Prototype testé en laboratoire. Les données d'entraînement des modèles sont simulées. Le matériel a été restitué après le projet : les résultats ci-dessous correspondent aux essais réalisés à l'époque.

## Architecture

```
ESP32 : DHT22, MQ-2, capteur de flamme, ventilateur, pompe, buzzer, LED
   │ mesures ──────────────────────► ThingsBoard (tableau de bord)
   │ appel HTTP
   ▼
Raspberry Pi 4 : API FastAPI (IA) + broker Mosquitto
   ├─ Random Forest : score de risque de moisissures
   ├─ Isolation Forest : score d'anomalie
   └─ niveau d'alerte, explications, tendance
   │
   ▼
Application mobile Flutter : état, alertes, notifications
```

## Niveaux d'alerte

| Niveau | Nom | Condition | Actions |
|---|---|---|---|
| 0 | Normal | Aucune condition critique | Surveillance continue |
| 1 | Vigilance | Température ou humidité au-dessus du seuil, ou risque combiné élevé | Notification et recommandation |
| 2 | Critique | Gaz au-dessus du seuil, ou mesures atypiques | Ventilateur activé, notification |
| 3 | Urgence | Flamme détectée | Pompe, buzzer, notification prioritaire |

Seuils principaux : température de 40 °C, humidité de 70 %, gaz de 1000 en valeur brute (sortie analogique du MQ-2, de 0 à 4095).

## Ce qui a été testé

Chaque capteur a été testé séparément, puis le système complet a été testé en conditions contrôlées.

- **Capteurs** : le DHT22 a été comparé à des instruments de référence. Le MQ-2 a été exposé à un gaz combustible, puis a retrouvé son niveau normal après ventilation. Le capteur de flamme (LM393) a été testé avec une bougie à différentes distances. Limite observée : il est sensible à la lumière directe du soleil.
- **Communication** : les mesures passent par MQTT (Mosquitto) puis HTTP vers l'API. Latence mesurée entre la publication et l'affichage : moins de 3 secondes.
- **Niveaux d'alerte** : chaque niveau a été déclenché volontairement.
  - Niveau 0 : température d'environ 23 °C, humidité d'environ 50 %, pas de gaz ni de flamme. Aucune alerte.
  - Niveau 1 : humidité au-dessus de 70 %. Alerte de vigilance et recommandation.
  - Niveau 2 : exposition du MQ-2 à un briquet. Ventilateur activé et notification.
  - Niveau 3 : flamme détectée. Pompe, buzzer et notification.
- **API** : les points d'accès ont été testés avec Swagger UI et des requêtes curl (`/api/ai/risk-score`, `/api/ai/metrics`, `/api/ai/feature-importance`, `/api/data/stats`, `/api/health`).

**Vidéos des tests :** [À COMPLÉTER : lien vers chaque vidéo, une par niveau d'alerte]

## Résultats de l'IA

- **Random Forest** : R² compris entre 0,85 et 0,96 sur des données **simulées**. Ce chiffre mesure l'ajustement sur des données générées, pas la performance sur des mesures réelles.
- **Variables les plus influentes** : l'humidité et le point de rosée, ce qui correspond à ce qu'on attend pour les moisissures.
- **Tendance** : calculée sur les dernières mesures (régression linéaire simple) et projetée sur 6 heures. Ce n'est pas une prévision météorologique.

## Limites

- Les modèles sont entraînés sur des données simulées, non validés sur des mesures réelles de longue durée.
- Le seuil de gaz est à calibrer : la valeur brute dépend du capteur et de la température.
- Le capteur de flamme est perturbé par la lumière solaire directe.
- La tendance est réinitialisée au redémarrage de l'API.
- Le matériel n'étant plus disponible, le système ne peut pas être relancé à l'identique.

## Essayer l'API en ligne

- Documentation interactive (Swagger) : [À COMPLÉTER : https://sicose-api.onrender.com/docs]
- Lancer les quatre scénarios des tests sans matériel :

```bash
pip install requests
python simuler_capteurs.py https://sicose-api.onrender.com
```

Le plan gratuit de Render met l'API en veille après quelques minutes sans requête : la première réponse peut prendre une minute.

## Lancer l'API

```bash
cd api
pip install -r requirements.txt
export TB_TOKEN="votre-token"          # facultatif
uvicorn main:app --host 0.0.0.0 --port 8002
```

Les modèles sont créés au premier lancement dans `data/` (ignoré par Git).

## Firmware ESP32

1. Copier `firmware/secrets.h.example` vers `firmware/secrets.h` et remplir les valeurs.
2. Ouvrir `firmware/sicose.ino` dans l'IDE Arduino (bibliothèques : PubSubClient, ESP32Servo, DHT sensor library, ArduinoJson).
3. Téléverser et ouvrir le moniteur série à 115200 bauds.

Le firmware n'est pas nécessaire pour essayer l'API : utilise plutôt `simuler_capteurs.py`.

## Ce qui reste à faire

- Calibrer le MQ-2 et régler les seuils sur des mesures réelles.
- Réentraîner les modèles sur les données collectées par l'API.
- Sécuriser l'API par une authentification.

## Équipe

Partie Esp32 : AKPA YAOVI RICHARD 
               CAMARA MOUSSA 
               QALEBI LAMYAA 
Partie ThingsBoard et Application Flutter : ELHAKMAOUI MAJIDA
Partie IA : KOUMASSI RUTH NAOMI(Moi)

Encadrée par PR. NOR-SAID ECHATOUI Professeur a la FST de FES.
