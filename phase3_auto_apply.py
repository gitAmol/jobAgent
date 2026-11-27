import asyncio
import json
import sqlite3
import re
from playwright.async_api import async_playwright, Page

# Load applicant profile
with open("applicant_profile.json", "r", encoding="utf-8") as f:
    PROFILE = json.load(f)

# Field mapping patterns for common form fields
FIELD_PATTERNS = {
    "first_name": ["first.?name", "fname", "given.?name"],
    "last_name": ["last.?name", "lname", "surname", "family.?name"],
    "full_name": ["full.?name", "name", "candidate.?name"],
    "email": ["email", "e-mail"],
    "phone": ["phone", "mobile", "tel", "contact.?number"],
    "city": ["city", "location"],
    "country": ["country"],
    "linkedin": ["linkedin"],
    "github": ["github", "portfolio"],
    "experience_years": ["experience", "years.?of.?exp", "total.?exp"],
    "current_company": ["current.?company", "employer", "organization"],
    "current_title": ["current.?title", "job.?title", "designation"],
    "notice_period": ["notice.?period", "availability", "start.?date"],
    "current_ctc": ["current.?ctc", "current.?salary", "present.?salary"],
    "expected_ctc": ["expected.?ctc", "expected.?salary", "desired.?salary"],
    "cover_letter": ["cover.?letter", "message", "why.?apply", "motivation"],
}


def get_db_connection():
    return sqlite3.connect("state.db")


def get_queued_jobs():
    conn = get_db_connection()
    cursor = conn.execute(
        "SELECT url, title, company FROM jobs WHERE status = 'QUEUED_FOR_APPLY'"
    )
    jobs = cursor.fetchall()
    conn.close()
    return jobs


def update_job_status(url: str, status: str, notes: str = ""):
    conn = get_db_connection()
    conn.execute(
        "UPDATE jobs SET status = ? WHERE url = ?",
        (status, url)
    )
    conn.commit()
    conn.close()


def match_field(field_name: str, label_text: str) -> bool:
    """Check if label text matches any pattern for the field."""
    label_lower = label_text.lower().replace(" ", "").replace("_", "").replace("-", "")
    for pattern in FIELD_PATTERNS.get(field_name, []):
        if re.search(pattern, label_lower, re.IGNORECASE):
            return True
    return False


def get_profile_value(field_name: str, job_title: str = "", company: str = "") -> str:
    """Get value from profile for a given field."""
    p = PROFILE["personal"]
    hr = PROFILE["hr_defaults"]
    
    mapping = {
        "first_name": p.get("first_name", ""),
        "last_name": p.get("last_name", ""),
        "full_name": p.get("full_name", ""),
        "email": p.get("email", ""),
        "phone": p.get("phone", ""),
        "city": p.get("city", ""),
        "country": p.get("country", ""),
        "linkedin": p.get("linkedin_url", ""),
        "github": p.get("github_url", ""),
        "experience_years": str(hr.get("total_experience_years", "")),
        "current_company": PROFILE["work_experience"][0]["company"] if PROFILE["work_experience"] else "",
        "current_title": PROFILE["work_experience"][0]["title"] if PROFILE["work_experience"] else "",
        "notice_period": str(hr.get("notice_period_days", "")) + " days",
        "current_ctc": str(hr.get("current_ctc_lpa", "")) + " LPA",
        "expected_ctc": str(hr.get("expected_ctc_lpa", "")) + " LPA",
        "cover_letter": PROFILE.get("cover_letter_template", "").format(
            job_title=job_title,
            company=company,
            relevant_skills=", ".join(PROFILE["skills"]["ai_ml"][:3])
        ),
    }
    return mapping.get(field_name, "")


