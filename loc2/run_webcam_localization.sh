#!/usr/bin/env bash
# Sets SWEEP_LOCALIZATION_RTSP_URL from ~/.secrets/sweep-field-relay.env and runs
# perception.webcam_localization against the aircraft stream. Never echoes the
# credentials: they are interpolated into the URL inside this shell and passed to
# the child process through the environment only.
set -euo pipefail

REPO_ROOT="${SWEEP_REPO_ROOT:-/home/gauntlet/sweep-agents/opus5-loc-commission}"
SECRETS_FILE="${SWEEP_FIELD_RELAY_SECRETS:-$HOME/.secrets/sweep-field-relay.env}"
STREAM_PATH="${SWEEP_STREAM_PATH:-drone1}"
RTSP_HOST="${SWEEP_MEDIA_RTSP_HOST:-127.0.0.1}"
RTSP_PORT="${SWEEP_MEDIA_RTSP_PORT:-18554}"

if [[ ! -r "$SECRETS_FILE" ]]; then
    echo "error: cannot read $SECRETS_FILE" >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "$SECRETS_FILE"
set +a

if [[ -z "${SWEEP_MEDIA_READ_USERNAME:-}" || -z "${SWEEP_MEDIA_READ_PASSWORD:-}" ]]; then
    echo "error: SWEEP_MEDIA_READ_USERNAME/PASSWORD are not set in $SECRETS_FILE" >&2
    exit 1
fi

export SWEEP_LOCALIZATION_RTSP_URL="rtsp://${SWEEP_MEDIA_READ_USERNAME}:${SWEEP_MEDIA_READ_PASSWORD}@${RTSP_HOST}:${RTSP_PORT}/${STREAM_PATH}"

CONFIG="${1:?usage: run_webcam_localization.sh CONFIG_JSON OUTPUT_JSONL [DURATION_S]}"
OUTPUT="${2:?usage: run_webcam_localization.sh CONFIG_JSON OUTPUT_JSONL [DURATION_S]}"
DURATION="${3:-60}"

cd "$REPO_ROOT"
exec python -m perception.webcam_localization \
    --config "$CONFIG" \
    --output "$OUTPUT" \
    --duration "$DURATION"
