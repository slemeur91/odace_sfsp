/**
 * ble_advertiser.h
 * Composant custom ESPHome pour diffuser un paquet BLE advertising
 * à partir d'un payload hexadécimal reçu via MQTT.
 *
 * Utilisation dans odace_esp32.yaml :
 *   esphome:
 *     includes:
 *       - ble_advertiser.h
 *
 * Puis dans le handler on_message MQTT :
 *   lambda: |-
 *     ble_advertiser_send(x.c_str());
 *
 * Compatibilité : ESP32 (framework Arduino + ESP-IDF 4.x)
 * NON compatible : ESP32-C3 (Bluetooth LE uniquement sans advertising raw)
 *
 * Principe :
 * Le payload Beagle est un paquet BLE Non-Connectable Undirected Advertising
 * (ADV_NONCONN_IND) de 31 octets. On utilise l'API esp_ble_gap pour le diffuser
 * exactement comme le ferait hcitool en v1.
 *
 * Séquence :
 *   1. Désactiver temporairement le scan passif (bluetooth_proxy)
 *   2. Configurer et activer l'advertising avec le payload
 *   3. Attendre 500 ms (même temporisation que la v1 hcitool)
 *   4. Désactiver l'advertising
 *   5. Réactiver le scan passif
 */

#pragma once

#include "esphome.h"
#include <esp_bt.h>
#include <esp_gap_ble_api.h>

// Durée d'advertising en millisecondes (identique à la v1)
static const uint32_t BLE_ADV_DURATION_MS = 500;

/**
 * Convertit une chaîne hexadécimale en tableau d'octets.
 * Retourne le nombre d'octets convertis (0 en cas d'erreur).
 */
static size_t hex_to_bytes(const std::string &hex, uint8_t *out, size_t max_len) {
  if (hex.length() % 2 != 0) return 0;
  size_t len = hex.length() / 2;
  if (len > max_len) return 0;
  for (size_t i = 0; i < len; i++) {
    char byte_str[3] = {hex[i * 2], hex[i * 2 + 1], '\0'};
    out[i] = (uint8_t) strtol(byte_str, nullptr, 16);
  }
  return len;
}

/**
 * Diffuse le payload Beagle comme un paquet BLE advertising.
 *
 * Le payload doit être une chaîne hex de 62 caractères (31 octets).
 * Les 2 premiers octets de manufacturer data (company ID 0x02B6) sont
 * déjà inclus dans le payload complet envoyé par HA.
 */
void ble_advertiser_send(const std::string &payload_hex) {
  // Validation basique
  if (payload_hex.length() != 62) {
    ESP_LOGW("ble_adv", "Payload invalide : longueur %d (attendu 62)", payload_hex.length());
    return;
  }

  uint8_t adv_data[31];
  size_t adv_len = hex_to_bytes(payload_hex, adv_data, sizeof(adv_data));
  if (adv_len != 31) {
    ESP_LOGW("ble_adv", "Erreur de conversion hex → bytes");
    return;
  }

  ESP_LOGI("ble_adv", "Envoi BLE advertising : %s", payload_hex.c_str());

  // --- Paramètres d'advertising ---
  esp_ble_adv_params_t adv_params = {};
  adv_params.adv_int_min       = 0x00A0;  // 100 ms (identique hcitool 0x0006 A0 00)
  adv_params.adv_int_max       = 0x00A0;
  adv_params.adv_type          = ADV_TYPE_NONCONN_IND;  // Non-connectable, comme hcitool
  adv_params.own_addr_type     = BLE_ADDR_TYPE_PUBLIC;
  adv_params.channel_map       = ADV_CHNL_ALL;          // 0x07 = tous les canaux
  adv_params.adv_filter_policy = ADV_FILTER_ALLOW_SCAN_ANY_CON_ANY;

  // 1. Configurer le raw advertising data
  esp_err_t ret = esp_ble_gap_config_adv_data_raw(adv_data, adv_len);
  if (ret != ESP_OK) {
    ESP_LOGE("ble_adv", "esp_ble_gap_config_adv_data_raw failed: %d", ret);
    return;
  }

  // 2. Démarrer l'advertising
  ret = esp_ble_gap_start_advertising(&adv_params);
  if (ret != ESP_OK) {
    ESP_LOGE("ble_adv", "esp_ble_gap_start_advertising failed: %d", ret);
    return;
  }

  // 3. Maintenir l'advertising pendant 500 ms
  delay(BLE_ADV_DURATION_MS);

  // 4. Arrêter l'advertising
  ret = esp_ble_gap_stop_advertising();
  if (ret != ESP_OK) {
    ESP_LOGE("ble_adv", "esp_ble_gap_stop_advertising failed: %d", ret);
  }

  // 5. Le scan passif (bluetooth_proxy) reprend automatiquement
  //    grâce à l'intégration ESPHome Bluetooth Proxy.
  ESP_LOGI("ble_adv", "Advertising terminé");
}
