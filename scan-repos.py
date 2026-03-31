import os
import requests
import base64
import json
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

# -----------------------------
# Load env
# -----------------------------
load_dotenv(override=True)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
ORG_NAME = os.getenv("ORG_NAME")
PACKAGE_NAME = os.getenv("PACKAGE_NAME")

REPO_PREFIXES = [
    p.strip() for p in os.getenv("REPO_PREFIXES", "").split(",") if p.strip()
]

BLACKLISTED_VERSIONS = set(
    v.strip() for v in os.getenv("BLACKLISTED_VERSIONS", "").split(",") if v.strip()
)

MAX_WORKERS = int(os.getenv("MAX_WORKERS", "10"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

BASE_URL = "https://api.github.com"

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json"
}

# -----------------------------
# Validation
# -----------------------------
def validate_config():
    logger.info("Validating configuration...")

    if not GITHUB_TOKEN:
        raise ValueError("GITHUB_TOKEN is required")
    if not ORG_NAME:
        raise ValueError("ORG_NAME is required")
    if not PACKAGE_NAME:
        raise ValueError("PACKAGE_NAME is required")

    logger.info(f"Org: {ORG_NAME}")
    logger.info(f"Package: {PACKAGE_NAME}")
    logger.info(f"Prefixes: {REPO_PREFIXES or 'None'}")
    logger.info(f"Blacklisted versions: {BLACKLISTED_VERSIONS or 'None'}")
    logger.info(f"Max workers: {MAX_WORKERS}")


def validate_github_token():
    logger.info("Validating GitHub token...")

    resp = requests.get(f"{BASE_URL}/user", headers=HEADERS)

    if resp.status_code == 200:
        logger.info(f"✅ Authenticated as: {resp.json().get('login')}")
        return True

    logger.error(f"❌ Token validation failed: {resp.status_code} - {resp.text}")
    return False


# -----------------------------
# API helper
# -----------------------------
def github_api(url, params=None):
    while True:
        resp = requests.get(url, headers=HEADERS, params=params)

        if resp.status_code == 200:
            return resp.json()

        elif resp.status_code == 403 and "rate limit" in resp.text.lower():
            logger.warning("Rate limited. Sleeping 60s...")
            time.sleep(60)

        else:
            logger.error(f"GitHub API error {resp.status_code}")
            logger.error(f"URL: {url}")
            logger.error(f"Response: {resp.text}")
            return None


# -----------------------------
# Search API (prefix-based)
# -----------------------------
def search_repos_by_prefix(org, prefixes):
    all_repos = {}

    logger.info("Using GitHub Search API")

    for prefix in prefixes:
        logger.info(f"Searching prefix: {prefix}")

        page = 1
        while True:
            url = f"{BASE_URL}/search/repositories"
            query = f"{prefix} in:name org:{org}"

            logger.debug(f"Search query: {query}")

            data = github_api(url, params={
                "q": query,
                "per_page": 100,
                "page": page
            })

            if not data or "items" not in data:
                logger.warning(f"No data for prefix: {prefix}")
                logger.debug(f"API response: {data}")
                break

            total_count = data.get("total_count", 0)
            items = data["items"]
            logger.info(f"Prefix '{prefix}' → page {page}, total_count: {total_count}, items: {len(items)}")

            if not items:
                break

            for repo in items:
                name = repo["name"]

                if name.lower().startswith(prefix.lower()):
                    all_repos[repo["full_name"]] = repo
                else:
                    logger.debug(f"Filtered out: {name}")

            if len(items) < 100:
                break

            if page >= 10:
                logger.warning(f"⚠️ Hit 1000 result limit for prefix '{prefix}'")
                break

            page += 1

    return list(all_repos.values())


# -----------------------------
# Full org scan (fallback)
# -----------------------------
def fetch_all_repos(org):
    repos = []
    page = 1

    logger.info("Fetching all repos (no prefix mode)")

    while True:
        url = f"{BASE_URL}/orgs/{org}/repos"
        data = github_api(url, params={"per_page": 100, "page": page})

        if data is None:
            break

        if len(data) == 0:
            logger.info(f"End of pagination at page {page}")
            break

        logger.info(f"Page {page} → {len(data)} repos")

        repos.extend(data)

        if len(data) < 100:
            break

        page += 1

    return repos


# -----------------------------
# File fetch
# -----------------------------
def get_file_content(repo, path):
    url = f"{BASE_URL}/repos/{repo}/contents/{path}"
    data = github_api(url)

    if not data or "content" not in data:
        logger.debug(f"{repo}: {path} not found")
        return None

    try:
        return base64.b64decode(data["content"]).decode("utf-8")
    except Exception as e:
        logger.error(f"{repo}: decode error {path} - {e}")
        return None


# -----------------------------
# Tree API - find files at any depth
# -----------------------------
def get_repo_tree(repo, branch="HEAD"):
    url = f"{BASE_URL}/repos/{repo}/git/trees/{branch}?recursive=1"
    data = github_api(url)

    if not data or "tree" not in data:
        logger.debug(f"{repo}: could not fetch tree")
        return []

    return data["tree"]


def find_package_files(repo):
    tree = get_repo_tree(repo)

    package_jsons = []
    package_locks = []

    for item in tree:
        if item["type"] != "blob":
            continue

        path = item["path"]

        if path.endswith("/package.json") or path == "package.json":
            package_jsons.append(path)
        elif path.endswith("/package-lock.json") or path == "package-lock.json":
            package_locks.append(path)

    logger.debug(f"{repo}: found {len(package_jsons)} package.json, {len(package_locks)} package-lock.json")

    return package_jsons, package_locks


# -----------------------------
# package.json
# -----------------------------
def check_package_json(repo, path):
    content = get_file_content(repo, path)
    if not content:
        return None

    try:
        pkg = json.loads(content)
    except:
        return None

    for section in ["dependencies", "devDependencies"]:
        if section in pkg and PACKAGE_NAME in pkg[section]:
            logger.info(f"{repo}: found in {path}")
            return pkg[section][PACKAGE_NAME]

    return None


# -----------------------------
# package-lock.json
# -----------------------------
def check_package_lock(repo, path):
    content = get_file_content(repo, path)
    if not content:
        return None

    try:
        lock = json.loads(content)
    except:
        return None

    if "packages" in lock:
        direct_key = f"node_modules/{PACKAGE_NAME}"
        if direct_key in lock["packages"]:
            logger.info(f"{repo}: found in {path} (direct)")
            return lock["packages"][direct_key].get("version")
        
        for k, v in lock["packages"].items():
            if k.endswith(f"node_modules/{PACKAGE_NAME}"):
                logger.info(f"{repo}: found in {path} (nested: {k})")
                return v.get("version")

    if "dependencies" in lock and PACKAGE_NAME in lock["dependencies"]:
        return lock["dependencies"][PACKAGE_NAME].get("version")

    return None


# -----------------------------
# Scan repo
# -----------------------------
def scan_repo(repo):
    full_name = repo["full_name"]

    logger.info(f"Scanning {full_name}")

    package_jsons, package_locks = find_package_files(full_name)

    if not package_jsons and not package_locks:
        logger.debug(f"{full_name}: no package files found")
        return None

    matches = []

    for pkg_path in package_jsons:
        declared = check_package_json(full_name, pkg_path)
        if declared:
            lock_path = pkg_path.replace("package.json", "package-lock.json")
            resolved = None
            if lock_path in package_locks:
                resolved = check_package_lock(full_name, lock_path)

            matches.append({
                "path": pkg_path,
                "declared_version": declared,
                "resolved_version": resolved
            })

    for lock_path in package_locks:
        pkg_path = lock_path.replace("package-lock.json", "package.json")
        if pkg_path in package_jsons:
            continue

        resolved = check_package_lock(full_name, lock_path)
        if resolved:
            matches.append({
                "path": lock_path,
                "declared_version": None,
                "resolved_version": resolved
            })

    if matches:
        return {
            "repo": full_name,
            "matches": matches
        }

    return None


# -----------------------------
# Report generation
# -----------------------------
def generate_report(results):
    declared_versions = set()
    resolved_versions = set()
    blacklisted_found = []

    for result in results:
        repo = result["repo"]
        for match in result["matches"]:
            declared = match.get("declared_version")
            resolved = match.get("resolved_version")

            if declared:
                declared_versions.add(declared)
            if resolved:
                resolved_versions.add(resolved)

            if BLACKLISTED_VERSIONS:
                if resolved and resolved in BLACKLISTED_VERSIONS:
                    blacklisted_found.append({
                        "repo": repo,
                        "path": match["path"],
                        "version": resolved,
                        "type": "resolved"
                    })
                if declared:
                    clean_declared = declared.lstrip("^~>=<")
                    if clean_declared in BLACKLISTED_VERSIONS:
                        blacklisted_found.append({
                            "repo": repo,
                            "path": match["path"],
                            "version": declared,
                            "type": "declared"
                        })

    print("\n" + "=" * 60)
    print("SUMMARY REPORT")
    print("=" * 60)

    print(f"\n📦 Package: {PACKAGE_NAME}")
    print(f"📊 Repos with matches: {len(results)}")

    print(f"\n📋 Unique declared versions ({len(declared_versions)}):")
    for v in sorted(declared_versions):
        print(f"   • {v}")

    print(f"\n🔒 Unique resolved versions ({len(resolved_versions)}):")
    for v in sorted(resolved_versions):
        print(f"   • {v}")

    if BLACKLISTED_VERSIONS:
        print(f"\n🚫 Blacklisted versions to check: {', '.join(sorted(BLACKLISTED_VERSIONS))}")
        if blacklisted_found:
            print(f"\n⚠️  BLACKLISTED VERSIONS DETECTED ({len(blacklisted_found)}):")
            for item in blacklisted_found:
                print(f"   ❌ {item['repo']}")
                print(f"      Path: {item['path']}")
                print(f"      Version: {item['version']} ({item['type']})")
        else:
            print("\n✅ No blacklisted versions detected!")
    else:
        print("\n⚠️  No BLACKLISTED_VERSIONS configured")

    print("\n" + "=" * 60)

    return blacklisted_found


# -----------------------------
# Main
# -----------------------------
def main():
    validate_config()

    if not validate_github_token():
        return

    # Choose strategy
    if REPO_PREFIXES:
        repos = search_repos_by_prefix(ORG_NAME, REPO_PREFIXES)
    else:
        repos = fetch_all_repos(ORG_NAME)

    logger.info(f"Repos to scan: {len(repos)}")

    if not repos:
        logger.warning("⚠️ No repositories found!")
        return

    results = []

    logger.info(f"Scanning {len(repos)} repos with {MAX_WORKERS} workers...")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_repo = {executor.submit(scan_repo, repo): repo for repo in repos}

        for future in as_completed(future_to_repo):
            repo = future_to_repo[future]
            try:
                result = future.result()
                if result:
                    results.append(result)
            except Exception as e:
                logger.error(f"Error scanning {repo.get('full_name', repo)}: {e}")

    logger.info(f"Total matches: {len(results)}")

    print("\n=== RESULTS ===")
    for r in results:
        repo = r["repo"]
        for match in r["matches"]:
            path = match["path"]
            declared = match.get("declared_version") or "-"
            resolved = match.get("resolved_version") or "-"
            print(f"{repo} ({path}) - declared: {declared}, resolved: {resolved}")

    blacklisted = generate_report(results)

    if blacklisted:
        exit(1)


if __name__ == "__main__":
    main()
