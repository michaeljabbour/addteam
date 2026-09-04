"""AI summary generation for welcome issues and end-of-run summaries.

Supports OpenAI, Anthropic, Google, and OpenRouter; the app auto-selects a
provider based on which API keys are present in the environment.
"""

from __future__ import annotations

import json
import os

import httpx

_AI_PROVIDERS = {
    "openai": {
        "env_var": "OPENAI_API_KEY",
        "url": "https://api.openai.com/v1/responses",
        "model": "gpt-5-mini",
        "format": "responses",
    },
    "anthropic": {
        "env_var": "ANTHROPIC_API_KEY",
        "url": "https://api.anthropic.com/v1/messages",
        "model": "claude-sonnet-4-5-20250929",
        "format": "anthropic",
    },
    "google": {
        "env_var": "GOOGLE_API_KEY",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.0-flash:generateContent",
        "model": "gemini-3.0-flash",
        "format": "google",
    },
    "openrouter": {
        "env_var": "OPENROUTER_API_KEY",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "meta-llama/llama-3.1-8b-instruct:free",
        "format": "chat",
    },
}

# Order used when --provider=auto: first provider with a configured key wins.
AUTO_PROVIDER_ORDER = ("openai", "anthropic", "google", "openrouter")


def available_providers() -> list[str]:
    """Providers that have an API key set, in auto-priority order."""
    return [name for name in AUTO_PROVIDER_ORDER if os.getenv(_AI_PROVIDERS[name]["env_var"])]


def _http_post_json(url: str, *, headers: dict[str, str], payload: dict, timeout_s: int = 30) -> dict:
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout_s)
    except httpx.RequestError as exc:
        raise RuntimeError(f"Network error calling {url}: {exc}") from exc

    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"HTTP {exc.response.status_code} from {url}: {exc.response.text}") from exc

    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Non-JSON response from {url}: {resp.text[:200]}") from exc


def _ai_request(provider_cfg: dict, api_key: str, prompt: str) -> tuple[str, dict, dict]:
    """Build (url, headers, payload) for an AI provider."""
    fmt = provider_cfg["format"]
    model = provider_cfg["model"]
    url = provider_cfg["url"]

    if fmt == "responses":
        return (
            url,
            {"authorization": f"Bearer {api_key}"},
            {
                "model": model,
                "input": prompt,
                "max_output_tokens": 1000,
                "reasoning": {"effort": "low"},
                "store": False,
            },
        )
    if fmt == "chat":
        return (
            url,
            {"authorization": f"Bearer {api_key}"},
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 500,
                "temperature": 0.2,
            },
        )
    if fmt == "anthropic":
        return (
            url,
            {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            {
                "model": model,
                "max_tokens": 500,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
    # google
    return (
        f"{url}?key={api_key}",
        {"Content-Type": "application/json"},
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 500, "temperature": 0.2},
        },
    )


def _ai_extract(provider_cfg: dict, response: dict) -> str:
    """Extract the text content from an AI provider response."""
    fmt = provider_cfg["format"]
    try:
        if fmt == "responses":
            for item in response["output"]:
                if item["type"] == "message":
                    for block in item["content"]:
                        if block["type"] == "output_text":
                            return block["text"].strip()
            raise KeyError("No output_text found in response")
        if fmt == "chat":
            return response["choices"][0]["message"]["content"].strip()
        if fmt == "anthropic":
            return response["content"][0]["text"].strip()
        # google
        return response["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, AttributeError) as exc:
        raise RuntimeError(f"Unexpected {fmt} response: {response}") from exc


def _generate_repo_summary(
    *, provider: str, repo_full_name: str, repo_description: str, readme_content: str | None = None, timeout_s: int = 30
) -> str:
    """Generate an AI summary with install/usage instructions from README."""
    repo_url = f"https://github.com/{repo_full_name}"

    prompt_parts = [
        "Generate a concise, terminal-friendly onboarding summary for a GitHub repository.",
        "",
        "Audience: A developer who was just added as a collaborator",
        "Tone: Calm, friendly, practical - like a senior engineer explaining to a peer",
        "Formatting: Plain text, monospace-safe, NO emojis, NO markdown",
        "",
        f"Repo: {repo_full_name}",
        f"URL: {repo_url}",
        f"Description: {repo_description or '(none provided)'}",
    ]

    if readme_content:
        readme_excerpt = readme_content[:2500]
        if len(readme_content) > 2500:
            readme_excerpt += "\n... (truncated)"
        prompt_parts.extend(
            [
                "",
                "README content:",
                "---",
                readme_excerpt,
                "---",
            ]
        )

    prompt_parts.extend(
        [
            "",
            "Output this EXACT structure (keep the labels, fill in the content):",
            "",
            f"{repo_full_name.split('/')[-1]}",
            f"{repo_url}",
            "",
            "What this is:",
            "<2-3 lines explaining the purpose and why it exists>",
            "",
            "What it does:",
            "- <concrete capability>",
            "- <concrete capability>",
            "- <concrete capability if relevant>",
            "",
            "Getting started:",
            "<1-3 lines: install and first run commands EXACTLY as they appear in the README>",
            "<If the README has no install/run instructions, write 'See README for setup'>",
            "",
            "RULES:",
            "- ONLY use commands that appear verbatim in the README. NEVER invent commands.",
            "- If you are not certain a command exists, do NOT include it",
            "- NO emojis anywhere",
            "- NO markdown formatting (no **, no ```, no headers)",
            "- NO fluff like 'Feel free to reach out' or 'Happy coding'",
            "- NO exclamation marks except maybe one",
            "- Assume reader has zero prior context",
            "- Focus on what the user can DO, not internals",
            "- Keep total output under 20 lines",
            "",
            "Generate the summary now.",
        ]
    )

    prompt = "\n".join(prompt_parts)

    provider_cfg = _AI_PROVIDERS.get(provider)
    if not provider_cfg:
        raise RuntimeError(f"Unknown provider: {provider}")

    api_key = os.getenv(provider_cfg["env_var"])
    if not api_key:
        raise RuntimeError(f"{provider_cfg['env_var']} is not set")

    url, headers, payload = _ai_request(provider_cfg, api_key, prompt)
    response = _http_post_json(url, headers=headers, payload=payload, timeout_s=timeout_s)
    return _ai_extract(provider_cfg, response)


def generate_summary(
    *, provider: str, repo_full_name: str, repo_description: str, readme_content: str | None = None
) -> tuple[str | None, str | None, list[str]]:
    """Try providers in order; return (summary, provider_used, failure_notes).

    provider="auto" walks AUTO_PROVIDER_ORDER over providers with keys set.
    Returns (None, None, failures) when nothing succeeded.
    """
    providers_to_try = [provider] if provider != "auto" else available_providers()
    failures: list[str] = []
    for name in providers_to_try:
        try:
            summary = _generate_repo_summary(
                provider=name,
                repo_full_name=repo_full_name,
                repo_description=repo_description,
                readme_content=readme_content,
            )
            return summary, name, failures
        except RuntimeError as e:
            failures.append(f"{name} failed: {str(e)[:50]}")
            continue
    return None, None, failures
