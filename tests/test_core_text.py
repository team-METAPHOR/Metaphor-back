import pytest

from app.core.text import redact, sanitize_text

PHONE, RRN, CARD = "[REDACTED_PHONE]", "[REDACTED_RRN]", "[REDACTED_CARD]"
BRN, ACCOUNT = "[REDACTED_BRN]", "[REDACTED_ACCOUNT]"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # phone: mobile, landline, VoIP, +82, separators -, space, ., none
        ("연락처 010-1234-5678", f"연락처 {PHONE}"),
        ("010 1234 5678", PHONE),
        ("010.1234.5678", PHONE),
        ("01012345678로 연락", f"{PHONE}로 연락"),
        ("011-123-4567", PHONE),
        ("02-123-4567", PHONE),
        ("02-1234-5678", PHONE),
        ("0212345678", PHONE),
        ("031-123-4567", PHONE),
        ("064 123 4567", PHONE),
        ("070-1234-5678", PHONE),
        ("+82-10-1234-5678", PHONE),
        ("+82 10 1234 5678", PHONE),
        ("+821012345678", PHONE),
        ("+82-2-123-4567", PHONE),
        ("tel:010-1234-5678", f"tel:{PHONE}"),
        ("/users/010-1234-5678/edit", f"/users/{PHONE}/edit"),
        # resident / foreigner registration number
        ("주민번호 900101-1234567", f"주민번호 {RRN}"),
        ("9001011234567", RRN),
        ("외국인 851231-5123456", f"외국인 {RRN}"),
        # card: 4-4-4-4 groups, contiguous/irregular only when Luhn passes
        ("카드 1234-5678-9012-3456", f"카드 {CARD}"),
        ("4111 1111 1111 1111", CARD),
        ("4111111111111111", CARD),
        ("3782-822463-10005", CARD),
        ("5555555555554444", CARD),
        # business registration number
        ("사업자 123-45-67890", f"사업자 {BRN}"),
        # account-like dashed groups
        ("계좌 110-123-456789", f"계좌 {ACCOUNT}"),
        ("1002-123-456789", ACCOUNT),
        ("123-456789-01-234", ACCOUNT),
        # several in one sentence, plus engine patterns still applied
        (
            "a@b.com 010-1234-5678 / 900101-1234567",
            f"[REDACTED_EMAIL] {PHONE} / {RRN}",
        ),
        ("password=hunter2", "password=[REDACTED]"),
    ],
)
def test_redact_positive(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "2026-11-05",
        "2026.11.05",
        "2026/11/05",
        "12:30:45",
        "2026-11-05T12:30:45.123456Z",
        "2026-11-05T12:30:45+09:00",
        "2026-11-05 12:30:45",
        "v1.2.3",
        "0.2.0",
        "version 10.12.1234",
        "HTTP 404 Not Found, 500, 302",
        "localhost:8000, 127.0.0.1:5432, 10.0.0.1",
        "port 65535",
        "3 steps, 12 items, 1280x800",
        "a3f2c4e5b6d7890112345678901234ab",  # 32-hex id
        "01012345678abcdef01012345678abcd",  # 32-hex id starting with phone-like digits
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",  # sha256
        "9001011234567fedcba9876543210fedcba9876543210fedcba98765432101a",
        "123e4567-e89b-12d3-a456-426614174000",  # uuid
        "550e8400-e29b-41d4-a716-010123456789",  # uuid ending in phone-like digits
        "1793600000000",  # ms timestamp
        "1234567890123",  # 13 digits, fails Luhn
        "4111111111111112",  # 16 contiguous digits, fails Luhn
        "order 12345678",
        "1.2.3.4.5",
        "010-12",
        "[REDACTED_EMAIL]",
        "https://staging.example.com/search",
    ],
)
def test_redact_negative(text):
    assert redact(text) == text


def test_sanitize_text_uses_redact():
    assert sanitize_text("연락처 010-1234-5678", 100) == f"연락처 {PHONE}"
