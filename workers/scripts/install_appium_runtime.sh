#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
RUNTIME_DIR="${ROOT_DIR}/.runtime/appium"
APPIUM_HOME="${APPIUM_HOME:-${RUNTIME_DIR}/home}"
APPIUM_BIN="${RUNTIME_DIR}/node_modules/.bin/appium"
APPIUM_VERSION="${APPIUM_VERSION:-2.19.0}"
UIA2_VERSION="${UIA2_VERSION:-4.2.9}"

mkdir -p "${RUNTIME_DIR}" "${APPIUM_HOME}"

echo "[1/3] install python client: Appium-Python-Client"
"${PYTHON_BIN}" -m pip install -q --disable-pip-version-check --upgrade Appium-Python-Client

echo "[2/3] install appium server runtime (local)"
npm install --prefix "${RUNTIME_DIR}" --silent "appium@${APPIUM_VERSION}"

echo "[3/3] install UiAutomator2 driver (${UIA2_VERSION})"
installed_version="$(
  APPIUM_HOME="${APPIUM_HOME}" "${APPIUM_BIN}" driver list --installed --json 2>/dev/null \
    | "${PYTHON_BIN}" -c 'import json,sys; d=json.load(sys.stdin); print(d.get("uiautomator2",{}).get("version",""))' \
    || true
)"
if [ "${installed_version}" != "${UIA2_VERSION}" ]; then
  if [ -n "${installed_version}" ]; then
    APPIUM_HOME="${APPIUM_HOME}" "${APPIUM_BIN}" driver uninstall uiautomator2 || true
  fi
  APPIUM_HOME="${APPIUM_HOME}" "${APPIUM_BIN}" driver install "uiautomator2@${UIA2_VERSION}"
else
  echo "uiautomator2 already installed: ${installed_version}"
fi

# Ensure transitive modules required by some packaged driver builds are present.
npm install --prefix "${APPIUM_HOME}" --silent appium-adb appium-android-driver >/dev/null 2>&1 || true

echo "[verify] installed drivers:"
APPIUM_HOME="${APPIUM_HOME}" "${APPIUM_BIN}" driver list --installed

echo "done:"
echo "  APPIUM_BIN=${APPIUM_BIN}"
echo "  APPIUM_HOME=${APPIUM_HOME}"
