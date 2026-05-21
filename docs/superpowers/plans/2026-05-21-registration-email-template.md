# Registration Email Template Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign `registration_html()` in `app/services/email_service.py` with a bold editorial layout — dark hero header, cream body, stats row, dark footer.

**Architecture:** Single function update. All styles inline for email-client compatibility. No new files, no external dependencies.

**Tech Stack:** Python f-string HTML template, inline CSS only.

---

### Task 1: Write failing tests for the new template structure

**Files:**
- Create: `tests/services/test_email_service.py`

- [ ] **Step 1: Write the failing tests**

```python
from app.services.email_service import registration_html


def test_registration_html_contains_name():
    html = registration_html("Ada")
    assert "Ada" in html


def test_registration_html_hero_section():
    html = registration_html("Ada")
    assert "YOU'RE IN" in html
    assert "Make your" in html
    assert "sound." in html


def test_registration_html_cta():
    html = registration_html("Ada")
    assert "Browse Loops" in html
    assert "https://litmusic.app" in html


def test_registration_html_stats():
    html = registration_html("Ada")
    assert "500+" in html
    assert "Loops" in html
    assert "Genres" in html
    assert "Subscriptions" in html


def test_registration_html_footer():
    html = registration_html("Ada")
    assert "LM" in html
    assert "LITMUSIC" in html
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
source .venv/bin/activate && python -m pytest tests/services/test_email_service.py -v
```

Expected: 3–5 FAILs (current template doesn't have the new copy/structure).

---

### Task 2: Implement the redesigned template

**Files:**
- Modify: `app/services/email_service.py` — replace `registration_html()` body

- [ ] **Step 1: Replace `registration_html()` with the new template**

Replace the entire `registration_html` function (lines 41–56) with:

```python
def registration_html(full_name: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#e8e3d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#e8e3d9;">
  <tr><td align="center" style="padding:32px 16px;">
    <table width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;">

      <!-- HEADER -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 0 32px;border-radius:8px 8px 0 0;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="color:#fff;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;">LITMUSIC</td>
            <td align="right" style="color:#555;font-size:11px;letter-spacing:0.08em;text-transform:uppercase;">NO. 0001 / WELCOME</td>
          </tr>
        </table>
      </td></tr>

      <!-- HERO -->
      <tr><td style="background:#0a0a0a;padding:32px 32px 8px 32px;">
        <p style="margin:0 0 16px 0;color:#6c3bdb;font-size:12px;font-weight:800;letter-spacing:0.15em;text-transform:uppercase;">YOU'RE IN.</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#fff;letter-spacing:-0.02em;">Make your</p>
        <p style="margin:0;font-size:52px;font-weight:800;line-height:1.05;color:#6c3bdb;font-style:italic;letter-spacing:-0.02em;">sound.</p>
      </td></tr>

      <!-- META STRIP -->
      <tr><td style="background:#0a0a0a;padding:24px 32px 28px 32px;border-bottom:1px solid #1f1f1f;">
        <p style="margin:0;color:#444;font-size:11px;letter-spacing:0.12em;text-transform:uppercase;">EST. 2024 &nbsp;&middot;&nbsp; PREMIUM LOOPS &nbsp;&middot;&nbsp; STEM PACKS</p>
      </td></tr>

      <!-- BODY -->
      <tr><td style="background:#f2ede4;padding:36px 32px 28px 32px;">
        <p style="margin:0 0 8px 0;font-size:17px;font-weight:700;color:#0a0a0a;">Hi {full_name},</p>
        <p style="margin:0 0 28px 0;font-size:15px;line-height:1.6;color:#333;">
          Your LitMusic account is ready. Browse premium loops and stem packs built for serious producers — no subscriptions, just the sounds you need.
        </p>
        <a href="https://litmusic.app"
           style="display:inline-block;padding:14px 28px;background:#0a0a0a;color:#fff;
                  font-size:14px;font-weight:700;text-decoration:none;border-radius:4px;
                  letter-spacing:0.02em;">
          Browse Loops &rarr;
        </a>
      </td></tr>

      <!-- STATS -->
      <tr><td style="background:#f2ede4;padding:28px 32px;border-top:1px solid #ddd8ce;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="text-align:left;">
              <p style="margin:0;font-size:28px;font-weight:800;color:#0a0a0a;">500+</p>
              <p style="margin:4px 0 0 0;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;color:#888;">Loops</p>
            </td>
            <td style="text-align:center;">
              <p style="margin:0;font-size:28px;font-weight:800;color:#0a0a0a;">10</p>
              <p style="margin:4px 0 0 0;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;color:#888;">Genres</p>
            </td>
            <td style="text-align:right;">
              <p style="margin:0;font-size:28px;font-weight:800;color:#0a0a0a;">0</p>
              <p style="margin:4px 0 0 0;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;color:#888;">Subscriptions</p>
            </td>
          </tr>
        </table>
      </td></tr>

      <!-- FOOTER -->
      <tr><td style="background:#0a0a0a;padding:24px 32px;border-radius:0 0 8px 8px;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="width:36px;vertical-align:middle;">
              <div style="width:32px;height:32px;background:#6c3bdb;border-radius:4px;
                          text-align:center;line-height:32px;color:#fff;
                          font-size:12px;font-weight:800;letter-spacing:0.05em;">LM</div>
            </td>
            <td style="padding-left:12px;vertical-align:middle;">
              <p style="margin:0;font-size:11px;font-weight:700;color:#fff;letter-spacing:0.1em;text-transform:uppercase;">LITMUSIC</p>
              <p style="margin:2px 0 0 0;font-size:11px;color:#555;">Professional Music Production Samples</p>
            </td>
          </tr>
        </table>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""
```

- [ ] **Step 2: Run the tests**

```bash
source .venv/bin/activate && python -m pytest tests/services/test_email_service.py -v
```

Expected: all 5 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add app/services/email_service.py tests/services/test_email_service.py
git commit -m "feat: redesign registration email template with bold editorial layout"
```
