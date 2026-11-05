#!/usr/bin/env bash
set -euo pipefail

lock_files=(
	build-project/pylock.toml
	pylock.dev.toml
	tools/pylock.update-rtd-redirects.toml
)

if git diff --quiet -- "${lock_files[@]}"; then
	echo "Lock files are already up to date."
	exit 0
fi

branch="update-build-project-lock"
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git switch -c "${branch}"
git add "${lock_files[@]}"
git commit -m "Update lock files"
git push --force origin "${branch}"

if gh pr view "${branch}" --json state --jq .state 2>/dev/null | grep -q OPEN; then
	echo "An open pull request already exists; its branch was updated."
else
	gh pr create \
		--base "${GITHUB_REF:?GITHUB_REF must name the target branch}" \
		--head "${branch}" \
		--title "Update lock files" \
		--body 'Update the build-project, development and redirect-script locks with pip and Nab.'
fi
