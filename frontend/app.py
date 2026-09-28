"""Business Lead Finder — Streamlit UI.

Searching no longer blocks the page: the backend runs the crawl as a job and
this UI polls it, so you see cells complete, leads arrive, and the rate limit
adapt in real time. Nothing is written to the database unless the run's save
mode says so — that choice is made in the form, before the search starts.
"""
import time

import pandas as pd
import streamlit as st
from _runtime import (
    _format_duration,
    _status_badge,
    inject_global_css,
    render_sidebar_brand,
)
from api_client import (
    cancel_discovery,
    delete_leads,
    delete_run_history,
    exclude_business,
    export_csv,
    export_excel,
    export_history_csv,
    export_history_excel,
    get_discovery_job,
    get_discovery_results,
    get_lead_stats,
    get_run_history,
    get_run_history_stats,
    get_settings,
    health_check,
    list_discovery_jobs,
    list_exclusions,
    list_lead_batches,
    list_leads,
    list_providers,
    list_resumable_runs,
    list_run_history,
    save_discovery_leads,
    start_discovery,
    test_api_key,
    unexclude_business,
    update_settings,
)

st.set_page_config(
    page_title="Business Lead Finder",
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="expanded",
)

PAGES = {
    "Find leads": "🔍",
    "Saved leads": "📇",
    "History": "🕘",
    "Settings": "⚙️",
}

SOURCE_LABELS = {
    "google_places": "Google Places — best data: phones, websites, ratings. Needs a Google key with billing on.",
    "geoapify": "Geoapify — free key, no card needed. Fewer phone numbers than Google.",
    "osm": "OpenStreetMap — no key at all. Slowest, and the thinnest data.",
}

KEY_HELP = {
    "google_places": (
        "1. Open console.cloud.google.com and create a project.\n"
        "2. Billing → link a billing account (Google gives a free monthly allowance).\n"
        "3. APIs & Services → Library → enable **Places API (New)**.\n"
        "4. APIs & Services → Credentials → Create credentials → API key. Paste it here."
    ),
    "geoapify": (
        "1. Sign up free at myprojects.geoapify.com — no card needed.\n"
        "2. Create a project.\n"
        "3. Copy its API key and paste it here."
    ),
}

PROVIDER_LABELS = {
    "google_places": "Google Places (best data, 60/query cap)",
    "osm": "OpenStreetMap (free, no key, uncapped)",
    "foursquare": "Foursquare",
    "geoapify": "Geoapify",
}

POLL_SECONDS = 1.5

# Authorship (the backend's copy is app/core/provenance.py).
BUILT_BY = "Built by Intikhab Azam · intikhabhunzai@gmail.com"


# ── shared helpers ────────────────────────────────────────────────

@st.cache_data(ttl=15, show_spinner=False)
def cached_health() -> dict | None:
    return health_check()


@st.cache_data(ttl=60, show_spinner=False)
def cached_providers() -> dict:
    try:
        return list_providers()
    except Exception:
        return {}


def state(key: str, default=None):
    if key not in st.session_state:
        st.session_state[key] = default
    return st.session_state[key]


def toast_error(exc: Exception) -> None:
    st.error(str(exc), icon="⚠️")


def results_frame(leads: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "Save": True,
            "Name": lead.get("name") or lead.get("business_name") or "",
            "Phone": lead.get("phone_number") or lead.get("phone") or "",
            "Email": lead.get("email") or "",
            "Website": lead.get("website") or "",
            "Address": lead.get("formatted_address") or lead.get("address") or "",
            "Category": ", ".join((lead.get("categories") or [])[:2])
            if isinstance(lead.get("categories"), list)
            else (lead.get("categories") or ""),
            "id": lead.get("fsq_place_id", ""),
        }
        for lead in leads
    ])


