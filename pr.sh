#!/bin/bash

# create-pr.sh - Create PR from develop to main

BASE_BRANCH="main"
HEAD_BRANCH="develop"

echo "Creating Pull Request from $HEAD_BRANCH to $BASE_BRANCH"
echo ""

read -p "Enter PR title: " PR_TITLE

# If title is empty, use default
if [ -z "$PR_TITLE" ]; then
    PR_TITLE="Merge develop into main"
fi

echo ""
echo "Enter PR description (press Ctrl+D when done, or Enter for empty):"
PR_BODY=$(cat)

# Create the PR
gh pr create --base "$BASE_BRANCH" --head "$HEAD_BRANCH" --title "$PR_TITLE" --body "$PR_BODY"

echo ""
echo "✅ PR created successfully!"