"""La interfaz agrupa importes Decimal sin convertir los dígitos a float."""

from playwright.sync_api import sync_playwright


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("http://127.0.0.1:8000/ui/#/inicio")
    page.wait_for_function("Boolean(window.OrbisUI)")
    result = page.evaluate("""() => ({
      eur: OrbisUI.fmtMoney('123456789012345678.123456789012', 'EUR'),
      carry: OrbisUI.fmtMoney('999999999999999999.999', 'EUR'),
      token: OrbisUI.fmtMoney('123456789012345678.123456789012', 'USDT'),
      qty: OrbisUI.fmtQty('123456789012345678.123456789012', 'BTC'),
      small: OrbisUI.fmtMoney('1.2', 'EUR')
    })""")
    assert result == {
        "eur": "123.456.789.012.345.678,12\u00a0€",
        "carry": "1.000.000.000.000.000.000,00\u00a0€",
        "token": "123.456.789.012.345.678,12345679 USDT",
        "qty": "123.456.789.012.345.678,12345679 BTC",
        "small": "1,20\u00a0€",
    }
    browser.close()
