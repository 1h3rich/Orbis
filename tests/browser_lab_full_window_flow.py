"""LAB loads the maximum Spot history in a single visual request per chart."""

from collections import defaultdict
import json
import os
from pathlib import Path
import re
from time import perf_counter
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright


base = os.environ["ORBIS_TEST_BASE_URL"]
count = 20_000
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width":1440,"height":900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/*",lambda route: route.continue_() if route.request.url.startswith(base+"/") else route.abort())
    assert page.request.post(base+"/lab/unlock",data={"passphrase":"Fixture LAB range 22"}).ok
    run, = page.request.get(base+"/lab/runs").json()
    dataset = page.request.get(base+"/lab/datasets/"+run["dataset_id"]+"?view=chart").json()
    assert dataset["count"] == count
    first, last = dataset["times"][0], dataset["times"][-1]
    page.goto(base+"/ui/#/laboratorio")
    page.wait_for_load_state("networkidle")
    page.evaluate("""() => {
      window.labRangeCharts = {};
      const original = window.LightweightCharts;
      window.LightweightCharts = Object.assign({}, original, {createChart(host, options) {
        const chart = original.createChart(host, options);
        if (['lab-chart','lab-equity-chart'].includes(host.id)) window.labRangeCharts[host.id] = chart;
        return chart;
      }});
    }""")
    responses = defaultdict(list)

    def track(response):
        url = urlparse(response.url)
        kind = url.path.rsplit("/",1)[-1]
        query = parse_qs(url.query)
        if response.ok and kind in ("candles","curves") and query.get("view") == ["plot"]:
            responses[kind].append((response,query))
        if response.ok and kind == "markers" and query == {"from_t":[str(first)],"to_t":[str(last)]}:
            responses[kind].append((response,query))

    page.on("response", track)
    started = perf_counter()
    page.locator("#lab-run-select").select_option(run["id"])
    price = page.locator("#lab-chart")
    equity = page.locator("#lab-equity-chart")
    expect(price).to_have_attribute("data-candle-visible-count",re.compile(r"^[1-9][0-9]*$"),timeout=30000)
    expect(equity).to_have_attribute("data-curve-visible-count",re.compile(r"^[1-9][0-9]*$"),timeout=30000)
    measures = {"first_view_ms":round((perf_counter()-started)*1000)}
    measures["first_price_points"] = int(price.get_attribute("data-candle-visible-count"))
    measures["first_equity_points"] = int(equity.get_attribute("data-curve-visible-count"))
    assert 0 < measures["first_price_points"] < count and 0 < measures["first_equity_points"] < count
    for kind, selector, chart, attr in (
        ("curves","#lab-equity-range select",equity,"data-curve-visible-count"),
        ("candles","#lab-run-history select",price,"data-candle-visible-count"),
    ):
        responses[kind].clear()
        started = perf_counter()
        page.locator(selector).select_option("all")
        expect(chart).to_have_attribute(attr,str(count),timeout=30000)
        (response, query), = responses[kind]
        assert query == {"from_t":[str(first)],"to_t":[str(last)],"view":["plot"],"limit":[str(count)]},query
        measures[kind] = {"milliseconds":round((perf_counter()-started)*1000),
                          "requests":len(responses[kind]),"bytes":len(response.body())}

    for width in (1440,390):
        page.set_viewport_size({"width":width,"height":900})
        page.wait_for_function("""([first,last]) => Object.keys(labRangeCharts).length === 2 &&
          Object.values(labRangeCharts).every(chart => {
            const range = chart.timeScale().getVisibleRange();
            return range && range.from <= first && range.to >= last;
          })""",arg=[first,last])
        expect(price).to_have_attribute("data-candle-visible-count",str(count))
        expect(equity).to_have_attribute("data-curve-visible-count",str(count))
    page.locator("#lab-equity-range select").select_option("1")
    expect(equity).to_have_attribute("data-curve-visible-count",str(measures["first_equity_points"]))
    expect(price).to_have_attribute("data-marker-visible-count",re.compile(r"^[1-9][0-9]*$"))
    assert len(responses["markers"]) == 1
    assert page.request.post(base+"/lab/lock").ok
    for kind in ("curves","candles"):
        assert page.request.get(responses[kind][0][0].url).status == 423
    assert not errors, errors
    measures.update({"desktop_full":True,"mobile_full":True,"locked_requests_rejected":True,
                     "full_marker_requests":len(responses["markers"]),"page_errors":errors})
    output = Path(os.environ["ORBIS_LAB_PATH"]).parent.parent/"full-window-result.json"
    output.write_text(json.dumps(measures,indent=2)+"\n")
    print(json.dumps(measures))
    browser.close()
