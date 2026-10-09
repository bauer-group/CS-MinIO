#!/usr/bin/env bash
# =============================================================================
# minio-init integration test: real MinIO server, real mc, real init image
# =============================================================================
# 1. configs/ok.json - every resource type plus four optional items -> exit 0.
#    The resulting state is checked with mc (groups, anonymous policy,
#    lifecycle rules, scoped service account, event binding).
# 2. ok.json again, after seeding a duplicate lifecycle rule -> exit 0, nothing
#    re-created, no restart, the duplicate removed.
# 3. consumer-quirks.json - bucket values existing consumer configs use that
#    minio-init never applied ("download", "none") -> exit 0, warning only.
# 4. One config per failure class -> exit 1 with a "Failed:" line each.
#
# Needs Docker with Compose v2, jq and openssl. Secrets are generated per run.
# Usage: tests/minio-init/integration/run.sh
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

IT_ROOT_PASSWORD="$(openssl rand -hex 16)"
IT_CONSOLE_PASSWORD="$(openssl rand -hex 16)"
IT_APP_PASSWORD="$(openssl rand -hex 16)"
IT_SHORT_PASSWORD="$(openssl rand -hex 2)" # 4 characters: MinIO requires 8
IT_WEBHOOK_TOKEN="$(openssl rand -hex 16)"
export IT_ROOT_PASSWORD IT_CONSOLE_PASSWORD IT_APP_PASSWORD IT_SHORT_PASSWORD IT_WEBHOOK_TOKEN

LOGS="$(mktemp -d)"
cleanup() {
  docker compose --profile init down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$LOGS"
}
trap cleanup EXIT

failures=0
check() { # check "<description>" <command...>
  local description=$1
  shift
  if "$@"; then
    echo "PASS  $description"
  else
    echo "FAIL  $description"
    failures=$((failures + 1))
  fi
}
not() { ! "$@"; }
logged() { grep -qF -- "$2" "$LOGS/$1"; } # logged <log file> <fixed text>

# Run minio-init with one scenario config; print its log, return its exit code.
run_init() {
  local config=$1 log=$2 rc=0
  IT_CONFIG="configs/$config" docker compose run --rm -T init >"$LOGS/$log" 2>&1 || rc=$?
  echo "----- minio-init with $config (exit $rc) -----"
  cat "$LOGS/$log"
  echo "----- end of $config -----"
  return "$rc"
}

# mc --json against the rig's MinIO as alias "it" (credentials from the init service env).
mcj() {
  docker compose run --rm -T --no-deps --entrypoint sh init -c \
    'mc alias set it "$MINIO_ENDPOINT" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null && exec mc --json "$@"' \
    sh "$@"
}

# Access key in a credentials file written by minio-init (never the secret).
stored_key() {
  docker compose run --rm -T --no-deps --entrypoint python init -c \
    "import json, sys; print(json.load(open(sys.argv[1]))['accessKey'])" "/data/credentials/$1.json"
}
has_credentials() {
  docker compose run --rm -T --no-deps --entrypoint sh init -c 'test -f "$1"' sh "/data/credentials/$1.json"
}

rule_count() { mcj ilm rule ls "it/${1:-it-docs}" | jq -s '[.[].config.Rules[]?] | length'; }
bucket_is_private() { mcj anonymous get "it/$1" | jq -e '.permission == "private"' >/dev/null; }
group_holds_app() {
  mcj admin group info it gItApps |
    jq -e '(.members | index("it-app")) and (.groupPolicy | split(",") | index("pItDocs"))' >/dev/null
}
public_is_download() { mcj anonymous get it/it-public | jq -e '.permission == "download"' >/dev/null; }
worker_is_scoped() {
  mcj admin user svcacct info it "$(stored_key it-worker)" |
    jq -e '(.impliedPolicy != true) and ([.policy.Statement[].Action] | flatten | unique == ["s3:GetObject"])' >/dev/null
}
hook_is_bound() {
  mcj event ls it/it-public | jq -s -e 'map(select(.arn | endswith(":ithook:webhook"))) | length == 1' >/dev/null
}
app_has_one_account() { mcj admin user svcacct ls it it-app | jq -s -e 'length == 1' >/dev/null; }

