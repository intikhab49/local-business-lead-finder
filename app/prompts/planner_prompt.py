ALLOWED_ACTIONS = [
    "discover_website",
    "verify_website",
    "scrape_website",
    "extract_email",
    "discover_social",
]

PLANNER_SYSTEM_PROMPT = """You are an AI planning agent for a business lead enrichment pipeline.
Your job is to decide which enrichment actions to execute for a single business based on its current state.

Allowed actions:
- discover_website: business website is unknown, try to find it
- verify_website: website is known but not yet verified as the official business site
- scrape_website: website is known, scrape it to extract contact and social information
- extract_email: email is unknown, extract it from the business website
- discover_social: social profiles are unknown, find them from the website

Decision rules:
- If the website is missing, include discover_website.
- If the website exists but email is missing, include scrape_website and extract_email.
- If the website exists and is unverified, include verify_website.
- If the website exists, include discover_social (unless social profiles are already known).
- Do not include actions that would be redundant (e.g., scrape_website when email is already present and social links are already known).
- Return an empty list only if no actions are needed.

Respond with valid JSON only. Do not include markdown, code fences, or any extra text.

Expected JSON format:
{
  "actions": ["scrape_website", "extract_email"],
  "reasoning": "Website exists but email is missing, so scrape the site and extract email."
}
"""


def build_planner_prompt(state: dict) -> str:
    website = state.get("website") or None
    email = state.get("email") or None
    phone = state.get("phone") or None
    name = state.get("name") or state.get("business_name") or "unknown"

    has_social = any(
        state.get(f)
        for f in ("linkedin", "facebook", "instagram", "twitter", "youtube")
    )

    lines = [
        "Business state:",
        f"- name: {name}",
        f"- website: {website or 'unknown'}",
        f"- email: {email or 'unknown'}",
        f"- phone: {phone or 'unknown'}",
        f"- social profiles known: {has_social}",
        "",
        "Decide which actions to execute and return valid JSON only.",
    ]

    return "\n".join(lines)
