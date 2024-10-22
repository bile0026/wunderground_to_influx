import configparser
from datetime import datetime
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from pprint import pprint
import requests

config = configparser.ConfigParser()
config.read("config.ini")
locations = config.sections()
common = config["COMMON"]
locations.remove("COMMON")

api_key = common["api_key"]
unit_of_measure = common["unit_of_measure"]
influxdb_url = f"http://{common['influxdb_server']}:{common['influxdb_port']}"
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

    try:
        response = requests.get(base_url, params=params, timeout=30)
        response.raise_for_status()  # Raises HTTPError for bad responses (4xx, 5xx)
        _weather_data = response.json()  # Parse the JSON response
        return _weather_data
    except requests.exceptions.RequestException as e:
        print(f"Error fetching weather data: {e}")
        return None


def write_to_influxdb(weather_data, influx_url, token, org, bucket):
    # Initialize InfluxDB client
    client = InfluxDBClient(url=influx_url, token=token, org=org)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    # Extract relevant data
    observation = weather_data["observations"][0]
    station_id = observation["stationID"]
    country = observation["country"]
    neighborhood = observation["neighborhood"]
    obs_time = observation["obsTimeUtc"]
    temperature = observation["imperial"]["temp"]
    humidity = observation["humidity"]
    dew_point = observation["imperial"]["dewpt"]
    heat_index = observation["imperial"]["heatIndex"]
    wind_speed = observation["imperial"]["windSpeed"]
    wind_direction = observation["winddir"]  # Wind direction in degrees
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

    # Convert obs_time to a Python datetime object
    obs_time_dt = datetime.strptime(obs_time, "%Y-%m-%dT%H:%M:%SZ")

    # Create InfluxDB data point
    point = (
        Point("weather")
        .tag("stationID", station_id)
        .tag("latitude", latitude)
        .tag("longitude", longitude)
        .tag("elevation", elevation)
        .tag("country", country)
        .tag("neighborhood", neighborhood)
        .field("temperature", temperature)
        .field("humidity", humidity)
        .field("dew_point", dew_point)
        .field("heat_index", heat_index)
        .field("wind_speed", wind_speed)
        .field("wind_direction", wind_direction)
        .field("wind_chill", wind_chill)
        .field("wind_gust", wind_gust)
        .field("solar_radiation", solar_radiation)
        .field("uv", uv)
        .field("pressure", pressure)
        .field("precipitation_rate", precipitation_rate)
        .field("precipitation_total", precipitation_total)
        .time(obs_time_dt, WritePrecision.S)
    )

    # Write the data to InfluxDB
    write_api.write(bucket=bucket, org=org, record=point)

    print("Data written to InfluxDB")


for location in locations:
    _loc = config[location]["station_id"]

    if _loc != "":
        weather_data = get_weather_data(_loc, unit_of_measure, api_key)

        if weather_data:
            pprint(weather_data)

            write_to_influxdb(
                weather_data=weather_data,
                influx_url=influxdb_url,
                token=influxdb_token,
                org=influxdb_org,
                bucket=influxdb_bucket
            )