async def detect_portal_type(url: str) -> str:
    """Detect which ATS/portal the URL belongs to."""
    url_lower = url.lower()
    if "workday" in url_lower or "myworkday" in url_lower:
        return "workday"
    elif "lever.co" in url_lower:
        return "lever"
    elif "greenhouse.io" in url_lower:
        return "greenhouse"
    elif "smartrecruiters" in url_lower:
        return "smartrecruiters"
    elif "icims" in url_lower:
        return "icims"
    elif "taleo" in url_lower:
        return "taleo"
    else:
        return "generic"


async def fill_text_field(page: Page, selector: str, value: str):
    """Fill a text field with retry logic."""
    try:
        field = page.locator(selector).first
        if await field.count() > 0:
            await field.click()
            await field.fill(value)
            return True
    except Exception as e:
        print(f"    Error filling field {selector}: {e}")
    return False


async def upload_resume(page: Page):
    """Upload resume file."""
    try:
        # Common resume upload selectors
        upload_selectors = [
            'input[type="file"][accept*="pdf"]',
            'input[type="file"][name*="resume"]',
            'input[type="file"][name*="cv"]',
            'input[type="file"]',
        ]
        for selector in upload_selectors:
            file_input = page.locator(selector).first
            if await file_input.count() > 0:
                await file_input.set_input_files(PROFILE["resume_path"])
                print("    ✓ Resume uploaded")
                return True
    except Exception as e:
        print(f"    Error uploading resume: {e}")
    return False


async def fill_generic_form(page: Page, job_title: str, company: str):
    """Fill form fields using generic pattern matching."""
    filled_count = 0
    
    # Get all input fields
    inputs = await page.locator('input[type="text"], input[type="email"], input[type="tel"], textarea').all()
    
    for input_el in inputs:
        try:
            # Get field identifiers
            name = await input_el.get_attribute("name") or ""
            placeholder = await input_el.get_attribute("placeholder") or ""
            aria_label = await input_el.get_attribute("aria-label") or ""
            field_id = await input_el.get_attribute("id") or ""
            
            # Try to find associated label
            label_text = ""
            if field_id:
                label = page.locator(f'label[for="{field_id}"]')
                if await label.count() > 0:
                    label_text = await label.inner_text()
            
            # Combine all identifiers for matching
            identifiers = f"{name} {placeholder} {aria_label} {label_text} {field_id}".lower()
            
            # Try to match and fill each field type
            for field_name in FIELD_PATTERNS.keys():
                if match_field(field_name, identifiers):
                    value = get_profile_value(field_name, job_title, company)
                    if value:
                        await input_el.fill(value)
                        filled_count += 1
                        print(f"    ✓ Filled {field_name}: {value[:30]}...")
                        break
        except Exception as e:
            continue
    
    return filled_count


async def handle_workday(page: Page, job_title: str, company: str):
    """Handle Workday application portal."""
    print("  Detected: Workday portal")
    await page.wait_for_timeout(3000)
    
    # Workday often requires clicking "Apply" first
    apply_btn = page.locator('button:has-text("Apply"), a:has-text("Apply")').first
    if await apply_btn.count() > 0:
        await apply_btn.click()
        await page.wait_for_timeout(2000)
    
    # Check for "Apply Manually" option (vs LinkedIn apply)
    manual_btn = page.locator('button:has-text("Apply Manually"), a:has-text("Apply Manually")').first
    if await manual_btn.count() > 0:
        await manual_btn.click()
        await page.wait_for_timeout(2000)
    
    # Upload resume first (Workday often parses it)
    await upload_resume(page)
    await page.wait_for_timeout(3000)
    
    # Fill remaining fields
    await fill_generic_form(page, job_title, company)


async def handle_lever(page: Page, job_title: str, company: str):
    """Handle Lever application portal."""
    print("  Detected: Lever portal")
    await page.wait_for_timeout(2000)
    
    # Lever has a simpler form structure
    await fill_text_field(page, 'input[name="name"]', PROFILE["personal"]["full_name"])
    await fill_text_field(page, 'input[name="email"]', PROFILE["personal"]["email"])
    await fill_text_field(page, 'input[name="phone"]', PROFILE["personal"]["phone"])
    await fill_text_field(page, 'input[name="urls[LinkedIn]"]', PROFILE["personal"]["linkedin_url"])
    await fill_text_field(page, 'input[name="urls[GitHub]"]', PROFILE["personal"]["github_url"])
    
    # Upload resume
    await upload_resume(page)
    
    # Fill additional questions
    await fill_generic_form(page, job_title, company)


