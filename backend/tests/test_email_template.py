from datetime import datetime

from app.models.lead import Lead
from app.services.email_template import render_email_html


def make_lead(**overrides) -> Lead:
    defaults = dict(
        created_time=datetime.utcnow(),
        full_name="Test Lead",
        email="test.lead@example.com",
    )
    defaults.update(overrides)
    return Lead(**defaults)


def test_rendered_email_contains_real_unsubscribe_link_not_placeholder():
    lead = make_lead()
    html = render_email_html(lead, "Subject", "<p>Hi there</p>")
    assert "{unsubscribe_link}" not in html
    assert f"lead_id={lead.id}" in html
    assert "Unsubscribe" in html


def test_rendered_email_includes_inner_content_verbatim():
    lead = make_lead()
    html = render_email_html(lead, "Subject", "<p>A specific unique sentence.</p>")
    assert "<p>A specific unique sentence.</p>" in html


def test_email_always_links_to_the_real_site_instead_of_a_featured_project():
    """
    Replaces the old per-lead "featured project" showcase (matched by ad
    campaign category) -- staff asked for a plain, fixed mention of the
    real site instead. Must be present regardless of campaign/lead data.
    """
    lead = make_lead(company="Acme Industrial")
    html = render_email_html(lead, "Subject", "<p>Hi</p>")
    assert "Work we're proud of" not in html
    assert "vrtvega.com" in html
    assert "Book a strategy call" in html
    assert "vrtvega.com/talk-to-rajesh" in html


def test_email_site_links_route_through_click_tracking_on_a_real_send():
    """A real click on either link should log as engagement (see
    app/services/click_tracking.py) before reaching the real page."""
    lead = make_lead()
    html = render_email_html(lead, "Subject", "<p>Hi</p>")
    assert f"/public/click?lead_id={lead.id}&target=website" in html
    assert f"/public/click?lead_id={lead.id}&target=get_started" in html


def test_default_unsubscribe_is_a_real_clickable_link():
    """What actually gets sent (§8) must have a working unsubscribe link."""
    lead = make_lead()
    html = render_email_html(lead, "Subject", "<p>Hi</p>")
    assert f'<a href="http://localhost:8000/public/unsubscribe?lead_id={lead.id}' in html


def test_preview_unsubscribe_is_inert_not_a_real_link():
    """
    Regression test: the staff-facing draft preview used to render the same
    real, working unsubscribe link as an actual sent email -- so a staff
    member just looking at a draft that was never sent could click it and
    really unsubscribe the lead. Preview must render the same-looking text
    with no navigable href.
    """
    lead = make_lead()
    html = render_email_html(lead, "Subject", "<p>Hi</p>", interactive_unsubscribe=False)
    assert "Unsubscribe" in html
    assert "/public/unsubscribe" not in html


def test_preview_site_links_skip_click_tracking_too():
    """
    Same bug class as the unsubscribe one above: a staff member clicking
    "Get started" out of curiosity while just looking at a draft must never
    falsely log as the *lead's* engagement. Preview points straight at the
    real URLs, bypassing /public/click entirely.
    """
    lead = make_lead()
    html = render_email_html(lead, "Subject", "<p>Hi</p>", interactive_unsubscribe=False)
    assert "/public/click" not in html
    assert 'href="https://www.vrtvega.com/"' in html
    assert 'href="https://www.vrtvega.com/talk-to-rajesh"' in html