def render_sidebar() -> str:
    inject_global_css()
    render_sidebar_brand()
    with st.sidebar:
        page = st.radio(
            "Navigation",
            options=list(PAGES),
            format_func=lambda key: f"{PAGES[key]}  {key}",
            label_visibility="collapsed",
            key="nav",
        )
        st.divider()

        status = cached_health()
        healthy = bool(status and status.get("status") == "healthy")
        st.markdown(
            f"<p class='{'metric-good' if healthy else 'metric-bad'}'>"
            f"● API {'connected' if healthy else 'unreachable'}</p>",
            unsafe_allow_html=True,
        )
        if not healthy:
            st.caption("Start the backend: `uvicorn app.main:app --port 8000`")

        providers = cached_providers()
        if providers:
            st.caption(f"Primary: **{providers.get('primary', '—')}**")
            st.caption(f"Fallback: **{providers.get('fallback') or 'off'}**")

        stats = None
        try:
            stats = get_lead_stats()
        except Exception:
            pass
        if stats:
            st.divider()
            st.caption("Database")
            st.markdown(
                f"**{stats.get('total', 0):,}** leads · "
                f"**{stats.get('with_email', 0):,}** with e-mail"
            )

        st.markdown(f"<div class='built-by'>{BUILT_BY}</div>", unsafe_allow_html=True)
    return page


# ── Find leads ────────────────────────────────────────────────────

def render_search_form() -> None:
    providers = cached_providers()
    available = providers.get("available", {}) if providers else {}
    usable = [name for name, ok in available.items() if ok] or ["osm"]

    with st.form("discovery_form"):
        top = st.columns([2, 2, 1])
        location = top[0].text_input(
            "Location", placeholder="Chicago, IL — or 41.8781,-87.6298",
            help="A city, an address, or raw coordinates.",
        )
        category = top[1].text_input(
            "Business type", placeholder="dentist, bakery, plumber",
        )
        max_results = top[2].number_input(
            "Max leads", min_value=10, max_value=5000, value=300, step=50,
        )

        mid = st.columns([1, 1, 1, 1])
        radius_km = mid[0].slider("Search radius (km)", 0.5, 50.0, 8.0, step=0.5)
        grid_size = mid[1].select_slider(
            "Grid detail", options=[1, 2, 3, 4, 5, 6], value=3,
            help="The area is split into N×N cells, each searched separately. "
                 "Higher = more coverage and more leads, but more API calls.",
        )
        provider = mid[2].selectbox(
            "Provider", options=usable,
            index=usable.index(providers.get("primary")) if providers.get("primary") in usable else 0,
            format_func=lambda name: PROVIDER_LABELS.get(name, name),
        )
        fallback_on = mid[3].toggle(
            "Free fallback", value=True,
            help="If the main provider errors, is rate-limited, or returns nothing "
                 "for a cell, retry that cell on OpenStreetMap (free, no key).",
        )

        st.markdown("**When results come in**")
        save_mode = st.radio(
            "Save mode",
            options=["auto", "review"],
            index=0,
            horizontal=True,
            label_visibility="collapsed",
            format_func=lambda key: (
                "Save to database as they arrive — resumable if interrupted"
                if key == "auto"
                else "Hold for review — nothing is saved until I pick"
            ),
        )

        opts = st.columns(3)
        exclude_saved = opts[0].checkbox(
            "Skip leads I already have", value=True,
            help="Filters out every place already in Saved leads or Excluded.",
        )
        auto_enrich = opts[1].checkbox(
            "Scrape websites for e-mail", value=True,
            help="Adds e-mail and social links where the website exposes them. "
                 "Adds time at the end of the run.",
        )
        concurrency = opts[2].slider(
            "Parallel cells", 1, 12, providers.get("concurrency", 4),
            help="How many grid cells are searched at once. The rate limiter "
                 "slows itself automatically if the provider pushes back.",
        )

        submitted = st.form_submit_button("Start search", type="primary", width="stretch")

    if not submitted:
        return
    if not location.strip() or not category.strip():
        st.warning("Location and business type are both required.")
        return

    try:
        job = start_discovery(
            location=location.strip(),
            business_category=category.strip(),
            radius=int(radius_km * 1000),
            max_results=int(max_results),
            grid_size=int(grid_size),
            exclude_saved=exclude_saved,
            auto_save=(save_mode == "auto"),
            auto_enrich=auto_enrich,
            provider=provider,
            fallback_provider=None if fallback_on else "none",
            concurrency=int(concurrency),
        )
    except Exception as exc:  # noqa: BLE001
        toast_error(exc)
        return

    st.session_state["job_id"] = job["job_id"]
    st.rerun()


