"""The samsungtv_smart integration."""

import socket
import asyncio
import logging
import os
from aiohttp import ClientConnectionError, ClientSession, ClientResponseError
from async_timeout import timeout
from websocket import WebSocketException
from .api.samsungws import SamsungTVWS
from .api.exceptions import ConnectionFailure
from .api.smartthings import SmartThingsTV

import voluptuous as vol
import homeassistant.helpers.config_validation as cv

from homeassistant.components.media_player.const import DOMAIN as MP_DOMAIN
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.helpers.typing import HomeAssistantType

from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_MAC,
    CONF_PORT,
    CONF_DEVICE_ID,
    CONF_TIMEOUT,
    CONF_API_KEY,
    CONF_BROADCAST_ADDRESS,
)

from .const import (
    DOMAIN,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    DEFAULT_UPDATE_METHOD,
    CONF_APP_LIST,
    CONF_DEVICE_NAME,
    CONF_DEVICE_MODEL,
    CONF_LOAD_ALL_APPS,
    CONF_SOURCE_LIST,
    CONF_SHOW_CHANNEL_NR,
    CONF_UPDATE_METHOD,
    CONF_UPDATE_CUSTOM_PING_URL,
    CONF_SCAN_APP_HTTP,
    CONF_USE_ST_CHANNEL_INFO,
    DATA_LISTENER,
    DEFAULT_SOURCE_LIST,
    UPDATE_METHODS,
    WS_PREFIX,
    RESULT_NOT_SUCCESSFUL,
    RESULT_NOT_SUPPORTED,
    RESULT_ST_DEVICE_NOT_FOUND,
    RESULT_SUCCESS,
    RESULT_WRONG_APIKEY,
)

SAMSMART_SCHEMA = {
    vol.Optional(CONF_MAC): cv.string,
    vol.Optional(CONF_SOURCE_LIST, default=DEFAULT_SOURCE_LIST): cv.string,
    vol.Optional(CONF_APP_LIST): cv.string,
    vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): cv.positive_int,
    vol.Optional(CONF_UPDATE_METHOD): vol.In(UPDATE_METHODS.values()),
    vol.Optional(CONF_SHOW_CHANNEL_NR, default=False): cv.boolean,
    vol.Optional(CONF_BROADCAST_ADDRESS): cv.string,
    vol.Optional(CONF_LOAD_ALL_APPS, default=True): cv.boolean,
    vol.Optional(CONF_UPDATE_CUSTOM_PING_URL): cv.string,
    vol.Optional(CONF_SCAN_APP_HTTP, default=True): cv.boolean,
}


def ensure_unique_hosts(value):
    """Validate that all configs have a unique host."""
    vol.Schema(vol.Unique("duplicate host entries found"))(
        [socket.gethostbyname(entry[CONF_HOST]) for entry in value]
    )
    return value


CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(
            cv.ensure_list,
            [
                cv.deprecated(CONF_PORT),
                cv.deprecated(CONF_UPDATE_CUSTOM_PING_URL),
                cv.deprecated(CONF_SCAN_APP_HTTP),
                vol.Schema(
                    {
                        vol.Required(CONF_HOST): cv.string,
                        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
                        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
                        vol.Optional(CONF_API_KEY): cv.string,
                        vol.Optional(CONF_DEVICE_NAME): cv.string,
                        vol.Optional(CONF_DEVICE_ID): cv.string,
                    }
                ).extend(SAMSMART_SCHEMA),
            ],
            ensure_unique_hosts,
        )
    },
    extra=vol.ALLOW_EXTRA,
)

_LOGGER = logging.getLogger(__name__)


def tv_url(host: str, address: str = "") -> str:
    return f"http://{host}:8001/api/v2/{address}"


