"""LAB sólo muestra CAGR para una prueba de al menos 365 días.

Requiere el servidor LAB aislado browser_lab_annualized_server.py en :8797.
"""

from playwright.sync_api import expect, sync_playwright


BASE = "http://127.0.0.1:8797"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    saved_runs = [page.request.get(BASE + "/lab/runs/" + row["id"]).json()
                  for row in page.request.get(BASE + "/lab/runs").json()]
    assert len(saved_runs) == 2
    long_run = next(run for run in saved_runs if run["risk"]["annualized_return_pct"] is not None)
    short_run = next(run for run in saved_runs if run["risk"]["annualized_return_pct"] is None)
    page.goto(BASE + "/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.locator("#lab-run-select").select_option(short_run["id"])
    expect(page.locator("#lab-report .lab-metrics")).not_to_contain_text("Anualizada")
    page.locator("#lab-run-select").select_option(long_run["id"])
    metric = page.locator("#lab-report .lab-metrics").get_by_text("Anualizada")
    expect(metric).to_be_visible()
    expect(page.locator("#lab-report .lab-metrics")).to_contain_text("Anualizada")
    assert "al menos 365 días" in metric.get_attribute("data-help")
    assert not errors, errors
    browser.close()