def render_job_panel(job_id: str) -> None:
    try:
        job = get_discovery_job(job_id)
    except Exception as exc:  # noqa: BLE001
        st.info("That search is no longer available (the backend restarted).")
        st.session_state.pop("job_id", None)
        return

    running = job["status"] in ("queued", "running")
    header = st.columns([3, 1])
    with header[0]:
        label = {
            "queued": "Starting…", "running": "Searching",
            "completed": "Search complete", "cancelled": "Stopped",
            "failed": "Search failed",
        }.get(job["status"], job["status"])
        st.subheader(label)
        st.caption(job.get("message", ""))
    with header[1]:
        if running:
            if st.button("Stop", width="stretch"):
                try:
                    cancel_discovery(job_id)
                except Exception as exc:  # noqa: BLE001
                    toast_error(exc)
                st.rerun()
        else:
            if st.button("New search", type="primary", width="stretch"):
                st.session_state.pop("job_id", None)
                st.rerun()

    searched = job["cells_done"] + job.get("cells_skipped", 0)
    stopped = job.get("cells_stopped", 0)
    caption = f"{searched}/{job['cells_total']} areas searched"
    if job.get("cells_skipped"):
        caption += f" · {job['cells_skipped']} already done before"
    if stopped:
        caption += (
            f" · {stopped} not needed (enough leads found)"
            if job["status"] != "cancelled"
            else f" · {stopped} skipped (stopped early)"
        )
    if job.get("phase") == "enriching":
        caption += " · now scraping websites"
    st.progress(min(1.0, float(job.get("progress", 0.0))), text=caption)

    cols = st.columns(5)
    cols[0].metric("Leads found", job["found"])
    cols[1].metric(
        "Saved" if job.get("auto_save") else "Held for review",
        job["saved"] if job.get("auto_save") else job["found"],
    )
    cols[2].metric("Duplicates skipped", job["duplicates_skipped"])
    cols[3].metric("Enriched", job["enriched"])
    cols[4].metric("Elapsed", _format_duration(job.get("elapsed", 0)))

    if job.get("phase") == "enriching":
        st.caption(
            "Searching is done — every site found is being opened to look for "
            "an e-mail address. Press Stop to keep what you have."
        )

    rate = job.get("rate_limit") or {}
    if rate:
        pace = (
            f"Pacing {rate.get('interval', 0):.2f}s between calls "
            f"({rate.get('requests', 0)} calls, {rate.get('throttled', 0)} throttled)"
        )
        if job.get("fallback_provider"):
            pace += f" · fallback: {job['fallback_provider']}"
        st.caption(pace)

    if job.get("errors"):
        with st.expander(f"{len(job['errors'])} cell issue(s) — the run continued"):
            for line in job["errors"]:
                st.write(f"• {line}")

    if job.get("cell_log"):
        with st.expander("Per-area detail"):
            st.dataframe(
                pd.DataFrame(job["cell_log"]).rename(columns={
                    "index": "Area", "status": "Result", "provider": "Provider",
                    "found": "Found", "new": "New", "error": "Error",
                }),
                width="stretch", hide_index=True,
            )

    render_job_results(job)

    if running:
        time.sleep(POLL_SECONDS)
        st.rerun()