class SamsungTVInfo:
    def __init__(self, hass, hostname, name=""):
        self._hass = hass
        self._hostname = hostname
        self._name = name

        self._uuid = None
        self._macaddress = None
        self._device_name = None
        self._device_model = None
        self._device_os = None
        self._token_support = False
        self._port = 0

    def _gen_token_file(self, port):
        if port != 8002:
            return None

        token_file = (
            os.path.dirname(os.path.realpath(__file__))
            + "/token-"
            + self._hostname
            + ".txt"
        )

        if os.path.isfile(token_file) is False:
            # Create token file for catch possible errors
            try:
                handle = open(token_file, "w+")
                handle.close()
            except:
                _LOGGER.error(
                    "Samsung TV - Error creating token file: %s", token_file
                )
                return None

        return token_file

    def _try_connect_ws(self):
        """Try to connect to device using web sockets on port 8001 and 8002"""

        for port in (8001, 8002):

            try:
                _LOGGER.debug("Try config with port: %s", str(port))
                with SamsungTVWS(
                    name=WS_PREFIX
                    + " "
                    + self._name,  # this is the name shown in the TV list of external device.
                    host=self._hostname,
                    port=port,
                    token_file=self._gen_token_file(port),
                    timeout=45,  # We need this high timeout because waiting for auth popup is just an open socket
                ) as remote:
                    remote.open()
                _LOGGER.debug("Working config with port: %s", str(port))
                self._port = port
                return RESULT_SUCCESS
            except (OSError, ConnectionFailure, WebSocketException) as err:
                _LOGGER.debug("Failing config with port: %s, error: %s", str(port), err)

        return RESULT_NOT_SUCCESSFUL

    async def _try_connect_st(self, api_key, device_id, session: ClientSession):
        """Try to connect to ST device"""

        try:
            with timeout(10):
                _LOGGER.debug(
                    "Try connection to SmartThings TV with id [%s]", device_id
                )
                with SmartThingsTV(
                    api_key=api_key, device_id=device_id, session=session,
                ) as st:
                    result = await st.async_device_health()
                if result:
                    _LOGGER.debug("Connection completed successfully.")
                    return RESULT_SUCCESS
                else:
                    _LOGGER.debug("Connection not available.")
                    return RESULT_ST_DEVICE_NOT_FOUND
        except ClientResponseError as err:
            _LOGGER.debug("Failed connecting to SmartThings deviceID, error: %s", err)
            if err.status == 400:  # Bad request, means that token is valid
                return RESULT_ST_DEVICE_NOT_FOUND
        except Exception as err:
            _LOGGER.debug("Failed connecting with SmartThings, error: %s", err)

        return RESULT_WRONG_APIKEY

    @staticmethod
    async def get_st_devices(api_key, session: ClientSession, st_device_label=""):
        """Get list of available ST devices"""

        try:
            with timeout(4):
                devices = await SmartThingsTV.get_devices_list(
                    api_key, session, st_device_label
                )
        except Exception as err:
            _LOGGER.debug("Failed connecting with SmartThings, error: %s", err)
            return None

        return devices

    async def get_device_info(
        self, session: ClientSession, api_key=None, st_device_id=None
    ):
        """Get device information"""

        if session is None:
            return RESULT_NOT_SUCCESSFUL

        result = await self._hass.async_add_executor_job(self._try_connect_ws)
        if result != RESULT_SUCCESS:
            return result

        try:
            with timeout(2):
                async with session.get(
                    tv_url(host=self._hostname),
                    raise_for_status=True
                ) as resp:
                    info = await resp.json()
        except (asyncio.TimeoutError, ClientConnectionError):
            _LOGGER.error("Error getting HTTP info for TV: " + self._hostname)
            return RESULT_NOT_SUCCESSFUL

        device = info.get("device", None)
        if not device:
            return RESULT_NOT_SUCCESSFUL

        device_id = device.get("id")
        if device_id and device_id.startswith("uuid:"):
            self._uuid = device_id[len("uuid:") :]
        else:
            self._uuid = device_id
        self._macaddress = device.get("wifiMac")
        self._device_name = device.get("name")
        if not self._name:
            self._name = self._device_name
        self._device_model = device.get("modelName")
        self._device_os = device.get("OS")
        self._token_support = device.get("TokenAuthSupport")
        if api_key and st_device_id:
            result = await self._try_connect_st(api_key, st_device_id, session)

        return result


async def async_setup(hass: HomeAssistantType, config: ConfigEntry):
    """Set up the Samsung TV integration."""
    if DOMAIN in config:
        hass.data[DOMAIN] = {}
        for entry_config in config[DOMAIN]:
            ip_address = await hass.async_add_executor_job(
                socket.gethostbyname, entry_config[CONF_HOST]
            )
            for key in SAMSMART_SCHEMA:
                hass.data[DOMAIN].setdefault(ip_address, {})[key] = entry_config.get(
                    key
                )
            if not entry_config.get(CONF_NAME):
                entry_config[CONF_NAME] = DEFAULT_NAME
            entry_config[SOURCE_IMPORT] = True
            hass.async_create_task(
                hass.config_entries.flow.async_init(
                    DOMAIN, context={"source": SOURCE_IMPORT}, data=entry_config
                )
            )

    return True


async def async_setup_entry(hass: HomeAssistantType, entry: ConfigEntry):
    """Set up the Samsung TV platform."""
    hass.data.setdefault(DOMAIN, {}).setdefault(
        entry.unique_id, {}
    )  # unique_id = host
    hass.data[DOMAIN].setdefault(
        entry.entry_id,
        {
            "options": {
                CONF_USE_ST_CHANNEL_INFO: entry.options.get(
                    CONF_USE_ST_CHANNEL_INFO, False
                )
            },
            DATA_LISTENER: [entry.add_update_listener(update_listener)],
        }
    )

    hass.async_create_task(
        hass.config_entries.async_forward_entry_setup(entry, MP_DOMAIN)
    )

    return True


async def async_unload_entry(hass, config_entry):
    """Unload a config entry."""
    await asyncio.gather(
        *[hass.config_entries.async_forward_entry_unload(config_entry, MP_DOMAIN)]
    )
    for listener in hass.data[DOMAIN][config_entry.entry_id][DATA_LISTENER]:
        listener()
    hass.data[DOMAIN].pop(config_entry.entry_id)
    hass.data[DOMAIN].pop(config_entry.unique_id)
    if not hass.data[DOMAIN]:
        hass.data.pop(DOMAIN)
    return True


async def update_listener(hass, config_entry):
    """Update when config_entry options update."""
    entry_id = config_entry.entry_id
    for key, old_value in hass.data[DOMAIN][entry_id][
        "options"
    ].items():
        hass.data[DOMAIN][entry_id]["options"][
            key
        ] = new_value = config_entry.options.get(key)
        if new_value != old_value:
            _LOGGER.debug(
                "Changing option %s from %s to %s",
                key,
                old_value,
                hass.data[DOMAIN][entry_id]["options"][key],
            )
