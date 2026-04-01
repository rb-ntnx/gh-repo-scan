import os
import re
import requests
import base64
import json
import time
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
import nodesemver

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
# Logging setup
# -----------------------------
# Create logs directory if it doesn't exist
log_dir = Path(__file__).parent / "logs"
log_dir.mkdir(exist_ok=True)

log_file = log_dir / "scan.log"

# Configure logging with both console and file handlers
logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(LOG_LEVEL)
console_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
console_handler.setFormatter(console_formatter)

# File handler with timestamps
file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
file_handler.setLevel(LOG_LEVEL)
file_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
file_handler.setFormatter(file_formatter)

# Add handlers to logger
logger.addHandler(console_handler)
logger.addHandler(file_handler)

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
# Semver helper
# -----------------------------
def check_blacklist_risk(declared_range, blacklisted_versions):
    """
    Check if any blacklisted version satisfies the declared version range.
    Returns a list of blacklisted versions that could match.
    """
    if not declared_range or not blacklisted_versions:
        return []

    at_risk = []
    for version in blacklisted_versions:
        try:
            if nodesemver.satisfies(version, declared_range):
                at_risk.append(version)
        except Exception as e:
            logger.debug(f"Semver check failed for {version} against {declared_range}: {e}")
    
    return at_risk


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
    dockerfiles = []

    for item in tree:
        if item["type"] != "blob":
            continue

        path = item["path"]
        filename = path.split("/")[-1].lower()

        if path.endswith("/package.json") or path == "package.json":
            package_jsons.append(path)
        elif path.endswith("/package-lock.json") or path == "package-lock.json":
            package_locks.append(path)
        elif filename == "dockerfile" or filename.startswith("dockerfile."):
            dockerfiles.append(path)

    logger.debug(f"{repo}: found {len(package_jsons)} package.json, {len(package_locks)} package-lock.json, {len(dockerfiles)} Dockerfile(s)")

    return package_jsons, package_locks, dockerfiles


# -----------------------------
# package.json
# -----------------------------
def check_package_json(repo, path):
    """
    Returns a dict with declared versions from dependencies and/or devDependencies.
    Example: {"dependencies": "^2.0.0", "devDependencies": "^1.5.0"}
    Returns None if package not found in either section.
    """
    content = get_file_content(repo, path)
    if not content:
        return None

    try:
        pkg = json.loads(content)
    except:
        return None

    found = {}
    for section in ["dependencies", "devDependencies"]:
        if section in pkg and PACKAGE_NAME in pkg[section]:
            logger.info(f"{repo}: found in {path} ({section})")
            found[section] = pkg[section][PACKAGE_NAME]

    return found if found else None


# -----------------------------
# Registry detection
# -----------------------------
PUBLIC_NPM_REGISTRY_PATTERNS = [
    "registry.npmjs.org",
    "registry.yarnpkg.com",
]

def is_public_registry(resolved_url):
    """
    Check if the resolved URL points to a public npm registry.
    Returns True if it's a public registry, False otherwise.
    """
    if not resolved_url:
        return False
    
    resolved_lower = resolved_url.lower()
    for pattern in PUBLIC_NPM_REGISTRY_PATTERNS:
        if pattern in resolved_lower:
            return True
    return False