def render_job_results(job: dict) -> None:
    job_id = job["job_id"]
    try:
        payload = get_discovery_results(job_id, limit=2000)
    except Exception as exc:  # noqa: BLE001
        toast_error(exc)
        return

    leads = payload.get("items", [])
    if not leads:
        if not job["status"] in ("queued", "running"):
            st.info("No leads found. Try a wider radius, a higher grid detail, "
                    "or turn off “Skip leads I already have”.")
        return

    st.divider()
    if job.get("auto_save"):
        st.caption(f"{job['saved']} of these are already in your database.")
    else:
        st.caption("Nothing here is saved yet — tick the rows you want and save below.")

    frame = results_frame(leads)
    edited = st.data_editor(
        frame,
        width="stretch",
        hide_index=True,
        height=420,
        column_config={
            "Save": st.column_config.CheckboxColumn("✓", width="small"),
            "Website": st.column_config.LinkColumn("Website", display_text="visit"),
            "id": None,
        },
        key=f"results_{job_id}",
    )
    selected = edited[edited["Save"]]["id"].tolist()

    actions = st.columns([1, 1, 1, 2])
    save_label = (
        f"Save {len(selected)} selected" if not job.get("auto_save")
        else f"Re-save {len(selected)} selected"
    )
    if actions[0].button(save_label, type="primary", disabled=not selected, width="stretch"):
        try:
            result = save_discovery_leads(job_id, selected)
            st.success(f"Saved {result.get('saved', 0)} leads.")
        except Exception as exc:  # noqa: BLE001
            toast_error(exc)

    if actions[1].button(
        f"Exclude {len(selected)}", disabled=not selected, width="stretch",
        help="Never show these places in future searches.",
    ):
        failures = 0
        for place_id in selected:
            try:
                exclude_business(place_id, "Excluded from results")
            except Exception:  # noqa: BLE001
                failures += 1
        st.success(f"Excluded {len(selected) - failures} places.")

    csv = frame.drop(columns=["Save"]).to_csv(index=False).encode("utf-8")
    actions[2].download_button(
        "Download CSV", csv,
        file_name=f"leads_{job_id}.csv", mime="text/csv", width="stretch",
    )


def render_find_leads() -> None:
    st.title("Find leads")
    job_id = st.session_state.get("job_id")
    if job_id:
        render_job_panel(job_id)
        return

    render_search_form()
    render_resume_panel()
    render_recent_jobs()


def render_resume_panel() -> None:
    try:
        runs = list_resumable_runs().get("items", [])
    except Exception:  # noqa: BLE001
        return
    if not runs:
        return

    with st.expander(f"Resume an interrupted run ({len(runs)})"):
        st.caption(
            "These runs stopped before every area was searched. Resuming skips "
            "the areas already covered and keeps the leads already saved."
        )
        for run in runs[:10]:
            cols = st.columns([3, 1, 1])
            cols[0].markdown(
                f"**Run #{run['run_id']}** — {run.get('business_category') or '?'} "
                f"in {run.get('location') or '?'} · "
                f"{run['cells_done']}/{run['cells_planned']} areas · "
                f"{run.get('found', 0)} leads"
            )
            cols[1].caption(str(run.get("status", "")))
            if cols[2].button("Resume", key=f"resume_{run['run_id']}", width="stretch"):
                params = run.get("params") or {}
                try:
                    job = start_discovery(
                        location=run.get("location", ""),
                        business_category=run.get("business_category", ""),
                        radius=params.get("radius", 8000),
                        max_results=params.get("max_results", 300),
                        grid_size=params.get("grid_size", 3),
                        resume_run_id=run["run_id"],
                    )
                    st.session_state["job_id"] = job["job_id"]
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    toast_error(exc)


def render_recent_jobs() -> None:
    try:
        jobs = list_discovery_jobs().get("items", [])
    except Exception:  # noqa: BLE001
        return
    if not jobs:
        return
    with st.expander(f"Searches this session ({len(jobs)})"):
        for job in jobs[:10]:
            cols = st.columns([3, 1, 1])
            params = job.get("params", {})
            cols[0].markdown(
                f"**{params.get('business_category', '?')}** in "
                f"{params.get('location', '?')} — {job['found']} leads"
            )
            cols[1].caption(job["status"])
            if cols[2].button("Open", key=f"open_{job['job_id']}", width="stretch"):
                st.session_state["job_id"] = job["job_id"]
                st.rerun()


