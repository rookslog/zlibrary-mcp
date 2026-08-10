# Version Control & Git Workflow Guide

<!-- Last Verified: 2026-02-01 -->

## Overview

This document defines the Git workflow, branching strategy, commit conventions, and version control best practices for the Z-Library MCP project.

> **For external contributors:** [CONTRIBUTING.md](../CONTRIBUTING.md) at the repo root
> is the authoritative guide. This document is the internal, more detailed reference.

## Branch Strategy

**Trunk-based on `master`.** There is a single long-lived branch — `master` — and
short-lived topic branches that branch from it and merge back into it via PR. (An older
revision of this document described a `master` + `development` gitflow model; no
`development` branch exists.)

### `master` (Trunk)
- Production-ready code only
- All commits must pass CI/CD
- Changes land via PR
- Release tags (`vX.Y.Z`) are cut from here

### Topic Branches

All topic branches branch from `master`, merge back to `master` via PR, and are
deleted after merge.

#### Feature Branches: `feature/*`
**Purpose**: Develop new features
**Naming**: `feature/descriptive-name` or `feature/ISSUE-###-description`
**Examples**:
- `feature/rag-robustness-enhancement`
- `feature/fuzzy-search`
- `feature/SRCH-001-advanced-filters`

#### Bug Fix Branches: `fix/*`
**Purpose**: Fix non-critical bugs
**Naming**: `fix/descriptive-name` or `fix/ISSUE-###-description`
**Examples**:
- `fix/venv-manager-warnings`
- `fix/ISSUE-002-test-failures`

#### Hotfix Branches: `hotfix/*`
**Purpose**: Emergency fixes for production issues
**Naming**: `hotfix/critical-issue-description`
**Examples**:
- `hotfix/authentication-failure`
- `hotfix/memory-leak`

Hotfixes additionally create a git tag for the version bump after merging.

#### Documentation Branches: `docs/*`
**Purpose**: Documentation-only changes
**Naming**: `docs/what-is-documented`
**Examples**:
- `docs/api-reference`
- `docs/contributing-guide`

## Commit Convention

