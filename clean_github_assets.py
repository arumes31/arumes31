import os
import sys
import time
import json
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

# Configuration from Environment Variables
TOKEN = os.getenv("GITHUB_TOKEN")
if not TOKEN:
    print("Error: GITHUB_TOKEN environment variable is not set.", file=sys.stderr)
    sys.exit(1)

DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")
KEEP_RUNS = int(os.getenv("KEEP_RUNS", "15"))
KEEP_VERSIONS = int(os.getenv("KEEP_VERSIONS", "3"))
KEEP_DAYS = int(os.getenv("KEEP_DAYS", "30"))
DELAY_MS = int(os.getenv("DELAY_MS", "500"))

print("==================================================")
print("           GitHub Asset Cleanup Script           ")
print("==================================================")
print(f"Dry Run Mode:       {DRY_RUN}")
print(f"Keep Runs (Actions):{KEEP_RUNS} per workflow")
print(f"Keep Versions (Pkg):{KEEP_VERSIONS} per package")
print(f"Retention Window:   {KEEP_DAYS} days")
print(f"Delay between API:  {DELAY_MS}ms")
print("==================================================\n")

BASE_URL = "https://api.github.com"

def make_request(url, method="GET", data=None):
    if not url.startswith("http"):
        url = f"{BASE_URL}{url}"
        
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "arumes31-github-cleanup")
    
    if data is not None:
        req.data = json.dumps(data).encode("utf-8")
        req.add_header("Content-Type", "application/json")
        
    retries = 3
    while retries > 0:
        try:
            # Rate limit compliance: sleep between API requests to avoid abuse detection
            time.sleep(DELAY_MS / 1000.0)
            
            with urllib.request.urlopen(req) as response:
                headers = response.info()
                
                # Proactively inspect and sleep if rate limits are nearing depletion
                check_rate_limits(headers)
                
                body = response.read()
                decoded_body = None
                if body:
                    decoded_body = json.loads(body.decode("utf-8"))
                return True, decoded_body, headers
                
        except urllib.error.HTTPError as e:
            headers = e.info()
            check_rate_limits(headers)
            
            if e.code in (403, 429):
                body_content = e.read().decode("utf-8")
                try:
                    err_json = json.loads(body_content)
                    err_msg = err_json.get("message", "")
                except Exception:
                    err_msg = body_content
                
                if "rate limit" in err_msg.lower() or "secondary rate limit" in err_msg.lower():
                    reset_time = headers.get("X-RateLimit-Reset")
                    if reset_time:
                        sleep_time = max(int(reset_time) - int(time.time()) + 5, 10)
                        print(f"[rate-limit] Rate limit hit ({e.code}). Sleeping for {sleep_time} seconds...")
                        time.sleep(sleep_time)
                        retries -= 1
                        continue
                print(f"[warning] HTTP Error {e.code} for URL {url}: {err_msg}")
                return False, None, headers
            elif e.code == 404:
                # 404 is a common output if package/run was already deleted or doesn't exist
                return False, None, headers
            else:
                try:
                    err_content = e.read().decode("utf-8")
                    print(f"[warning] HTTP Error {e.code} for URL {url}: {err_content}")
                except Exception:
                    print(f"[warning] HTTP Error {e.code} for URL {url}: {e.reason}")
                return False, None, headers
        except Exception as e:
            print(f"[warning] Connection error: {e}")
            time.sleep(2)
            retries -= 1
            
    return False, None, None

def check_rate_limits(headers):
    if not headers:
        return
    remaining = headers.get("X-RateLimit-Remaining")
    reset = headers.get("X-RateLimit-Reset")
    
    if remaining is not None:
        rem = int(remaining)
        if rem < 200:
            res_val = int(reset) if reset else int(time.time() + 60)
            sleep_time = max(res_val - int(time.time()) + 5, 5)
            print(f"\n[rate-limit-warning] Only {rem} API requests remaining. Pausing for {sleep_time}s until reset...")
            time.sleep(sleep_time)

