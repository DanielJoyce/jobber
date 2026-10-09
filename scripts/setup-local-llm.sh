#!/usr/bin/env bash
# Plan (and, only with your approval, perform) a local model runtime for the openai-compat
# scorer: llama.cpp's llama-server or Ollama, plus one model. See specs/016.
#
# Nothing is installed or downloaded without a y/N answer to the exact command shown first.
# --dry-run prints everything and runs nothing. The server is only ever bound to 127.0.0.1.
# llama-server gets a random API key: generated once, stored as LLAMA_API_KEY in ~/.env after
# a y/N, and never printed.
#
# Usage: scripts/setup-local-llm.sh [--runtime llama.cpp|ollama]
#          [--model gpt-oss-20b|gemma-4-26b-a4b|granite-4.0-h-micro|lfm-2.5-2.6b]
#          [--yes] [--dry-run]
set -euo pipefail

runtime=""
model=""
assume_yes=false
dry_run=false

usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --runtime) runtime="${2:-}"; shift 2 ;;
    --model) model="${2:-}"; shift 2 ;;
    --yes) assume_yes=true; shift ;;
    --dry-run) dry_run=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$runtime" in ""|llama.cpp|ollama) ;; *) echo "unknown --runtime: $runtime" >&2; exit 2 ;; esac
case "$model" in
  ""|gpt-oss-20b|gemma-4-26b-a4b|granite-4.0-h-micro|lfm-2.5-2.6b) ;;
  *) echo "unknown --model: $model" >&2; exit 2 ;;
esac

# -- 1. Detect ----------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }

echo "== What is on this machine =="
if have llama-server; then echo "llama-server: found ($(command -v llama-server))"; else echo "llama-server: not found"; fi
if have ollama; then echo "ollama: found ($(command -v ollama))"; else echo "ollama: not found"; fi

# What llama-server itself can run on. A Homebrew build is often CPU/BLAS only, and then the
# GPU in nvidia-smi is not used at all. llama_gpu: yes = a GPU backend is listed, no = only
# CPU/BLAS listed, unknown = no list (no llama-server, or it printed no devices).
llama_gpu=unknown
llama_devices=""
if have llama-server; then
  devices_out="$(llama-server --list-devices 2>/dev/null || true)"
  llama_devices="$(echo "$devices_out" | sed -n 's/^  \([^:]*\): .*/\1/p' | tr '\n' ' ')"
  llama_devices="${llama_devices% }"
  if [[ -n "$llama_devices" ]]; then
    llama_gpu=no
    for dev in $llama_devices; do
      case "$dev" in CUDA*|Vulkan*|ROCm*|HIP*|MTL*|Metal*|SYCL*|MUSA*|CANN*) llama_gpu=yes ;; esac
    done
    if [[ "$llama_gpu" == yes ]]; then
      echo "llama-server backends: $llama_devices (GPU build)"
    else
      echo "llama-server backends: $llama_devices (CPU-only build)"
    fi
  else
    echo "llama-server --list-devices printed no devices; the GPU check falls back to nvidia-smi"
  fi
fi

vram_mib=0
if have nvidia-smi; then
  gpu_line="$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null | head -n1 || true)"
  if [[ -n "$gpu_line" ]]; then
    vram_mib="$(echo "$gpu_line" | awk -F', *' '{print $NF}' | tr -dc '0-9')"
    vram_mib="${vram_mib:-0}"
    echo "GPU: ${gpu_line%%,*}, ${vram_mib} MiB VRAM (nvidia-smi)"
  else
    echo "GPU: nvidia-smi present but reported no GPU"
  fi
else
  echo "GPU: nvidia-smi not found (assuming CPU only)"
fi

ram_mib=0
if [[ -r /proc/meminfo ]]; then
  ram_mib="$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)"
fi
echo "Free RAM: ${ram_mib} MiB available"

if grep -qm1 -w avx2 /proc/cpuinfo 2>/dev/null; then avx2=yes; else avx2=no; fi
echo "AVX2: $avx2"
disk_gib="$(df -Pk "${HOME:-.}" 2>/dev/null | awk 'NR==2 {print int($4/1048576)}')"
echo "Free disk in \$HOME: ${disk_gib:-?} GiB"
echo

# -- 2. Recommend -------------------------------------------------------------
if [[ -z "$model" ]]; then
  if (( ram_mib >= 16384 )); then
    model=gpt-oss-20b
    echo "Recommended model: $model (enough free RAM for a 20-26B MoE on CPU, about 13-16 GB; best quality here, overnight speed)"
  else
    model=granite-4.0-h-micro
    echo "Recommended model: $model (under 16 GB free RAM, so a 20-26B MoE would swap; a 3B model fits on the GPU. Close the console to free RAM for the MoE models)"
  fi
else
  echo "Model: $model (chosen by you)"
fi
if [[ -z "$runtime" ]]; then
  runtime=llama.cpp
  echo "Recommended runtime: llama.cpp (smallest footprint, best prefix caching)"
else
  echo "Runtime: $runtime (chosen by you)"
