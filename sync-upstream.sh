#!/bin/bash
set -e

echo "=== Fetching upstream ==="
git fetch upstream

echo "=== Switching to develop ==="
git checkout develop

echo "=== Merging upstream/develop ==="
git merge upstream/develop

echo "=== Pushing to origin ==="
git push origin develop

echo "=== Switching back ==="
git checkout refactor/python

echo "=== Done ==="
