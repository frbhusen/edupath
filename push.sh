#!/usr/bin/env bash
set -euo pipefail

read -r -p "Commit message: " msg

git status
git add .
git commit -m "$msg"
git push
