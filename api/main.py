"""
SICoSE - API de surveillance environnementale (prototype).

Reçoit les mesures de l'ESP32 (via le Raspberry Pi), calcule un score de risque
de moisissures (Random Forest), détecte les mesures atypiques (Isolation Forest),
fusionne les informations en un niveau d'alerte et publie le résultat sur ThingsBoard.

Toutes les données d'entraînement utilisées ici sont SIMULÉES. Voir README.md.
"""
import os
import csv
import json
import math
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import paho.mqtt.client as mqtt
import requests
from fastapi import FastAPI
from pydantic import BaseModel
from sklearn.ensemble import IsolationForest, RandomForestRegressor
from sklearn.preprocessing import MinMaxScaler

# ------------------------------------------------------------------
# Configuration (les secrets viennent des variables d'environnement)
# ------------------------------------------------------------------
BASE = Path(os.getenv("SICOSE_DIR", Path(__file__).resolve().parent.parent / "data"))
BASE.mkdir(parents=True, exist_ok=True)

MODEL_RF = BASE / "model_moisissures.pkl"
MODEL_ISO = BASE / "model_anomalie.pkl"
SCALER_ISO = BASE / "scaler_anomalie.pkl"
METRICS_FILE = BASE / "metrics_history.json"
DATA_FILE = BASE / "data_reelle.csv"

TB_TOKEN = os.getenv("TB_TOKEN", "")
TB_URL = f"https://demo.thingsboard.io/api/v1/{TB_TOKEN}/telemetry"
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")

TEMP_SEUIL = 40.0          # °C
HUM_SEUIL = 70.0           # %
GAS_SEUIL_BRUT = 1000      # valeur brute du CAN de l'ESP32 (0 à 4095)

# Ordre des variables utilisé à l'entraînement ET à l'inférence de l'Isolation Forest
ISO_FEATURES = ["temperature", "humidity", "gas", "point_rosee", "temp_ext"]

app = FastAPI(title="SICoSE API", version="6.3.0")
historique_risque = deque(maxlen=12)


class Mesure(BaseModel):
    temperature: float
    humidity: float
    point_rosee: float
    gas: int = 0            # valeur brute, 0 à 4095
    flame: int = 1          # 0 = flamme détectée, 1 = pas de flamme
    temp_ext: float = 20.0
    hum_ext: float = 50.0


# ------------------------------------------------------------------
# Outils
# ------------------------------------------------------------------
def point_de_rosee(temp: float, hum: float) -> float:
    """Formule de Magnus-Tetens."""
    a, b = 17.62, 243.12
    gamma = (a * temp) / (b + temp) + math.log(max(hum, 0.01) / 100.0)
    return round((b * gamma) / (a - gamma), 2)


def publier_thingsboard(payload: dict) -> None:
    if not TB_TOKEN:
        return
    try:
        r = requests.post(TB_URL, json=payload, timeout=5)
        print("ThingsBoard OK" if r.status_code == 200 else f"ThingsBoard erreur {r.status_code}")
    except requests.RequestException as e:
        print(f"Erreur ThingsBoard : {e}")


def r2(y_true, y_pred) -> float:
    ss_res = np.sum((y_true - y_pred) ** 2)
    ss_tot = np.sum((y_true - np.mean(y_true)) ** 2)
    return max(0.0, float(1 - ss_res / ss_tot)) if ss_tot else 0.0


def sauvegarder_metriques(version: str, score: float, n: int, description: str) -> None:
    historique = []
    if METRICS_FILE.exists():
        historique = json.loads(METRICS_FILE.read_text(encoding="utf-8"))
    historique.append({
        "version": version,
        "date": datetime.now().isoformat(),
        "score_r2": round(score, 4),
        "n_samples": n,
        "description": description,
    })
    METRICS_FILE.write_text(json.dumps(historique, indent=2), encoding="utf-8")


# ------------------------------------------------------------------
# Modèles
# ------------------------------------------------------------------
def creer_random_forest():
    """Entraîne le modèle de risque de moisissures sur des données SIMULÉES."""
    rng = np.random.default_rng(42)
    n = 1000
    temp = rng.uniform(15, 40, n)
    hum = rng.uniform(30, 95, n)
    rosee = np.array([point_de_rosee(t, h) for t, h in zip(temp, hum)])
    temp_ext = rng.uniform(5, 45, n)
    grad_t = temp - temp_ext
    grad_h = hum - rng.uniform(10, 70, n)
    risque = np.clip(
        ((temp - 20) / 20) * 0.3
        + ((hum - 50) / 50) * 0.5
        + np.clip((rosee - (temp - 5)) / 5, 0, 1) * 0.2,
        0, 1,
    )
    X = np.column_stack([temp, hum, rosee, temp_ext, grad_t, grad_h])
    split = int(0.8 * n)
    model = RandomForestRegressor(n_estimators=100, random_state=42)
    model.fit(X[:split], risque[:split])
    score = r2(risque[split:], model.predict(X[split:]))
    sauvegarder_metriques("v1.0-simule", score, n, "Données simulées")
    joblib.dump(model, MODEL_RF)
    print(f"Random Forest créé (R2 = {score:.3f})")
    return model


