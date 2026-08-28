#!/usr/bin/env bash
# github_sync.sh — verified bidirectional sync between Omega (the master) and
# GitHub (buffbot88/AshatCodingAgent).
#
# The sync never "randomly pulls files": every operation first fetches the
# remote, computes the exact divergence (which commits, which files, in which
# direction), verifies nothing dangerous is staged, and only then applies.
#
#   ./scripts/github_sync.sh init            one-time baseline: make the master
#                                            a git repo with origin and commit
#                                            the local state on top of GitHub's
#                                            history (no push)
#   ./scripts/github_sync.sh status          fetch + report direction, commits,
#                                            changed files (dry run, no changes)
#   ./scripts/github_sync.sh pull            apply remote commits (fast-forward
#                                            only), verify (fmt+test+build with
#                                            auto-rollback), propagate to peers
#   ./scripts/github_sync.sh push            push local commits to GitHub (needs
#                                            the deploy key)
#
# Flags:
#   --json            machine-readable report on stdout (for the admin endpoint)
#   --yes             non-interactive (never prompt)
#   --restart-service pull: restart the local systemd unit after a build
#
# Env overrides:
#   ASHAT_GITHUB_URL       HTTPS remote (fetch)   default https://github.com/buffbot88/AshatCodingAgent.git
#   ASHAT_GITHUB_SSH_URL   SSH remote (push)      default git@github.com:buffbot88/AshatCodingAgent.git
#   ASHAT_GITHUB_BRANCH                          default main
#   ASHAT_GITHUB_KEY       deploy key            default ~/.ssh/ashat_github
#   ASHAT_GIT_NAME / ASHAT_GIT_EMAIL             commit identity

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

HTTPS_URL="${ASHAT_GITHUB_URL:-https://github.com/buffbot88/AshatCodingAgent.git}"
SSH_URL="${ASHAT_GITHUB_SSH_URL:-git@github.com:buffbot88/AshatCodingAgent.git}"
BRANCH="${ASHAT_GITHUB_BRANCH:-main}"
SSH_KEY="${ASHAT_GITHUB_KEY:-$HOME/.ssh/ashat_github}"
GIT_NAME="${ASHAT_GIT_NAME:-Ashat Master Coding Agent}"
GIT_EMAIL="${ASHAT_GIT_EMAIL:-omega@ashat-neural-host.local}"

# Push goes through SSH with the deploy key (IdentitiesOnly so other ssh
# agents on the box can't interfere). Fetch stays on HTTPS (public repo).
export GIT_SSH_COMMAND="ssh -i $SSH_KEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new"

YES=0
JSON=0
RESTART=0
FORCE=0
CMD=""

# Files that must never appear in the index, ever. server-config.json embeds
# ASHAT_KEY + slave api_keys; oraclehost_id_rsa is the Beta/Delta SSH key.
PROTECTED_RE='(^|/)(server-config\.json|oraclehost_id_rsa(\.pub)?)$|^models/|^logs/|^workspaces/|^target/'

log()  { printf '\n==> %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

confirm() {
    [ "$YES" = 1 ] && return 0
    if [ ! -t 0 ]; then
        die "not a TTY and --yes not given (pass --yes to run non-interactively)"
    fi
    read -r -p "Proceed? [y/N] " ans || exit 1
    case "$ans" in y|Y) return 0 ;; *) exit 1 ;; esac
}

ensure_repo() {
    git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
        || die "not a git repo — run 'scripts/github_sync.sh init' first"
    git remote get-url origin >/dev/null 2>&1 || die "no origin remote — run init"
}

guard_secrets() {
    local bad
    bad="$(git ls-files | grep -E "$PROTECTED_RE" || true)"
    if [ -n "$bad" ]; then
        printf 'REFUSING: tracked files that must never be in the repo:\n%s\n' "$bad" >&2
        return 1
    fi
}

