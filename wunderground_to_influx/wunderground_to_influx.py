# main.py
import configparser
import logging
import logging.handlers
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.exceptions import InfluxDBError
from influxdb_client.client.write_api import SYNCHRONOUS


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

logging.info("Script setup complete!")


class WeatherAPIAuthError(Exception):
    """Raised when the Wunderground API rejects the API key (HTTP 401).

    A 401 means the key has expired or is invalid, which is a definitive
    failure (not a transient outage) that affects every location, so the caller
    treats it specially: signal a failure healthcheck and never a success one.
    """


def get_weather_data(station_id, units, wu_api_key):
    """Get current weather data for a given station ID.

    Returns the parsed JSON on success, or ``None`` on a transient/other
    failure. Raises :class:`WeatherAPIAuthError` on a 401 so the caller can
    distinguish an expired/invalid API key from a temporary Wunderground outage.
    """
    base_url = "https://api.weather.com/v2/pws/observations/current"
    params = {
        "stationId": station_id,
        "format": "json",
        "units": units,
        "apiKey": wu_api_key,
    }

    logging.info(f"Gathering weather data from {station_id}")

    try:
        response = requests.get(base_url, params=params, timeout=30)
        # Check 401 before raise_for_status() so it surfaces as a distinct
        # auth error rather than a generic RequestException.
        if response.status_code == 401:
            raise WeatherAPIAuthError(
                f"Wunderground API returned 401 for {station_id}: "
                "API key expired or invalid"
            )
        response.raise_for_status()
        _weather_data = response.json()
        return _weather_data
    except requests.exceptions.RequestException as e:
        logging.critical(f"Error fetching weather data: {e}")
        return None


