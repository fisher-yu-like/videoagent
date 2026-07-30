#!/usr/bin/env bash
# Launch one validated whole-story VACE job and retain all execution evidence.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <job.json> <output-dir>" >&2
  exit 64
fi

JOB_JSON=$(cd -- "$(dirname -- "$1")" && pwd)/$(basename -- "$1")
OUTPUT_DIR=$2
if [ -e "$OUTPUT_DIR" ]; then
  echo "output directory already exists: $OUTPUT_DIR" >&2
  exit 65
fi
if [ "$(basename -- "$JOB_JSON")" != "vace_job.json" ] || [ ! -f "$JOB_JSON" ]; then
  echo "job.json must be an existing vace_job.json" >&2
  exit 66
fi

PROJECT_ROOT=${PROJECT_ROOT:-$(cd -- "$(dirname -- "$0")/.." && pwd)}
VACE_ROOT=${VACE_ROOT:-"$PROJECT_ROOT/third_party/VACE"}
WAN_ROOT=${WAN_ROOT:-"$PROJECT_ROOT/third_party/Wan2.1"}
VACE_PYTHON=${VACE_PYTHON:-python3}
VACE_CKPT_DIR=${VACE_CKPT_DIR:-"$VACE_ROOT/models/Wan2.1-VACE-1.3B"}
INFERENCE=${INFERENCE:-"$VACE_ROOT/vace/vace_wan_inference.py"}
JOB_DIR=$(dirname -- "$JOB_JSON")
EXPECTED_VACE=48eb44f1c4be87cc65a98bff985a26976841e9f3
EXPECTED_WAN=9737cba9c1c3c4d04b33fcad41c111989865d315

export PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}"
VALIDATION_LOG=$(mktemp)
cleanup_validation() { rm -f "$VALIDATION_LOG"; }
trap cleanup_validation EXIT
"$VACE_PYTHON" -m videoactagent.vace_full_chain validate "$JOB_DIR" >"$VALIDATION_LOG"

if [ "$(git -C "$VACE_ROOT" rev-parse HEAD)" != "$EXPECTED_VACE" ]; then
  echo "VACE commit gate failed" >&2
  exit 67
fi
if [ "$(git -C "$WAN_ROOT" rev-parse HEAD)" != "$EXPECTED_WAN" ]; then
  echo "Wan commit gate failed" >&2
  exit 68
fi
if [ ! -f "$INFERENCE" ] || [ ! -d "$VACE_CKPT_DIR" ]; then
  echo "VACE inference program or checkpoint is unavailable" >&2
  exit 69
fi

gpu_compute=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader,nounits)
if printf '%s\n' "$gpu_compute" | grep -Eq '^[[:space:]]*[0-9]+[[:space:]]*$'; then
  echo "GPU already has a compute process" >&2
  exit 70
fi

matching_inferences() { pgrep -fc '[v]ace_wan_inference.py' || true; }
if [ "$(matching_inferences)" -ne 0 ]; then
  echo "a VACE inference process is already present" >&2
  exit 71
fi

mkdir -p "$OUTPUT_DIR"
mv "$VALIDATION_LOG" "$OUTPUT_DIR/input_validation.json"
trap - EXIT
cp -- "$JOB_JSON" "$OUTPUT_DIR/job_input.json"

mapfile -t VALUES < <("$VACE_PYTHON" - "$JOB_JSON" <<'PY'
import base64
import json
import sys
job = json.load(open(sys.argv[1], encoding="utf-8"))
for value in (
    job["mapping"]["src_video"],
    job["mapping"]["src_mask"],
    job["mapping"]["src_ref_images"][0],
    job["mapping"]["prompt"],
):
    print(base64.b64encode(value.encode("utf-8")).decode("ascii"))
PY
)
if [ "${#VALUES[@]}" -ne 4 ]; then
  echo "validated job settings could not be materialized" >&2
  exit 72
fi
decode_base64() { printf '%s' "$1" | base64 --decode; }
SRC_VIDEO="$JOB_DIR/$(decode_base64 "${VALUES[0]}")"
SRC_MASK="$JOB_DIR/$(decode_base64 "${VALUES[1]}")"
FIRST_FRAME="$JOB_DIR/$(decode_base64 "${VALUES[2]}")"
PROMPT=$(decode_base64 "${VALUES[3]}")

COMMAND=(
  "$VACE_PYTHON" "$INFERENCE"
  --model_name vace-1.3B
  --size 480p
  --frame_num 81
  --ckpt_dir "$VACE_CKPT_DIR"
  --src_video "$SRC_VIDEO"
  --src_mask "$SRC_MASK"
  --src_ref_images "$FIRST_FRAME"
  --prompt "$PROMPT"
  --use_prompt_extend plain
  --base_seed 2026
  --sample_steps 20
  --offload_model true
  --t5_cpu
  --save_dir "$OUTPUT_DIR"
)
printf '%q ' "${COMMAND[@]}" >"$OUTPUT_DIR/command.txt"
printf '\n' >>"$OUTPUT_DIR/command.txt"

