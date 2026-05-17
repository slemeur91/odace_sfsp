"""Intégration Odace SFSP (Schneider) pour Home Assistant."""
from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import OdaceSFSPCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.EVENT]

# Le frontend HA charge l'icône de marque via plusieurs URL selon le contexte :
#   icon.png      → carte intégration (panneau Appareils & Services)
#   logo.png      → page de détail de l'intégration / appareil
#   icon@2x.png   → variante haute résolution de l'icône
#   logo@2x.png   → variante haute résolution du logo
# On enregistre une vue pour chacune afin de couvrir tous les emplacements.
_BRAND_ASSETS = ["icon.png", "logo.png", "icon@2x.png", "logo@2x.png"]


def _make_brand_view(asset: str, icon_data: bytes) -> HomeAssistantView:
    """Fabrique une HomeAssistantView pour une URL de marque donnée."""
    safe_name = asset.replace(".", "_").replace("@", "_at_")

    class _BrandView(HomeAssistantView):
        url = f"/api/brands/integration/{DOMAIN}/{asset}"
        name = f"api:brands:integration:{DOMAIN}:{safe_name}"
        requires_auth = False

        async def get(self, request: web.Request) -> web.Response:
            return web.Response(
                body=icon_data,
                content_type="image/png",
                headers={"Cache-Control": "no-cache, no-store"},
            )

    return _BrandView()


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Enregistre les vues HTTP de l'icône de marque.

    Le frontend HA charge l'icône via quatre URL selon le contexte
    (icon.png, logo.png et variantes @2x). On enregistre une vue pour
    chacune afin que l'icône apparaisse partout (carte intégration,
    page de détail, panneau appareil).
    """
    component_dir = Path(__file__).parent
    icon_file = component_dir / "icon.png"

    # Icône de marque — lecture unique dans un executor (pas de blocking I/O
    # dans la boucle asyncio), puis service depuis la mémoire pour toutes les
    # URL de marque connues (icon.png, logo.png, variantes @2x).
    if icon_file.exists():
        try:
            icon_data: bytes = await hass.async_add_executor_job(icon_file.read_bytes)
            for asset in _BRAND_ASSETS:
                view = _make_brand_view(asset, icon_data)
                hass.http.register_view(view)
                _LOGGER.debug(
                    "Vue brand enregistrée : /api/brands/integration/%s/%s (%d bytes)",
                    DOMAIN, asset, len(icon_data),
                )
        except Exception as err:  # pragma: no cover
            _LOGGER.warning("Impossible d'enregistrer les vues brand : %s", err)
    else:
        _LOGGER.warning(
            "icon.png introuvable dans %s — les icônes d'intégration ne s'afficheront pas",
            component_dir,
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Initialise une gateway Odace SFSP."""
    coordinator = OdaceSFSPCoordinator(hass, entry)
    await coordinator.async_start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Enregistrement explicite du device gateway pour que les entités enfants
    # puissent y référencer leur via_device sans déclencher d'avertissement.
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="Schneider Electric",
        model="Odace SFSP Gateway",
        name=entry.title,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Services
    async def _svc_send_command(call: ServiceCall) -> None:
        await coordinator.async_send_command(
            call.data["uuid"],
            call.data["ac"],
            call.data.get("options"),
        )

    async def _svc_learn(call: ServiceCall) -> None:
        coordinator.start_learn(call.data.get("timeout", 60))

    async def _svc_add_device(call: ServiceCall) -> None:
        await coordinator.async_add_device(
            {
                "uuid": call.data["uuid"],
                "mac": call.data.get("mac", ""),
                "model": call.data["model"],
                "name": call.data.get("name", f"Odace SFSP {call.data['model']} {call.data['uuid']}"),
            }
        )

    async def _svc_remove_device(call: ServiceCall) -> None:
        await coordinator.async_remove_device(call.data["uuid"])

    hass.services.async_register(DOMAIN, "send_command", _svc_send_command)
    hass.services.async_register(DOMAIN, "start_learn", _svc_learn)
    hass.services.async_register(DOMAIN, "add_device", _svc_add_device)
    hass.services.async_register(DOMAIN, "remove_device", _svc_remove_device)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: OdaceSFSPCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_stop()
    return unload_ok
