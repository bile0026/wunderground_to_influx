#!/bin/bash
# copy the crontab file into the cron directory
cp /app/crontab /etc/cron.d/weather-cron

# Give execution rights on the cron job
chmod 0644 /etc/cron.d/weather-cron

# Apply cron job
crontab /etc/cron.d/weather-cron

touch /var/log/cron.log

# Start the cron service
cron -f && tail -f /var/log/cron.log