def creer_isolation_forest():
    """
    Entraîne la détection d'anomalies sur des données SIMULÉES d'air propre,
    avec les MÊMES colonnes et le MÊME ordre que l'inférence (ISO_FEATURES).
    Valeurs brutes du gaz : air propre entre 300 et 600 (à ajuster après calibration).
    """
    rng = np.random.default_rng(42)
    n = 800
    temp = rng.uniform(18, 28, n)
    hum = rng.uniform(35, 60, n)
    gas = rng.uniform(300, 600, n)
    rosee = np.array([point_de_rosee(t, h) for t, h in zip(temp, hum)])
    temp_ext = rng.uniform(5, 25, n)
    X = np.column_stack([temp, hum, gas, rosee, temp_ext])
    scaler = MinMaxScaler().fit(X)
    model = IsolationForest(n_estimators=100, contamination=0.05, random_state=42)
    model.fit(scaler.transform(X))
    joblib.dump(model, MODEL_ISO)
    joblib.dump(scaler, SCALER_ISO)
    print("Isolation Forest créé")
    return model, scaler


if MODEL_RF.exists():
    rf_model = joblib.load(MODEL_RF)
else:
    rf_model = creer_random_forest()

if MODEL_ISO.exists() and SCALER_ISO.exists():
    iso_model = joblib.load(MODEL_ISO)
    iso_scaler = joblib.load(SCALER_ISO)
else:
    iso_model, iso_scaler = creer_isolation_forest()


# ------------------------------------------------------------------
# Calculs
# ------------------------------------------------------------------
def score_moisissures(m: Mesure) -> float:
    features = np.array([[
        m.temperature, m.humidity, m.point_rosee, m.temp_ext,
        m.temperature - m.temp_ext, m.humidity - m.hum_ext,
    ]])
    return round(float(np.clip(rf_model.predict(features)[0], 0, 1)), 3)


def score_anomalie(m: Mesure) -> float:
    x = np.array([[m.temperature, m.humidity, m.gas, m.point_rosee, m.temp_ext]])
    decision = iso_model.decision_function(iso_scaler.transform(x))[0]
    return round(float(np.clip(-decision + 0.5, 0, 1)), 3)


def ventilation(m: Mesure) -> str:
    grad_t = m.temperature - m.temp_ext
    grad_h = m.humidity - m.hum_ext
    if grad_t < 0 and grad_h > 0:
        return "élevée"      # air intérieur plus froid et plus humide : évacuation difficile
    if grad_t > 0 and grad_h > 0:
        return "moyenne"     # air chaud et humide : évacuation facilitée
    if grad_h < 0:
        return "faible"      # extérieur plus humide : limiter la ventilation
    return "normale"


def niveau_alerte(score_m: float, score_a: float, m: Mesure) -> tuple[int, str]:
    v = ventilation(m)
    if m.flame == 0:
        return 3, "Flamme détectée : intervention immédiate"
    if m.gas > GAS_SEUIL_BRUT:
        return 2, f"Gaz élevé (valeur brute {m.gas}) : ventilation {v} requise"
    if score_a > 0.75:
        return 2, f"Mesures atypiques (score {score_a:.0%}) : vérification recommandée"
    combine = 0.5 * score_m + 0.3 * score_a
    if m.temperature > TEMP_SEUIL or m.humidity > HUM_SEUIL or combine > 0.45:
        return 1, f"Risque modéré ({combine:.0%}) : ventilation {v}"
    return 0, f"Environnement normal : ventilation {v}"


def explications(m: Mesure) -> list[str]:
    raisons = []
    grad_t = m.temperature - m.temp_ext
    grad_h = m.humidity - m.hum_ext
    if m.flame == 0:
        raisons.append("Flamme détectée par le capteur")
    if m.humidity > HUM_SEUIL:
        raisons.append(f"Humidité élevée ({m.humidity:.0f} %)")
    if m.point_rosee > m.temperature - 3:
        raisons.append(f"Point de rosée proche de la température ({m.point_rosee:.1f} °C)")
    if grad_t > 8:
        raisons.append(f"Écart de température intérieur/extérieur important ({grad_t:.1f} °C)")
    if grad_h > 20:
        raisons.append(f"Écart d'humidité intérieur/extérieur important ({grad_h:.1f} %)")
    if m.temperature > 28:
        raisons.append(f"Température favorable aux moisissures ({m.temperature:.1f} °C)")
    if m.gas > GAS_SEUIL_BRUT:
        raisons.append(f"Gaz élevé (valeur brute {m.gas})")
    return raisons or ["Conditions environnementales normales"]


