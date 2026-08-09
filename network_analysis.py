from playwright.sync_api import sync_playwright
import os, re, datetime

# Where we save each captured response body so we can inspect exactly what each call returns
CAPTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capture")
os.makedirs(CAPTURE_DIR, exist_ok=True)

def silent_recon():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        # Skip static assets so only the real API/AJAX calls show up
        NOISE = (".css", ".js", ".png", ".jpg", ".gif", ".svg", ".woff", ".woff2", ".ico")
        def is_noise(url):
            return url.split("?")[0].lower().endswith(NOISE)

        counter = {"n": 0}

        def on_request(request):
            if is_noise(request.url):
                return
            print(f">>> Request: {request.method} {request.url}")
            if request.post_data:
                print(f"    payload: {request.post_data}")

        def on_response(response):
            url = response.url
            if is_noise(url):
                return
            print(f"<<< Response: {response.status} {url}")
            # Save the body of every /sale/ data call so we can inspect it afterwards
            try:
                if "/sale/" in url:
                    body = response.text()
                    counter["n"] += 1
                    # build a readable filename from the endpoint name
                    name = re.sub(r"[^A-Za-z0-9_]+", "_", url.split("/sale/")[-1])[:80] or "root"
                    fname = f"{counter['n']:03d}_{response.status}_{name}.txt"
                    path = os.path.join(CAPTURE_DIR, fname)
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(f"URL: {url}\nSTATUS: {response.status}\nTIME: {datetime.datetime.now()}\n")
                        f.write("=" * 70 + "\n")
                        f.write(body)
                    # flag the ones likely to hold the FPS detail tables
                    if re.search(r"Coarse|Wheat|Distributed Quantity|Priority Household|e-Transaction|Barley", body, re.I):
                        print(f"    *** SAVED (contains FPS-detail table): {fname} ***")
                    else:
                        print(f"    saved: {fname}")
            except Exception as e:
                print(f"    (could not save body: {e})")

        page.on("request", on_request)
        page.on("response", on_response)

        print("Navigating to impds.nic.in...")
        print(f"Response bodies are being saved to: {CAPTURE_DIR}")
        page.goto("https://impds.nic.in/sale/", timeout=60000)

        # Pause so you can drill to the exact FPS detail view (North Goa, March),
        # then click the + on Coarse Grains. Watch the terminal for the *** SAVED *** line.
        page.pause()

        browser.close()

if __name__ == "__main__":
    silent_recon()