fi
if [[ "$avx2" == no ]]; then echo "warning: no AVX2 detected; CPU inference will be very slow."; fi

# Model facts: size, RAM, placement, llama.cpp -hf repo, Ollama tag.
# Repo and tag names are the best known; verify them before relying on a download.
case "$model" in
  gpt-oss-20b)
    size="about 12-13 GB"; need_ram="13-16 GB"; where="CPU, attention on the GPU (MoE experts stay in RAM)"
    hf="ggml-org/gpt-oss-20b-GGUF"; otag="gpt-oss:20b"; kind=moe ;;
  gemma-4-26b-a4b)
    size="about 15 GB (Q4)"; need_ram="16-18 GB"; where="CPU, attention on the GPU (MoE experts stay in RAM)"
    hf="google/gemma-4-26B-A4B-it-qat-q4_0-gguf"; otag="gemma4:26b"; kind=moe ;;
  granite-4.0-h-micro)
    size="about 2 GB (Q4)"; need_ram="3 GB"; where="entirely on the 4 GB GPU"
    hf="ibm-granite/granite-4.0-h-micro-GGUF"; otag="granite4:micro-h"; kind=small ;;
  lfm-2.5-2.6b)
    size="about 1.7 GB (Q4)"; need_ram="3 GB"; where="entirely on the 4 GB GPU"
    hf="LiquidAI/LFM2.5-2.6B-GGUF"; otag="lfm2.5:2.6b"; kind=small ;;
esac

# The GPU plan needs a GPU the build can use. When llama-server lists its devices, that list
# decides; otherwise nvidia-smi is the only evidence.
if [[ "$llama_gpu" == unknown ]]; then
  if (( vram_mib > 0 )); then gpu_present=yes; else gpu_present=no; fi
else
  gpu_present="$llama_gpu"
fi

cpu_build=no
if [[ "$gpu_present" == no && "$llama_gpu" == no ]]; then
  cpu_build=yes
  gpu_flags=""  # a CPU-only build offloads nothing; no --n-gpu-layers at all
  gpu_note="this llama-server build is CPU-only: everything runs on the CPU"
elif [[ "$gpu_present" == no ]]; then
  gpu_flags=" --n-gpu-layers 0"
  gpu_note="no GPU detected: everything runs on the CPU"
elif [[ "$kind" == small ]]; then
  gpu_flags=" --n-gpu-layers 99"
  gpu_note="all layers fit in the GPU${vram_mib:+ ($vram_mib MiB)}"
else
  # 4 GB holds attention + KV cache but not the experts: keep expert tensors on the CPU.
  gpu_flags=" --n-gpu-layers 99 --n-cpu-moe 99"
  gpu_note="attention and KV cache on the GPU, expert weights on the CPU; needs a recent llama.cpp, else use --n-gpu-layers 6 and tune"
fi

echo
echo "== Plan for $model on $runtime =="
echo "Download size: $size. RAM needed while scoring: $need_ram. Placement: $where."
echo "Disk: keep at least double the download size free (${disk_gib:-?} GiB free now)."
if [[ "$cpu_build" == yes ]]; then
  echo "Plan: CPU placement (threads 6); this build is CPU-only; a CUDA or Vulkan build would use the GPU."
  echo "  Build options: cmake -B build -DGGML_CUDA=ON (NVIDIA) or -DGGML_VULKAN=ON (any GPU),"
  echo "  then cmake --build build --config Release. Use that llama-server, then rerun this script."
fi
echo "Speed is a guess until measured: run 'jobhunter llm bench --scorer local:$model --n 10'."
echo

# -- 3. Steps (each asks first) -------------------------------------------------
confirm() {  # confirm "<what will happen>" "<command>"
  local what="$1" cmd="$2"
  echo "STEP: $what"
  echo "  command: $cmd"
  if $dry_run; then echo "  [dry-run] would ask y/N; running nothing."; return 1; fi
  if ! $assume_yes; then
    local reply=""
    printf "  Run it? [y/N] "
    read -r reply || true
    echo
    [[ "$reply" =~ ^[Yy]$ ]] || { echo "  skipped."; return 1; }
  fi
  return 0
}

run() { bash -c "$1"; }

# The API key: 32 random bytes, hex-encoded. Printed nowhere; only written to the env file.
key_file="${HOME:-.}/.env"
gen_key() {
  if have python3; then
    python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
  elif have openssl; then
    openssl rand -hex 32
  else
    echo "need python3 or openssl to generate a key" >&2
    return 1
  fi
}

if [[ "$runtime" == llama.cpp ]]; then
  install_cmd="brew install llama.cpp"
  if have llama-server; then
    echo "llama-server already installed; no install step."
  else
    echo "Note: the Homebrew build may lack CUDA. If GPU offload does nothing, build llama.cpp"
    echo "with -DGGML_CUDA=ON (or use a Vulkan build); the CPU path works regardless."
    if confirm "install llama.cpp (a few hundred MB, Homebrew)" "$install_cmd"; then run "$install_cmd"; fi
  fi
