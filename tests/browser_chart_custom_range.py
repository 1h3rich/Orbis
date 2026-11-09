"""El selector compartido funciona con Lightweight Charts real en Chromium."""

import os

from playwright.sync_api import sync_playwright


BASE = os.environ.get("ORBIS_TEST_BASE_URL", "http://127.0.0.1:8765") + "/ui/"


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1200, "height": 800})
    page.goto(BASE)
    page.wait_for_load_state("networkidle")
    page.evaluate("""() => {
      const host = document.createElement('div');
      host.id = 'range-smoke';
      host.style.width = '800px';
      host.style.height = '420px';
      document.body.appendChild(host);
      const controls = document.createElement('div');
      controls.id = 'range-smoke-controls';
      controls.className = 'lab-history-controls';
      host.appendChild(controls);
      const surface = document.createElement('div');
      surface.style.width = '800px';
      surface.style.height = '320px';
      host.appendChild(surface);
      const chart = LightweightCharts.createChart(surface, {autoSize: true});
      const series = chart.addSeries(LightweightCharts.LineSeries);
      const first = Date.parse('2020-01-01T00:00:00Z') / 1000;
      const rows = Array.from({length: 10}, (_, i) => ({time: first + i * 86400, value: i + 1}));
      series.setData(rows);
      OrbisChartRange.mount(controls, chart, rows);
      window.__rangeSmoke = chart;
    }""")
    control = page.locator("#range-smoke-controls")
    before = page.evaluate("window.__rangeSmoke.timeScale().getVisibleRange()")
    control.locator("select").select_option("custom")
    assert control.locator("[data-range-dates]").is_visible()
    assert control.locator("select").input_value() == "all"
    control.locator("[data-range-from]").fill("2020-01-03")
    control.locator("[data-range-to]").fill("2020-01-02")
    control.locator("[data-range-apply]").click()
    assert "en orden" in control.locator("[data-range-error]").inner_text()
    assert page.evaluate("window.__rangeSmoke.timeScale().getVisibleRange()") == before
    control.locator("[data-range-to]").fill("2020-01-06")
    assert page.evaluate("window.__rangeSmoke.timeScale().getVisibleRange()") == before
    control.locator("[data-range-apply]").click()
    expected_from = 1578009600  # 2020-01-03 UTC
    expected_to = 1578268800  # 2020-01-06 UTC
    page.wait_for_function("""([start, end]) => {
      const visible = window.__rangeSmoke.timeScale().getVisibleRange();
      return visible && visible.from === start && visible.to === end;
    }""", arg=[expected_from, expected_to])
    visible = page.evaluate("window.__rangeSmoke.timeScale().getVisibleRange()")
    assert visible and visible["from"] == expected_from and visible["to"] == expected_to, (
        visible,
        control.locator("[data-range-from]").input_value(),
        control.locator("[data-range-to]").input_value(),
        control.locator("[data-range-coverage]").inner_text(),
    )
    assert "2020-01-03 → 2020-01-06" in control.locator("[data-range-coverage]").inner_text()
    control.locator("[data-range-edit]").click()
    control.locator("[data-range-from]").fill("2020-01-01")
    control.locator("[data-range-cancel]").click()
    assert control.locator("[data-range-from]").input_value() == "2020-01-03"
    assert page.evaluate("window.__rangeSmoke.timeScale().getVisibleRange()") == visible
    control.locator("select").select_option("all")
    assert not control.locator("[data-range-dates]").is_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("""() => {
      document.querySelector('#range-smoke').style.width = '340px';
      document.querySelector('#range-smoke > div:last-child').style.width = '340px';
    }""")
    control.locator("select").select_option("custom")
    box = control.locator("[data-range-dates]").bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 390
    browser.close()
    print("Selector de fechas compartido: render, rango y cobertura OK")

