from app.feishu import extract_text, verify_signature


def test_extract_text_from_feishu_message_content() -> None:
    assert extract_text({"content": '{"text":"查一下库存"}'}) == "查一下库存"


def test_verify_signature() -> None:
    raw_body = b'{"hello":"world"}'
    timestamp = "1"
    nonce = "abc"
    encrypt_key = "key"
    signature = "e25b2a88da8d9ee749f22e9af004e3452432aa17652b29577a8e58ca1919d3ed"
    assert verify_signature(
        raw_body=raw_body,
        timestamp=timestamp,
        nonce=nonce,
        signature=signature,
        encrypt_key=encrypt_key,
    )