else
  install_cmd="curl -fsSL https://ollama.com/install.sh | sh"
  if have ollama; then
    echo "ollama already installed; no install step."
  else
    echo "Note: Ollama's official installer needs sudo, adds a systemd service and bundles CUDA (about 1.5 GB)."
    echo "Read the script first: curl -fsSL https://ollama.com/install.sh | less"
    if confirm "install Ollama with its official installer" "$install_cmd"; then run "$install_cmd"; fi
  fi
fi

if [[ "$runtime" == ollama ]]; then
  url="http://127.0.0.1:11434/v1"
  pull_cmd="ollama pull $otag"
  if confirm "download $model ($size)" "$pull_cmd"; then run "$pull_cmd"; fi
  echo
  echo "Ollama serves on 127.0.0.1:11434 by default. Do not set OLLAMA_HOST to 0.0.0.0."
else
  url="http://127.0.0.1:8080"
  serve_cmd="llama-server -hf $hf --host 127.0.0.1 --port 8080 --ctx-size 8192${gpu_flags} --threads 6 --parallel 1 --cache-reuse 256 --jinja --no-slots --api-key \"\$LLAMA_API_KEY\""

  echo
  echo "== API key for llama-server =="
  key_ready=no
  if [[ -z "${LLAMA_API_KEY:-}" && -r "$key_file" ]]; then
    file_key="$(grep -m1 '^LLAMA_API_KEY=' "$key_file" | cut -d= -f2- | tr -d "\"'\r" || true)"
    if [[ -n "$file_key" ]]; then export LLAMA_API_KEY="$file_key"; fi
    file_key=""
  fi
  if [[ -n "${LLAMA_API_KEY:-}" ]]; then
    echo "LLAMA_API_KEY is set (value not shown)."
    key_ready=yes
  elif $dry_run; then
    echo "No LLAMA_API_KEY yet. A real run asks y/N, then generates one random 32-byte key and"
    echo "stores it as LLAMA_API_KEY in $key_file (mode 600). A dry run writes nothing."
  else
    echo "No LLAMA_API_KEY yet. llama-server needs one: a random 32-byte key, stored as"
    echo "LLAMA_API_KEY in $key_file (mode 600). It is never printed."
    reply="y"
    if ! $assume_yes; then
      reply=""
      printf "  Generate it and store it? [y/N] "
      read -r reply || true
      echo
    fi
    if [[ "$reply" =~ ^[Yy]$ ]]; then
      LLAMA_API_KEY="$(gen_key)"
      export LLAMA_API_KEY
      (umask 077; touch "$key_file")
      if [[ -s "$key_file" && "$(tail -c1 "$key_file" | od -An -c | tr -d ' ')" != '\n' ]]; then
        echo >> "$key_file"
      fi
      printf 'LLAMA_API_KEY=%s\n' "$LLAMA_API_KEY" >> "$key_file"
      chmod 600 "$key_file"
      echo "  stored in $key_file (value not shown)."
      key_ready=yes
    else
      echo "  not stored. llama-server is not started without a key: without one, any web page"
      echo "  open in your browser could read its answers (see the CORS note below)."
      echo "  To add one yourself, without this script printing it:"
      echo "    echo \"LLAMA_API_KEY=\$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')\" >> $key_file"
    fi
  fi

  echo
  echo "== llama-server command (binds 127.0.0.1 only) =="
  echo "$serve_cmd"
  cat <<NOTES
  --ctx-size 8192    rubric + profile (~3k tokens) + posting (~2k) + reply fit
  GPU: $gpu_note
  --threads 6        the 6 physical cores of the i7-10850H
  --parallel 1       one slot, so the whole context belongs to one job
  --cache-reuse 256  reuse the KV cache for the shared rubric+profile prefix
  --jinja            use the model's own chat template
  --no-slots         /slots would show cached prompts, which hold the resume: turned off
  --api-key          the key from LLAMA_API_KEY; jobhunter sends it as a Bearer token
  CORS               llama-server allows every origin by default. Kept, on purpose: a web page
                     cannot read answers without the key, and no page has it. See specs/016,
                     "Local server security".
  JSON schema: llama-server turns response_format json_schema into a grammar; no flag needed.
  The first start downloads the model ($size) into llama.cpp's cache.
NOTES
  if [[ "$key_ready" == yes || "$dry_run" == true ]]; then
    if confirm "start llama-server now in the foreground (downloads $size on first start; Ctrl-C stops it)" "$serve_cmd"; then
      run "$serve_cmd"
    fi
  else
    echo "Not starting llama-server: it needs LLAMA_API_KEY first (see above)."
  fi
fi

echo
echo "== Point jobhunter at it (config.toml) =="
cat <<TOML
[scoring]
screen_scorer = "local:$model"

[scoring.local]
runtime = "$runtime"      # base URL $url
api_key_env = "LLAMA_API_KEY"  # the key is read from ~/.env or the environment
TOML
echo
echo "Then: jobhunter llm status && jobhunter llm bench --scorer local:$model --n 10"
echo "Nothing is installed unless you answered y above."
