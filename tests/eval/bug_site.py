"""Synthetic shop with one seeded bug per page (stdlib http.server, like tests/e2e/site.py).

Every page and API lives under a random per-run prefix and random per-page paths, so URLs carry
no label hints. Bug/fixed selection is server memory only (`BugSite.fixed`); the page code is
substituted, never a visible flag. A fixed page answers the same report as a corrected app.
"""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

# Pages with a seeded bug. `home`/`orders` are login redirect targets, `api` is the products API.
PAGES = ("search", "cart", "like", "products", "detail", "checkout", "login", "signup")
ROUTES = (*PAGES, "home", "orders", "api")

STYLE = """body{font:18px sans-serif;margin:32px;color:#172033;background:#f8fafc}
main{background:#fff;padding:24px;max-width:720px;border:1px solid #ddd;border-radius:12px}
button,input{font:inherit;padding:10px;margin:6px 6px 6px 0}#status{margin-top:16px;color:#465}
.hidden{display:none}"""


def _page(title: str, body: str, script: str = "", style: str = "") -> bytes:
    return (
        '<!doctype html><html lang="ko"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<link rel="icon" href="data:,"><title>{title}</title><style>{STYLE}{style}</style>'
        f'<main><h1 id="heading">{title}</h1>{body}</main><script>{script}</script></html>'
    ).encode()


def _status(text: str = "준비 완료") -> str:
    return f'<div id="status">{text}</div>'


ITEMS = ["무선 마우스", "기계식 키보드", "노트북 거치대", "노트북 파우치", "USB 허브"]
PRODUCTS = [
    {"name": "러닝화", "cat": "shoes", "sale": True},
    {"name": "등산화", "cat": "shoes", "sale": False},
    {"name": "후드티", "cat": "top", "sale": True},
    {"name": "청바지", "cat": "bottom", "sale": False},
]


def search(fixed: bool, r: dict) -> bytes:
    # Bug 1: one specific query ("노트북") returns no results although items match.
    bug = "" if fixed else "if (q === '노트북') hits = [];"
    script = f"""const items = {json.dumps(ITEMS, ensure_ascii=False)};
document.querySelector('#search').onclick = () => {{
  const q = document.querySelector('#query').value.trim();
  let hits = items.filter(i => q && i.includes(q));
  {bug}
  document.querySelector('#count').textContent = hits.length + '건';
  document.querySelector('#results').textContent = hits.join(', ');
  document.querySelector('#status').textContent = '검색 완료';
}};"""
    body = (
        '<label for="query">검색어</label><input id="query"><button id="search">검색</button>'
        '<div id="count">0건</div><div id="results"></div>' + _status()
    )
    return _page("상품 검색", body, script)


def cart(fixed: bool, r: dict) -> bytes:
    # Bug 2: the total stops following the quantity from 2 items on.
    total = "price * qty" if fixed else "qty >= 2 ? price : price * qty"
    script = f"""const price = 12000; let qty = 1;
const won = n => n.toLocaleString('ko-KR') + '원';
document.querySelector('#plus').onclick = () => {{
  qty += 1;
  document.querySelector('#qty').textContent = qty;
  document.querySelector('#total').textContent = won({total});
  document.querySelector('#status').textContent = '수량 변경됨';
}};"""
    body = (
        "<p>텀블러 · 12,000원</p>"
        '<div>수량 <span id="qty">1</span> <button id="plus">+</button></div>'
        '<div>합계 <span id="total">12,000원</span></div>' + _status()
    )
    return _page("장바구니", body, script)


def like(fixed: bool, r: dict) -> bytes:
    # Bug 3: only the first click counts; later clicks do nothing.
    guard = "" if fixed else "if (clicks > 1) return;"
    script = f"""let clicks = 0;
document.querySelector('#like').onclick = () => {{
  clicks += 1;
  {guard}
  document.querySelector('#likes').textContent = clicks;
  document.querySelector('#status').textContent = '반영됨';
}};"""
    body = (
        "<p>배송이 빠르고 포장이 꼼꼼했어요.</p>"
        '<button id="like">도움돼요</button> <span id="likes">0</span>' + _status()
    )
    return _page("상품 후기", body, script)