# Belt-and-braces: verify .gitignore rules are actually effective for the
# protected paths before anything is staged. This catches a corrupted or
# clobbered .gitignore (e.g. inline comments, which git treats literally).
assert_ignore_effective() {
    local probe fail=0
    for probe in server-config.json oraclehost_id_rsa \
        models/models.sentinel target/sentinel \
        logs/sentinel workspaces/sentinel; do
        if ! git check-ignore -q "$probe" 2>/dev/null; then
            printf 'REFUSING: ignore rule NOT effective for %s — .gitignore is broken or missing.\n' "$probe" >&2
            fail=1
        fi
    done
    return "$fail"
}

tracked_dirty() {
    # Non-empty if tracked files have uncommitted changes (ignores untracked/
    # ignored files like server-config.json, models/, target/).
    git status --porcelain --untracked-files=no
}

emit_json() {
    # Reads GS_* env vars and prints a JSON report. Multiline fields are
    # split on newlines. Called via a heredoc so nothing is shell-quoted.
    GS_COMMAND="$CMD" GS_OK="$1" python3 - <<'PY'
import json, os
def get(k):
    return os.environ.get("GS_" + k, "")
def lines(k):
    return [l for l in get(k).split("\n") if l]
num = lambda k: int(get(k)) if str(get(k)).lstrip("-").isdigit() else 0
report = {
    "command": get("COMMAND"),
    "ok": get("OK") in ("1", "ok", "true"),
    "repo": get("REPO"),
    "branch": get("BRANCH"),
    "direction": get("DIRECTION"),
    "local_sha": get("LOCAL_SHA"),
    "remote_sha": get("REMOTE_SHA"),
    "ahead": num("AHEAD"),
    "behind": num("BEHIND"),
    "outgoing_commits": lines("OUTGOING_COMMITS"),
    "incoming_commits": lines("INCOMING_COMMITS"),
    "outgoing_files": lines("OUTGOING_FILES"),
    "incoming_files": lines("INCOMING_FILES"),
    "tracked_secrets": lines("TRACKED_SECRETS"),
    "service_restart_required": get("RESTART_REQUIRED") == "1",
    "peers": lines("PEERS"),
    "message": get("MESSAGE"),
}
print(json.dumps(report, indent=2))
PY
}

refresh_divergence() {
    LOCAL_SHA="$(git rev-parse HEAD)"
    REMOTE_SHA="$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo unknown)"
    AHEAD="$(git rev-list --count "origin/$BRANCH..HEAD" 2>/dev/null || echo 0)"
    BEHIND="$(git rev-list --count "HEAD..origin/$BRANCH" 2>/dev/null || echo 0)"
    if [ "$AHEAD" -eq 0 ] && [ "$BEHIND" -eq 0 ]; then
        DIRECTION="in_sync"
    elif [ "$AHEAD" -gt 0 ] && [ "$BEHIND" -eq 0 ]; then
        DIRECTION="local_ahead"
    elif [ "$AHEAD" -eq 0 ] && [ "$BEHIND" -gt 0 ]; then
        DIRECTION="remote_ahead"
    else
        DIRECTION="diverged"
    fi
}

set_common_report() {
    export GS_REPO="$HTTPS_URL" GS_BRANCH="$BRANCH" GS_DIRECTION="$DIRECTION"
    export GS_LOCAL_SHA="${LOCAL_SHA:-}" GS_REMOTE_SHA="${REMOTE_SHA:-}"
    export GS_AHEAD="${AHEAD:-0}" GS_BEHIND="${BEHIND:-0}"
    export GS_TRACKED_SECRETS="$(git ls-files | grep -E "$PROTECTED_RE" || true)"
}

finish_overlay_commit() {
    # add first, *then* un-track: `git add -A` re-stages HEAD-tracked files
    # even when ignored, so the rm must come after the add to win.
    assert_ignore_effective || die "aborted: .gitignore is not effective"
    git add -A
    git rm -r --cached --ignore-unmatch server-config.json logs workspaces \
        oraclehost_id_rsa models target 2>/dev/null || true
    guard_secrets || die "aborted: protected file would be staged"

    if git diff --cached --quiet; then
        log "no changes vs origin/$BRANCH — nothing to commit"
    else
        git commit -q -m "Omega master v6.2 — weighted row-chain routing, admin-key split, GitHub sync tooling"
        log "baseline committed: $(git rev-parse --short HEAD)"
    fi
}