def fetch_paginated(endpoint):
    items = []
    url = endpoint
    while url:
        success, data, headers = make_request(url)
        if not success or data is None:
            break
            
        if isinstance(data, list):
            items.extend(data)
        elif isinstance(data, dict):
            # Certain GitHub APIs return objects wrapping arrays
            if "workflow_runs" in data:
                items.extend(data["workflow_runs"])
            elif "packages" in data:
                items.extend(data["packages"])
            else:
                # If no list found, wrap dict
                items.append(data)
        
        # Determine next page from Link headers
        url = None
        if headers:
            link_header = headers.get("Link")
            if link_header:
                parts = link_header.split(",")
                for part in parts:
                    if 'rel="next"' in part:
                        url = part.split(";")[0].strip("<> ")
    return items

def clean_workflow_runs(owner, repo):
    print(f"--> Scanning workflow runs in {owner}/{repo}...")
    runs = fetch_paginated(f"/repos/{owner}/{repo}/actions/runs?per_page=100")
    if not runs:
        print("    No runs found.")
        return
        
    print(f"    Found {len(runs)} total runs. Filtering and identifying deletions...")
    
    # Group runs by workflow_id
    runs_by_workflow = {}
    for run in runs:
        wf_id = run.get("workflow_id")
        if not wf_id:
            continue
        if wf_id not in runs_by_workflow:
            runs_by_workflow[wf_id] = []
        runs_by_workflow[wf_id].append(run)
        
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    deleted_count = 0
    
    for wf_id, wf_runs in runs_by_workflow.items():
        # GitHub action runs are already returned in reverse chronological order, 
        # but let's sort them explicitly to ensure correct order
        wf_runs.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        
        # Select workflows to keep (first KEEP_RUNS runs)
        runs_to_evaluate = wf_runs[KEEP_RUNS:]
        wf_name = wf_runs[0].get("name", f"Workflow {wf_id}") if wf_runs else f"Workflow {wf_id}"
        
        for run in runs_to_evaluate:
            status = run.get("status")
            if status != "completed":
                # Do not delete in-progress, queued, or active workflow runs
                continue
                
            created_at_str = run.get("created_at")
            if not created_at_str:
                continue
                
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            
            if created_at < cutoff_date:
                run_id = run["id"]
                run_num = run.get("run_number", "unknown")
                if DRY_RUN:
                    print(f"    [dry-run] Would delete run {run_id} (#{run_num}) of workflow '{wf_name}' (created {created_at_str})")
                else:
                    print(f"    Deleting run {run_id} (#{run_num}) of workflow '{wf_name}'...")
                    success, _, _ = make_request(f"/repos/{owner}/{repo}/actions/runs/{run_id}", method="DELETE")
                    if success:
                        deleted_count += 1
                    else:
                        print(f"      [failed] Could not delete run {run_id}")
                        
    if not DRY_RUN:
        print(f"    Deleted {deleted_count} workflow runs.")

def clean_package_versions(owner, pkg_name, p_type, is_org=False):
    pkg_name_encoded = urllib.parse.quote(pkg_name, safe='')
    
    if is_org:
        endpoint = f"/orgs/{owner}/packages/{p_type}/{pkg_name_encoded}/versions?per_page=100"
    else:
        endpoint = f"/user/packages/{p_type}/{pkg_name_encoded}/versions?per_page=100"
        
    versions = fetch_paginated(endpoint)
    if not versions:
        return
        
    print(f"  Package '{pkg_name}' has {len(versions)} versions.")
    
    # Sort versions by created_at descending (latest first)
    versions.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    
    # Evaluate versions after the first KEEP_VERSIONS
    versions_to_evaluate = versions[KEEP_VERSIONS:]
    deleted_count = 0
    
    for ver in versions_to_evaluate:
        ver_id = ver["id"]
        ver_name = ver.get("name", "unknown")
        
        created_at_str = ver.get("created_at")
        if not created_at_str:
            continue
            
        created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        
        if created_at < cutoff_date:
            metadata = ver.get("metadata", {})
            container = metadata.get("container", {})
            tags = container.get("tags", [])
            
            # Protect versions with 'latest' or 'main' tags from deletion
            protected_tags = [t for t in tags if t.lower() in ('latest', 'main')]
            if protected_tags:
                print(f"    [protected] Skipping package version {ver_id} ({ver_name}) because of protected tag(s): {', '.join(protected_tags)}")
                continue
                
            tag_str = f" [tags: {', '.join(tags)}]" if tags else ""
            
            if is_org:
                del_url = f"/orgs/{owner}/packages/{p_type}/{pkg_name_encoded}/versions/{ver_id}"
            else:
                del_url = f"/user/packages/{p_type}/{pkg_name_encoded}/versions/{ver_id}"
                
            if DRY_RUN:
                print(f"    [dry-run] Would delete version {ver_id} ({ver_name}){tag_str} (created {created_at_str})")
            else:
                print(f"    Deleting version {ver_id} ({ver_name}){tag_str}...")
                success, _, _ = make_request(del_url, method="DELETE")
                if success:
                    deleted_count += 1
                else:
                    print(f"      [failed] Could not delete package version {ver_id}")
                    
    if not DRY_RUN and deleted_count > 0:
        print(f"  Deleted {deleted_count} versions of package '{pkg_name}'.")

