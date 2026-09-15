#!/usr/bin/env bash
set -euo pipefail
cd /var/oled/data/Galileo
remote='https://github.com/buffbot88/Galileo.git'
branch='main'
git fetch "$remote" "$branch"
git reset --hard FETCH_HEAD
corepack pnpm run build
sudo -n systemctl restart galileo.service
git rev-parse HEAD