async def handle_greenhouse(page: Page, job_title: str, company: str):
    """Handle Greenhouse application portal."""
    print("  Detected: Greenhouse portal")
    await page.wait_for_timeout(2000)
    
    # Greenhouse standard fields
    await fill_text_field(page, '#first_name', PROFILE["personal"]["first_name"])
    await fill_text_field(page, '#last_name', PROFILE["personal"]["last_name"])
    await fill_text_field(page, '#email', PROFILE["personal"]["email"])
    await fill_text_field(page, '#phone', PROFILE["personal"]["phone"])
    
    # Upload resume
    await upload_resume(page)
    
    # Fill additional fields
    await fill_generic_form(page, job_title, company)


async def apply_to_job(context, url: str, job_title: str, company: str) -> bool:
    """Apply to a single job."""
    print(f"\n{'='*60}")
    print(f"Applying to: {job_title} @ {company}")
    print(f"URL: {url}")
    print(f"{'='*60}")
    
    page = await context.new_page()
    
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        
        # Detect portal type
        portal_type = await detect_portal_type(url)
        
        # Handle based on portal type
        if portal_type == "workday":
            await handle_workday(page, job_title, company)
        elif portal_type == "lever":
            await handle_lever(page, job_title, company)
        elif portal_type == "greenhouse":
            await handle_greenhouse(page, job_title, company)
        else:
            print(f"  Detected: Generic portal")
            await upload_resume(page)
            await fill_generic_form(page, job_title, company)
        
        # Pause for manual review before submitting
        print("\n  >>> REVIEW: Check the form in the browser window.")
        print("  >>> Press ENTER to submit, or type 'skip' to skip this job:")
        user_input = input("  >>> ").strip().lower()
        
        if user_input == "skip":
            print("  Skipped by user")
            await page.close()
            return False
        
        # Try to find and click submit button
        submit_selectors = [
            'button[type="submit"]',
            'button:has-text("Submit")',
            'button:has-text("Apply")',
            'input[type="submit"]',
        ]
        
        for selector in submit_selectors:
            submit_btn = page.locator(selector).first
            if await submit_btn.count() > 0:
                await submit_btn.click()
                print("  ✓ Application submitted!")
                await page.wait_for_timeout(3000)
                await page.close()
                return True
        
        print("  ⚠ Could not find submit button")
        await page.close()
        return False
        
    except Exception as e:
        print(f"  ✗ Error: {e}")
        await page.close()
        return False


async def main():
    print("="*60)
    print("PHASE 3: AUTO-APPLY TO QUEUED JOBS")
    print("="*60)
    
    # Get queued jobs
    jobs = get_queued_jobs()
    print(f"\nFound {len(jobs)} jobs queued for application\n")
    
    if not jobs:
        print("No jobs to apply to. Run phase2 first to discover jobs.")
        return
    
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir="E:\\job_agent\\chrome_profile",
            channel="chrome",
            headless=False,
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
            ],
            ignore_default_args=["--enable-automation"],
            no_viewport=True
        )
        
        applied_count = 0
        failed_count = 0
        
        for url, title, company in jobs:
            success = await apply_to_job(context, url, title, company)
            
            if success:
                update_job_status(url, "APPLIED")
                applied_count += 1
            else:
                update_job_status(url, "APPLY_FAILED")
                failed_count += 1
        
        await context.close()
    
    print("\n" + "="*60)
    print(f"AUTO-APPLY COMPLETE")
    print(f"  Applied: {applied_count}")
    print(f"  Failed:  {failed_count}")
    print("="*60)


if __name__ == "__main__":
    asyncio.run(main())
