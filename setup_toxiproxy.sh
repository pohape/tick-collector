#!/usr/bin/env bash
set -euo pipefail

echo "Starting toxiproxy container..."
docker run -d --name toxiproxy --restart always \
  -p 8474:8474 -p 29080:29080 -p 29081:29081 \
  ghcr.io/shopify/toxiproxy:latest

echo "Waiting for toxiproxy to start..."
sleep 2

echo "Creating proxies..."
toxiproxy-cli create binance_ws -l 0.0.0.0:29080 -u fstream.binance.com:443
toxiproxy-cli create bybit_ws   -l 0.0.0.0:29081 -u stream.bybit.com:443

echo "Done. Proxies:"
toxiproxy-cli list
