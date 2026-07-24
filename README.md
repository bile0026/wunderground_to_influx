# wunderground_to_influx
Pull weather data from Weather Underground and save it to Influxdb


## Setup

Make a copy of the `config.ini.example` file called `config.ini` and update the values as necessary. Additional Location sections can be added to pull as many locations as you'd like. By default the job runs the scrape every 5 minutes. This can be adjusted using the `interval` parameter in `config.ini`.

## Run

This is setup to be run in docker. All you should have to do once you have your .ini file setup is to run `docker compose up -d --build` to get rolling. There's also an example influxdb container in the `influxdb_example` folder which can be started using `docker compose up -d` as well.

## Metric backends (InfluxDB and/or OpenObserve)

The scraper can write the same weather data to **InfluxDB**, **[OpenObserve](https://openobserve.ai/)**, or both at once. Each backend has its own `enabled` toggle, so you can dual-write while migrating and then flip InfluxDB off once you are running on OpenObserve alone. At least one backend must be enabled.

```ini
[influxdb]
enabled = true
influxdb_server = 127.0.0.1
influxdb_port = 8086
influxdb_api_key = NotAStrongToken0=
influxdb_org = home
influxdb_bucket = weather-data

[openobserve]
enabled = true
openobserve_scheme = http
openobserve_server = 127.0.0.1
openobserve_port = 5080
openobserve_org = default
openobserve_user = root@example.com
openobserve_password = your_openobserve_password_or_token
verify_ssl = false
```

For OpenObserve the data is written as **metrics** (not logs) via the OTLP/HTTP JSON endpoint (`/api/{org}/v1/metrics`) using HTTP basic auth (an OpenObserve user email plus its password or ingestion token). Each numeric field becomes its own gauge metric named `weather_<field>` (e.g. `weather_temperature`, `weather_humidity`, `weather_wind_speed`). The tags (station ID, location, neighborhood, country, lat/long, elevation, realtime frequency) are attached as data-point attributes, so the data is queryable with SQL and PromQL under **Metrics**. Leave `openobserve_port` blank to use the scheme's default port (e.g. 443 behind an HTTPS reverse proxy).

InfluxDB (when enabled) still receives the full measurement/tag/field records — both backends stay in sync from the same source data, they just shape it to fit each system.

Typical migration path:

1. `[influxdb] enabled = true` + `[openobserve] enabled = true` — dual-write and validate OpenObserve.
2. `[influxdb] enabled = false` + `[openobserve] enabled = true` — OpenObserve only.

Each enabled backend is written independently: a failure in one is logged and does not stop the other. The healthchecks.io ping (if configured) is only sent when **all** enabled backends wrote successfully, so a failure in any active backend will surface as a missed check-in.

## Development

Use the poetry environment by running `poetry shell` and `poetry install --with dev`. Before committing, there is a pre-commit hook that checks for secrets with GitGuardian, and uses `black` for formatting, and `flake8` for some basic syntax checks. Before committing changes run `pre-commit install` then `pre-commit run --all-files`.