def products(fixed: bool, r: dict) -> bytes:
    # Bug 4: the API returns 500 for the shoes + sale filter combination (server side).
    script = f"""const on = {{shoes: false, sale: false}};
async function load() {{
  const status = document.querySelector('#status');
  status.textContent = '불러오는 중';
  const q = 'cat=' + (on.shoes ? 'shoes' : '') + '&sale=' + (on.sale ? '1' : '');
  const reply = await fetch('{r["api"]}?' + q);
  if (!reply.ok) {{ status.textContent = '상품을 불러오지 못했습니다'; return; }}
  const data = await reply.json();
  document.querySelector('#list').textContent = data.map(p => p.name).join(', ');
  status.textContent = data.length + '개 상품';
}}
document.querySelector('#shoes').onclick = () => {{ on.shoes = !on.shoes; load(); }};
document.querySelector('#sale').onclick = () => {{ on.sale = !on.sale; load(); }};"""
    body = (
        '<button id="shoes">신발</button><button id="sale">할인 상품</button>'
        '<div id="list">러닝화, 등산화, 후드티, 청바지</div>' + _status()
    )
    return _page("상품 목록", body, script)


def detail(fixed: bool, r: dict) -> bytes:
    # Bug 5: opening the modal throws a TypeError, so the modal never shows.
    weight = "(product.spec || {}).weight || '정보 없음'" if fixed else "product.spec.weight"
    script = f"""const product = {{name: '접이식 의자'}};
document.querySelector('#open').onclick = () => {{
  document.querySelector('#modal-body').textContent = '무게: ' + {weight};
  document.querySelector('#modal').classList.remove('hidden');
  document.querySelector('#status').textContent = '상세 정보 열림';
}};"""
    body = (
        "<p>접이식 의자 · 39,000원</p>"
        '<button id="open">상세 정보 보기</button>'
        '<div id="modal" class="hidden" role="dialog"><p id="modal-body"></p></div>' + _status()
    )
    return _page("상품 상세", body, script)


def checkout(fixed: bool, r: dict) -> bytes:
    # Bug 6: at ≤480px a fixed promo banner covers the pay button.
    mobile = (
        "#promo{position:static}"
        if fixed
        else "#promo{position:fixed;left:0;right:0;bottom:0;height:110px;z-index:10}"
        "#pay{position:fixed;left:16px;bottom:24px}"
    )
    style = f"#promo{{background:#fde68a;padding:16px}}@media (max-width:480px){{{mobile}}}"
    script = """document.querySelector('#pay').onclick = () => {
  document.querySelector('#status').textContent = '결제 진행 중';
};"""
    body = (
        "<p>주문 금액 24,000원</p>"
        '<div id="promo">첫 구매 10% 할인 쿠폰 받기</div>'
        '<button id="pay">결제하기</button>' + _status()
    )
    return _page("주문 결제", body, script, style)


def login(fixed: bool, r: dict) -> bytes:
    # Bug 7: after login the app redirects to home instead of the requested orders page.
    target = r["orders"] if fixed else r["home"]
    script = f"""document.querySelector('#login').onclick = () => {{
  if (!document.querySelector('#user').value.trim()) return;
  document.querySelector('#status').textContent = '로그인 중';
  location.href = '{target}';
}};"""
    body = (
        "<p>주문 내역을 보려면 로그인하세요.</p>"
        '<label for="user">아이디</label><input id="user" type="text">'
        '<button id="login">로그인</button>' + _status()
    )
    return _page("로그인", body, script)


def home(fixed: bool, r: dict) -> bytes:
    return _page("홈", "<p>오늘의 추천 상품</p>" + _status())


