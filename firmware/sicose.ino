/*
 * SICoSE - firmware ESP32 (prototype)
 *
 * - Lit un capteur DHT22 (intérieur), un MQ-2 (gaz, valeur brute) et un capteur de flamme.
 * - Récupère la météo extérieure (Open-Meteo) toutes les 10 minutes.
 * - Envoie les mesures brutes à ThingsBoard et les appelle l'API IA du Raspberry Pi.
 * - Commande un ventilateur, une pompe, un servo de balayage, une LED et un buzzer.
 *
 * Les identifiants Wi-Fi, le token ThingsBoard et l'adresse du Raspberry Pi sont dans
 * secrets.h (non publié). Copier secrets.h.example vers secrets.h et le remplir.
 */

#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ESP32Servo.h>
#include "DHT.h"
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include "secrets.h"

// ---------------- Configuration ----------------
const char* MQTT_SERVER = "demo.thingsboard.io";
const int   MQTT_PORT   = 1883;
const char* METEO_URL =
  "http://api.open-meteo.com/v1/forecast"
  "?latitude=34.03&longitude=-5.00"
  "&current_weather=true&hourly=relativehumidity_2m&forecast_days=1";

// ---------------- Broches ----------------
const int MQ2_PIN       = 33;   // sortie analogique du MQ-2
const int FLAME_PIN     = 35;   // sortie numérique du capteur de flamme (LOW = flamme)
const int FAN_PIN       = 27;
const int LED_RED_PIN   = 15;
const int LED_YELLOW_PIN= 17;
const int BUZZER_PIN    = 14;
const int PUMP_PIN      = 23;
const int SERVO_PIN     = 26;
const int SERVO_PUMP_PIN= 25;
const int DHT_PIN       = 13;
#define DHTTYPE DHT22

// Seuil de gaz en valeur brute (CAN 12 bits, 0 à 4095). À calibrer.
const int GAS_SEUIL_BRUT = 1000;

// ---------------- Objets et état ----------------
Servo servoCapteur;
Servo servoPompe;
DHT dht(DHT_PIN, DHTTYPE);
WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);

bool manualFan  = false;
bool manualPump = false;
int  angleActuel = 0;
int  directionBalayage = 1;

unsigned long lastSend  = 0;
unsigned long lastMeteo = 0;
unsigned long lastAI    = 0;
const unsigned long SEND_INTERVAL  = 5000;     // 5 s
const unsigned long METEO_INTERVAL = 600000;   // 10 min
const unsigned long AI_INTERVAL    = 3000;     // 3 s

float tempExt = 20.0;
float humExt  = 50.0;

// ---------------- Point de rosée (Magnus-Tetens) ----------------
float pointDeRosee(float T, float RH) {
  const double a = 17.62, b = 243.12;
  double gamma = (a * T) / (b + T) + log(RH / 100.0);
  return (b * gamma) / (a - gamma);
}

// ---------------- Commandes reçues de ThingsBoard ----------------
void callbackRpc(char* topic, byte* payload, unsigned int length) {
  String msg;
  for (unsigned int i = 0; i < length; i++) msg += (char)payload[i];
  Serial.println("RPC: " + msg);
  if (msg.indexOf("fan_on")   >= 0) manualFan  = true;
  if (msg.indexOf("fan_off")  >= 0) manualFan  = false;
  if (msg.indexOf("pump_on")  >= 0) manualPump = true;
  if (msg.indexOf("pump_off") >= 0) manualPump = false;
}

void reconnecterMqtt() {
  while (!mqtt.connected()) {
    Serial.print("Connexion MQTT...");
    if (mqtt.connect("SICoSE_ESP32", TB_TOKEN, "")) {
      Serial.println(" OK");
      mqtt.subscribe("v1/devices/me/rpc/request/+");
    } else {
      Serial.printf(" echec rc=%d, nouvel essai dans 3 s\n", mqtt.state());
      delay(3000);
    }
  }
}

// ---------------- Météo extérieure ----------------
void recupererMeteo() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  http.begin(METEO_URL);
  if (http.GET() == 200) {
    StaticJsonDocument<2048> doc;
    if (!deserializeJson(doc, http.getString())) {
      tempExt = doc["current_weather"]["temperature"] | tempExt;
      humExt  = doc["hourly"]["relativehumidity_2m"][0] | humExt;
      Serial.printf("Meteo exterieure : %.1f C, %.1f %%\n", tempExt, humExt);
    }
  }
  http.end();
}

