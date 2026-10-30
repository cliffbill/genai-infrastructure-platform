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
MODULE_REPO = "cliffbill/terraform-aws-modules"


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
                "Eres un arquitecto cloud experto en Terraform que conversa en español. "
                "El usuario puede pedir uno o múltiples recursos en un solo issue. "
                "Analiza el texto y selecciona exclusivamente módulos del catálogo proporcionado. No inventes módulos ni variables. "
                "Devuelve un objeto JSON con la estructura: "
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
                "}. Si no hay coincidencias, resources debe ser []."
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


def validate_multi(data, catalog=None):
    if catalog is None:
        from module_catalog import discover_modules
        catalog = discover_modules()
    errors = []
    repo_name = data.get("repository_name")
    if not isinstance(repo_name, str) or not re.fullmatch(r"infra-[a-z0-9]+(?:-[a-z0-9]+)*", repo_name):
        errors.append("¿Cómo se llama la aplicación o equipo? Por ejemplo, pagos (repositorio infra-pagos).")
    
    resources = data.get("resources", [])
    if not resources or not isinstance(resources, list):
        errors.append("No encontré módulos compatibles en el catálogo actual. Precisa qué servicio o recurso necesitas.")
        return errors

    for idx, res in enumerate(resources):
        mod_path = res.get("module_path")
        module = next((m for m in catalog if m["path"] == mod_path), None)
        if module is None:
            errors.append(f"El recurso #{idx+1} no coincide con un módulo válido del catálogo.")
            continue
        
        params = res.get("parameters")
        if not isinstance(params, dict):
            params = {}
        declared = {v["name"]: v for v in module["variables"]}
        res["parameters"] = {k: v for k, v in params.items() if k in declared}
        for name, variable in declared.items():
            if variable["required"] and name not in res["parameters"]:
                errors.append(f"Para el módulo {module['name']} ({res.get('instance_name', 'recurso')}), ¿qué valor debe tener {name}? {variable['description']}")
        res["module"] = module

    if data.get("environment") not in {"dev", "test", "staging", "prod"}:
        errors.append("¿En qué entorno se utilizará? Responde dev, test, staging o prod.")
    if not isinstance(data.get("region"), str) or not re.fullmatch(r"[a-z]{2}(-gov)?-[a-z]+-\d", data["region"]):
        errors.append("¿En qué región de AWS se utilizará? Por ejemplo, us-east-1.")
    
    return errors


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