def clean_packages(username):
    print("=== Scanning Packages ===")
    package_types = ['container', 'npm', 'maven', 'rubygems', 'nuget', 'docker']
    
    # 1. User Packages
    print("\nScanning user packages...")
    for p_type in package_types:
        packages = fetch_paginated(f"/user/packages?package_type={p_type}&per_page=100")
        if packages:
            print(f"Found {len(packages)} '{p_type}' packages owned by user.")
            for pkg in packages:
                pkg_name = pkg["name"]
                clean_package_versions(username, pkg_name, p_type, is_org=False)
                
    # 2. Organization Packages
    print("\nScanning user organizations for packages...")
    orgs = fetch_paginated("/user/orgs?per_page=100")
    if orgs:
        for org in orgs:
            org_name = org["login"]
            print(f"\nScanning organization '{org_name}'...")
            for p_type in package_types:
                packages = fetch_paginated(f"/orgs/{org_name}/packages?package_type={p_type}&per_page=100")
                if packages:
                    print(f"Found {len(packages)} '{p_type}' packages in org '{org_name}'.")
                    for pkg in packages:
                        pkg_name = pkg["name"]
                        clean_package_versions(org_name, pkg_name, p_type, is_org=True)
    else:
        print("No organizations found for the user.")

def main():
    # 1. Determine username
    username = os.getenv("GITHUB_REPOSITORY_OWNER")
    if username:
        print(f"Running in GitHub environment. Target owner: {username}\n")
    else:
        # If not running in GitHub Actions, query /user endpoint to find authenticated user
        success, user_info, _ = make_request("/user")
        if success and user_info:
            username = user_info.get("login")
            print(f"Authenticated successfully as: {username}\n")
        else:
            print("Error: Could not authenticate with GitHub. Check your GITHUB_TOKEN.", file=sys.stderr)
            sys.exit(1)
            
    # 2. Clean Workflow Runs
    print("=== Scanning Repositories for Actions Workflow Runs ===")
    
    # Try fetching all repositories first
    repos = fetch_paginated("/user/repos?per_page=100")
    
    # Fallback to the current repository only if listing all repos returns empty (likely GITHUB_TOKEN restriction)
    if not repos:
        current_repo = os.getenv("GITHUB_REPOSITORY")
        if current_repo and "/" in current_repo:
            owner, repo_name = current_repo.split("/", 1)
            print(f"[fallback] Could not list all repositories. Falling back to the current repository: {current_repo}")
            repos = [{
                "name": repo_name,
                "owner": {"login": owner},
                "archived": False,
                "permissions": {"admin": True, "push": True}
            }]
        else:
            print("Error: Could not retrieve repositories and GITHUB_REPOSITORY is not set.", file=sys.stderr)
            sys.exit(1)
            
    # Filter out archived repositories, or repositories where the user has no write access
    valid_repos = []
    for r in repos:
        if r.get("archived"):
            continue
        perms = r.get("permissions", {})
        if perms.get("admin") or perms.get("push"):
            valid_repos.append(r)
            
    print(f"Found {len(valid_repos)} active repositories with write access.\n")
    for r in valid_repos:
        repo_name = r["name"]
        repo_owner = r["owner"]["login"]
        clean_workflow_runs(repo_owner, repo_name)
        
    # 3. Clean Packages
    clean_packages(username)

    
    print("\n==================================================")
    print("           Cleanup Task Completed!               ")
    print("==================================================")

if __name__ == "__main__":
    main()
