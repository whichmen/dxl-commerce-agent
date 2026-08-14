#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_DIR="$(cd "${ROOT_DIR}/../.." && pwd)"

if [[ -f "${PROJECT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${PROJECT_DIR}/.env"
  set +a
fi

CONFIG_PATH="${1:-$HOME/.openclaw/openclaw.json}"
OPENCLAW_BIN="${CLAWBOT_OPENCLAW_BIN:-openclaw}"
PLUGIN_DIR="${CLAWBOT_PLUGIN_DIR:-${ROOT_DIR}/../plugins/kefu-tools}"
SKILL_DIR="${ROOT_DIR}/skills"
MANAGED_DUP_SKILL_DIR="${HOME}/.openclaw/skills/kefu-core"
TOOLS_ALLOW_JSON='["kefu-tools"]'

case "${CLAWBOT_ENABLE_OPENCLAW_BUILTINS:-0}" in
  1|true|TRUE|yes|YES|on|ON)
    TOOLS_ALLOW_JSON='["group:openclaw", "kefu-tools"]'
    ;;
esac

if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq is required."
  exit 1
fi

if ! command -v "${OPENCLAW_BIN}" >/dev/null 2>&1; then
  echo "ERROR: OpenClaw executable not found: ${OPENCLAW_BIN}" >&2
  echo "Install OpenClaw and run 'openclaw onboard --install-daemon' first." >&2
  exit 1
fi

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo "ERROR: config not found: ${CONFIG_PATH}"
  echo "Run 'openclaw onboard --install-daemon' first, then rerun this script."
  exit 1
fi

if [[ ! -f "${PLUGIN_DIR}/openclaw.plugin.json" || ! -f "${PLUGIN_DIR}/index.ts" ]]; then
  echo "ERROR: plugin files missing in ${PLUGIN_DIR}"
  exit 1
fi

if [[ ! -f "${SKILL_DIR}/kefu-core/SKILL.md" ]]; then
  echo "ERROR: skill file missing: ${SKILL_DIR}/kefu-core/SKILL.md"
  exit 1
fi

tmp_file="$(mktemp)"
jq \
  --arg workspace "${ROOT_DIR}" \
  --arg plugin "${PLUGIN_DIR}" \
  --arg skill_dir "${SKILL_DIR}" \
  --argjson tools_allow "${TOOLS_ALLOW_JSON}" \
  '
  .agents.list = (
    (.agents.list // []) as $agents |
    if (($agents | map(.id) | index("kefu_ops")) != null) then
      ($agents | map(
        if .id == "kefu_ops" then
          .workspace = $workspace
          | .tools = ((.tools // {}) + {allow: $tools_allow})
        else
          .
        end
      ))
    else
      ($agents + [{
        id: "kefu_ops",
        name: "kefu_ops",
        workspace: $workspace,
        tools: {allow: $tools_allow}
      }])
    end
  )
  | .plugins = ((.plugins // {}) + {
      load: {paths: [$plugin]},
      entries: ((.plugins.entries // {}) + {"kefu-tools": {"enabled": true}}),
      installs: ((.plugins.installs // {}) + {
        "kefu-tools": ((.plugins.installs["kefu-tools"] // {}) + {
          source: "path",
          sourcePath: $plugin,
          installPath: $plugin
        })
      })
    })
  | .skills = ((.skills // {}) + {
      load: ((.skills.load // {}) + {extraDirs: [$skill_dir]}),
      limits: (.skills.limits // {})
    })
  ' \
  "${CONFIG_PATH}" > "${tmp_file}"

mv "${tmp_file}" "${CONFIG_PATH}"

if [[ -d "${MANAGED_DUP_SKILL_DIR}" ]]; then
  echo "WARN: duplicate skill directory exists: ${MANAGED_DUP_SKILL_DIR}"
  echo "      Review it manually; this script does not delete local data."
fi

echo "OK: OpenClaw config updated."
echo "  workspace : ${ROOT_DIR}"
echo "  skills    : ${SKILL_DIR}"
echo "  plugin    : ${PLUGIN_DIR}"
echo "  tools     : ${TOOLS_ALLOW_JSON}"
echo
echo "Next:"
echo "  1) openclaw gateway restart"
echo "  2) openclaw skills info kefu-core"
echo "  3) openclaw plugins info kefu-tools"