# -----------------------------
# package-lock.json
# -----------------------------
def check_package_lock(repo, path):
    """
    Returns a list of all occurrences of the package in the lockfile.
    Each occurrence is a dict: {
        "location": "node_modules/...",
        "version": "x.y.z",
        "resolved": "https://...",
        "is_public_registry": True/False
    }
    """
    content = get_file_content(repo, path)
    if not content:
        return []

    try:
        lock = json.loads(content)
    except:
        return []

    found = []

    # Lockfile v2/v3 format (packages object)
    if "packages" in lock:
        for k, v in lock["packages"].items():
            if k == f"node_modules/{PACKAGE_NAME}" or k.endswith(f"/node_modules/{PACKAGE_NAME}"):
                version = v.get("version")
                if version:
                    location_type = "direct" if k == f"node_modules/{PACKAGE_NAME}" else "nested"
                    resolved_url = v.get("resolved", "")
                    public_registry = is_public_registry(resolved_url)
                    logger.info(f"{repo}: found in {path} ({location_type}: {k})")
                    if public_registry:
                        logger.info(f"{repo}: ⚠️ uses PUBLIC registry: {resolved_url}")
                    found.append({
                        "location": k,
                        "version": version,
                        "resolved": resolved_url,
                        "is_public_registry": public_registry
                    })

    # Lockfile v1 format (dependencies object) - fallback if nothing found in packages
    if not found and "dependencies" in lock and PACKAGE_NAME in lock["dependencies"]:
        dep_info = lock["dependencies"][PACKAGE_NAME]
        version = dep_info.get("version")
        if version:
            resolved_url = dep_info.get("resolved", "")
            public_registry = is_public_registry(resolved_url)
            logger.info(f"{repo}: found in {path} (v1 format)")
            if public_registry:
                logger.info(f"{repo}: ⚠️ uses PUBLIC registry: {resolved_url}")
            found.append({
                "location": f"node_modules/{PACKAGE_NAME}",
                "version": version,
                "resolved": resolved_url,
                "is_public_registry": public_registry
            })

    return found


# -----------------------------
# Dockerfile npm detection
# -----------------------------
NPM_INSTALL_PATTERN = re.compile(
    r'\b(npm\s+(?:i|ci|install)(?:\s+[^\n&|;]*)?)',
    re.IGNORECASE
)

def is_global_install(command):
    """Check if npm command is a global install (-g or --global)."""
    parts = command.lower().split()
    return '-g' in parts or '--global' in parts


def check_dockerfile(repo, path):
    """
    Check a Dockerfile for npm install commands.
    Returns a list of npm commands found with line numbers.
    Ignores global installs (-g, --global) as they don't affect project dependencies.
    """
    content = get_file_content(repo, path)
    if not content:
        return []

    found = []
    lines = content.split('\n')
    
    for line_num, line in enumerate(lines, 1):
        # Skip comments
        stripped = line.strip()
        if stripped.startswith('#'):
            continue
        
        matches = NPM_INSTALL_PATTERN.findall(line)
        for match in matches:
            command = match.strip()
            
            # Skip global installs (npm i -g, npm install --global)
            if is_global_install(command):
                logger.debug(f"{repo}: skipping global install in {path}:{line_num} → {command}")
                continue
            
            # Classify the command type
            if 'npm ci' in command.lower() or command.lower().startswith('npm ci'):
                cmd_type = 'npm ci'
            elif 'npm i ' in command.lower() or command.lower().endswith('npm i'):
                cmd_type = 'npm i'
            else:
                cmd_type = 'npm install'
            
            found.append({
                "line": line_num,
                "command": command,
                "type": cmd_type
            })
            logger.info(f"{repo}: found '{cmd_type}' in {path}:{line_num}")

    return found


# -----------------------------
# Scan repo
# -----------------------------
def scan_repo(repo):
    full_name = repo["full_name"]

    logger.info(f"Scanning {full_name}")

    package_jsons, package_locks, dockerfiles = find_package_files(full_name)

    if not package_jsons and not package_locks and not dockerfiles:
        logger.debug(f"{full_name}: no package files or Dockerfiles found")
        return None

    matches = []
    processed_locks = set()
    dockerfile_matches = []

    # Process each package.json with its corresponding lockfile
    for pkg_path in package_jsons:
        lock_path = pkg_path.replace("package.json", "package-lock.json")
        declared_info = check_package_json(full_name, pkg_path)
        resolved_list = []
        missing_lockfile = lock_path not in package_locks
        
        if not missing_lockfile:
            resolved_list = check_package_lock(full_name, lock_path)
            processed_locks.add(lock_path)
        elif declared_info:
            logger.warning(f"{full_name}: missing lockfile for {pkg_path}")

        # Report if package is found in either package.json OR lockfile
        if declared_info or resolved_list:
            matches.append({
                "path": pkg_path,
                "declared_versions": declared_info,
                "resolved_versions": resolved_list,
                "missing_lockfile": missing_lockfile if declared_info else False,
                "is_transitive_only": not declared_info and bool(resolved_list)
            })

    # Process standalone lockfiles (no corresponding package.json)
    for lock_path in package_locks:
        if lock_path in processed_locks:
            continue

        resolved_list = check_package_lock(full_name, lock_path)
        if resolved_list:
            matches.append({
                "path": lock_path,
                "declared_versions": None,
                "resolved_versions": resolved_list,
                "missing_lockfile": False,
                "is_transitive_only": True
            })

    # Process Dockerfiles for npm install commands
    for dockerfile_path in dockerfiles:
        npm_commands = check_dockerfile(full_name, dockerfile_path)
        if npm_commands:
            dockerfile_matches.append({
                "path": dockerfile_path,
                "npm_commands": npm_commands
            })

    if matches or dockerfile_matches:
        return {
            "repo": full_name,
            "matches": matches,
            "dockerfile_matches": dockerfile_matches
        }

    return None