def tendance_risque(score: float) -> dict:
    """Tendance calculée sur les dernières mesures (régression linéaire simple)."""
    historique_risque.append(score)
    if len(historique_risque) < 3:
        return {"tendance": "insuffisant", "projection_6h": None,
                "message": "Données insuffisantes, collecte en cours"}
    valeurs = list(historique_risque)
    pente = np.polyfit(np.arange(len(valeurs)), valeurs, 1)[0]
    projection = float(np.clip(score + pente * 12, 0, 1))
    if pente > 0.01:
        tendance, msg = "hausse", f"Risque en hausse, projection {projection:.0%}"
    elif pente < -0.01:
        tendance, msg = "baisse", f"Risque en baisse, projection {projection:.0%}"
    else:
        tendance, msg = "stable", f"Risque stable autour de {score:.0%}"
    return {"tendance": tendance, "projection_6h": round(projection, 3), "message": msg}


def enregistrer_mesure(m: Mesure, risque: int) -> None:
    nouveau = not DATA_FILE.exists()
    with DATA_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "timestamp", "temperature", "humidity", "point_rosee", "gas",
            "flame", "temp_ext", "hum_ext", "risque_moisissures"])
        if nouveau:
            w.writeheader()
        w.writerow({
            "timestamp": datetime.now().isoformat(),
            "temperature": m.temperature, "humidity": m.humidity,
            "point_rosee": m.point_rosee, "gas": m.gas, "flame": m.flame,
            "temp_ext": m.temp_ext, "hum_ext": m.hum_ext,
            "risque_moisissures": risque,
        })


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------
@app.get("/")
def accueil():
    return {"systeme": "SICoSE", "statut": "actif", "version": app.version}


@app.post("/api/ai/risk-score")
def calculer_risque(m: Mesure):
    s_m = score_moisissures(m)
    s_a = score_anomalie(m)
    niveau, message = niveau_alerte(s_m, s_a, m)
    tendance = tendance_risque(s_m)
    recommandation = {
        3: "Couper les sources électriques et appeler les secours",
        2: "Activer la ventilation et éviter toute flamme",
        1: "Surveiller l'humidité et aérer régulièrement",
        0: "Conditions normales, aucune action requise",
    }[niveau]

    try:
        enregistrer_mesure(m, 1 if (m.humidity > HUM_SEUIL and m.point_rosee > m.temperature - 3) else 0)
    except OSError as e:
        print(f"Erreur d'enregistrement : {e}")

    publier_thingsboard({
        "score_moisissures": s_m,
        "score_anomalie": s_a,
        "niveau_alerte": niveau,
        "ventilation_mode": ventilation(m),
        "recommandation": recommandation,
        "tendance": tendance["tendance"],
        "projection_6h": tendance["projection_6h"],
    })

    return {
        "score": s_m,
        "score_anomalie": s_a,
        "niveau_alerte": niveau,
        "message": message,
        "raisons": explications(m),
        "recommandation": recommandation,
        "ventilation_mode": ventilation(m),
        "tendance": tendance,
    }


@app.get("/api/ai/metrics")
def metriques():
    if not METRICS_FILE.exists():
        return {"historique": [], "message": "Aucune métrique disponible"}
    historique = json.loads(METRICS_FILE.read_text(encoding="utf-8"))
    return {
        "historique": historique,
        "version_actuelle": historique[-1]["version"] if historique else "-",
        "meilleur_r2": max(h["score_r2"] for h in historique) if historique else None,
    }


@app.get("/api/ai/feature-importance")
def importance_variables():
    noms = ["temperature", "humidity", "point_rosee", "temp_ext", "gradient_temp", "gradient_hum"]
    imp = rf_model.feature_importances_
    rangs = np.argsort(imp)[::-1].tolist()
    return {"variables": [
        {"nom": noms[i], "importance": round(float(imp[i]) * 100, 1), "rang": rangs.index(i) + 1}
        for i in range(len(noms))
    ]}


@app.get("/api/data/stats")
def stats_donnees():
    if not DATA_FILE.exists():
        return {"lignes": 0, "message": "Aucune donnée collectée"}
    with DATA_FILE.open(encoding="utf-8") as f:
        lignes = sum(1 for _ in f) - 1
    return {"lignes": lignes, "pret_reentrainement": lignes >= 100}


@app.get("/api/health")
def sante():
    return {"rf_model": rf_model is not None, "iso_model": iso_model is not None,
            "mqtt_local": mqtt_connecte}


# ------------------------------------------------------------------
# MQTT local (optionnel)
# ------------------------------------------------------------------
mqtt_connecte = False


def demarrer_mqtt_local() -> None:
    global mqtt_connecte
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="SICoSE-local")
    try:
        client.connect(MQTT_HOST, 1883, 60)
        client.subscribe("v1/devices/me/telemetry")
        mqtt_connecte = True
        print("MQTT local connecté")
        client.loop_forever()
    except OSError as e:
        print(f"MQTT local indisponible : {e}")


threading.Thread(target=demarrer_mqtt_local, daemon=True).start()
# Lancement : uvicorn main:app --host 0.0.0.0 --port 8002
