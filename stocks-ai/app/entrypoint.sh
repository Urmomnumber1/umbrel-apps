#!/bin/sh
# Start as root only to make sure the data folder is writable, then run the app as uid 1000.
set -e
mkdir -p /data
chown -R 1000:1000 /data
exec setpriv --reuid=1000 --regid=1000 --clear-groups uvicorn stocksai.main:app --host 0.0.0.0 --port 8000
