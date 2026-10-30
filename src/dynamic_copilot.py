import base64
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request

API_KEY = os.environ.get("LLM_API_KEY", "")
GH_PLATFORM_TOKEN = os.environ.get("GH_PLATFORM_TOKEN", "")
BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
MODEL = os.environ.get("LLM_MODEL", "gpt-5.4-nano")
OWNER = os.environ.get("GITHUB_REPOSITORY", "cliffbill/genai-infrastructure-platform").split("/")[0]
TEMPLATE_OWNER = "cliffbill"
TEMPLATE_REPO = "terraform-infrastructure-template"


def gh(*args, env=None):
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(["gh", *args], text=True, check=True, capture_output=True, env=merged_env).stdout.strip()


def issue_comment(repo, number, body):
    gh("issue", "comment", str(number), "--repo", repo, "--body", body)


def llm_extract_multi(text, catalog=None):
    if catalog is None:
        from module_catalog import discover_modules
        catalog = discover_modules()
    if not API_KEY:
        raise RuntimeError("LLM_API_KEY secret is not configured.")
    
    catalog_data = [{"path": m["path"], "name": m["name"], "variables": m["variables"]} for m in catalog]
    body = {
        "model": MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": (
                "Eres un arquitecto cloud experto en Terraform. El usuario puede pedir múltiples recursos en un solo issue. "
                "Analiza el texto y devuelve un objeto JSON estrictamente con la estructura: "
                "{"
                "  \"repository_name\": \"infra-<slug>\", "
                "  \"environment\": \"dev|test|staging|prod\", "
                "  \"region\": \"us-east-1\", "
                "  \"resources\": ["
                "    {"
                "      \"module_path\": \"path/del/catalogo\", "
                "      \"instance_name\": \"nombre-unico-recurso\", "
                "      \"parameters\": { ... }"
                "    }"
                "  ]"
                "}. No inventes módulos fuera del catálogo."
            )},
            {"role": "user", "content": "CATÁLOGO REAL:\n" + json.dumps(catalog_data, ensure_ascii=False) + "\nSOLICITUD:\n" + text[:10000]}
        ],
        "response_format": {"type": "json_object"}
    }
    req = urllib.request.Request(BASE_URL + "/chat/completions", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + API_KEY, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as response:
        payload = json.loads(response.read().decode())
    return json.loads(payload["choices"][0]["message"]["content"])


def hcl_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(hcl_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(json.dumps(str(k)) + " = " + hcl_value(v) for k, v in value.items()) + " }"
    raise ValueError("Tipo de parámetro Terraform no admitido.")


def create_repo_from_template(repo_name):
    if not GH_PLATFORM_TOKEN:
        raise RuntimeError("GH_PLATFORM_TOKEN secret is not configured.")
    url = f"https://api.github.com/repos/{TEMPLATE_OWNER}/{TEMPLATE_REPO}/generate"
    payload = {
        "owner": OWNER,
        "name": repo_name,
        "description": f"Dynamic multi-resource IaC for {repo_name.removeprefix('infra-')}",
        "private": True,
        "include_all_branches": False
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {GH_PLATFORM_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError:
        return {"full_name": f"{OWNER}/{repo_name}", "reused_existing": True}


def main():
    event_path = os.environ["GITHUB_EVENT_PATH"]
    with open(event_path, encoding="utf-8") as event_file:
        event = json.load(event_file)
    issue = event["issue"]
    comment = event.get("comment", {})
    if comment.get("user", {}).get("type") == "Bot":
        return
    
    source_repo = os.environ["GITHUB_REPOSITORY"]
    number = str(issue["number"])
    issue_text = f"Issue title:\n{issue.get('title', '')}\nIssue description:\n{issue.get('body', '')}"

    try:
        data = llm_extract_multi(issue_text)
        explicit_repo = re.findall(r"(?i)\binfra-[a-z0-9]+(?:-[a-z0-9]+)*\b", issue_text)
        if explicit_repo:
            data["repository_name"] = explicit_repo[-1].lower()
            
        repo_name = data["repository_name"]
        target_repo = f"{OWNER}/{repo_name}"
        create_repo_from_template(repo_name)
        
        cli_env = {"GH_TOKEN": GH_PLATFORM_TOKEN}
        auth_value = base64.b64encode(("x-access-token:" + GH_PLATFORM_TOKEN).encode()).decode()
        git_env = {
            **os.environ, **cli_env,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + auth_value,
        }

        with tempfile.TemporaryDirectory(prefix="infra-dinamica-") as temp_dir:
            checkout = os.path.join(temp_dir, repo_name)
            gh("repo", "clone", target_repo, checkout, env=cli_env)
            
            default_branch = "main"
            branch = f"copilot/issue-{number}-dynamic"
            
            subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True, cwd=checkout)
            subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], check=True, cwd=checkout)
            
            try:
                subprocess.run(["git", "fetch", "origin", default_branch], check=True, env=git_env, cwd=checkout)
            except subprocess.CalledProcessError:
                readme_path = os.path.join(checkout, "README.md")
                if not os.path.exists(readme_path):
                    with open(readme_path, "w", encoding="utf-8") as f:
                        f.write(f"# {repo_name}\n\nDynamic Infrastructure Hub\n")
                    subprocess.run(["git", "add", "README.md"], check=True, cwd=checkout)
                    subprocess.run(["git", "commit", "-m", "chore: initial commit"], check=True, cwd=checkout)
                
                try:
                    subprocess.run(["git", "push", "-u", "origin", default_branch], check=True, env=git_env, cwd=checkout)
                except subprocess.CalledProcessError:
                    subprocess.run(["git", "pull", "origin", default_branch, "--allow-unrelated-histories", "--no-edit"], check=False, env=git_env, cwd=checkout)
                    subprocess.run(["git", "push", "-u", "origin", default_branch], check=True, env=git_env, cwd=checkout)
                
                subprocess.run(["git", "fetch", "origin", default_branch], check=True, env=git_env, cwd=checkout)

            base_ref = f"origin/{default_branch}"
            subprocess.run(["git", "checkout", "-B", branch, base_ref], check=True, cwd=checkout)

            tf_lines = [
                'terraform {',
                '  required_version = ">= 1.5.0"',
                '  required_providers {',
                '    aws = { source = "hashicorp/aws", version = ">= 5.0, < 7.0" }',
                '  }',
                '}',
                '',
                'provider "aws" {',
                f'  region = {json.dumps(data.get("region", "us-east-1"))}',
                '}',
                ''
            ]

            resources = data.get("resources", [])
            for idx, res in enumerate(resources):
                mod_path = res.get("module_path")
                inst_name = res.get("instance_name", f"recurso_{idx}")
                params = res.get("parameters", {})
                
                if "tags" not in params:
                    params["tags"] = {
                        "Environment": data.get("environment", "dev"),
                        "ManagedBy": "genai-infrastructure-platform"
                    }

                tf_lines.extend([
                    f'module "{inst_name}" {{',
                    f'  source = "../../modules/{mod_path}"' if not mod_path.startswith("git") else f'  source = "{mod_path}"',
                ])
                for key, value in params.items():
                    tf_lines.append(f'  {key} = {hcl_value(value)}')
                
                tf_lines.extend([
                    '}',
                    ''
                ])

            with open(os.path.join(checkout, "main.tf"), "w", encoding="utf-8") as tf:
                tf.write("\n".join(tf_lines))

            with open(os.path.join(checkout, "REQUEST.md"), "w", encoding="utf-8") as doc:
                doc.write(
                    f"# Dynamic Infrastructure Request #{number}\n\n"
                    f"- Entorno: {data.get('environment')}\n"
                    f"- Región: {data.get('region')}\n"
                    f"- Recursos solicitados: {len(resources)}\n"
                )

            subprocess.run(["git", "add", "main.tf", "REQUEST.md"], check=True, cwd=checkout)
            if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=checkout).returncode != 0:
                subprocess.run(["git", "commit", "-m", f"feat: dynamic multi-resource provisioning from issue #{number}"], check=True, cwd=checkout)

            subprocess.run(["git", "push", "--set-upstream", "origin", branch, "--force"], check=True, env=git_env, cwd=checkout)

            pr_payload = {
                "title": f"Dynamic Proposal: Multi-resource architecture for issue #{number}",
                "head": branch,
                "base": default_branch,
                "body": f"Generado dinámicamente para {len(resources)} recursos bajo estándares de gobernanza."
            }
            pr_req = urllib.request.Request(
                f"https://api.github.com/repos/{target_repo}/pulls",
                data=json.dumps(pr_payload).encode(),
                headers={
                    "Authorization": f"Bearer {GH_PLATFORM_TOKEN}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Content-Type": "application/json"
                },
                method="POST"
            )
            with urllib.request.urlopen(pr_req, timeout=30) as pr_response:
                pr_result = json.loads(pr_response.read().decode())
                pr_url = pr_result["html_url"]

        issue_comment(source_repo, number, f"¡Infraestructura dinámica generada con éxito! Revisa el Pull Request: {pr_url}")

    except Exception as exc:
        issue_comment(source_repo, number, f"No pude completar la solicitud dinámica: {str(exc)[:1500]}")
        raise


if __name__ == "__main__":
    main()
