from seruen.services.dgis_keys import classify, mask


def test_classify_2gis_errors():
    assert classify(200, {"meta": {"code": 200}}) is None
    assert classify(200, {"meta": {"code": 403, "error": {"message": "Authorization error, incorrect key.", "type": "forbidden"}}}) == "invalid"
    assert classify(403, {"type": "forbidden", "message": "invalid_key"}) == "invalid"
    assert classify(200, {"meta": {"code": 403, "error": {"message": "Key limit exceeded", "type": "forbidden"}}}) == "limit"
    assert classify(429, {"message": "Too many requests"}) == "rate"
    assert classify(200, {"message": None, "type": "result"}) is None


def test_mask_hides_the_key():
    assert mask("0a1b2c3d-1111-2222-3333-444455556666") == "0a1b…666"
