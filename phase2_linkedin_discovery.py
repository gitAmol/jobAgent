import asyncio
import json
import os
import sqlite3
import urllib.parse
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field

load_dotenv()

class FitScore(BaseModel):
    match_score: int = Field(description="0 to 100 fit score against candidate profile")
    reasoning: str = Field(description="2-sentence summary of why it matches or misses")

def init_db(db_path: str = "state.db"):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            url TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            location TEXT,
            match_score INTEGER,
            reasoning TEXT,
            status TEXT DEFAULT 'DISCOVERED'
        )
    """)
    conn.commit()
    return conn

async def harvest_from_profile(max_jobs_per_role: int = 5):
    with open("profile.json", "r", encoding="utf-8") as f:
        profile = json.load(f)

    target_roles = profile["preferences"]["target_roles"]
    preferred_locations = profile["preferences"]["preferred_locations"]
    search_location = next(
        (loc for loc in preferred_locations if loc.lower() not in ["remote", "hybrid"]),
        "Bengaluru, Karnataka, India"
    )

    conn = init_db()
    llm = ChatGoogleGenerativeAI(model="gemini-3.5-flash", temperature=0)
    scorer = llm.with_structured_output(FitScore)

    async with async_playwright() as p:
        # Playwright launches & manages its own persistent Chrome profile directly
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
        page = context.pages[0] if context.pages else await context.new_page()
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        print("Opening LinkedIn to verify login...")
        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # If not logged in inside THIS exact window, pause and let you sign in right now
        if "foryou" in page.url or "login" in page.url or await page.locator("a:has-text('Sign in')").count() > 0:
            print("\n>>> ACTION REQUIRED: Sign into LinkedIn in the Chrome window that just popped up.")
            input(">>> Once you see your LinkedIn Home Feed in that window, press ENTER here to continue...")

        for role in target_roles:
            query = urllib.parse.urlencode({"keywords": role, "location": search_location})
            search_url = f"https://www.linkedin.com/jobs/search/?{query}"

            print(f"\n--- Searching LinkedIn for '{role}' in '{search_location}' ---")
            await page.goto(search_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)

            # Scroll to trigger lazy-loading of job cards
            for _ in range(3):
                await page.mouse.wheel(0, 500)
                await page.wait_for_timeout(1000)

            # Debug: find what elements exist
            all_links = await page.locator('a').all()
            job_links = []
            for link in all_links:
                href = await link.get_attribute('href') or ''
                if '/jobs/view/' in href or '/jobs/search/' in href:
                    text = await link.inner_text()
                    job_links.append((href, text.strip()[:50]))
            
            print(f"Debug: Found {len(job_links)} job-related links")
            for href, text in job_links[:5]:
                print(f"  - {text} -> {href[:80]}")
            
            # Try to find clickable job items in the left panel
            # Look for any clickable elements that might be job cards
            job_cards = page.locator('a[href*="/jobs/view/"]')
            card_count = await job_cards.count()
            print(f"Found {card_count} job card links")
            
            if card_count == 0:
                print(f"No job cards found. Current URL: {page.url}")
                continue
            
            processed = 0
            
            for i in range(min(card_count, max_jobs_per_role + 5)):
                try:
                    # Click on job card to select it
                    card = job_cards.nth(i)
                    await card.scroll_into_view_if_needed()
                    await card.click()
                    await page.wait_for_timeout(2000)
                    
                    # Get job title from the clicked card (stored before click changes context)
                    title = (await card.inner_text()).strip().split('\n')[0]  # First line is usually title
                    
                    # Get company name
                    company = "Unknown"
                    company_el = page.locator('a[href*="/company/"]').first
                    if await company_el.count() > 0:
                        company = (await company_el.inner_text()).strip()
                    
                    print(f"Processing: {title} @ {company}")
                    
                    # Get job description
                    jd_text = ""
                    main_content = page.locator('main')
                    if await main_content.count() > 0:
                        jd_text = await main_content.inner_text()
                    
                    # Find Apply button - try multiple selectors
                    apply_btn = None
                    for sel in ['button:has-text("Apply")', 'a:has-text("Apply")', '[aria-label*="Apply"]']:
                        btn = page.locator(sel).first
                        if await btn.count() > 0:
                            apply_btn = btn
                            break
                    
                    if apply_btn is None:
                        print(f"  No Apply button found, skipping")
                        continue
                    
                    btn_text = (await apply_btn.inner_text()).strip()
                    if "Easy Apply" in btn_text:
                        print(f"  Skipping Easy Apply")
                        continue
                    
                    # Capture external application URL
                    try:
                        async with context.expect_page(timeout=8000) as new_page_info:
                            await apply_btn.click()
                        ext_page = await new_page_info.value
                        await ext_page.wait_for_load_state("domcontentloaded")
                        apply_url = ext_page.url
                        await ext_page.close()
                    except:
                        print(f"  No external page opened, skipping")
                        continue
                    
                    if conn.execute("SELECT 1 FROM jobs WHERE url = ?", (apply_url,)).fetchone():
                        print(f"  Already in DB")
                        continue
                    
                    fit = scorer.invoke(f"""
                    Score how well this job matches the candidate profile.
                    Candidate Profile: {json.dumps(profile)}
                    Job Title: {title} at {company}
                    Job Description: {jd_text[:6000]}
                    """)
                    
                    status = (
                        "QUEUED_FOR_APPLY"
                        if fit.match_score >= profile["preferences"]["min_match_score"]
                        else "SKIPPED_LOW_FIT"
                    )
                    
                    conn.execute(
                        "INSERT INTO jobs (url, title, company, location, match_score, reasoning, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (apply_url, title, company, search_location, fit.match_score, fit.reasoning, status)
                    )
                    conn.commit()
                    print(f"  [{status}] ({fit.match_score}/100) -> {apply_url}")
                    processed += 1
                    
                    if processed >= max_jobs_per_role:
                        break
                    
                except Exception as e:
                    print(f"  Error: {e}")
            
            print(f"Processed {processed} jobs for '{role}'")


            await page.wait_for_timeout(3000)

        await context.close()
    conn.close()
    print("\nDiscovery run complete. Check state.db for queued jobs.")

if __name__ == "__main__":
    asyncio.run(harvest_from_profile(max_jobs_per_role=5))