// ---------------- Appel de l'API IA (Raspberry Pi) ----------------
void appellerIA(float temp, float hum, int gasBrut, int feu) {
  if (WiFi.status() != WL_CONNECTED) return;

  float rosee = pointDeRosee(temp, hum);
  char body[256];
  snprintf(body, sizeof(body),
    "{\"temperature\":%.1f,\"humidity\":%.1f,\"point_rosee\":%.1f,"
    "\"gas\":%d,\"flame\":%d,\"temp_ext\":%.1f,\"hum_ext\":%.1f}",
    temp, hum, rosee, gasBrut, feu, tempExt, humExt);

  HTTPClient http;
  http.begin(PI_API_URL);
  http.addHeader("Content-Type", "application/json");
  if (http.POST(body) == 200) {
    StaticJsonDocument<512> doc;
    if (!deserializeJson(doc, http.getString())) {
      char tb[256];
      snprintf(tb, sizeof(tb),
        "{\"score_moisissures\":%.2f,\"score_anomalie\":%.2f,"
        "\"niveau_alerte\":%d,\"tendance\":\"%s\"}",
        (float)(doc["score"] | 0.0f),
        (float)(doc["score_anomalie"] | 0.0f),
        (int)(doc["niveau_alerte"] | 0),
        (const char*)(doc["tendance"]["tendance"] | "stable"));
      mqtt.publish("v1/devices/me/telemetry", tb);
      Serial.println("Scores IA envoyes : " + String(tb));
    }
  }
  http.end();
}

// ---------------- Envoi des mesures brutes ----------------
void envoyerMesures(float temp, float hum, int gasBrut, bool flame, bool fan, bool pump) {
  char buf[256];
  snprintf(buf, sizeof(buf),
    "{\"temperature\":%.1f,\"humidity\":%.1f,\"gas\":%d,"
    "\"flame\":%s,\"fan\":%s,\"pump\":%s}",
    temp, hum, gasBrut,
    flame ? "true" : "false", fan ? "true" : "false", pump ? "true" : "false");
  mqtt.publish("v1/devices/me/telemetry", buf);
}

// ---------------- Initialisation ----------------
void setup() {
  Serial.begin(115200);

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.println("\nWi-Fi OK");

  mqtt.setServer(MQTT_SERVER, MQTT_PORT);
  mqtt.setCallback(callbackRpc);

  pinMode(MQ2_PIN, INPUT);
  pinMode(FLAME_PIN, INPUT);
  pinMode(FAN_PIN, OUTPUT);
  pinMode(LED_RED_PIN, OUTPUT);
  pinMode(LED_YELLOW_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(PUMP_PIN, OUTPUT);

  digitalWrite(FAN_PIN, LOW);
  digitalWrite(PUMP_PIN, LOW);
  digitalWrite(LED_RED_PIN, LOW);
  digitalWrite(LED_YELLOW_PIN, HIGH);
  digitalWrite(BUZZER_PIN, LOW);

  servoCapteur.attach(SERVO_PIN, 500, 2400);
  servoCapteur.write(90);
  servoPompe.attach(SERVO_PUMP_PIN, 500, 2400);
  servoPompe.write(90);

  dht.begin();
  recupererMeteo();
  Serial.println("Systeme pret");
}

// ---------------- Boucle principale ----------------
void loop() {
  if (!mqtt.connected()) reconnecterMqtt();
  mqtt.loop();

  unsigned long now = millis();

  // Capteurs
  int  gasBrut = analogRead(MQ2_PIN);
  bool flamme  = (digitalRead(FLAME_PIN) == LOW);
  float hum    = dht.readHumidity();
  float temp   = dht.readTemperature();

  if (isnan(hum) || isnan(temp)) {
    Serial.println("Erreur DHT22");
    delay(2000);
    return;
  }

  // Ventilateur : commande manuelle ou gaz élevé
  bool gazEleve = gasBrut > GAS_SEUIL_BRUT;
  bool ventilateur = manualFan || gazEleve;
  digitalWrite(FAN_PIN, ventilateur ? HIGH : LOW);

  // Balayage du servo capteur, arrêt et pompe si flamme
  if (flamme || manualPump) {
    servoCapteur.write(angleActuel);
    servoPompe.write(angleActuel);
    digitalWrite(PUMP_PIN, HIGH);
    delay(500);
  } else {
    digitalWrite(PUMP_PIN, LOW);
    angleActuel += directionBalayage * 5;
    if (angleActuel >= 180) directionBalayage = -1;
    else if (angleActuel <= 0) directionBalayage = 1;
    servoCapteur.write(angleActuel);
    delay(100);
  }

  // LED et buzzer
  digitalWrite(LED_RED_PIN, flamme ? HIGH : LOW);
  digitalWrite(LED_YELLOW_PIN, flamme ? LOW : HIGH);
  digitalWrite(BUZZER_PIN, flamme ? HIGH : LOW);

  Serial.printf("Gaz brut:%d | Int: %.1f C / %.1f %% | Flamme:%s | Ventilateur:%s\n",
                gasBrut, temp, hum, flamme ? "OUI" : "NON", ventilateur ? "ON" : "OFF");

  if (now - lastSend >= SEND_INTERVAL) {
    lastSend = now;
    envoyerMesures(temp, hum, gasBrut, flamme, ventilateur, digitalRead(PUMP_PIN) == HIGH);
  }
  if (now - lastMeteo >= METEO_INTERVAL) {
    lastMeteo = now;
    recupererMeteo();
  }
  if (now - lastAI >= AI_INTERVAL) {
    lastAI = now;
    appellerIA(temp, hum, gasBrut, flamme ? 0 : 1);   // API : 0 = flamme, 1 = pas de flamme
  }

  delay(20);
}