def build_points(
    location_label: str, weather_data: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Convert a Wunderground API response into backend-neutral records.

    Each record is a ``{"measurement", "tags", "fields", "time"}`` dict. Both
    the InfluxDB and OpenObserve writers consume these records, so the two
    backends always stay in sync from the same source data. Field values keep
    their native JSON types (InfluxDB stores them as-is); the OpenObserve writer
    coerces numeric fields to float when building its OTLP payload.
    """
    observation = weather_data["observations"][0]
    imperial = observation["imperial"]

    obs_time_dt = datetime.strptime(observation["obsTimeUtc"], "%Y-%m-%dT%H:%M:%SZ")

    record = {
        "measurement": "weather",
        "tags": {
            "stationID": observation["stationID"],
            "realtime_frequency": observation["realtimeFrequency"],
            "location": location_label,
            "latitude": observation["lat"],
            "longitude": observation["lon"],
            "elevation": imperial["elev"],
            "country": observation["country"],
            "neighborhood": observation["neighborhood"],
        },
        "fields": {
            "temperature": imperial["temp"],
            "heat_index": imperial["heatIndex"],
            "humidity": observation["humidity"],
            "dew_point": imperial["dewpt"],
            "wind_speed": imperial["windSpeed"],
            "wind_gust": imperial["windGust"],
            "wind_direction": observation["winddir"],
            "wind_chill": imperial["windChill"],
            "solar_radiation": observation["solarRadiation"],
            "uv": observation["uv"],
            "pressure": imperial["pressure"],
            "precipitation_rate": imperial["precipRate"],
            "precipitation_total": imperial["precipTotal"],
        },
        "time": obs_time_dt,
    }

    return [record]


class InfluxDBAPI:
    """API Class for interacting with InfluxDB."""

    def __init__(self, influxdb_url: str, token: str, org: str, bucket: str):
        self.client = InfluxDBClient(url=influxdb_url, token=token, org=org)
        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
        self.bucket = bucket
        self.org = org

    def write_points(
        self, points: List[Dict[str, Any]], location_label: str = "unknown"
    ):
        """Write pre-built neutral records to InfluxDB.

        A write failure is treated as critical and raises, so the caller can
        gate its healthcheck ping on success.
        """
        logging.debug(
            f"Writing {len(points)} InfluxDB datapoints for {location_label}."
        )
        for record in points:
            point = Point(record["measurement"])
            for key, value in record["tags"].items():
                point = point.tag(key, value)
            for key, value in record["fields"].items():
                if value is not None:
                    point = point.field(key, value)
            if record.get("time") is not None:
                point = point.time(record["time"], WritePrecision.S)

            try:
                self.write_api.write(bucket=self.bucket, org=self.org, record=point)
                logging.info(f"{location_label} data written to InfluxDB")
            except InfluxDBError as e:
                logging.error(f"Failed to write data to InfluxDB: {e}")
                raise RuntimeError(
                    f"Critical: Failed to write {location_label} data to InfluxDB."
                ) from e

    def close(self):
        """Close the InfluxDB client."""
        self.client.close()


# Weather readings are instantaneous, so every numeric field is a gauge. Add
# field names here if any are ever ingested as monotonic counters instead.
_COUNTER_FIELDS: set = set()


def _metric_name(name: str) -> str:
    """Sanitize a string into a valid Prometheus/OpenObserve metric name."""
    return re.sub(r"[^a-zA-Z0-9_:]", "_", name)


def _metric_type(field_name: str) -> str:
    """Classify a numeric field as a counter or gauge metric."""
    if field_name in _COUNTER_FIELDS:
        return "counter"
    return "gauge"


def _otlp_attributes(labels: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert a label dict into OTLP attribute objects."""
    return [
        {"key": key, "value": {"stringValue": str(value)}}
        for key, value in labels.items()
    ]


def build_otlp_metrics(points: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Convert backend-neutral records into an OTLP/HTTP JSON metrics payload.

    Each numeric or boolean field becomes an OTLP data point on a metric named
    ``<measurement>_<field>`` (e.g. ``weather_temperature``). Counter fields are
    emitted as monotonic cumulative ``sum`` metrics; everything else (including
    booleans as 0/1) as ``gauge`` metrics. The record's tags become data-point
    attributes. Numeric fields are coerced to float and sent as OTLP
    ``asDouble`` so values graph as smooth line series (and to keep the metric's
    type stable if the source ever returns a decimal where it once returned an
    int); booleans use ``asInt`` (0/1). Returns ``None`` when there is nothing
    to send.
    """
    # metric name -> {"type": "counter"|"gauge", "points": [data_point, ...]}
    metrics: Dict[str, Dict[str, Any]] = {}
    default_ns = str(time.time_ns())

    for point in points:
        measurement = point["measurement"]

        labels = {k: v for k, v in point["tags"].items() if v != ""}

        if point.get("time") is not None:
            ts_ns = str(int(point["time"].timestamp() * 1_000_000_000))
        else:
            ts_ns = default_ns
        base_attributes = _otlp_attributes(labels)

        for key, val in point["fields"].items():
            # bool is a subclass of int, so it must be checked first.
            if isinstance(val, bool):
                mtype = "gauge"
                number = ("asInt", str(int(val)))
            elif isinstance(val, (int, float)):
                # Emit all numeric readings as doubles so they graph as smooth
                # line series in OpenObserve regardless of the source JSON type.
                mtype = _metric_type(key)
                number = ("asDouble", float(val))
            else:
                continue

            data_point = {
                "timeUnixNano": ts_ns,
                number[0]: number[1],
                "attributes": base_attributes,
            }
            entry = metrics.setdefault(
                _metric_name(f"{measurement}_{key}"), {"type": mtype, "points": []}
            )
            entry["points"].append(data_point)

    if not metrics:
        return None

    otlp_metrics: List[Dict[str, Any]] = []
    for name, entry in metrics.items():
        if entry["type"] == "counter":
            otlp_metrics.append(
                {
                    "name": name,
                    "sum": {
                        "aggregationTemporality": 2,  # AGGREGATION_TEMPORALITY_CUMULATIVE
                        "isMonotonic": True,
                        "dataPoints": entry["points"],
                    },
                }
            )
        else:
            otlp_metrics.append(
                {"name": name, "gauge": {"dataPoints": entry["points"]}}
            )

    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": _otlp_attributes({"service.name": "wunderground"})
                },
                "scopeMetrics": [
                    {
                        "scope": {"name": "wunderground-to-influx"},
                        "metrics": otlp_metrics,
                    }
                ],
            }
        ]
    }


class OpenObserveAPI:
    """API Class for interacting with OpenObserve.

    Writes weather data to OpenObserve as metrics (gauges) via the OTLP/HTTP
    JSON endpoint (POST /api/{org}/v1/metrics) using HTTP basic auth. Each
    numeric field becomes its own metric named ``weather_<field>``, with tags
    carried as data-point attributes.

    A write failure raises so the caller can treat the write as failed (e.g.
    skip the healthcheck ping).
    """

    def __init__(
        self,
        base_url: str,
        org: str,
        username: str,
        password: str,
        verify_ssl: bool = False,
        timeout: int = 10,
    ):
        self.base_url = base_url.rstrip("/")
        self.org = org
        self.auth = (username, password)
        self.verify_ssl = verify_ssl
        self.timeout = timeout

    def write_points(
        self, points: List[Dict[str, Any]], location_label: str = "unknown"
    ):
        """Write pre-built neutral records to OpenObserve as OTLP metrics.

        Numeric/boolean fields are exploded into individual gauge OTLP metric
        data points (see :func:`build_otlp_metrics`) and sent in a single
        request. A failure raises a RuntimeError so the caller can treat the
        write as failed (e.g. skip the healthcheck ping).
        """
        payload = build_otlp_metrics(points)
        if payload is None:
            return

        sample_count = sum(
            len((metric.get("gauge") or metric["sum"])["dataPoints"])
            for rm in payload["resourceMetrics"]
            for sm in rm["scopeMetrics"]
            for metric in sm["metrics"]
        )

        url = f"{self.base_url}/api/{self.org}/v1/metrics"  # noqa: E231
        try:
            response = requests.post(
                url,
                json=payload,
                auth=self.auth,
                verify=self.verify_ssl,
                timeout=self.timeout,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            logging.info(
                f"{location_label} data written to OpenObserve "
                f"({sample_count} metric sample(s))"
            )
        except requests.RequestException as e:
            logging.error(
                f"Failed to write metrics to OpenObserve for {location_label}: {e}"
            )
            raise RuntimeError(
                f"Failed to write metrics to OpenObserve for {location_label}."
            ) from e


def read_config(config_file: str = "config.ini") -> Dict[str, Any]:
    """Read configuration from an .ini file."""
    config = configparser.ConfigParser()
    config.read(config_file)

    common = config["COMMON"]

    # Location sections are any section that defines a station_id, which keeps
    # the backend/healthcheck sections from being mistaken for locations.
    locations = [
        section
        for section in config.sections()
        if config.has_option(section, "station_id")
    ]

    # InfluxDB backend (enabled by default for backward compatibility).
    influxdb_enabled = config.getboolean("influxdb", "enabled", fallback=True)
    influxdb_config: Optional[Dict[str, Any]] = None
    if influxdb_enabled:
        influx_server = config.get("influxdb", "influxdb_server")
        influx_port = config.get("influxdb", "influxdb_port")
        influxdb_config = {
            "url": f"http://{influx_server}:{influx_port}",  # noqa: E231
            "token": config.get("influxdb", "influxdb_api_key"),
            "org": config.get("influxdb", "influxdb_org"),
            "bucket": config.get("influxdb", "influxdb_bucket"),
        }

    # OpenObserve backend (disabled by default).
    openobserve_enabled = config.getboolean("openobserve", "enabled", fallback=False)
    openobserve_config: Optional[Dict[str, Any]] = None
    if openobserve_enabled:
        oo_scheme = config.get("openobserve", "openobserve_scheme", fallback="http")
        oo_server = config.get("openobserve", "openobserve_server")
        # An explicit port is optional: leave openobserve_port blank to use the
        # scheme default (e.g. 443 behind an HTTPS reverse proxy).
        oo_port = config.get("openobserve", "openobserve_port", fallback="5080").strip()
        oo_host = f"{oo_server}:{oo_port}" if oo_port else oo_server  # noqa: E231
        openobserve_config = {
            "base_url": f"{oo_scheme}://{oo_host}",  # noqa: E231
            "org": config.get("openobserve", "openobserve_org", fallback="default"),
            "username": config.get("openobserve", "openobserve_user"),
            "password": config.get("openobserve", "openobserve_password"),
            "verify_ssl": config.getboolean(
                "openobserve", "verify_ssl", fallback=False
            ),
        }

    return {
        "api_key": common["api_key"],
        "unit_of_measure": common["unit_of_measure"],
        "enable_healthcheck": common.get("enable_healthcheck", "false") == "true",
        "hc_guid": common.get("hc_guid", ""),
        "config": config,
        "locations": locations,
        "influxdb_enabled": influxdb_enabled,
        "influxdb_config": influxdb_config,
        "openobserve_enabled": openobserve_enabled,
        "openobserve_config": openobserve_config,
    }


def send_healthcheck(hc_guid: str, success: bool = True):
    """Ping Healthchecks.io for this job run.

    ``success=True`` sends the normal "up" ping. ``success=False`` hits the
    ``/fail`` endpoint, which immediately flags the check as down (used when the
    API key has expired) instead of waiting for the grace period to elapse.
    """
    url = f"https://hc-ping.com/{hc_guid}"  # noqa: E231
    if not success:
        url += "/fail"
    try:
        requests.get(url, timeout=10)
        logging.info(
            "Healthchecks %s ping sent successfully!",
            "fail" if not success else "success",
        )
    except requests.RequestException as e:
        logging.critical(f"Failed to ping healthcheck: {e}")


def run_weather_job():
    """Main function to gather weather data and write to the enabled backends."""

    logging.info("Starting Weather job...")

    config = read_config("config.ini")
    api_key = config["api_key"]
    unit_of_measure = config["unit_of_measure"]
    ini = config["config"]
    locations = config["locations"]

    # Build the list of enabled metric backends as (name, api) pairs. InfluxDB
    # and OpenObserve can be toggled independently, so the scraper can write to
    # either one or both (e.g. dual-write during a migration, then InfluxDB off
    # / OpenObserve only once it is complete).
    backends = []
    if config["influxdb_enabled"] and config["influxdb_config"]:
        influx = config["influxdb_config"]
        backends.append(
            (
                "InfluxDB",
                InfluxDBAPI(
                    influx["url"], influx["token"], influx["org"], influx["bucket"]
                ),
            )
        )
        logging.info("InfluxDB writing enabled")
    if config["openobserve_enabled"] and config["openobserve_config"]:
        oo = config["openobserve_config"]
        backends.append(
            (
                "OpenObserve",
                OpenObserveAPI(
                    base_url=oo["base_url"],
                    org=oo["org"],
                    username=oo["username"],
                    password=oo["password"],
                    verify_ssl=oo["verify_ssl"],
                ),
            )
        )
        logging.info("OpenObserve writing enabled")

    if not backends:
        logging.error(
            "No metric backends enabled. Enable [influxdb] and/or [openobserve] "
            "in config.ini."
        )
        return

    enable_healthcheck = config["enable_healthcheck"]
    hc_guid = config["hc_guid"]

    for location in locations:
        _loc = ini[location]["station_id"]

        if _loc == "":
            continue

        try:
            weather_data = get_weather_data(_loc, unit_of_measure, api_key)
        except WeatherAPIAuthError as e:
            # An expired/invalid API key affects every location (they share one
            # key), so signal a failure to Healthchecks -- never a success --
            # and stop this cycle instead of hammering the API with more 401s.
            logging.critical(str(e))
            if enable_healthcheck:
                send_healthcheck(hc_guid, success=False)
            return

        if not weather_data:
            # Skip this location for this cycle rather than exiting: a bad key
            # or a transient Wunderground outage must not kill the scheduler
            # (which would crash-loop under a container restart policy).
            logging.error(f"No weather data retrieved for {_loc}, skipping this cycle")
            continue

        # Build the backend-neutral records once so both backends get identical
        # data.
        points = build_points(location, weather_data)

        # Write to every enabled backend independently. A failure in one backend
        # is logged but does not stop the others; the healthcheck ping is only
        # sent when all enabled backends succeeded.
        write_ok = True
        for name, api in backends:
            try:
                api.write_points(points, location)
            except Exception as e:
                write_ok = False
                logging.error(f"Failed to write {location} data to {name}: {e}")

        if enable_healthcheck and write_ok:
            send_healthcheck(hc_guid, success=True)


if __name__ == "__main__":
    run_weather_job()