# -----------------------------
# Report generation
# -----------------------------
def generate_report(results):
    declared_versions = {"dependencies": set(), "devDependencies": set()}
    resolved_versions = set()
    blacklisted_found = []
    missing_lockfiles = []
    transitive_only_count = 0
    dockerfile_findings = {"npm ci": [], "npm install": [], "npm i": []}
    public_registry_findings = []
    
    # Collect all report lines for file output
    report_lines = []
    
    def print_and_log(text=""):
        """Print to console and collect for file output"""
        print(text)
        report_lines.append(text)

    for result in results:
        repo = result["repo"]
        for match in result.get("matches", []):
            declared_info = match.get("declared_versions") or {}
            resolved_list = match.get("resolved_versions", [])
            
            if match.get("is_transitive_only"):
                transitive_only_count += 1

            for dep_type, version in declared_info.items():
                declared_versions[dep_type].add(version)

            if match.get("missing_lockfile") and declared_info:
                missing_lockfiles.append({
                    "repo": repo,
                    "path": match["path"],
                    "declared_info": declared_info
                })
            
            for resolved_item in resolved_list:
                version = resolved_item["version"]
                location = resolved_item["location"]
                resolved_url = resolved_item.get("resolved", "")
                is_public = resolved_item.get("is_public_registry", False)
                resolved_versions.add(version)

                if is_public:
                    public_registry_findings.append({
                        "repo": repo,
                        "path": match["path"],
                        "location": location,
                        "version": version,
                        "resolved": resolved_url
                    })

                if BLACKLISTED_VERSIONS and version in BLACKLISTED_VERSIONS:
                    dep_types = list(declared_info.keys()) if declared_info else ["transitive"]
                    blacklisted_found.append({
                        "repo": repo,
                        "path": match["path"],
                        "location": location,
                        "version": version,
                        "type": "resolved",
                        "dep_types": dep_types
                    })

            if BLACKLISTED_VERSIONS and declared_info:
                for dep_type, declared_version in declared_info.items():
                    clean_declared = declared_version.lstrip("^~>=<")
                    if clean_declared in BLACKLISTED_VERSIONS:
                        blacklisted_found.append({
                            "repo": repo,
                            "path": match["path"],
                            "location": None,
                            "version": declared_version,
                            "type": "declared",
                            "dep_types": [dep_type]
                        })

        # Process Dockerfile findings
        for df_match in result.get("dockerfile_matches", []):
            for cmd in df_match["npm_commands"]:
                cmd_type = cmd["type"]
                dockerfile_findings[cmd_type].append({
                    "repo": repo,
                    "path": df_match["path"],
                    "line": cmd["line"],
                    "command": cmd["command"]
                })

    print_and_log("\n" + "=" * 60)
    print_and_log("SUMMARY REPORT")
    print_and_log("=" * 60)

    print_and_log(f"\n📦 Package: {PACKAGE_NAME}")
    print_and_log(f"📊 Repos with matches: {len(results)}")
    if transitive_only_count > 0:
        print_and_log(f"   ↳ {transitive_only_count} as transitive dependency only (not in package.json)")

    all_declared = declared_versions["dependencies"] | declared_versions["devDependencies"]
    print_and_log(f"\n📋 Unique declared versions ({len(all_declared)}):")
    if declared_versions["dependencies"]:
        print_and_log(f"   dependencies:")
        for v in sorted(declared_versions["dependencies"]):
            print_and_log(f"      • {v}")
    if declared_versions["devDependencies"]:
        print_and_log(f"   devDependencies:")
        for v in sorted(declared_versions["devDependencies"]):
            print_and_log(f"      • {v}")
    if not all_declared:
        print_and_log(f"   (none)")

    print_and_log(f"\n🔒 Unique resolved versions ({len(resolved_versions)}):")
    for v in sorted(resolved_versions):
        print_and_log(f"   • {v}")

    if BLACKLISTED_VERSIONS:
        print_and_log(f"\n🚫 Blacklisted versions to check: {', '.join(sorted(BLACKLISTED_VERSIONS))}")
        if blacklisted_found:
            print_and_log(f"\n⚠️  BLACKLISTED VERSIONS DETECTED ({len(blacklisted_found)}):")
            for item in blacklisted_found:
                dep_type_str = ", ".join(item.get('dep_types', ['unknown']))
                print_and_log(f"   ❌ {item['repo']}")
                print_and_log(f"      Path: {item['path']}")
                if item.get('location'):
                    print_and_log(f"      Location: {item['location']}")
                print_and_log(f"      Version: {item['version']} ({item['type']}, {dep_type_str})")
        else:
            print_and_log("\n✅ No blacklisted versions detected!")
    else:
        print_and_log("\n⚠️  No BLACKLISTED_VERSIONS configured")

    if missing_lockfiles:
        at_risk_count = 0
        print_and_log(f"\n📁 MISSING LOCKFILES ({len(missing_lockfiles)}):")
        print_and_log("   (Cannot determine resolved versions without package-lock.json)")
        
        for item in missing_lockfiles:
            declared_info = item['declared_info']
            all_at_risk = {}
            
            if BLACKLISTED_VERSIONS:
                for dep_type, version in declared_info.items():
                    at_risk = check_blacklist_risk(version, BLACKLISTED_VERSIONS)
                    if at_risk:
                        all_at_risk[dep_type] = {"version": version, "at_risk": at_risk}
            
            declared_str = ", ".join(f"{v} ({k})" for k, v in declared_info.items())
            
            if all_at_risk:
                at_risk_count += 1
                print_and_log(f"   🚨 {item['repo']}")
                print_and_log(f"      Path: {item['path']}")
                print_and_log(f"      Declared: {declared_str}")
                for dep_type, info in all_at_risk.items():
                    print_and_log(f"      ⚠️  AT RISK ({dep_type}): {info['version']} could resolve to: {', '.join(sorted(info['at_risk']))}")
            else:
                print_and_log(f"   📦 {item['repo']}")
                print_and_log(f"      Path: {item['path']}")
                print_and_log(f"      Declared: {declared_str}")
        
        if at_risk_count > 0:
            print_and_log(f"\n   🚨 {at_risk_count} repo(s) at risk of resolving to blacklisted versions!")
    else:
        print_and_log("\n✅ All matched packages have lockfiles")

    # Dockerfile npm command findings
    total_dockerfile_findings = sum(len(v) for v in dockerfile_findings.values())
    if total_dockerfile_findings > 0:
        print_and_log(f"\n🐳 DOCKERFILE NPM COMMANDS ({total_dockerfile_findings} found):")
        
        if dockerfile_findings["npm ci"]:
            print_and_log(f"\n   ✅ npm ci ({len(dockerfile_findings['npm ci'])}) - recommended for CI/CD:")
            for item in dockerfile_findings["npm ci"]:
                print_and_log(f"      • {item['repo']}")
                print_and_log(f"        {item['path']}:{item['line']} → {item['command']}")
        
        if dockerfile_findings["npm install"]:
            print_and_log(f"\n   ⚠️  npm install ({len(dockerfile_findings['npm install'])}) - consider using 'npm ci' for reproducible builds:")
            for item in dockerfile_findings["npm install"]:
                print_and_log(f"      • {item['repo']}")
                print_and_log(f"        {item['path']}:{item['line']} → {item['command']}")
        
        if dockerfile_findings["npm i"]:
            print_and_log(f"\n   ⚠️  npm i ({len(dockerfile_findings['npm i'])}) - consider using 'npm ci' for reproducible builds:")
            for item in dockerfile_findings["npm i"]:
                print_and_log(f"      • {item['repo']}")
                print_and_log(f"        {item['path']}:{item['line']} → {item['command']}")

    # Public registry findings
    if public_registry_findings:
        print_and_log(f"\n🌐 PUBLIC REGISTRY USAGE ({len(public_registry_findings)} occurrences):")
        print_and_log("   The following resolved packages from public npmjs registry:")
        
        # Group by repo for cleaner output
        repos_using_public = {}
        for item in public_registry_findings:
            repo = item["repo"]
            if repo not in repos_using_public:
                repos_using_public[repo] = []
            repos_using_public[repo].append(item)
        
        for repo, items in sorted(repos_using_public.items()):
            print_and_log(f"\n   ⚠️  {repo}")
            for item in items:
                print_and_log(f"      • {item['version']} @ {item['location']}")
                print_and_log(f"        resolved: {item['resolved']}")
        
        print_and_log(f"\n   Total repos using public registry: {len(repos_using_public)}")
    else:
        print_and_log(f"\n✅ No packages resolved from public npmjs registry")

    print_and_log("\n" + "=" * 60)
    
    # Write report to file
    report_file = log_dir / "summary_report.txt"
    try:
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(report_lines))
        logger.info(f"Summary report saved to: {report_file}")
    except Exception as e:
        logger.error(f"Failed to write summary report to file: {e}")

    return blacklisted_found