cmd_init() {
    if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        git remote get-url origin >/dev/null 2>&1 || git remote add origin "$HTTPS_URL"
        git remote set-url --push origin "$SSH_URL"
        git fetch origin "$BRANCH" --quiet 2>/dev/null || true
        # Resume an interrupted baseline: HEAD still parked on origin/$BRANCH
        # with an uncommitted local overlay → apply the overlay commit now.
        if [ "$(git rev-parse HEAD 2>/dev/null || true)" = "$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo none)" ] \
           && [ -n "$(tracked_dirty)" ]; then
            log "resuming interrupted baseline (HEAD == origin/$BRANCH with uncommitted changes)"
            finish_overlay_commit
        else
            log "already a git repo — origin ensured (fetch=$HTTPS_URL, push=$SSH_URL)"
        fi
        return 0
    fi

    log "initializing git repo on $BRANCH"
    git init -b "$BRANCH" 2>/dev/null || { git init; git checkout -b "$BRANCH"; }
    git remote add origin "$HTTPS_URL"
    git remote set-url --push origin "$SSH_URL"
    git config user.name  "$GIT_NAME"
    git config user.email "$GIT_EMAIL"

    log "fetching GitHub history (origin/$BRANCH)"
    if ! git fetch origin "$BRANCH" --quiet 2>/dev/null; then
        log "warning: could not fetch origin/$BRANCH — continuing without a remote base"
    fi

    # Baseline: check out the remote base, then lay the *local* working tree
    # over it, so local state becomes the new tip of the branch.
    #
    # Safety: only files that origin tracks are ever moved (they would block
    # `git checkout`); everything else — models/, target/, logs/, workspaces/,
    # server-config.json, keys — stays exactly where it is and is never
    # touched or deleted. tmp is cleaned up by a trap that restores first.
    if git rev-parse --verify "origin/$BRANCH" >/dev/null 2>&1; then
        local tmp
        tmp="$(mktemp -d "$(dirname "$REPO_ROOT")/.gs_tmp.XXXXXX")"
        trap 'if [ -d "$tmp" ]; then rsync -a --exclude .git --exclude origin-paths.txt "$tmp/" "$REPO_ROOT/"; fi; rm -rf "$tmp"' EXIT

        log "moving only origin-tracked files aside (models/, target/, keys stay put)"
        git ls-tree -r --name-only "origin/$BRANCH" > "$tmp/origin-paths.txt"
        local p
        while IFS= read -r p; do
            [ -n "$p" ] || continue
            if [ -e "$REPO_ROOT/$p" ]; then
                mkdir -p "$tmp/$(dirname "$p")"
                mv "$REPO_ROOT/$p" "$tmp/$p"
            fi
        done < "$tmp/origin-paths.txt"

        log "checking out origin/$BRANCH"
        git checkout -b "$BRANCH" "origin/$BRANCH" >/dev/null 2>&1

        log "laying local state over origin/$BRANCH"
        rsync -a --exclude .git --exclude origin-paths.txt "$tmp/" "$REPO_ROOT/"
    else
        git checkout -b "$BRANCH" >/dev/null 2>&1
        log "no remote base found — starting fresh branch $BRANCH"
    fi

    finish_overlay_commit

    log "init complete. Next:"
    log "  1) add ~/.ssh/ashat_github.pub as a deploy key (write access) on $HTTPS_URL"
    log "  2) ./scripts/github_sync.sh status   # confirm direction"
    log "  3) ./scripts/github_sync.sh push     # publish local state"
}

