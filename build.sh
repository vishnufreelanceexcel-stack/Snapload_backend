#!/usr/bin/env bash
# SnapLoad Build Script for Render.com
# This runs BEFORE your app starts

echo "==> Installing Python dependencies..."
pip install -r requirements.txt

echo "==> Installing ffmpeg..."
apt-get update -qq && apt-get install -y -qq ffmpeg

echo "==> Verifying ffmpeg..."
ffmpeg -version | head -1

echo "==> Build complete!"