def main():
    event_path = os.environ["GITHUB_EVENT_PATH"]
    with open(event_path, encoding="utf-8") as event_file:
        event = json.load(event_file)
    issue = event["issue"]
    comment = event.get("comment", {})
    if comment.get("user", {}).get("type") == "Bot":
        return
    command = comment.get("body", "").strip()
    source_repo = os.environ["GITHUB_REPOSITORY"]
    number = str(issue["number"])
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if event_name == "issue_comment" and issue.get("pull_request"):
        return

    conversation = ""
    if event_name == "issue_comment":
        try:
            conversation = gh("api", "repos/" + source_repo + "/issues/" + number + "/comments", "--paginate", "--jq", ".[].body")
        except Exception:
            conversation = command
    issue_text = (
        "Issue title:\n" + issue.get("title", "") +
        "\nIssue description:\n" + issue.get("body", "") +
        "\nConversation comments:\n" + conversation
    )[:16000]

    try:
        from module_catalog import discover_modules
        catalog = discover_modules()
        
        data = llm_extract_multi(issue_text, catalog=catalog)
        
        explicit_repo = re.findall(r"(?i)\binfra-[a-z0-9]+(?:-[a-z0-9]+)*\b", issue_text)
        if explicit_repo:
            data["repository_name"] = explicit_repo[-1].lower()
        else:
            app_match = re.search(r"(?im)^\s*(?:aplicaci[oó]n|equipo)\s*:\s*([a-z0-9][a-z0-9-]*)\s*$", issue_text)
            if app_match:
                slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "-", app_match.group(1).lower())).strip("-")
                if slug:
                    data["repository_name"] = "infra-" + slug

        explicit_region = re.findall(r"(?i)\bregi[oó]n\s*:\s*([a-z0-9-]+)", issue_text)
        if explicit_region:
            data["region"] = explicit_region[-1].strip()
        explicit_environment = re.findall(r"(?i)\bentorno\s*:\s*(dev|test|staging|prod)\b", issue_text)
        if explicit_environment:
            data["environment"] = explicit_environment[-1].lower()

        errors = validate_multi(data, catalog=catalog)
        if errors:
            issue_comment(
                source_repo, number,
                "## Infrastructure Copilot — faltan datos obligatorios\n\n" +
                "Entendí tu solicitud. Antes de generar Terraform y crear el repositorio, necesito estos datos obligatorios:\n\n" +
                "\n".join("- " + error for error in errors) +
                "\n\n**Puedes responder normalmente en español**; no necesitas usar comandos ni repetir los datos que ya proporcionaste. " +
                "Cuando estén completos los datos requeridos, continuaré con el repositorio y el Pull Request. No se despliegan recursos en AWS."
            )
            return

        repo_name = data["repository_name"]
        target_repo = OWNER + "/" + repo_name
        
        cli_env = {"GH_TOKEN": GH_PLATFORM_TOKEN}
        auth_value = base64.b64encode(("x-access-token:" + GH_PLATFORM_TOKEN).encode()).decode()
        git_env = {
            **os.environ,
            **cli_env,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + auth_value,
        }

        with tempfile.TemporaryDirectory(prefix="infra-copilot-") as temp_dir:
            checkout = os.path.join(temp_dir, repo_name)
            
            # Verificar si el repositorio ya existe; si no, crearlo usando gh repo create --template
            repo_exists = subprocess.run(
                ["gh", "repo", "view", target_repo],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=cli_env
            ).returncode == 0

            if repo_exists:
                print(f"Repository {target_repo} already exists; cloning it.")
                gh("repo", "clone", target_repo, checkout, env=cli_env)
            else:
                template_slug = f"{TEMPLATE_OWNER}/{TEMPLATE_REPO}"
                try:
                    gh("repo", "create", target_repo, "--template", template_slug, "--private", "--clone", checkout, env=cli_env)
                except Exception as template_exc:
                    raise RuntimeError(f"Could not create repository from template {template_slug}: {template_exc}") from template_exc

            default_branch = ""
            try:
                default_branch = gh(
                    "api", "repos/" + target_repo, "--jq", ".default_branch // empty",
                    env=cli_env
                ).strip()
            except Exception:
                pass
            if not default_branch:
                for candidate in ("main", "master"):
                    exists = subprocess.run(
                        ["git", "ls-remote", "--exit-code", "--heads", "origin", candidate],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        env=git_env, cwd=checkout
                    ).returncode == 0
                    if exists:
                        default_branch = candidate
                        break
            if not default_branch:
                default_branch = "main"

            branch = "copilot/issue-" + number + "-infra"
            existing_pr = gh(
                "pr", "list", "--repo", target_repo, "--head", branch,
                "--state", "all", "--json", "url", "--jq", ".[0].url // empty",
                env=cli_env
            )
            if existing_pr:
                issue_comment(source_repo, number, "A Pull Request for this request already exists in " + target_repo + ": " + existing_pr)
                return

            subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True, cwd=checkout)
            subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], check=True, cwd=checkout)
            subprocess.run(["git", "config", "pull.rebase", "false"], check=False, cwd=checkout)

            # Sincronización robusta con la rama remota existente de la plantilla
            subprocess.run(["git", "fetch", "origin", default_branch], check=True, env=git_env, cwd=checkout)
            subprocess.run(["git", "checkout", "-B", default_branch, f"origin/{default_branch}"], check=True, cwd=checkout)
            subprocess.run(["git", "pull", "origin", default_branch, "--allow-unrelated-histories", "--no-edit"], check=False, env=git_env, cwd=checkout)

            # Crear la rama de propuesta para el PR
            subprocess.run(["git", "checkout", "-B", branch], check=True, cwd=checkout)

            # Generar main.tf multi-recurso
            tf_lines = [
                'terraform {',
                '  required_version = ">= 1.5.0"',
                '  required_providers {',
                '    aws = { source = "hashicorp/aws", version = ">= 5.0, < 7.0" }',
                '  }',
                '}',
                '',
                'provider "aws" {',
                '  region = ' + json.dumps(data["region"]),
                '}',
                ''
            ]

            resources = data.get("resources", [])
            for idx, res in enumerate(resources):
                mod_path = res.get("module_path")
                inst_name = res.get("instance_name", f"recurso_{idx}")
                module = res.get("module") or next((m for m in catalog if m["path"] == mod_path), None)
                
                module_source = module["source"] if module else f"git::https://github.com/{MODULE_REPO}.git//{mod_path}?ref=main"
                params = res.get("parameters", {})
                
                if "tags" not in params:
                    params["tags"] = {
                        "Environment": data["environment"],
                        "ManagedBy": "genai-infrastructure-platform"
                    }

                tf_lines.extend([
                    'module "' + re.sub(r'[^a-zA-Z0-9_]', '_', inst_name) + '" {',
                    '  source = ' + json.dumps(module_source),
                ])
                for key, value in params.items():
                    tf_lines.append('  ' + key + ' = ' + hcl_value(value))
                tf_lines.extend(['}', ''])

            with open(os.path.join(checkout, "main.tf"), "w", encoding="utf-8") as tf:
                tf.write("\n".join(tf_lines))

            # REQUEST.md sencillo original
            with open(os.path.join(checkout, "REQUEST.md"), "w", encoding="utf-8") as doc:
                doc.write(
                    "# Generated infrastructure request\n\n"
                    "- Source issue: https://github.com/" + source_repo + "/issues/" + number + "\n"
                    "- Application repository: " + target_repo + "\n"
                    "- Environment: " + data["environment"] + "\n"
                    "- Region: " + data["region"] + "\n"
                    "- Resources count: " + str(len(resources)) + "\n\n"
                    "Generated proposal only. Terraform downloads the shared modules during terraform init. "
                    "Review code and checks before merge. No terraform apply is run by this workflow.\n"
                )

            subprocess.run(["git", "add", "main.tf", "REQUEST.md"], check=True, cwd=checkout)
            staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=checkout).returncode != 0
            if staged:
                subprocess.run(["git", "commit", "-m", "feat: propose multi-resource infrastructure from issue #" + number], check=True, cwd=checkout)

            subprocess.run(["git", "push", "--set-upstream", "origin", branch, "--force"], check=True, env=git_env, cwd=checkout)

            pr_body = (
                "## GenAI Infrastructure proposal\n\nGenerated from https://github.com/" + source_repo + "/issues/" + number + ".\n\n"
                "- [ ] Review the remote module source and inputs\n"
                "- [ ] Confirm environment, region, and repository ownership\n"
                "- [ ] Inspect Terraform validation and governance checks\n"
                "- [ ] Review a Terraform plan in an authorized environment\n\n"
                "No AWS resources were created. This PR does not run terraform apply."
            )
            pr_payload = {
                "title": "Proposal: infrastructure for issue #" + number,
                "head": branch,
                "base": default_branch,
                "body": pr_body
            }
            pr_req = urllib.request.Request(
                "https://api.github.com/repos/" + target_repo + "/pulls",
                data=json.dumps(pr_payload).encode(),
                headers={
                    "Authorization": "Bearer " + GH_PLATFORM_TOKEN,
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Content-Type": "application/json"
                },
                method="POST"
            )
            try:
                with urllib.request.urlopen(pr_req, timeout=30) as pr_response:
                    pr_result = json.loads(pr_response.read().decode())
                pr_url = pr_result["html_url"]
            except urllib.error.HTTPError as pr_exc:
                detail = pr_exc.read().decode(errors="replace")[:1200]
                if pr_exc.code == 422 and "pull request already exists" in detail.lower():
                    pr_url = "https://github.com/" + target_repo + "/pulls"
                else:
                    raise RuntimeError(
                        "GitHub could not create the Pull Request (HTTP " + str(pr_exc.code) +
                        "): " + detail +
                        ". Verify that GH_PLATFORM_TOKEN has pull requests write access to " + target_repo + "."
                    ) from pr_exc

        issue_comment(
            source_repo, number,
            "Created repository " + target_repo + " from the public template and opened a Pull Request: " + pr_url +
            "\n\nThe selected shared Terraform modules are referenced remotely and downloaded during terraform init. No AWS resources were created."
        )
    except Exception as exc:
        operation_error = str(exc)[:1500]
        issue_comment(source_repo, number, "I couldn't complete the request: " + operation_error + "\nNo AWS resources were deployed.")
        raise


if __name__ == "__main__":
    main()