# ── Saved leads ───────────────────────────────────────────────────

def render_saved_leads() -> None:
    st.title("Saved leads")

    filters = st.columns([2, 1, 1, 1])
    search_text = filters[0].text_input("Search", placeholder="Name, e-mail, phone…")
    status_filter = filters[1].selectbox(
        "Status", ["", "discovered", "approved", "rejected", "edited"],
        format_func=lambda key: key.capitalize() if key else "Any status",
    )
    page_size = filters[2].selectbox("Per page", [25, 50, 100, 200], index=1)

    batch_options = {"": "All searches"}
    try:
        for batch in (list_lead_batches().get("items") or []):
            location = batch.get("location") or ""
            category = batch.get("business_category") or ""
            suffix = " — ".join(part for part in (location, category) if part)
            batch_options[str(batch["run_id"])] = (
                f"Run #{batch['run_id']}"
                + (f" · {suffix}" if suffix else "")
                + f" ({batch.get('lead_count', 0)})"
            )
    except Exception:  # noqa: BLE001
        pass
    selected_batch = filters[3].selectbox(
        "Search run", list(batch_options), format_func=lambda key: batch_options[key]
    )
    run_id = int(selected_batch) if selected_batch else None

    page_key = f"page_{search_text}_{status_filter}_{selected_batch}"
    page = state(page_key, 0)

    try:
        data = list_leads(
            search=search_text, review_status=status_filter, run_id=run_id,
            limit=page_size, offset=page * page_size,
        )
    except Exception as exc:  # noqa: BLE001
        toast_error(exc)
        return

    items = data.get("items", [])
    total = data.get("total", 0)
    if not items:
        st.info("Nothing here yet. Run a search — results land here when a run "
                "saves them.")
        return

    frame = pd.DataFrame([
        {
            "Name": lead.get("business_name", ""),
            "Phone": lead.get("phone") or "",
            "E-mail": lead.get("email") or "",
            "Website": lead.get("website") or "",
            "City": lead.get("city") or "",
            "Category": lead.get("categories") or "",
            "Status": lead.get("review_status", ""),
            "Added": str(lead.get("created_at", ""))[:10],
        }
        for lead in items
    ])

    pages = max(1, -(-total // page_size))
    st.caption(f"{total:,} leads · page {page + 1} of {pages}")
    st.dataframe(
        frame, width="stretch", hide_index=True, height=460,
        column_config={
            "Website": st.column_config.LinkColumn("Website", display_text="visit"),
        },
    )

    nav = st.columns([1, 1, 4, 1, 1, 1])
    if nav[0].button("← Prev", disabled=page == 0, width="stretch"):
        st.session_state[page_key] = page - 1
        st.rerun()
    if nav[1].button("Next →", disabled=page + 1 >= pages, width="stretch"):
        st.session_state[page_key] = page + 1
        st.rerun()

    try:
        filename, content = export_csv(search_text, status_filter, run_id)
        nav[3].download_button("CSV", content, filename, "text/csv", width="stretch")
        filename, blob = export_excel(search_text, status_filter, run_id)
        nav[4].download_button(
            "Excel", blob, filename,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )
    except Exception:  # noqa: BLE001
        nav[3].caption("Export unavailable")

    with nav[5].popover("Delete"):
        st.caption(f"Delete all {total:,} leads matching the current filters?")
        if st.button("Yes, delete them", type="primary"):
            try:
                deleted = delete_leads(search_text, status_filter, run_id)
                st.success(f"Deleted {deleted} leads.")
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                toast_error(exc)

    render_exclusions()


def render_exclusions() -> None:
    st.divider()
    try:
        exclusions = list_exclusions().get("exclusions") or []
    except Exception:  # noqa: BLE001
        return
    with st.expander(f"Excluded places ({len(exclusions)})"):
        if not exclusions:
            st.caption("None. Exclude a place from a search result to keep it "
                       "out of future runs.")
            return
        for place_id in exclusions[:100]:
            cols = st.columns([5, 1])
            cols[0].code(place_id, language=None)
            if cols[1].button("Restore", key=f"unex_{place_id}", width="stretch"):
                unexclude_business(place_id)
                st.rerun()


# ── History ───────────────────────────────────────────────────────

RUN_TYPES = ["", "discovery", "search", "agent_search", "enrichment"]
RUN_STATUSES = ["", "success", "running", "failed", "cancelled", "interrupted"]


def history_page() -> None:
    st.title("History")

    try:
        stats = get_run_history_stats()
    except Exception:  # noqa: BLE001
        stats = {}
    if stats:
        cols = st.columns(4)
        cols[0].metric("Runs", stats.get("total_runs", 0))
        cols[1].metric("Successful", stats.get("successful", 0))
        cols[2].metric("Failed", stats.get("failed", 0))
        cols[3].metric("Avg duration", _format_duration(stats.get("avg_duration", 0)))

    filters = st.columns([2, 1, 1, 1])
    search_text = filters[0].text_input(
        "Search", key="history_search", placeholder="Location or category…"
    )
    run_type = filters[1].selectbox(
        "Type", RUN_TYPES, key="history_type",
        format_func=lambda key: key.replace("_", " ").capitalize() if key else "Any type",
    )
    run_status = filters[2].selectbox(
        "Status", RUN_STATUSES, key="history_status",
        format_func=lambda key: key.capitalize() if key else "Any status",
    )
    page_size = filters[3].selectbox("Per page", [25, 50, 100], key="history_page_size")

    offset = state("history_offset", 0)

    try:
        data = list_run_history(
            search=search_text, run_type=run_type, status=run_status,
            limit=page_size, offset=offset,
        )
    except Exception as exc:  # noqa: BLE001
        toast_error(exc)
        return

    runs = data.get("items", [])
    total = data.get("total", 0)
    st.markdown(f"**{total}** run(s) found.")

    exports = st.columns([1, 1, 1, 3])
    try:
        filename, blob = export_history_csv(
            search=search_text, run_type=run_type, status=run_status
        )
        exports[0].download_button("CSV", blob, filename, "text/csv", width="stretch")
        filename, blob = export_history_excel(
            search=search_text, run_type=run_type, status=run_status
        )
        exports[1].download_button(
            "Excel", blob, filename,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )
    except Exception:  # noqa: BLE001
        exports[0].caption("Export unavailable")
    if exports[2].button("Refresh", key="history_refresh", width="stretch"):
        st.rerun()

    if not runs:
        st.info("No runs match these filters yet.")
        return

    frame = pd.DataFrame([
        {
            "Run": run.get("id"),
            "Type": run.get("run_type", ""),
            "Location": run.get("location", ""),
            "Category": run.get("business_category", ""),
            "Status": _status_badge(run.get("status", "")),
            "Leads": run.get("total_results", 0),
            "Duration": _format_duration(run.get("processing_time", 0)),
            "Started": str(run.get("started_at", ""))[:16].replace("T", " "),
        }
        for run in runs
    ])
    st.dataframe(frame, width="stretch", hide_index=True, height=420)

    run_ids = [run.get("id") for run in runs]
    actions = st.columns([2, 1, 1, 1, 1])
    selected = actions[0].selectbox(
        "Run", run_ids, key="history_run", format_func=lambda run_id: f"Run #{run_id}"
    )
    if actions[1].button("View", key="history_view", width="stretch"):
        try:
            st.json(get_run_history(selected))
        except Exception as exc:  # noqa: BLE001
            toast_error(exc)
    if actions[2].button("Delete", key="history_delete", width="stretch"):
        try:
            delete_run_history(selected)
            st.success(f"Deleted run #{selected}.")
        except Exception as exc:  # noqa: BLE001
            toast_error(exc)

    pages = max(1, -(-total // page_size))
    page_number = offset // page_size + 1
    if actions[3].button(
        "← Prev", key="history_prev", disabled=offset == 0, width="stretch"
    ):
        st.session_state["history_offset"] = max(0, offset - page_size)
        st.rerun()
    if actions[4].button(
        "Next →", key="history_next", disabled=page_number >= pages, width="stretch"
    ):
        st.session_state["history_offset"] = offset + page_size
        st.rerun()
    st.caption(f"Page {page_number} of {pages}")


# ── Settings ──────────────────────────────────────────────────────

def show_key_test(provider: str, api_key: str | None) -> None:
    try:
        with st.spinner("Checking the key…"):
            result = test_api_key(provider, api_key)
    except Exception as exc:  # noqa: BLE001
        toast_error(exc)
        return
    if result["ok"]:
        st.success(result["message"], icon="✅")
    else:
        st.error(result["message"], icon="⚠️")


def render_key_field(provider: str, label: str, saved: dict) -> str:
    """A masked input for one key with its own Test button; returns what was typed."""
    cols = st.columns([4, 1], vertical_alignment="bottom")
    typed = cols[0].text_input(
        label,
        type="password",
        # A new key after each save gives an empty box showing the saved hint.
        key=f"key_{provider}_{state('key_version', 0)}",
        placeholder=(
            f"Saved ({saved['hint']}) — leave blank to keep it"
            if saved.get("set") else "Paste your key here"
        ),
    )
    if cols[1].button("Test key", key=f"test_{provider}", width="stretch"):
        show_key_test(provider, typed.strip() or None)
    with st.expander(f"How do I get a {label.removesuffix(' API key')} key?"):
        st.markdown(KEY_HELP[provider])
    return typed.strip()


def render_settings() -> None:
    st.title("Settings")
    notice = st.session_state.pop("settings_notice", None)
    if notice:
        st.success(notice, icon="✅")
        if st.session_state.get("saved_source") in KEY_HELP:
            show_key_test(st.session_state["saved_source"], None)

    try:
        current = get_settings()
    except Exception as exc:  # noqa: BLE001
        toast_error(exc)
        return
    keys = current["keys"]

    st.subheader("Where to search")
    options = list(SOURCE_LABELS)
    source = st.radio(
        "Data source",
        options=options,
        index=options.index(current["provider_name"]) if current["provider_name"] in options else 0,
        format_func=SOURCE_LABELS.get,
        label_visibility="collapsed",
    )

    st.subheader("API keys")
    st.caption("Keys are stored only on this computer, in the .env file next to the app.")
    google_key = render_key_field("google_places", "Google Places API key", keys["google_places"])
    geoapify_key = render_key_field("geoapify", "Geoapify API key", keys["geoapify"])

    if not st.button("Save", type="primary"):
        return
    fields = {"provider_name": source}
    if google_key:
        fields["google_places_api_key"] = google_key
    if geoapify_key:
        fields["geoapify_api_key"] = geoapify_key
    try:
        update_settings(**fields)
    except Exception as exc:  # noqa: BLE001
        toast_error(exc)
        return

    cached_providers.clear()
    st.session_state["key_version"] = state("key_version", 0) + 1
    st.session_state["saved_source"] = source
    st.session_state["settings_notice"] = (
        f"Saved. New searches use {SOURCE_LABELS[source].split(' —')[0]}."
    )
    st.rerun()


# ── main ──────────────────────────────────────────────────────────

def main() -> None:
    page = render_sidebar()
    if page == "Find leads":
        render_find_leads()
    elif page == "Saved leads":
        render_saved_leads()
    elif page == "Settings":
        render_settings()
    else:
        history_page()


if __name__ == "__main__":
    main()
