#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
nginx_dir="${NGINX_DIR:-/home/lin/nginx}"
site="$nginx_dir/sites-enabled/stock.conf"
source_conf=docker/nginx-stock.conf
compose=(docker compose -f docker-compose.yml --env-file .env)

for file in .env .worker.env docker-compose.yml "$source_conf" "$site"; do
    if [[ ! -f "$file" ]]; then
        echo "Missing required file: $file" >&2
        exit 1
    fi
done
command -v docker >/dev/null
command -v curl >/dev/null
command -v make >/dev/null
"${compose[@]}" config --quiet
docker exec nginx nginx -t
make check

# Build before changing the running site or its proxy configuration.
"${compose[@]}" build web
if ! cmp -s "$source_conf" "$site"; then
    backup="$site.bak.$(date +%Y%m%d_%H%M%S).$$"
    cp -p "$site" "$backup"
    staging=$(mktemp "$site.tmp.XXXXXX")
    cp -p "$source_conf" "$staging"
    mv "$staging" "$site"
    if ! docker exec nginx nginx -t; then
        cp -p "$backup" "$site"
        docker exec nginx nginx -t
        echo "Nginx config rejected; restored $backup" >&2
        exit 1
    fi
    echo "Nginx config backup: $backup"
fi

"${compose[@]}" up -d --no-deps --force-recreate web
ready=0
for _ in {1..10}; do
    if curl --fail --silent --max-time 3 -o /dev/null \
        -H 'Host: stock.keyi.win' http://127.0.0.1:8550/; then
        ready=1
        break
    fi
    sleep 2
done
if [[ "$ready" -ne 1 ]]; then
    echo 'Web health check failed; Nginx was not reloaded. Inspect the web container.' >&2
    exit 1
fi

# Reload resolves the recreated container's new Docker IP without restarting other sites.
docker exec nginx nginx -t
docker exec nginx nginx -s reload
# Direct origin uses a Cloudflare Origin CA certificate, hence -k for this localhost probe only.
ready=0
for _ in {1..5}; do
    if curl --fail --silent --max-time 5 -k -o /dev/null \
        --resolve stock.keyi.win:443:127.0.0.1 https://stock.keyi.win/; then
        ready=1
        break
    fi
    sleep 2
done
if [[ "$ready" -ne 1 ]]; then
    echo 'HTTPS origin health check failed after reload; inspect Nginx and web logs.' >&2
    exit 1
fi
"${compose[@]}" up -d --no-deps --force-recreate worker
sleep 2
if [[ "$(docker inspect --format '{{.State.Running}} {{.RestartCount}}' a-stock-screen-worker)" != 'true 0' ]]; then
    echo 'Worker failed to start; web is live but updates will not run.' >&2
    exit 1
fi
echo 'Deployed: web and worker running, Nginx reloaded, HTTPS origin healthy.'