nvidia-smi --query-gpu=timestamp,index,memory.used,utilization.gpu,temperature.gpu \
  --format=csv,noheader,nounits -lms 250 >"$OUTPUT_DIR/gpu_metrics.csv" 2>&1 &
MONITOR_PID=$!
stop_monitor() {
  if kill -0 "$MONITOR_PID" 2>/dev/null; then
    kill "$MONITOR_PID" 2>/dev/null || true
    wait "$MONITOR_PID" 2>/dev/null || true
  fi
}

START_EPOCH=$SECONDS
"${COMMAND[@]}" >"$OUTPUT_DIR/stdout.log" 2>"$OUTPUT_DIR/stderr.log" &
INFERENCE_PID=$!
sleep 0.2
if [ "$(matching_inferences)" -ne 1 ]; then
  kill "$INFERENCE_PID" 2>/dev/null || true
  wait "$INFERENCE_PID" 2>/dev/null || true
  stop_monitor
  echo "expected exactly one vace_wan_inference.py process" >&2
  exit 73
fi
set +e
wait "$INFERENCE_PID"
EXIT_CODE=$?
set -e
WALL_SECONDS=$((SECONDS - START_EPOCH))
stop_monitor

PEAK_MEMORY_MIB=$(awk -F, '
  $3 ~ /[0-9]/ { value=$3; gsub(/[^0-9.]/, "", value); if (value + 0 > peak) peak=value + 0 }
  END { printf "%.0f", peak + 0 }
' "$OUTPUT_DIR/gpu_metrics.csv")
OUTPUT_VIDEO="$OUTPUT_DIR/out_video.mp4"
DECODE_OK=false
OUTPUT_SHA256=""
if [ "$EXIT_CODE" -eq 0 ] && [ -s "$OUTPUT_VIDEO" ] && \
  ffprobe -v error -count_frames -show_entries stream=codec_type,width,height,avg_frame_rate,nb_read_frames:format=duration \
    -of json "$OUTPUT_VIDEO" >"$OUTPUT_DIR/output_decode.json"; then
  if "$VACE_PYTHON" - "$OUTPUT_DIR/output_decode.json" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
streams = data.get("streams", [])
video = [item for item in streams if item.get("codec_type") == "video"]
duration_seconds = float(data.get("format", {}).get("duration", 0))
raise SystemExit(0 if len(video) == 1 and int(video[0].get("width", 0)) > 0 and int(video[0].get("height", 0)) > 0 and int(video[0].get("nb_read_frames", 0)) == 81 and video[0].get("avg_frame_rate") == "16/1" and abs(duration_seconds - 5.0625) <= 0.02 else 1)
PY
  then
    DECODE_OK=true
    OUTPUT_SHA256=$(sha256sum "$OUTPUT_VIDEO" | awk '{print $1}')
  fi
fi

export EXIT_CODE WALL_SECONDS PEAK_MEMORY_MIB DECODE_OK OUTPUT_SHA256 JOB_JSON OUTPUT_VIDEO
"$VACE_PYTHON" - "$OUTPUT_DIR/run_report.json" <<'PY'
import json
import os
import sys
report = {
    "schema_version": 1,
    "job_json": os.environ["JOB_JSON"],
    "command": "command.txt",
    "stdout": "stdout.log",
    "stderr": "stderr.log",
    "gpu_metrics": "gpu_metrics.csv",
    "exit_code": int(os.environ["EXIT_CODE"]),
    "wall_seconds": int(os.environ["WALL_SECONDS"]),
    "peak_memory_mib": int(os.environ["PEAK_MEMORY_MIB"]),
    "output_video": os.environ["OUTPUT_VIDEO"],
    "output_sha256": os.environ["OUTPUT_SHA256"] or None,
    "output_decodable": os.environ["DECODE_OK"] == "true",
    "status": "success" if os.environ["EXIT_CODE"] == "0" and os.environ["DECODE_OK"] == "true" else "failed",
}
with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as handle:
    json.dump(report, handle, sort_keys=True, indent=2)
    handle.write("\n")
PY

if [ "$EXIT_CODE" -ne 0 ]; then
  exit "$EXIT_CODE"
fi
if [ "$DECODE_OK" != true ]; then
  exit 1
fi
echo "VACE_FULL_CHAIN_SUCCESS $OUTPUT_DIR/run_report.json"
