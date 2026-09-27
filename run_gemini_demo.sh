#!/bin/bash
# One-command live test with Google Gemini (free tier friendly). Run:  bash run_gemini_demo.sh
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Setting up (first time only)..."
  python3 -m venv .venv
  ./.venv/bin/pip install -q httpx pydantic PyYAML starlette uvicorn
fi
if [ -z "$MS_LLM_API_KEY" ]; then
  read -s -p "Paste your Gemini API key (hidden): " MS_LLM_API_KEY; echo
fi
export MS_PROVIDER=openai
export MS_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
export MS_LLM_API_KEY
export MS_MODEL="${MS_MODEL:-gemini-3.6-flash}"
export MS_DB="${MS_DB:-demo.db}"
export MS_ECONOMY="${MS_ECONOMY:-1}"          # fewest AI requests
PERSONA="${PERSONA:-marcus}"
TARGET="${TARGET:-demo-sim-flawed}"

echo "Checking the key and model $MS_MODEL..."
ok=""
for wait in 0 10 20 40 60; do
  [ "$wait" != "0" ] && { echo "Google is busy (HTTP $code). Retrying in ${wait}s..."; sleep $wait; }
  code=$(curl -s -o /tmp/ms_check.json -w "%{http_code}" \
    -H "Authorization: Bearer $MS_LLM_API_KEY" -H "Content-Type: application/json" \
    -d "{\"model\":\"$MS_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Say OK\"}]}" \
    "$MS_BASE_URL/chat/completions")
  if [ "$code" = "200" ]; then ok=1; break; fi
  case "$code" in 429|500|502|503) continue ;; *) break ;; esac
done
if [ -z "$ok" ]; then
  echo "Gemini answered HTTP $code:"; head -c 400 /tmp/ms_check.json; echo
  echo
  echo "Models available to your key:"
  curl -s -H "Authorization: Bearer $MS_LLM_API_KEY" "$MS_BASE_URL/models" \
    | grep -o '"id": *"[^"]*"' | sed 's/.*"models\/\{0,1\}\([^"]*\)"/  \1/' | grep -i gemini | head -25
  echo
  echo "Then rerun with:  MS_MODEL=<name> bash run_gemini_demo.sh"
  exit 1
fi
echo "Key works. Running $PERSONA against $TARGET (economy mode)..."
./.venv/bin/python -m mystery_shopper run --country FI --persona "$PERSONA" --scenario website_chat \
  --target "$TARGET" --targets config/targets.example.yaml
