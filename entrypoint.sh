#!/bin/bash

# A script to map old python file names to new paths

# Get the script name from the first argument
SCRIPT_ARG=$1
if [ -z "$SCRIPT_ARG" ]; then
    exec python "$@"
fi

# Extract just the filename (e.g. reddit_fetch.py from scrapers/reddit_fetch.py)
FILENAME=$(basename "$SCRIPT_ARG")

# Now check where it is in the new structure
if [ -f "/app/scrapers/news/$FILENAME" ]; then
    NEW_PATH="/app/scrapers/news/$FILENAME"
elif [ -f "/app/scrapers/blogs/$FILENAME" ]; then
    NEW_PATH="/app/scrapers/blogs/$FILENAME"
elif [ -f "/app/scrapers/social_media/$FILENAME" ]; then
    NEW_PATH="/app/scrapers/social_media/$FILENAME"
elif [ -f "/app/scrapers/review_sites/$FILENAME" ]; then
    NEW_PATH="/app/scrapers/review_sites/$FILENAME"
else
    # Fallback to the original argument
    NEW_PATH="$SCRIPT_ARG"
fi

shift # remove the first argument
exec python "$NEW_PATH" "$@"
