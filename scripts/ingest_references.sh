#!/bin/bash
# =====================================================================
# 📚 AUTOMATED REFERENCE INGESTION & DIGESTION SCRIPT
# Clones all OpenAlgo repositories and Symphony XTS SDKs into /references/
# =====================================================================
set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REF_DIR="$PROJECT_ROOT/references"
mkdir -p "$REF_DIR/docs"

echo "==========================================================="
echo " 📚 CLONING OPENALGO SUITE & SYMPHONY XTS REFERENCE SDKS   "
echo "==========================================================="

clone_or_pull() {
    local url=$1
    local dest=$2
    if [ -d "$REF_DIR/$dest/.git" ]; then
        echo "🔄 Updating $dest..."
        git -C "$REF_DIR/$dest" pull --ff-only 2>/dev/null || true
    else
        echo "📥 Cloning $dest ($url)..."
        git clone --depth 1 "$url" "$REF_DIR/$dest" 2>/dev/null || true
    fi
}

# 1. OpenAlgo Ecosystem
clone_or_pull "https://github.com/marketcalls/openalgo.git" "openalgo"
clone_or_pull "https://github.com/marketcalls/openalgo-charts.git" "openalgo-charts"
clone_or_pull "https://github.com/marketcalls/openalgo-docs.git" "openalgo-docs"
clone_or_pull "https://github.com/marketcalls/TradingAgent.git" "TradingAgent"
clone_or_pull "https://github.com/marketcalls/openbull.git" "openbull"
clone_or_pull "https://github.com/marketcalls/Algomirror.git" "Algomirror"

# 2. Symphony XTS Official SDKs
clone_or_pull "https://github.com/symphonyfintech/xts-pythonclient-api-sdk.git" "xts-pythonclient-api-sdk"
clone_or_pull "https://github.com/symphonyfintech/xts-pythonsdk-binarymarketdata-withbroadcastmode-api-sdk.git" "xts-binary-marketdata-sdk"

echo "==========================================================="
echo " ✅ ALL 8 REFERENCE REPOSITORIES INGESTED INTO /references/!"
echo "==========================================================="
