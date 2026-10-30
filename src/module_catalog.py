"""Dynamic discovery of Terraform modules in the shared public module repository."""
import base64
import json
import os
import re
import urllib.request

MODULE_REPO = "cliffbill/terraform-aws-modules"


def api(path):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "genai-infrastructure-platform",
    }
    # Authenticated requests avoid the very low unauthenticated GitHub API rate limit.
    token = os.environ.get("GH_PLATFORM_TOKEN") or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        "https://api.github.com" + path,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def discover_modules():
    repo = api("/repos/" + MODULE_REPO)
    tree = api("/repos/" + MODULE_REPO + "/git/trees/" + repo["default_branch"] + "?recursive=1")
    if tree.get("truncated"):
        raise RuntimeError("Module repository tree is too large to parse safely.")
    paths = [entry["path"] for entry in tree.get("tree", []) if entry.get("type") == "blob"]
    roots = sorted({path.rsplit("/variables.tf", 1)[0] for path in paths
                    if path.startswith("modules/") and path.endswith("/variables.tf")})
    modules = []
    for root in roots:
        file_data = api("/repos/" + MODULE_REPO + "/contents/" + root + "/variables.tf")
        source = base64.b64decode(file_data["content"]).decode("utf-8", errors="replace")
        variables = []
        for block in re.finditer(r'variable\s+"([A-Za-z0-9_]+)"\s*\{(.*?)(?=\n\})', source, re.S):
            name, body = block.group(1), block.group(2)
            type_match = re.search(r'(?m)^\s*type\s*=\s*([^\n]+)', body)
            default_match = re.search(r'(?m)^\s*default\s*=', body)
            description_match = re.search(r'(?m)^\s*description\s*=\s*"([^"]*)"', body)
            variables.append({
                "name": name,
                "type": type_match.group(1).strip() if type_match else "any",
                "required": default_match is None,
                "description": description_match.group(1) if description_match else "",
            })
        modules.append({
            "path": root,
            "name": root.rsplit("/", 1)[-1],
            "source": "git::https://github.com/" + MODULE_REPO + ".git//" + root + "?ref=" + repo["default_branch"],
            "variables": variables,
        })
    return modules
