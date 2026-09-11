import time
from pathlib import Path

import streamlit as st
from api_client import health_check

# Palette variables injected per active theme (Streamlit 1.60 has no [data-theme] attr).
_LIGHT_VARS = """
--primary:#4F46E5;--primary-hover:#4338CA;--primary-soft:#EEF2FF;--primary-light:#C7D2FE;
--button-blue:#2563EB;--button-blue-hover:#1D4ED8;
--button-danger:#DC2626;--button-danger-hover:#B91C1C;
--secondary:#64748B;--success:#16A34A;--success-light:#DCFCE7;--warning:#D97706;--warning-light:#FEF3C7;
--danger:#DC2626;--danger-light:#FEE2E2;--info:#0EA5E9;--info-light:#E0F2FE;
--surface:#FFFFFF;--surface-2:#F8FAFC;--surface-hover:#F1F5F9;--border:#E2E8F0;
--text-primary:#0F172A;--text-secondary:#475569;--text-muted:#94A3B8;
--shadow-sm:0 1px 2px rgba(15,23,42,0.06);--shadow-md:0 6px 16px -4px rgba(15,23,42,0.12);
--shadow-lg:0 16px 32px -8px rgba(15,23,42,0.18);
"""

_DARK_VARS = """
--primary:#818CF8;--primary-hover:#A5B4FC;--primary-soft:rgba(129,140,248,0.16);--primary-light:#6366F1;
--button-blue:#2563EB;--button-blue-hover:#1D4ED8;
--button-danger:#DC2626;--button-danger-hover:#B91C1C;
--secondary:#94A3B8;--success:#4ADE80;--success-light:rgba(74,222,128,0.16);--warning:#FBBF24;--warning-light:rgba(251,191,36,0.16);
--danger:#F87171;--danger-light:rgba(248,113,113,0.16);--info:#38BDF8;--info-light:rgba(56,189,248,0.16);
--surface:#111A2E;--surface-2:#0B1220;--surface-hover:#1E293B;--border:#26344F;
--text-primary:#E2E8F0;--text-secondary:#94A3B8;--text-muted:#64748B;
--shadow-sm:0 1px 2px rgba(0,0,0,0.3);--shadow-md:0 6px 16px -4px rgba(0,0,0,0.45);
--shadow-lg:0 16px 32px -8px rgba(0,0,0,0.55);
"""


def _active_palette() -> str:
    """Return CSS variable overrides matching the current Streamlit theme."""
    try:
        theme_type = st.context.theme.type
    except Exception:
        theme_type = None
    return _DARK_VARS if theme_type == "dark" else _LIGHT_VARS


def inject_global_css() -> None:
    """Inject the global CSS from assets/style.css with theme-aware variables."""
    css_path = Path(__file__).resolve().parent / "assets" / "style.css"
    try:
        css = css_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        css = ""
    st.markdown(
        f"<style>:root{{{_active_palette()}}}{css}</style>",
        unsafe_allow_html=True,
    )


def render_sidebar_brand() -> None:
    """Render the sidebar brand/logo section."""
    with st.sidebar:
        st.markdown(
            """
            <div class="sidebar-brand">
                <div class="sidebar-brand-logo">
                    <span style="font-size:1.6rem; line-height:1;">BLF</span>
                </div>
                <div class="sidebar-brand-text">
                    <h3>Business Lead Finder</h3>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_api_status() -> dict | None:
    """Check and render API connection status in sidebar."""
    with st.sidebar:
        with st.spinner("Checking API..."):
            status = health_check()

        if status and status.get("status") == "healthy":
            st.markdown(
                "<p class='metric-good'>● API: Connected</p>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                "<p class='metric-bad'>● API: Disconnected</p>",
                unsafe_allow_html=True,
            )

        provider = status.get("provider", "N/A") if status else "N/A"
        version = status.get("version", "N/A") if status else "N/A"

        st.markdown(f"**Provider:** {provider}")
        st.markdown(f"**Version:** {version}P")
        st.divider()
        st.caption("Built with Streamlit © 2026")

    return status


def _format_duration(seconds: float) -> str:
    """Format duration in seconds to human-readable string."""
    seconds = float(seconds or 0.0)
    if seconds < 0.001:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    return f"{minutes}m {remainder:.0f}s"


def _format_dt(iso_value) -> str:
    """Format ISO datetime string to readable format."""
    if not iso_value:
        return "—"
    text = str(iso_value)
    try:
        text = text.replace("Z", "+00:00")
        return time.strftime("%Y-%m-%d %H:%M", time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return text[:19] if len(text) >= 19 else text


def _status_badge(status: str) -> str:
    """Return a formatted status badge string."""
    if status == "success":
        return "✅ Success"
    if status == "failed":
        return "❌ Failed"
    if status == "running":
        return "⏳ Running"
    if status == "interrupted":
        return "⚠️ Interrupted"
    return status.capitalize() or "Unknown"


def _business_to_lead_payload(biz: dict) -> dict:
    """Convert a business dict from search results to lead payload."""
    location = parse_address_fields(
        biz.get("formatted_address") or biz.get("address") or "",
    )
    categories = biz.get("categories") or []
    categories_str = ", ".join(categories) if isinstance(categories, list) else str(categories or "")
    return {
        "fsq_place_id": biz.get("fsq_place_id", ""),
        "business_name": biz.get("name", "") or biz.get("business_name", ""),
        "address": biz.get("formatted_address", "") or biz.get("address", ""),
        "categories": categories_str,
        "city": location.get("city"),
        "state": location.get("state"),
        "country": location.get("country"),
        "phone": biz.get("phone_number") or biz.get("phone"),
        "website": biz.get("website"),
        "email": None if biz.get("email") == "N/A" else biz.get("email"),
        "linkedin": biz.get("linkedin"),
        "facebook": biz.get("facebook"),
        "instagram": biz.get("instagram"),
        "twitter": biz.get("twitter"),
        "youtube": biz.get("youtube"),
        "latitude": biz.get("latitude"),
        "longitude": biz.get("longitude"),
        "source": "google_places",
        "confidence_score": biz.get("confidence_score", 0.0) or 0.0,
        "review_status": "",
    }


def parse_address_fields(address: str) -> dict[str, str | None]:
    """Best-effort split of a formatted address into city/state/country."""
    parts = [part.strip() for part in (address or "").split(",") if part.strip()]
    city = state = country = None
    if parts:
        country = parts[-1]
    if len(parts) >= 2:
        state = parts[-2]
    if len(parts) >= 3:
        city = parts[-3]
    if isinstance(city, str) and city.isdigit():
        city = parts[-4] if len(parts) >= 4 else None
    return {"city": city, "state": state, "country": country}
