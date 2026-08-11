#!/usr/bin/env bash
# Warn — never block — when a new git branch is created while open PRs still
# carry unresolved review threads.
#
# Why warn rather than block: CodeRabbit and Codex both auto-review every PR in
# this repo, so unresolved threads are the normal steady state rather than an
# exception. A hard gate would stop work on a nitpick. This makes the debt
# visible at the moment you would otherwise walk away from it.
#
# Every failure path exits 0 silently. A hook that breaks branch creation
# because gh is logged out is worse than the problem it solves.

set -uo pipefail

payload=$(cat 2>/dev/null) || exit 0
cmd=$(printf '%s' "$payload" | jq -r '.tool_input.command // ""' 2>/dev/null) || exit 0
[ -n "$cmd" ] || exit 0

# Branch-creating forms only. Plain `git checkout <branch>` and `git switch
# <branch>` move between existing branches and are deliberately not gated.
printf '%s' "$cmd" | grep -qE '(^|[;&|]|&&|\|\|)[[:space:]]*git[[:space:]]+((checkout|switch)[[:space:]]+(-[bBcC])|branch[[:space:]]+[^-[:space:]])' || exit 0

command -v gh >/dev/null 2>&1 || exit 0
command -v jq >/dev/null 2>&1 || exit 0
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null) || exit 0
[ -n "$repo" ] || exit 0
owner=${repo%%/*}
name=${repo##*/}

raw=$(gh api graphql -f query='
query($owner:String!,$repo:String!){
  repository(owner:$owner,name:$repo){
    pullRequests(states:OPEN,first:30){
      nodes{
        number
        title
        isDraft
        author{ login }
        reviewThreads(first:100){
          nodes{ isResolved comments(first:1){ nodes{ author{login} path } } }
        }
      }
    }
  }
}' -F owner="$owner" -F repo="$name" 2>/dev/null) || exit 0

summary=$(printf '%s' "$raw" | jq -r '
  [ .data.repository.pullRequests.nodes[]
    | { number, title, author: .author.login,
        unresolved: [ .reviewThreads.nodes[] | select(.isResolved == false) ] }
    | select(.unresolved | length > 0) ]
  | if length == 0 then empty
    else
      "\(. | map(.unresolved | length) | add) unresolved review thread(s) across \(length) open PR(s):\n"
      + ( map("  #\(.number) \(.title[0:58]) — \(.unresolved | length) unresolved"
              + " [" + ( [ .unresolved[].comments.nodes[0].author.login ] | unique | join(", ") ) + "]"
            ) | join("\n") )
    end' 2>/dev/null) || exit 0

[ -n "$summary" ] || exit 0

export SUMMARY="$summary"
jq -n --arg s "$SUMMARY" '{
  systemMessage: ("Branching with review debt open.\n" + $s + "\nNot blocked — but resolve or reply before this grows."),
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    additionalContext: ("Unresolved PR review threads exist in this repo:\n" + $s + "\nThe user has a standing rule that review comments should be resolved before starting new work on a branch. Surface this and offer to triage the threads before continuing.")
  }
}'
exit 0
