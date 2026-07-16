import os
import sys
import asyncio
import requests
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
# 1. PYDANTIC SCHEMA FOR GEMINI STRUCTURED OUTPUT
# ---------------------------------------------------------------------------
class CrousListing(BaseModel):
    availability_found: bool = Field(
        description="Must be True only if there is an active, bookable room or offer currently available at 'Les Dolines' or 'Isaac Newton' / 'Newton' in Sophia Antipolis / Valbonne."
    )
    residence_name: str | None = Field(
        description="The exact matching residence name found: 'Les Dolines' or 'Isaac Newton' (or 'Newton'). Null if none available."
    )
    price: str | None = Field(
        description="The monthly rent price listed for the room (e.g., '380 €'). Null if none available."
    )
    booking_url: str | None = Field(
        description="The relative or absolute URL to book or view this specific listing. Null if none available."
    )

# ---------------------------------------------------------------------------
# 2. SCRAPING STAGE (Playwright)
# ---------------------------------------------------------------------------
async def scrape_crous_page(url: str) -> str:
    """Uses Playwright to navigate the JS-heavy CROUS portal and pull down the fully rendered HTML."""
    print("🚀 Initializing headless browser...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # Emulate a standard desktop browser profile to avoid basic anti-bot blocks
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = await context.new_page()
        
        print(f"📡 Navigating to CROUS Search Portal...")
        # Force navigation and wait for network activity to fully settle down
        await page.goto(url, wait_until="networkidle", timeout=60000)
        
        # Give the cards component a moment to render on top of the map
        try:
            await page.wait_for_selector(".fr-card", timeout=15000)
            print("✅ Found listing card elements in the DOM.")
        except Exception:
            print("⚠️ Warning: Standard card elements did not render in time. Extracting raw body state as-is.")
            
        html_content = await page.content()
        await browser.close()
        return html_content

# ---------------------------------------------------------------------------
# 3. ANALYSIS STAGE (Gemini Pro via Structured Outputs)
# ---------------------------------------------------------------------------
def analyze_html_with_gemini(html_data: str, api_key: str) -> CrousListing:
    """Uses the modern google-genai SDK to parsing HTML directly into a validated Pydantic schema."""
    print("🤖 Passing rendered HTML to Gemini Pro for structured parsing...")
    
    # Initialize the modern Gemini client
    client = genai.Client(api_key=api_key)
    
    prompt = f"""
    Analyze the raw HTML of a CROUS housing results page.
    Check if there are any ACTIVE, BOOKABLE, or OPEN housing listings belonging to:
    - "Les Dolines"
    - "Isaac Newton" (sometimes referred to as "Newton")
    
    Look specifically for actions/buttons/text indicators that show a room can actually be booked or reserved right now.
    If all listed rooms are full, marked "complet", "indisponible", or if no cards match our target residences, set 'availability_found' to false.
    
    HTML CONTENT:
    \"\"\"
    {html_data}
    \"\"\"
    """
    
    # Run structured JSON schema parsing
    response = client.models.generate_content(
        model='gemini-3.5-flash',
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=CrousListing,
            system_instruction=(
                "You are an expert HTML parsing agent designed to extract housing room availability "
                "from raw CROUS portal listings. Be highly strict and logical. Avoid false positives."
            )
        ),
    )
    
    # Parse the response safely into our validated Pydantic model
    return CrousListing.model_validate_json(response.text)

# ---------------------------------------------------------------------------
# 4. NOTIFICATION STAGE (Telegram Bot API)
# ---------------------------------------------------------------------------
def send_telegram_notification(listing: CrousListing, token: str, chat_id: str):
    """Sends formatted Markdown messaging containing direct deep links if housing is open."""
    if not listing.availability_found:
        print("ℹ️ No target listings available in this cycle.")
        return

    print(f"🔔 ALERT: Active room found in {listing.residence_name}! Dispatching Telegram ping...")
    
    # Normalize relative links to absolute URLs
    booking_link = listing.booking_url if listing.booking_url else "https://trouverunlogement.lescrous.fr/"
    if booking_link.startswith("/"):
        booking_link = f"https://trouverunlogement.lescrous.fr{booking_link}"

    message = (
        f"🚨 **URGENT: CROUS ROOM FOUND!** 🚨\n\n"
        f"🏢 **Residence:** {listing.residence_name}\n"
        f"💰 **Estimated Rent:** {listing.price if listing.price else 'N/A'}\n\n"
        f"🔗 [CLICK HERE TO SECURE THE BOOKING]({booking_link})"
    )
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    
    try:
        res = requests.post(url, json=payload)
        if res.status_code == 200:
            print("✅ Telegram alert successfully delivered!")
        else:
            print(f"❌ Telegram API Error: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"❌ Failed to dispatch Telegram Webhook: {e}")

# ---------------------------------------------------------------------------
# 5. ORCHESTRATION PIPELINE
# ---------------------------------------------------------------------------
async def main():
    # Read pipeline credentials from environment variables to secure them
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    
    # Verification and defensive guards
    if not GEMINI_API_KEY or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Error: Missing configuration credentials.")
        print("Please export GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, and TELEGRAM_CHAT_ID in your shell environment.")
        sys.exit(1)
        
    # CROUS Mapbounds URL focusing specifically on Sophia Antipolis/Valbonne listings
    valbonne_crous_url = "https://trouverunlogement.lescrous.fr/tools/37/search?bounds=7.0016_43.5932_7.0871_43.6393"
    
    try:
        # Step A: Perform headless browser scraping
        html_content = await scrape_crous_page(valbonne_crous_url)
        
        # Step B: Pass extracted DOM context to Gemini Pro parser
        analysis_result = analyze_html_with_gemini(html_content, GEMINI_API_KEY)
        
        # Print a CLI diagnostic summary
        print("\n" + "="*30)
        print("        AI PIPELINE READOUT        ")
        print("="*30)
        print(f"Availability Found : {analysis_result.availability_found}")
        print(f"Residence Name     : {analysis_result.residence_name}")
        print(f"Rent Price         : {analysis_result.price}")
        print(f"Booking URL        : {analysis_result.booking_url}")
        print("="*30 + "\n")
        
        # Step C: Send Telegram alert if room matching constraints is open
        send_telegram_notification(analysis_result, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        
    except Exception as e:
        print(f"💥 Application pipeline crashed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
