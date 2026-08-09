#!/usr/bin/env bash
# Verifies that the `neo4j-admin database import` command an extractor prints
# for `--format csv` actually loads into a real Neo4j database, and that the
# resulting graph's node/relationship counts match the generated CSVs.
#
# This exists because two real bugs slipped past the unit tests and were only
# found by actually running the printed command against real neo4j-admin:
# the <database> positional argument being placed where neo4j-admin's own
# --help says to (which it then mis-parses), and a CSV column typed from only
# the first value seen (which neo4j-admin rejects once a later row disagrees).
# Both are now regression-tested at the unit level too, but this script is
# what would have caught them in the first place, and is the only check that
# exercises the exact command text a user would copy and run.
#
# Usage: verify-neo4j-import.sh <label> <sample-path> <extractor-argv...>
set -euo pipefail

LABEL="$1"
SAMPLE_PATH="$2"
shift 2
EXTRACTOR_ARGV=("$@")

NEO4J_IMAGE="neo4j:5-community"
NEO4J_PASSWORD="verifyImportPw1"

WORKDIR=$(mktemp -d)
OUTPUT_DIR="$WORKDIR/import"
VOLUME="neo4j_import_verify_${LABEL}_$$"
CONTAINER="neo4j_import_verify_server_${LABEL}_$$"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker volume rm -f "$VOLUME" >/dev/null 2>&1 || true
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

echo "== [$LABEL] generating CSV output =="
"${EXTRACTOR_ARGV[@]}" --format csv --output-dir "$OUTPUT_DIR" "$SAMPLE_PATH" > "$WORKDIR/import_command.txt"
IMPORT_CMD=$(cat "$WORKDIR/import_command.txt")
echo "$IMPORT_CMD"

case "$IMPORT_CMD" in
  "neo4j-admin database import full neo4j "*) ;;
  *)
    echo "FAIL [$LABEL]: printed command doesn't start with the expected 'neo4j-admin database import full neo4j ...' shape" >&2
    exit 1
    ;;
esac

PYTHON=$(command -v python3 || command -v python)

echo "== [$LABEL] counting expected rows from the CSVs =="
read -r CSV_NODES CSV_RELS < <("$PYTHON" - "$OUTPUT_DIR" <<'PYEOF'
import csv, glob, os, sys

output_dir = sys.argv[1]


def count(pattern):
    total = 0
    for path in glob.glob(os.path.join(output_dir, pattern)):
        with open(path, newline="", encoding="utf-8") as handle:
            total += sum(1 for _ in csv.reader(handle)) - 1
    return total


print(count("nodes/*.csv"), count("relationships/*.csv"))
PYEOF
)
echo "expected: $CSV_NODES nodes, $CSV_RELS relationships"

echo "== [$LABEL] running the exact printed neo4j-admin import command in Docker =="
docker volume create "$VOLUME" >/dev/null
IMPORT_OUTPUT=$(docker run --rm \
  -v "$OUTPUT_DIR:$OUTPUT_DIR" \
  -v "$VOLUME:/data" \
  "$NEO4J_IMAGE" \
  bash -c "$IMPORT_CMD" 2>&1) || {
    echo "$IMPORT_OUTPUT" >&2
    echo "FAIL [$LABEL]: neo4j-admin import exited with an error" >&2
    exit 1
  }
echo "$IMPORT_OUTPUT"
echo "$IMPORT_OUTPUT" | grep -q "IMPORT DONE" || {
  echo "FAIL [$LABEL]: neo4j-admin import did not report success" >&2
  exit 1
}

echo "== [$LABEL] starting a Neo4j server on the imported data and querying it =="
docker run -d --name "$CONTAINER" \
  -v "$VOLUME:/data" \
  -e NEO4J_AUTH="neo4j/$NEO4J_PASSWORD" \
  "$NEO4J_IMAGE" >/dev/null

ready=0
for _ in $(seq 1 60); do
  if docker exec "$CONTAINER" cypher-shell -u neo4j -p "$NEO4J_PASSWORD" "RETURN 1;" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" -ne 1 ]]; then
  echo "FAIL [$LABEL]: Neo4j server never became ready" >&2
  docker logs "$CONTAINER" >&2 || true
  exit 1
fi

actual_nodes=$(docker exec "$CONTAINER" cypher-shell -u neo4j -p "$NEO4J_PASSWORD" --format plain "MATCH (n) RETURN count(n);" | tail -n1 | tr -d '"')
actual_rels=$(docker exec "$CONTAINER" cypher-shell -u neo4j -p "$NEO4J_PASSWORD" --format plain "MATCH ()-[r]->() RETURN count(r);" | tail -n1 | tr -d '"')
echo "actual: $actual_nodes nodes, $actual_rels relationships"

if [[ "$actual_nodes" != "$CSV_NODES" || "$actual_rels" != "$CSV_RELS" ]]; then
  echo "FAIL [$LABEL]: imported graph counts don't match the CSVs (expected $CSV_NODES/$CSV_RELS, got $actual_nodes/$actual_rels)" >&2
  exit 1
fi

echo "PASS [$LABEL]: neo4j-admin import loaded $actual_nodes nodes / $actual_rels relationships matching the generated CSVs"
