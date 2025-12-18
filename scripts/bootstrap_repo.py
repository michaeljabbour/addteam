from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def _run_checked(cmd: list[str], *, what: str) -> subprocess.CompletedProcess[str]:
    try:
        result = _run(cmd)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing dependency for {what}: {cmd[0]!r} not found") from exc

    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(f"Failed to {what}: {details}")
    return result


def _gh_json(args: list[str], *, what: str) -> dict:
    result = _run_checked(["gh", *args], what=what)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Unexpected non-JSON output while trying to {what}") from exc


def _gh_text(args: list[str], *, what: str) -> str:
    result = _run_checked(["gh", *args], what=what)
    return result.stdout.strip()


def _load_usernames(path: Path) -> list[str]:
    if not path.exists():
        raise RuntimeError(f"{path.as_posix()} not found")

    seen: set[str] = set()
    users: list[str] = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            line = line[1:]
        if line not in seen:
            seen.add(line)
            users.append(line)
    return users


def _http_post_json(url: str, *, headers: dict[str, str], payload: dict, timeout_s: int = 30) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"content-type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error calling {url}: {exc}") from exc

    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Non-JSON response from {url}: {data[:200]}") from exc


def _generate_repo_summary(
    *, provider: str, repo_full_name: str, repo_description: str, timeout_s: int = 30
) -> str:
    prompt = "\n".join(
        [
            "In 2–3 short sentences, describe this GitHub repository for a collaborator.",
            "",
            f"Repo: {repo_full_name}",
            f"Existing description: {repo_description or '(none)'}",
            "",
            "Include:",
            "- what it does",
            "- the fastest way to get started",
            "Keep it crisp and practical.",
        ]
    )

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        response = _http_post_json(
            "https://api.openai.com/v1/chat/completions",
            headers={"authorization": f"Bearer {api_key}"},
            payload={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 120,
                "temperature": 0.2,
            },
            timeout_s=timeout_s,
        )
        try:
            return response["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError) as exc:
            raise RuntimeError(f"Unexpected OpenAI response: {response}") from exc

    if provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        response = _http_post_json(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            payload={
                "model": "claude-3-haiku-20240307",
                "max_tokens": 120,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout_s=timeout_s,
        )
        try:
            return response["content"][0]["text"].strip()
        except (KeyError, IndexError, AttributeError) as exc:
            raise RuntimeError(f"Unexpected Anthropic response: {response}") from exc

    raise RuntimeError(f"Unknown provider: {provider}")


def _write_readme_summary(readme_path: Path, summary: str) -> None:
    begin = "<!-- BEGIN AUTO SUMMARY -->"
    end = "<!-- END AUTO SUMMARY -->"

    if readme_path.exists():
        existing = readme_path.read_text()
    else:
        existing = ""

    block = f"{begin}\n\n{summary.strip()}\n\n{end}\n"

    if begin in existing and end in existing:
        before = existing.split(begin, 1)[0]
        after = existing.split(end, 1)[1]
        readme_path.write_text(before + block + after.lstrip("\n"))
        return

    if existing.strip():
        readme_path.write_text(existing.rstrip() + "\n\n" + block)
    else:
        readme_path.write_text(block)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap repo collaborators + optional AI summary.")
    parser.add_argument(
        "--collaborators-file",
        default="collaborators.txt",
        help="Path to a newline-delimited list of GitHub usernames.",
    )
    parser.add_argument(
        "--permission",
        default="push",
        choices=["pull", "triage", "push", "maintain", "admin"],
        help="Permission to grant collaborators.",
    )
    parser.add_argument(
        "--repo",
        help="Repo to target as OWNER/NAME (defaults to the current directory's repo).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions without modifying GitHub.")
    parser.add_argument("--no-ai", action="store_true", help="Skip AI-generated repo summary.")
    parser.add_argument(
        "--provider",
        default="auto",
        choices=["auto", "openai", "anthropic"],
        help="AI provider to use (default: auto-detect via env vars).",
    )
    parser.add_argument(
        "--write-readme",
        action="store_true",
        help="Write the generated summary into README.md between markers.",
    )
    args = parser.parse_args()

    # ---------- Resolve repo ----------
    print("🔍 Resolving repo...")
    view_args = ["repo", "view"]
    if args.repo:
        view_args.append(args.repo)
    view_args.extend(["--json", "name,owner,description"])

    repo = _gh_json(view_args, what="resolve repo")
    repo_name = repo["name"]
    repo_owner = repo["owner"]["login"]
    description = repo.get("description") or ""

    me = _gh_text(["api", "user", "--jq", ".login"], what="resolve authenticated user")

    repo_full_name = f"{repo_owner}/{repo_name}"
    print(f"📦 Repo: {repo_full_name}")
    print(f"👑 Repo owner: {repo_owner}")
    print(f"👤 Auth user: {me}\n")

    # ---------- Load collaborators ----------
    users = _load_usernames(Path(args.collaborators_file))
    if not users:
        print("ℹ️  No collaborators found; nothing to do.")
        return 0

    # ---------- Add collaborators ----------
    added = 0
    skipped = 0
    failed = 0
    would_add = 0

    for u in users:
        if u == repo_owner:
            print(f"⏭️  Skipping {u} (repo owner)")
            skipped += 1
            continue
        if u == me:
            print(f"⏭️  Skipping {u} (you)")
            skipped += 1
            continue

        print(f"➕ Adding {u} … ", end="")
        if args.dry_run:
            print("🟦 dry-run")
            would_add += 1
            continue

        r = _run(
            [
                "gh",
                "api",
                "-X",
                "PUT",
                f"repos/{repo_owner}/{repo_name}/collaborators/{u}",
                "-f",
                f"permission={args.permission}",
            ]
        )

        if r.returncode == 0:
            print("✅ invited / updated")
            added += 1
        else:
            print("❌ failed")
            details = r.stderr.strip() or r.stdout.strip() or "unknown error"
            print(f"   {details}")
            failed += 1

    if args.dry_run:
        print(f"\n✅ Done (dry-run). would_add={would_add} skipped={skipped}")
    else:
        print(f"\n✅ Done. added={added} skipped={skipped} failed={failed}")

    # ---------- Optional AI-generated blurb ----------
    if args.no_ai:
        return 0

    provider = args.provider
    if provider == "auto":
        provider = "openai" if os.getenv("OPENAI_API_KEY") else "anthropic" if os.getenv("ANTHROPIC_API_KEY") else ""

    print("\n🧠 Checking for local AI keys...")
    if not provider:
        print("ℹ️  No OPENAI_API_KEY or ANTHROPIC_API_KEY found. Skipping summary.")
        return 0

    print(f"✍️  Generating short repo summary via {provider}...")
    try:
        summary = _generate_repo_summary(
            provider=provider,
            repo_full_name=repo_full_name,
            repo_description=description,
        )
    except Exception as exc:
        print(f"❌ Failed to generate summary: {exc}")
        return 0

    print("\n📣 Repo summary:\n")
    print(summary)

    if args.write_readme:
        _write_readme_summary(Path("README.md"), summary)
        print("\n📝 Wrote summary into README.md")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
