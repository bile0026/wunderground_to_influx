# main.py
import configparser
from datetime import datetime
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
import logging
import logging.handlers
from pprint import pprint
import requests
import sys


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

logging.info("Script setup complete!")


def run_weather_job():
    """Main function to gather weather data and write to InfluxDB"""

    logging.info("Starting Weather job...")

    config = configparser.ConfigParser()
    config.read("config.ini")
    locations = config.sections()
    common = config["COMMON"]
    locations.remove("COMMON")

    api_key = common["api_key"]
    unit_of_measure = common["unit_of_measure"]
    influxdb_url = (
        f"http://{common['influxdb_server']}:{common['influxdb_port']}"  # noqa: E231
    )
    influxdb_token = common["influxdb_api_key"]
    influxdb_org = common["influxdb_org"]
    influxdb_bucket = common["influxdb_bucket"]

    def get_weather_data(station_id, units, wu_api_key):
        """Get current weather data for a given station ID."""
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
            response.raise_for_status()
            _weather_data = response.json()
            return _weather_data
        except requests.exceptions.RequestException as e:
            logging.critical(f"Error fetching weather data: {e}")
            return None

    def write_to_influxdb(location_label, weather_data, influx_url, token, org, bucket):
        """Write data gathered from Wunderground API to InfluxDB."""
        client = InfluxDBClient(url=influx_url, token=token, org=org)
        write_api = client.write_api(write_options=SYNCHRONOUS)

        logging.debug(f"Building influxdb datapoint for {location_label}.")

        # Extract relevant data
        observation = weather_data["observations"][0]
        realtime_frequency = observation["realtimeFrequency"]
        station_id = observation["stationID"]
        country = observation["country"]
        neighborhood = observation["neighborhood"]
        obs_time = observation["obsTimeUtc"]
        temperature = observation["imperial"]["temp"]
        humidity = observation["humidity"]
        heat_index = observation["imperial"]["heatIndex"]
        dew_point = observation["imperial"]["dewpt"]
        wind_speed = observation["imperial"]["windSpeed"]
        wind_direction = observation["winddir"]
        wind_chill = observation["imperial"]["windChill"]
        wind_gust = observation["imperial"]["windGust"]
        solar_radiation = observation["solarRadiation"]
        uv = observation["uv"]
        pressure = observation["imperial"]["pressure"]
        precipitation_rate = observation["imperial"]["precipRate"]
        precipitation_total = observation["imperial"]["precipTotal"]
        latitude = observation["lat"]
        longitude = observation["lon"]
        elevation = observation["imperial"]["elev"]

        obs_time_dt = datetime.strptime(obs_time, "%Y-%m-%dT%H:%M:%SZ")

        point = (
            Point("weather")
            .tag("stationID", station_id)
            .tag("realtime_frequency", realtime_frequency)
            .tag("location", location_label)
            .tag("latitude", latitude)
            .tag("longitude", longitude)
            .tag("elevation", elevation)
            .tag("country", country)
            .tag("neighborhood", neighborhood)
            .field("temperature", temperature)
            .field("heat_index", heat_index)
            .field("humidity", humidity)
            .field("dew_point", dew_point)
            .field("wind_speed", wind_speed)
            .field("wind_gust", wind_gust)
            .field("wind_direction", wind_direction)
            .field("wind_chill", wind_chill)
            .field("solar_radiation", solar_radiation)
            .field("uv", uv)
            .field("pressure", pressure)
            .field("precipitation_rate", precipitation_rate)
            .field("precipitation_total", precipitation_total)
            .time(obs_time_dt, WritePrecision.S)
        )

        write_api.write(bucket=bucket, org=org, record=point)

        logging.info(f"{location_label} Data written to InfluxDB")
        print(f"{location_label} Data written to InfluxDB")

    for location in locations:
        _loc = config[location]["station_id"]

        if _loc != "":
            weather_data = get_weather_data(_loc, unit_of_measure, api_key)

            if weather_data:
                pprint(weather_data)
                write_to_influxdb(
                    location_label=location,
                    weather_data=weather_data,
                    influx_url=influxdb_url,
                    token=influxdb_token,
                    org=influxdb_org,
                    bucket=influxdb_bucket,
                )
                if common.get("enable_healthcheck", "false") == "true":
                    try:
                        requests.get(
                            f"https://hc-ping.com/{common['hc_guid']}",  # noqa: E231
                            timeout=10,
                        )
                        logging.info("Healthchecks ping sent successfully!")
                    except requests.RequestException as e:
                        logging.critical(f"Failed to ping healthcheck: {e}")
                        print("Ping failed: %s" % e)
            else:
                logging.critical(f"No weather data retrieved for {_loc}")
                sys.exit(
                    f"There was an issue retrieving weather data for location {_loc}"
                )


if __name__ == "__main__":
    run_weather_job()