cmd_status() {
    ensure_repo
    if ! git fetch origin "$BRANCH" --quiet 2>/dev/null; then
        die "fetch failed — is origin reachable? (https)"
    fi
    refresh_divergence
    local out_commits in_commits out_files in_files
    out_commits="$(git log --oneline "origin/$BRANCH..HEAD" 2>/dev/null || true)"
    in_commits="$(git log --oneline "HEAD..origin/$BRANCH" 2>/dev/null || true)"
    out_files="$(git diff --name-status "origin/$BRANCH..HEAD" 2>/dev/null || true)"
    in_files="$(git diff --name-status "HEAD..origin/$BRANCH" 2>/dev/null || true)"
    export GS_OUTGOING_COMMITS="$out_commits" GS_INCOMING_COMMITS="$in_commits"
    export GS_OUTGOING_FILES="$out_files" GS_INCOMING_FILES="$in_files"
    set_common_report

    log "direction: $DIRECTION (local $AHEAD ahead / $BEHIND behind origin/$BRANCH)"
    log "local:  ${LOCAL_SHA:0:10}   remote: ${REMOTE_SHA:0:10}"
    if [ -n "$out_commits" ]; then
        log "outgoing commits (would push):"
        printf '%s\n' "$out_commits" >&2
        log "outgoing files:"
        printf '%s\n' "$out_files" >&2
    fi
    if [ -n "$in_commits" ]; then
        log "incoming commits (would pull):"
        printf '%s\n' "$in_commits" >&2
        log "incoming files:"
        printf '%s\n' "$in_files" >&2
    fi
    if [ "$JSON" = 1 ]; then
        export GS_MESSAGE="ok"
        emit_json 1
    fi
}

cmd_pull() {
    ensure_repo
    git fetch origin "$BRANCH" --quiet 2>/dev/null || die "fetch failed"
    refresh_divergence
    set_common_report

    if [ "$BEHIND" -eq 0 ] && [ "$FORCE" -eq 0 ]; then
        log "already up to date with origin/$BRANCH"
        [ "$JSON" = 1 ] && { export GS_MESSAGE="already up to date" GS_PEERS=""; emit_json 1; }
        return 0
    fi
    if [ "$AHEAD" -gt 0 ]; then
        die "diverged: local is $AHEAD ahead and remote is $BEHIND behind — pull is fast-forward only. Resolve manually (merge/rebase) first."
    fi

    local in_commits in_files
    in_commits="$(git log --oneline "HEAD..origin/$BRANCH")"
    in_files="$(git diff --name-status "HEAD..origin/$BRANCH")"
    export GS_INCOMING_COMMITS="$in_commits" GS_INCOMING_FILES="$in_files"

    log "pull plan — applying $BEHIND commit(s) from origin/$BRANCH (ff-only):"
    printf '%s\n' "$in_commits" >&2
    log "files:"
    printf '%s\n' "$in_files" >&2

    local dirty
    dirty="$(tracked_dirty)"
    [ -n "$dirty" ] && die "working tree has uncommitted tracked changes: $(printf '%s' "$dirty" | head -1) — commit or stash first"
    guard_secrets || die "pull aborted: protected file is tracked"
    assert_ignore_effective || die "pull aborted: .gitignore is not effective"

    confirm

    if [ "$BEHIND" -gt 0 ]; then
        log "merging (fast-forward)"
        git merge --ff-only "origin/$BRANCH" || die "fast-forward merge failed"
    else
        log "already on origin/$BRANCH; forcing verification and propagation"
    fi

    local rollback_done=0
    rollback() {
        if [ "$rollback_done" = 0 ]; then
            log "verification failed — rolling back to pre-merge state (@{1})"
            git reset --hard '@{1}' 2>/dev/null || git reset --hard 'HEAD@{1}' 2>/dev/null || true
            rollback_done=1
        fi
    }

    log "verifying: fmt + tests + release build"
    if ! cargo fmt --all --check >/dev/null 2>&1; then
        rollback; die "fmt check failed — pulled code does not pass cargo fmt"
    fi
    if ! cargo test --quiet >/dev/null 2>&1; then
        rollback; die "tests failed — pulled code rolled back"
    fi
    if ! cargo build --release >/dev/null 2>&1; then
        rollback; die "release build failed — pulled code rolled back"
    fi
    log "verification passed — new binary at target/release/ashat-master-coding-agent"

    # Propagate the new build to enabled peers (same seed path as
    # POST /api/admin/update).
    local peers peer_results="" any_failed=0 host install port
    peers="$(python3 -c '
import json
d = json.load(open("server-config.json"))
for p in d.get("update", {}).get("peers", []):
    if p.get("enabled"):
        print(f"{p[\"host\"]} {p[\"install\"]} {p[\"port\"]}")
' 2>/dev/null || true)"
    if [ -z "$peers" ]; then
        log "no enabled peers configured — skipping propagation"
    else
        while read -r host install port; do
            log "propagating to $host (port $port)"
            if ./scripts/seed_slave.sh "$host" "$install" "$port" >/dev/null 2>&1; then
                peer_results="$peer_results${peer_results:+,}$host:ok"
            else
                peer_results="$peer_results${peer_results:+,}$host:failed"
                any_failed=1
            fi
        done <<< "$peers"
        log "propagation: $peer_results"
    fi
    local restart_required=1
    if [ "$RESTART" = 1 ]; then
        log "restarting ashat-neural-host.service"
        if sudo systemctl restart ashat-neural-host.service; then
            restart_required=0
        else
            log "restart failed — run manually"
        fi
    fi
    export GS_PEERS="$peer_results" GS_RESTART_REQUIRED=$restart_required
    export GS_MESSAGE="merged+verified+rebuilt (peers: ${peer_results:-none})"

    if [ "$JSON" = 1 ]; then
        emit_json 1
    fi
}

