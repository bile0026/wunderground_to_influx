# wunderground_to_influx
Pull weather data from Weather Underground and save it to Influxdb


## Setup

Make a copy of the `config.ini.example` file called `config.ini` and update the values as necessary. Additional Location sections can be added to pull as many locations as you'd like. By default the job runs the scrape every 5 minutes. This can be adjusted using the `interval` parameter in `config.ini`.

## Run

This is setup to be run in docker. All you should have to do once you have your .ini file setup is to run `docker compose up -d --build` to get rolling. There's also an example influxdb container in the `influxdb_example` folder which can be started using `docker compose up -d` as well.
