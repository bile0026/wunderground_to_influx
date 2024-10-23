# job.py
import configparser
import schedule
import time
import logging
from wunderground_to_influx import run_weather_job  # Updated import

config = configparser.ConfigParser()
config.read("config.ini")
interval = int(config["COMMON"]["interval"])

# Setup logging if needed
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

logging.info("Weather app started successfully...")
print("Weather app started successfully...")

# Schedule the job every 5 minutes
schedule.every(interval).minutes.do(run_weather_job)

logging.info("Starting scheduler...")

# Keep the script running to execute scheduled tasks
while True:
    schedule.run_pending()
    time.sleep(1)