cmd_push() {
    ensure_repo
    [ -f "$SSH_KEY" ] || die "deploy key $SSH_KEY missing — add ~/.ssh/ashat_github.pub to the repo as a deploy key (write access) first"
    git fetch origin "$BRANCH" --quiet 2>/dev/null || die "fetch failed"
    refresh_divergence
    set_common_report

    if [ "$AHEAD" -eq 0 ]; then
        log "nothing to push — in sync with origin/$BRANCH"
        [ "$JSON" = 1 ] && { export GS_MESSAGE="in sync"; emit_json 1; }
        return 0
    fi
    if [ "$BEHIND" -gt 0 ]; then
        die "remote is $BEHIND ahead — pull first (or push would be rejected anyway)"
    fi

    local out_commits out_files
    out_commits="$(git log --oneline "origin/$BRANCH..HEAD")"
    out_files="$(git diff --name-status "origin/$BRANCH..HEAD")"
    export GS_OUTGOING_COMMITS="$out_commits" GS_OUTGOING_FILES="$out_files"

    log "push plan — publishing $AHEAD commit(s) to origin/$BRANCH:"
    printf '%s\n' "$out_commits" >&2
    log "files:"
    printf '%s\n' "$out_files" >&2

    local dirty
    dirty="$(tracked_dirty)"
    [ -n "$dirty" ] && die "working tree has uncommitted tracked changes: $(printf '%s' "$dirty" | head -1) — commit or stash first"
    guard_secrets || die "push aborted: protected file is tracked"
    assert_ignore_effective || die "push aborted: .gitignore is not effective"

    confirm

    log "pushing (deploy key $SSH_KEY)"
    git push origin "$BRANCH"

    log "pushed. GitHub is now in sync with Omega."
    [ "$JSON" = 1 ] && { export GS_MESSAGE="pushed $AHEAD commit(s)"; emit_json 1; }
}

for arg in "$@"; do
    case "$arg" in
        --json) JSON=1 ;;
        --yes)  YES=1 ;;
        --restart-service) RESTART=1 ;;
        --force) FORCE=1 ;;
        init|status|pull|push) CMD="$arg" ;;
        *) die "unknown argument: $arg" ;;
    esac
done

[ -n "$CMD" ] || die "usage: $0 {init|status|pull|push} [--json] [--yes] [--restart-service]"

case "$CMD" in
    init)   cmd_init ;;
    status) cmd_status ;;
    pull)   cmd_pull ;;
    push)   cmd_push ;;
esac