We follow [Conventional Commits](https://www.conventionalcommits.org/) specification.

### Format

```
<type>(<scope>): <subject>

<body>

<footer>
```

### Commit Types

| Type | Description | Example |
|------|-------------|---------|
| `feat` | New feature | `feat(search): add fuzzy matching support` |
| `fix` | Bug fix | `fix(venv): handle undefined config values` |
| `docs` | Documentation only | `docs(readme): update setup instructions` |
| `style` | Code style changes (formatting) | `style: apply prettier formatting` |
| `refactor` | Code refactoring | `refactor(bridge): extract retry logic` |
| `perf` | Performance improvements | `perf(search): implement result caching` |
| `test` | Adding/updating tests | `test(api): add error handling tests` |
| `chore` | Maintenance tasks | `chore(deps): update dependencies` |
| `ci` | CI/CD changes | `ci: add automated testing workflow` |
| `build` | Build system changes | `build: update typescript config` |
| `revert` | Revert previous commit | `revert: revert "feat: add feature"` |

### Scope

The scope should be the area of the codebase affected:
- `search` - Search functionality
- `download` - Download operations
- `rag` - RAG processing
- `api` - API/MCP interface
- `bridge` - Python bridge
- `venv` - Virtual environment
- `tests` - Test suite
- `docs` - Documentation
- `deps` - Dependencies
- `zlib` - Z-Library integration

### Subject

- Use imperative mood: "add" not "added" or "adds"
- Don't capitalize first letter
- No period at the end
- Maximum 50 characters

### Body (Optional but Recommended)

- Explain **why** the change was made
- Wrap at 72 characters
- Can include multiple paragraphs

### Footer (Optional)

- Reference issues: `Closes #123`, `Fixes #456`, `Relates to #789`
- Breaking changes: `BREAKING CHANGE: description`

### Examples

#### Good Commits

```
feat(search): implement fuzzy matching with Levenshtein distance

Add fuzzy search capability to improve user experience when searching
with typos or approximate titles. Uses fast-levenshtein library with
configurable threshold.

Closes #42
```

```
fix(venv): add null check before trimming config path

Prevents "Cannot read properties of undefined" error when venv config
is empty or malformed. Adds proper error handling with descriptive
message.

Fixes ISSUE-002
```

```
docs(claude): add comprehensive version control guide

Create VERSION_CONTROL.md with Git workflows, branching strategy,
and commit conventions to improve developer experience and maintain
code quality.
```

#### Bad Commits

```
❌ Updated stuff
❌ Fixed bug
❌ WIP
❌ asdf
❌ Fixed the thing that was broken yesterday
```

## Pull Request Process

### Before Creating a PR

1. **Ensure branch is up to date**:
   ```bash
   git checkout master
   git pull origin master
   git checkout your-feature-branch
   git merge master
   # Or: git rebase master
   ```

2. **Run quality checks**:
   ```bash
   npm run build     # TypeScript compilation
   npm test          # All tests pass
   npm run lint      # Linting passes (when configured)
   pytest            # Python tests pass
   ```

3. **Review your changes**:
   ```bash
   git diff master...your-feature-branch
   ```

### Creating the PR

1. **Title**: Use conventional commit format
   - `feat(search): add fuzzy matching`
   - `fix(venv): resolve config reading error`

2. **Description Template**:
   ```markdown
   ## Summary
   Brief description of what this PR does and why.

   ## Changes
   - List key changes made
   - Include technical details
   - Mention any breaking changes

   ## Testing
   - [ ] Unit tests added/updated
   - [ ] Integration tests pass
   - [ ] Manual testing completed

   ## Related Issues
   Closes #123
   Relates to #456

   ## Screenshots (if applicable)
   (Add screenshots for UI changes)

   ## Checklist
   - [ ] Code follows project patterns
   - [ ] Documentation updated
   - [ ] Tests added/updated
   - [ ] CHANGELOG.md updated (if needed)
   - [ ] No console.log statements
   - [ ] No commented code
   ```

3. **Labels**: Add appropriate labels
   - `bug`, `feature`, `documentation`, `enhancement`
   - `priority: high`, `priority: medium`, `priority: low`

4. **Reviewers**: Request reviews from team members

### Code Review Guidelines

#### For Authors

- Respond to all comments
- Ask for clarification if needed
- Don't take feedback personally
- Update PR based on feedback
- Re-request review after changes

#### For Reviewers

Check for:
1. **Correctness**: Does it work as intended?
2. **Testing**: Are tests adequate?
3. **Patterns**: Follows established patterns?
4. **Documentation**: Changes documented?
5. **Performance**: Any performance implications?
6. **Security**: Any security concerns?
7. **Maintainability**: Is code readable and maintainable?

**Review Responses**:
- **Approve**: Code is good to merge
- **Request Changes**: Must address before merge
- **Comment**: Suggestions but not blocking

**Response Time**: Within 24-48 hours for standard PRs, 2-4 hours for hotfixes

### Merge Strategies

#### Squash and Merge (Default)
**Use for**: Most feature branches
**Effect**: Combines all commits into one
**Benefit**: Clean main branch history

```bash
# GitHub does this automatically with "Squash and merge"
```

#### Rebase and Merge
**Use for**: Clean commit history on feature branch
**Effect**: Replays commits on top of base branch
**Benefit**: Linear history without merge commits

```bash
git checkout feature-branch
git rebase master
git push --force-with-lease
```

#### Merge Commit
**Use for**: Preserving complete history (rarely)
**Effect**: Creates explicit merge commit
**Benefit**: Shows branch topology

```bash
git checkout master
git merge --no-ff feature-branch
```

### Post-Merge

1. **Delete branch**: Both local and remote
   ```bash
   git branch -d feature-branch
   git push origin --delete feature-branch
   ```

2. **Update local repo**:
   ```bash
   git checkout master
   git pull origin master
   ```

## Release Process

### Versioning Strategy

We follow [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`

- **MAJOR**: Breaking changes
- **MINOR**: New features (backward compatible)
- **PATCH**: Bug fixes (backward compatible)

**Examples**:
- `1.0.0` → `1.0.1`: Bug fix
- `1.0.1` → `1.1.0`: New feature
- `1.1.0` → `2.0.0`: Breaking change

### Release Workflow

1. **Prepare release branch**:
   ```bash
   git checkout master
   git pull origin master
   git checkout -b release/v1.2.0
   ```

2. **Update version**:
   ```bash
   npm version minor  # or major/patch
   # Updates package.json and creates git tag
   ```

3. **Update CHANGELOG.md**:
   ```markdown
   ## [1.2.0] - 2025-10-15

   ### Added
   - Fuzzy search capability
   - Download queue management

   ### Fixed
   - Venv manager test warnings
   - Memory leak in long sessions

   ### Changed
   - Improved error messages
   ```

4. **Merge to master**:
   ```bash
   git checkout master
   git merge release/v1.2.0
   git push origin master
   git push origin v1.2.0  # Push tag
   ```

5. **Create GitHub Release**:
   - Go to Releases → Draft new release
   - Select tag v1.2.0
   - Title: "v1.2.0 - Feature Description"
   - Description: Copy from CHANGELOG.md
   - Attach compiled binaries if applicable

## Git Hooks

Git hooks automate quality checks. We use [Husky](https://typicode.github.io/husky/) for management.

### Setup Husky

```bash
npm install --save-dev husky
npx husky install
npm pkg set scripts.prepare="husky install"
```

### Pre-commit Hook

Runs before commit is created. Checks code quality.

```bash
#!/usr/bin/env sh
. "$(dirname -- "$0")/_/husky.sh"

echo "🔍 Running pre-commit checks..."

# Lint staged files
npm run lint-staged

# TypeScript compilation
npm run build || {
  echo "❌ TypeScript compilation failed"
  exit 1
}

echo "✅ Pre-commit checks passed"
```

### Commit-msg Hook

Validates commit message format.

```bash
#!/usr/bin/env sh
. "$(dirname -- "$0")/_/husky.sh"

# Validate conventional commit format
npx commitlint --edit $1
```

### Pre-push Hook

Runs tests before pushing.

```bash
#!/usr/bin/env sh
. "$(dirname -- "$0")/_/husky.sh"

echo "🧪 Running tests before push..."

# Run all tests
npm test || {
  echo "❌ Tests failed. Fix before pushing."
  exit 1
}

echo "✅ All tests passed"
```

## Common Git Workflows

### Starting a New Feature

```bash
# 1. Update master branch
git checkout master
git pull origin master

# 2. Create feature branch
git checkout -b feature/my-awesome-feature

# 3. Make changes and commit
git add .
git commit -m "feat(scope): add awesome feature"

# 4. Push to remote
git push -u origin feature/my-awesome-feature

# 5. Create PR on GitHub
```

### Fixing a Bug

```bash
# 1. Create fix branch
git checkout master
git pull origin master
git checkout -b fix/bug-description

# 2. Fix the bug and test
# ... make changes ...
npm test

# 3. Commit and push
git add .
git commit -m "fix(scope): resolve bug description"
git push -u origin fix/bug-description

# 4. Create PR
```

### Emergency Hotfix

```bash
# 1. Branch from master
git checkout master
git pull origin master
git checkout -b hotfix/critical-issue

# 2. Fix and test thoroughly
# ... make changes ...
npm test

# 3. Commit
git add .
git commit -m "fix(scope): resolve critical production issue"

# 4. Merge to master
git checkout master
git merge hotfix/critical-issue
git tag -a v1.0.1 -m "Hotfix: critical issue"
git push origin master --tags

# 5. Delete hotfix branch
git branch -d hotfix/critical-issue
git push origin --delete hotfix/critical-issue
```

### Updating Feature Branch from Development

```bash
# Option 1: Merge (preserves history)
git checkout feature/my-feature
git merge master

# Option 2: Rebase (cleaner history)
git checkout feature/my-feature
git rebase master
git push --force-with-lease  # If already pushed
```

### Resolving Merge Conflicts

```bash
# 1. Attempt merge/rebase
git merge master
# or
git rebase master

# 2. Git shows conflicts
# CONFLICT (content): Merge conflict in src/file.ts

# 3. Open conflicted files and resolve
# Look for conflict markers: <<<<<<<, =======, >>>>>>>

# 4. Mark as resolved
git add src/file.ts

# 5. Complete merge/rebase
git commit  # For merge
# or
git rebase --continue  # For rebase

# 6. Push changes
git push
```

### Undoing Changes

```bash
# Undo uncommitted changes
git restore file.ts

# Undo last commit (keep changes)
git reset --soft HEAD~1

# Undo last commit (discard changes)
git reset --hard HEAD~1

# Revert a pushed commit (creates new commit)
git revert <commit-hash>

# Amend last commit message
git commit --amend -m "new message"
```

## GitHub Integration

### GitHub MCP Alternative: GitHub CLI

**Note**: GitHub MCP server is not available for this project. Use the GitHub CLI (`gh`) as the primary alternative for automation.

#### Installation
```bash
# macOS
brew install gh

# Ubuntu/Debian
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
sudo apt update
sudo apt install gh

# Authenticate
gh auth login
```

#### Common Operations
```bash
# Create PR
gh pr create --title "feat: add feature" --body "Description"

# List PRs
gh pr list

# Review PR
gh pr view 123
gh pr review 123 --approve
gh pr review 123 --request-changes --body "Please fix X"

# Create issue
gh issue create --title "Bug: description" --label bug

# Work with releases
gh release create v1.0.0 --notes "Release notes"

# Workflow operations
gh workflow list
gh workflow run ci.yml
gh workflow view

# Repository operations
gh repo clone rookslog/zlibrary-mcp
gh repo view --web  # Open in browser
```

#### Alternative: Git Commands
For version control operations without automation:
```bash
# Standard git operations
git push origin feature-branch
git fetch --all
git merge origin/master

# Then use GitHub web interface for:
# - Creating PRs
# - Managing issues
# - Reviewing code
# - Managing releases
```

### Branch Protection Rules

Configure for `master`:

1. **Required Reviews**: At least 1 approval
2. **Required Status Checks**:
   - CI tests pass
   - Linting passes
   - Build succeeds
3. **Require Branches Up to Date**: Yes
4. **Restrict Pushes**: Require PR
5. **Restrict Force Pushes**: Enabled
6. **Restrict Deletions**: Enabled

### Issue Linking

Link commits and PRs to issues:

```bash
# In commit messages
git commit -m "fix(venv): resolve config error

Fixes #42"

# In PR description
Closes #123
Fixes #456
Relates to #789
```

GitHub automatically:
- Links commit/PR to issue
- Closes issue when PR merges (with Closes/Fixes)

### GitHub Actions Integration

Workflows triggered on:
- **Push to any branch**: Run tests, linting
- **Pull request**: Full CI suite
- **Push to master**: Deploy to production
- **Tag creation**: Create release

## Best Practices

### Do's

✅ **Atomic Commits**: One logical change per commit
✅ **Meaningful Messages**: Explain why, not just what
✅ **Small PRs**: Easier to review (aim for <400 lines)
✅ **Test Before Push**: All tests pass locally
✅ **Update Documentation**: Keep docs in sync with code
✅ **Review Your Own PR**: Check diff before requesting review
✅ **Respond to Feedback**: Address all review comments
✅ **Keep Branches Short-Lived**: Merge within days, not weeks

### Don'ts

❌ **Don't Force Push to Shared Branches**: Especially master
❌ **Don't Commit Secrets**: API keys, passwords, tokens
❌ **Don't Commit Large Files**: Use Git LFS if needed
❌ **Don't Leave WIP Commits**: Clean up before pushing
❌ **Don't Commit Commented Code**: Delete it - it's in git history
❌ **Don't Merge Without Review**: Always get approval
❌ **Don't Mix Concerns**: Keep features, fixes, refactors separate
❌ **Don't Push Broken Code**: Tests must pass

## Git Configuration

### Recommended Git Config

```bash
# User identity
git config --global user.name "Your Name"
git config --global user.email "your.email@example.com"

# Default branch name
git config --global init.defaultBranch master

# Better diff output
git config --global diff.algorithm histogram
git config --global diff.colorMoved zebra

# Rebase by default on pull
git config --global pull.rebase true

# Prune on fetch
git config --global fetch.prune true

# Better log format
git config --global alias.lg "log --graph --pretty=format:'%Cred%h%Creset -%C(yellow)%d%Creset %s %Cgreen(%cr) %C(bold blue)<%an>%Creset' --abbrev-commit"
```

### Git Aliases

```bash
# Useful shortcuts
git config --global alias.co checkout
git config --global alias.br branch
git config --global alias.ci commit
git config --global alias.st status
git config --global alias.unstage 'reset HEAD --'
git config --global alias.last 'log -1 HEAD'
git config --global alias.visual 'log --graph --oneline --all'
```

## Troubleshooting

### Accidentally Committed to Wrong Branch

```bash
# Move commit to correct branch
git checkout correct-branch
git cherry-pick <commit-hash>

# Remove from wrong branch
git checkout wrong-branch
git reset --hard HEAD~1
```

### Forgot to Create Branch

```bash
# Create branch from current state
git checkout -b feature/my-feature

# Push to new branch
git push -u origin feature/my-feature
```

### Need to Undo Published Commit

```bash
# Use revert (creates new commit)
git revert <commit-hash>
git push origin branch-name
```

### Merge Conflict Confusion

```bash
# Abort merge and start over
git merge --abort

# Or abort rebase
git rebase --abort
```

---

*This version control guide should be treated as a living document. Update it as workflows evolve and new patterns emerge.*

**Last Updated**: 2026-02-01
**Version**: 1.1.0
**Maintained By**: Z-Library MCP Team

**Note**: A comprehensive 7-phase cleanup (Jan-Feb 2026) was completed on master branch, modernizing the codebase with EAPI transport, MCP SDK 1.25+, and decomposed Python modules. Work proceeds on short-lived topic branches from master.