echo "== Build the init image and start MinIO"
docker compose --profile init build init
docker compose up -d --wait minio hook

# -----------------------------------------------------------------------------
echo "== 1. ok.json: full configuration with optional items"
rc=0
run_init ok.json run1.log || rc=$?
check "first run exits 0" test "$rc" -eq 0
check "optional user is skipped" logged run1.log "Skipped: user 'it-backup': secret_key is empty"
check "group of the skipped user is skipped" logged run1.log "Skipped: group 'gItBackup' has no members"
check "service account of the skipped user is skipped" \
  logged run1.log "Skipped: service account 'it-backup-agent': parent user 'it-backup' was not created"
check "unreachable notification receiver is skipped" \
  logged run1.log "Skipped: notification 'itworker': endpoint minio-worker:8080 is not reachable"
check "summary counts the four optional items" logged run1.log "4 optional item(s) skipped"
check "nothing failed" not logged run1.log "Failed"
check "gItApps holds it-app and pItDocs" group_holds_app
check "it-public allows anonymous download" public_is_download
check "it-docs has exactly the 2 configured lifecycle rules" test "$(rule_count)" -eq 2
check "it-worker is restricted to pItDocsRead, not the parent's policies" worker_is_scoped
check "no credentials for the skipped service account" not has_credentials it-backup-agent
check "ithook is bound to it-public" hook_is_bound

# -----------------------------------------------------------------------------
echo "== 2. ok.json again: idempotent, cleans up a duplicate lifecycle rule"
key_before="$(stored_key it-worker || true)"
mcj ilm rule add --prefix daily/ --expire-days 15 it/it-docs >/dev/null || true
check "duplicate lifecycle rule seeded" test "$(rule_count)" -eq 3
rc=0
run_init ok.json run2.log || rc=$?
check "second run exits 0" test "$rc" -eq 0
check "duplicate lifecycle rule removed" test "$(rule_count)" -eq 2
check "it-worker kept, not re-created" test "$(stored_key it-worker || true)" = "$key_before"
check "it-app still has exactly one service account" app_has_one_account
check "notification target unchanged, no restart" logged run2.log "Target unchanged: ithook"
check "no service account created" not logged run2.log "Created service account"

# -----------------------------------------------------------------------------
echo "== 3. Values from existing consumer configs stay non-fatal"
rc=0
run_init consumer-quirks.json quirks.log || rc=$?
check "consumer-quirks.json exits 0" test "$rc" -eq 0
check "policy 'download' only warns" logged quirks.log "Warning: unknown policy 'download' on it-ota"
check "policy 'none' only warns" logged quirks.log "Warning: unknown policy 'none' on it-canva"
check "it-ota keeps private anonymous access" bucket_is_private it-ota
check "it-ota gets its lifecycle rule" test "$(rule_count it-ota)" -eq 1
check "nothing failed" not logged quirks.log "Failed"

# -----------------------------------------------------------------------------
echo "== 4. Real failures exit 1"
expect_failure() { # expect_failure <config> <text expected after "Failed: ">
  local config=$1 text=$2 rc=0
  run_init "$config" "$config.log" || rc=$?
  check "$config exits 1" test "$rc" -eq 1
  check "$config reports: $text" logged "$config.log" "Failed: $text"
}
expect_failure missing-policy.json "attach policy pItMissing to group gItApps"
expect_failure group-typo.json "group 'gItApp' does not exist: no configured user is in it"
expect_failure short-secret.json "create user it-short"
expect_failure missing-sa-policy.json "create service account it-unscoped (parent: it-app): read policy pItMissing"
expect_failure retention-without-lock.json "set retention on it-docs"
check "no unrestricted service account was created" not has_credentials it-unscoped

echo
if [ "$failures" -ne 0 ]; then
  echo "minio-init integration test: $failures check(s) failed"
  exit 1
fi
echo "minio-init integration test: all checks passed"
