"""Evaluator-only probes: drive each page's bug condition directly and report the symptom.

Used by test_bug_site_bugs_are_real to prove the seeded bugs manifest (and the fixed versions
don't). Never sent to the model.
"""

MOBILE = {"width": 375, "height": 667}


async def _text(page, selector):
    return (await page.inner_text(selector)).strip()


async def search(page, site):
    await page.fill("#query", "노트북")
    await page.click("#search")
    return await _text(page, "#count") == "0건"


async def cart(page, site):
    await page.click("#plus")
    return await _text(page, "#total") != "24,000원"


async def like(page, site):
    for _ in range(3):
        await page.click("#like")
    return await _text(page, "#likes") != "3"


async def products(page, site):
    await page.click("#shoes")
    await page.wait_for_function("document.querySelector('#status').textContent !== '불러오는 중'")
    await page.click("#sale")
    await page.wait_for_function("document.querySelector('#status').textContent !== '불러오는 중'")
    return await _text(page, "#status") == "상품을 불러오지 못했습니다"


async def detail(page, site):
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    await page.click("#open")
    await page.wait_for_timeout(100)
    return bool(errors) and not await page.is_visible("#modal")


async def checkout(page, site):
    pay = await page.locator("#pay").bounding_box()
    promo = await page.locator("#promo").bounding_box()
    return (
        min(pay["x"] + pay["width"], promo["x"] + promo["width"]) - max(pay["x"], promo["x"]) > 1
        and min(pay["y"] + pay["height"], promo["y"] + promo["height"]) - max(pay["y"], promo["y"])
        > 1
    )


async def login(page, site):
    await page.fill("#user", "tester")
    await page.click("#login")
    await page.wait_for_function("document.querySelector('#heading').textContent !== '로그인'")
    return await _text(page, "#heading") == "홈"


async def signup(page, site):
    await page.fill("#name", "테스터")
    await page.click("#next1")
    await page.fill("#email", "tester@example.com")
    await page.click("#next2")
    await page.fill("#zip", "06236")
    await page.click("#submit")
    return await _text(page, "#error") == "우편번호 형식이 올바르지 않습니다"


# page -> (probe, viewport)
PROBES = {
    "search": (search, None),
    "cart": (cart, None),
    "like": (like, None),
    "products": (products, None),
    "detail": (detail, None),
    "checkout": (checkout, MOBILE),
    "login": (login, None),
    "signup": (signup, None),
}