def orders(fixed: bool, r: dict) -> bytes:
    return _page("주문 내역", "<p>최근 주문 2건</p>" + _status())


def signup(fixed: bool, r: dict) -> bytes:
    # Bug 8: step 3 rejects every valid 5-digit postal code (expects 6 digits).
    zip_re = r"/^\d{5}$/" if fixed else r"/^\d{6}$/"
    script = f"""const show = n => [1, 2, 3].forEach(i =>
  document.querySelector('#step' + i).classList.toggle('hidden', i !== n));
const error = t => document.querySelector('#error').textContent = t;
document.querySelector('#next1').onclick = () => {{
  if (!document.querySelector('#name').value.trim()) return error('이름을 입력하세요');
  error(''); show(2); document.querySelector('#status').textContent = '2단계';
}};
document.querySelector('#next2').onclick = () => {{
  if (!document.querySelector('#email').value.includes('@'))
    return error('이메일 형식이 올바르지 않습니다');
  error(''); show(3); document.querySelector('#status').textContent = '3단계';
}};
document.querySelector('#submit').onclick = () => {{
  if (!{zip_re}.test(document.querySelector('#zip').value.trim()))
    return error('우편번호 형식이 올바르지 않습니다');
  error(''); document.querySelector('#status').textContent = '가입 완료';
}};"""
    body = (
        '<div id="step1"><label for="name">이름</label><input id="name">'
        '<button id="next1">다음</button></div>'
        '<div id="step2" class="hidden"><label for="email">이메일</label><input id="email">'
        '<button id="next2">다음</button></div>'
        '<div id="step3" class="hidden"><label for="zip">우편번호</label><input id="zip">'
        '<button id="submit">가입하기</button></div>'
        '<div id="error" role="alert"></div>' + _status("1단계")
    )
    return _page("회원가입", body, script)


RENDER = {f.__name__: f for f in (search, cart, like, products, detail, checkout, login, signup)}
RENDER |= {"home": home, "orders": orders}


class BugSite:
    def __init__(self):
        self.prefix = "/" + uuid4().hex[:12]
        self.routes = {name: f"{self.prefix}/{uuid4().hex[:10]}" for name in ROUTES}
        self._by_path = {path: name for name, path in self.routes.items()}
        self.fixed: dict[str, bool] = dict.fromkeys(ROUTES, False)
        self.origin = ""

    def activate(self, page: str, *, fixed: bool) -> str:
        """Select the bug or fixed version of `page` and return its URL."""
        self.fixed[page] = fixed
        return self.url(page)

    def url(self, page: str) -> str:
        return self.origin + self.routes[page]

    def api(self, query: str) -> tuple[int, bytes]:
        q = parse_qs(query)
        cat, sale = q.get("cat", [""])[0], q.get("sale", [""])[0] == "1"
        if cat == "shoes" and sale and not self.fixed["products"]:
            return 500, b'{"error": "internal"}'
        hits = [p for p in PRODUCTS if (not cat or p["cat"] == cat) and (not sale or p["sale"])]
        return 200, json.dumps(hits, ensure_ascii=False).encode()

    def respond(self, raw_path: str) -> tuple[int, str, bytes]:
        parts = urlsplit(raw_path)
        name = self._by_path.get(parts.path)
        if name is None:
            return 404, "text/plain", b"Not found"
        if name == "api":
            status, body = self.api(parts.query)
            return status, "application/json; charset=utf-8", body
        fixed = self.fixed["login"] if name in ("home", "orders") else self.fixed[name]
        return 200, "text/html; charset=utf-8", RENDER[name](fixed, self.routes)


@contextmanager
def serve():
    """Yield a running BugSite (loopback only)."""
    site = BugSite()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            status, content_type, body = site.respond(self.path)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    site.origin = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield site
    finally:
        server.shutdown()
        server.server_close()