# -----------------------------
# Main
# -----------------------------
def main():
    logger.info("=" * 60)
    logger.info(f"Starting NPM package scan - logs will be written to: {log_file}")
    logger.info("=" * 60)
    
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

    print("\n=== PACKAGE RESULTS ===")
    for r in results:
        repo = r["repo"]
        for match in r.get("matches", []):
            path = match["path"]
            declared_info = match.get("declared_versions") or {}
            is_transitive = match.get("is_transitive_only", False)
            declared = ", ".join(f"{v} ({k})" for k, v in declared_info.items()) if declared_info else "(transitive only)"
            resolved_list = match.get("resolved_versions", [])
            
            if resolved_list:
                for resolved_item in resolved_list:
                    version = resolved_item["version"]
                    location = resolved_item["location"]
                    is_public = resolved_item.get("is_public_registry", False)
                    transitive_marker = " [transitive]" if is_transitive else ""
                    public_marker = " [PUBLIC REGISTRY]" if is_public else ""
                    print(f"{repo} ({path}) - declared: {declared}, resolved: {version} @ {location}{transitive_marker}{public_marker}")
            else:
                print(f"{repo} ({path}) - declared: {declared}, resolved: -")

    print("\n=== DOCKERFILE RESULTS ===")
    dockerfile_count = 0
    for r in results:
        repo = r["repo"]
        for df_match in r.get("dockerfile_matches", []):
            dockerfile_count += 1
            path = df_match["path"]
            for cmd in df_match["npm_commands"]:
                print(f"{repo} ({path}:{cmd['line']}) - {cmd['type']}: {cmd['command']}")
    
    if dockerfile_count == 0:
        print("(no npm commands found in Dockerfiles)")

    blacklisted = generate_report(results)

    logger.info("=" * 60)
    logger.info(f"Scan completed!")
    logger.info(f"  - Full logs: {log_file}")
    logger.info(f"  - Summary report: {log_dir / 'summary_report.txt'}")
    logger.info("=" * 60)

    if blacklisted:
        exit(1)


if __name__ == "__main__":
    main()
