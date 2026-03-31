# gh-repo-scan

Scans GitHub organization repositories to find usage of a specific npm package and detect blacklisted versions.

## Setup

```bash
cp .env.example .env
# Edit .env with your values
uv sync
uv run python scan-repos.py
```

## GitHub Token Permissions

### Classic PAT

Required scope:
- **`repo`** — Full control of private repositories (needed to read file contents from private repos)

### Fine-Grained PAT

- **Repository access**: All repositories (or select specific ones)
- **Permissions**:
  - Contents: **Read-only**
  - Metadata: **Read-only**

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_TOKEN` | Yes | GitHub PAT with repo access |
| `ORG_NAME` | Yes | GitHub organization to scan |
| `PACKAGE_NAME` | Yes | npm package name to search for |
| `REPO_PREFIXES` | No | Comma-separated prefixes to filter repos |
| `BLACKLISTED_VERSIONS` | No | Comma-separated versions to flag |
| `MAX_WORKERS` | No | Parallel scan threads (default: 10) |
| `LOG_LEVEL` | No | Logging level (default: INFO) |
