"""
Wetterdaten von Home Assistant (Ecowitt GW2000A + met.no Vorhersage).
Ruft Sensorwerte und Vorhersagen per HA REST API ab.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Europe/Berlin")
except Exception:
    _TZ = None

logger = logging.getLogger(__name__)

_WIND_DIRS = ["N","NNO","NO","ONO","O","OSO","SO","SSO","S","SSW","SW","WSW","W","WNW","NW","NNW"]

_WDAY_DE = ["Mo","Di","Mi","Do","Fr","Sa","So"]

_COND = {
    "sunny":           ("☀",  "Sonnig"),
    "partlycloudy":    ("⛅", "Wlkig"),
    "cloudy":          ("☁",  "Bewlkt"),
    "rainy":           ("🌧", "Regen"),
    "pouring":         ("🌧", "Stark-Regen"),
    "snowy":           ("❄",  "Schnee"),
    "snowy-rainy":     ("🌨", "Schneeregen"),
    "hail":            ("🌨", "Hagel"),
    "lightning":       ("⚡", "Gewitter"),
    "lightning-rainy": ("⚡", "Gewitter"),
    "windy":           ("💨", "Windig"),
    "windy-variant":   ("💨", "Windig"),
    "fog":             ("🌫", "Nebel"),
    "clear-night":     ("🌙", "Klar"),
    "exceptional":     ("⚠",  "Extrem"),
}

_FC_ENTITY = "weather.forecast_home"

_ENTITIES = [
    "sensor.gw2000a_outdoor_temperature",
    "sensor.gw2000a_humidity",
    "sensor.gw2000a_relative_pressure",
    "sensor.gw2000a_wind_speed",
    "sensor.gw2000a_wind_gust",
    "sensor.gw2000a_wind_direction",
    "sensor.gw2000a_max_daily_gust",
    "sensor.gw2000a_daily_rain_piezo",
    "sensor.gw2000a_rain_rate_piezo",
    "sensor.gw2000a_uv_index",
]


def _deg_to_dir(deg: float) -> str:
    return _WIND_DIRS[round(deg / 22.5) % 16]


def _make_connector(verify_ssl):
    """Baut den aiohttp-Connector fuer die HA-Verbindung.
    verify_ssl=True  → normale Zertifikatspruefung (Default, sicher).
    verify_ssl=False → Pruefung deaktiviert (nur fuer Alt-Setups; Token ist dann
                       MITM-gefaehrdet – im Log wird gewarnt).
    verify_ssl=<str> → Pfad zu einer CA-Datei (internes/self-signed Zertifikat pinnen)."""
    import aiohttp
    if verify_ssl is True:
        return aiohttp.TCPConnector()
    if verify_ssl is False:
        logger.warning("HA: TLS-Zertifikatspruefung deaktiviert (verify_ssl=false) – "
                       "Token ist bei MITM abgreifbar. Besser CA-Pfad pinnen.")
        return aiohttp.TCPConnector(ssl=False)
    import ssl as _ssl
    ctx = _ssl.create_default_context(cafile=str(verify_ssl))
    return aiohttp.TCPConnector(ssl=ctx)


async def _fetch_one(session, ha_url: str, eid: str) -> tuple[str, Optional[str]]:
    try:
        async with session.get(f"{ha_url}/api/states/{eid}") as resp:
            if resp.status == 200:
                data = await resp.json()
                return eid, data.get("state")
            logger.warning("HA %s HTTP %d", eid, resp.status)
            return eid, None
    except Exception as exc:
        logger.warning("HA fetch %s: %s", eid, exc)
        return eid, None


async def fetch_weather(ha_url: str, ha_token: str, qth: str = "QTH",
                        verify_ssl=True) -> list[str]:
    """Gibt einen mehrzeiligen Wetterbericht als Liste von Strings zurueck.
    Alle Zeilen zusammen passen in einen einzigen BBS-Chunk (<200 Zeichen).
    """
    try:
        import aiohttp
    except ImportError:
        return ["WX: aiohttp nicht installiert (pip install aiohttp)"]

    headers = {
        "Authorization": f"Bearer {ha_token}",
        "Content-Type": "application/json",
    }

    try:
        connector = _make_connector(verify_ssl)
        async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
            results = await asyncio.gather(
                *[_fetch_one(session, ha_url, eid) for eid in _ENTITIES]
            )
        values: dict[str, Optional[str]] = dict(results)
    except Exception:
        logger.error("WX: HA nicht erreichbar")
        return ["WX: HA nicht erreichbar"]

    def v(eid: str, decimals: int = 1) -> str:
        val = values.get(eid)
        if val in (None, "unavailable", "unknown"):
            return "?"
        try:
            return f"{float(val):.{decimals}f}"
        except (ValueError, TypeError):
            return str(val)

    wind_deg_raw = values.get("sensor.gw2000a_wind_direction")
    try:
        wind_dir = _deg_to_dir(float(wind_deg_raw))
    except (ValueError, TypeError):
        wind_dir = "?"

    if _TZ:
        now = datetime.now(_TZ).strftime("%H:%M")
    else:
        now = datetime.now(timezone.utc).strftime("%H:%Mz")

    msg = "\n".join([
        f"\U0001f324 WX {qth}  {now}",
        f"\U0001f321 {v('sensor.gw2000a_outdoor_temperature')}\xb0C  UV-Index: {v('sensor.gw2000a_uv_index', 0)}",
        f"\U0001f4a7 {v('sensor.gw2000a_humidity', 0)}%  QNH {v('sensor.gw2000a_relative_pressure', 0)} hPa",
        f"\U0001f4a8 {wind_dir}  {v('sensor.gw2000a_wind_speed')} km/h  B\xf6en {v('sensor.gw2000a_wind_gust')}  Max {v('sensor.gw2000a_max_daily_gust')}",
        f"\U0001f327 Heute {v('sensor.gw2000a_daily_rain_piezo')} mm  Rate {v('sensor.gw2000a_rain_rate_piezo')} mm/h",
    ])
    return [msg]


# ---------------------------------------------------------------------------
# Vorhersage (met.no via HA weather.forecast_home)
# ---------------------------------------------------------------------------

async def _fetch_forecasts(ha_url: str, ha_token: str, verify_ssl=True) -> list[dict]:
    """Shared helper: oeffnet aiohttp-Session und ruft taegliche Vorhersage ab."""
    import aiohttp
    headers = {"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"}
    connector = _make_connector(verify_ssl)
    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        return await _get_daily_forecasts(session, ha_url)


async def _get_daily_forecasts(session, ha_url: str) -> list[dict]:
    try:
        async with session.post(
            f"{ha_url}/api/services/weather/get_forecasts",
            params={"return_response": "true"},
            json={"entity_id": _FC_ENTITY, "type": "daily"},
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                # HA wraps response in service_response (REST API format)
                body = data.get("service_response", data)
                return body.get(_FC_ENTITY, {}).get("forecast", [])
            logger.warning("HA forecast HTTP %d", resp.status)
            return []
    except Exception as exc:
        logger.warning("HA forecast fetch: %s", exc)
        return []


def _fmt_fc_line(fc: dict) -> str:
    """Eine Zeile fuer die 3-Tage-Ansicht: 'Mo: ☀ 19-29° 💧1.2mm'"""
    dt = datetime.fromisoformat(fc["datetime"])
    if _TZ:
        dt = dt.astimezone(_TZ)
    day  = _WDAY_DE[dt.weekday()]
    icon, _ = _COND.get(fc.get("condition", ""), ("?", "?"))
    tmax = int(round(fc.get("temperature", 0)))
    tmin = int(round(fc.get("templow",    0)))
    rain = fc.get("precipitation", 0) or 0
    return f"{day}: {icon} {tmin}-{tmax}° \U0001f4a7{rain:.1f}mm"


async def fetch_forecast_1day(ha_url: str, ha_token: str, qth: str = "QTH",
                              verify_ssl=True) -> list[str]:
    """Vorhersage fuer morgen (Index 1 der taeglichen HA-Vorhersage)."""
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return ["WX1: aiohttp nicht installiert"]

    try:
        forecasts = await _fetch_forecasts(ha_url, ha_token, verify_ssl)
    except Exception:
        logger.error("WX1: HA nicht erreichbar")
        return ["WX: HA nicht erreichbar"]

    if len(forecasts) < 2:
        return ["WX1: Keine Vorhersage verfuegbar"]

    fc = forecasts[1]
    dt = datetime.fromisoformat(fc["datetime"])
    if _TZ:
        dt = dt.astimezone(_TZ)
    day      = _WDAY_DE[dt.weekday()]
    date_str = dt.strftime("%d.%m.")
    icon, cond_name = _COND.get(fc.get("condition", ""), ("?", fc.get("condition", "?")))
    tmax     = int(round(fc.get("temperature", 0)))
    tmin     = int(round(fc.get("templow",    0)))
    rain     = fc.get("precipitation", 0) or 0
    wind     = fc.get("wind_speed",    0) or 0
    wind_dir = _deg_to_dir(fc.get("wind_bearing", 0))

    msg = "\n".join([
        f"⛅ Morgen {day} {date_str} – {qth}",
        f"{icon} {cond_name}  {tmin}\xb0-{tmax}\xb0C",
        f"\U0001f4a7 {rain:.1f}mm  \U0001f4a8 {wind_dir} {wind:.0f}km/h",
    ])
    return [msg]


async def fetch_forecast_3days(ha_url: str, ha_token: str, qth: str = "QTH",
                               verify_ssl=True) -> list[str]:
    """3-Tage-Vorhersage ab morgen."""
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return ["WX3: aiohttp nicht installiert"]

    try:
        forecasts = await _fetch_forecasts(ha_url, ha_token, verify_ssl)
    except Exception:
        logger.error("WX3: HA nicht erreichbar")
        return ["WX: HA nicht erreichbar"]

    next3 = forecasts[1:4]
    if not next3:
        return ["WX3: Keine Vorhersage verfuegbar"]

    dt0   = datetime.fromisoformat(next3[0]["datetime"])
    dt_e  = datetime.fromisoformat(next3[-1]["datetime"])
    if _TZ:
        dt0  = dt0.astimezone(_TZ)
        dt_e = dt_e.astimezone(_TZ)

    header = f"\U0001f4c5 {_WDAY_DE[dt0.weekday()]}-{_WDAY_DE[dt_e.weekday()]}  {qth}"
    lines  = [header] + [_fmt_fc_line(fc) for fc in next3]
    return ["\n".join(lines)]
